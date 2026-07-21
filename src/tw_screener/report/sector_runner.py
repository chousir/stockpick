"""次產業資金流向輪動（sector）指令編排（自 cli.py 下沉）。

universe / flows / rotation / calibrate 四命令的資料載入與報表產出。CLI 只保留
參數解析＋呼叫 run_sector_*；calibrate 為研究軌（research/rotation/），其餘屬主流程。
"""

from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console

from tw_screener.analysis.watchlist import read_holdings_csv, read_watchlist_csv

console = Console()


def _warn_otc_lag(lag_info: tuple[int, str | None, str | None]) -> None:
    """上櫃法人快取落後上市時印警告（TPEX 缺日不可回補，落後＝上櫃資金流被低估）。"""
    lag, listed_max, otc_max = lag_info
    if lag >= 1:
        console.print(
            f"[yellow]⚠ 上櫃法人快取落後上市 {lag} 個交易日"
            f"（上市至 {listed_max}・上櫃至 {otc_max}）——TPEX 缺日不可回補，"
            f"上櫃股近期資金流被低估；請每交易日跑 make fetch-twse[/yellow]"
        )


def run_sector_universe(list_all: bool, audit: bool, settings: Path) -> None:
    """次產業宇宙總覽：concepts.yaml 手標次產業 + TWSE 28 類對照覆蓋率（純讀快取）。

    --audit 另比對日線快取，列出無價成員供手動清 concepts.yaml（本指令不改檔）。
    """
    import polars as pl
    import yaml

    from tw_screener.analysis.sector_universe import (
        audit_priceless_members,
        list_subindustries,
        load_industry_mapping,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    members = list_subindustries()
    industry = load_industry_mapping(cache_dir)

    if members.is_empty():
        console.print("[red]concepts.yaml 無次產業標記[/red]")
        raise typer.Exit(1)

    n_subs = members["sub_industry"].n_unique()
    n_stocks = members["stock_id"].n_unique()
    console.print(f"[bold]次產業：{n_subs} 個・成員股：{n_stocks} 檔（去重）[/bold]")
    if not industry.is_empty():
        mapped = members.join(industry, on="stock_id", how="inner")["stock_id"].n_unique()
        console.print(
            f"TWSE 28 類對照：全市場 {industry.height} 檔・"
            f"次產業成員可對照 {mapped}/{n_stocks} 檔"
        )
    else:
        console.print("[yellow]無 industry_YYYYMM 快取（請先 make fetch-twse）[/yellow]")

    if list_all:
        counts = (
            members.group_by("sub_industry")
            .agg(pl.col("stock_id").count().alias("members"))
            .sort("members", descending=True)
        )
        for sub, n in counts.iter_rows():
            sample = members.filter(pl.col("sub_industry") == sub)["stock_id"].head(6)
            console.print(f"  {sub}（{n} 檔）：{', '.join(sample)}{' …' if n > 6 else ''}")

    if audit:
        from tw_screener.analysis.rotation import load_market_history

        rot = cfg.get("rotation", {})
        lookback = int(rot.get("audit_lookback_days", 20))
        market = load_market_history(cache_dir, n_days=lookback)
        if market.is_empty():
            console.print(
                "[yellow]無日線快取（daily_*/otc_daily_*），無法清查無價成員；"
                "請先 make fetch-twse[/yellow]"
            )
            return
        priced = set(market["stock_id"].unique().to_list())
        priceless = audit_priceless_members(members, priced)
        n_priceless = priceless["stock_id"].n_unique()
        console.print(
            f"\n[bold]── 清查：近 {lookback} 交易日無日線收盤的次產業成員 ──[/bold]"
        )
        if priceless.is_empty():
            console.print("[green]全數有價，無需清理[/green]")
            return
        console.print(
            f"[yellow]{n_priceless}/{n_stocks} 檔無價（{n_priceless / n_stocks:.0%}）"
            "——多為興櫃/下市/誤標，會讓籃子悄悄縮水[/yellow]"
        )
        # 受影響次產業（縮水家數 / 標記家數），按縮水比例排序
        impact = (
            priceless.group_by("sub_industry")
            .agg(pl.col("stock_id").n_unique().alias("priceless"))
            .join(
                members.group_by("sub_industry").agg(
                    pl.col("stock_id").n_unique().alias("tagged")
                ),
                on="sub_industry",
                how="left",
            )
            .with_columns((pl.col("priceless") / pl.col("tagged")).alias("ratio"))
            .sort("ratio", descending=True)
        )
        for r in impact.iter_rows(named=True):
            console.print(
                f"  {r['sub_industry']}：{r['priceless']}/{r['tagged']} 縮水（{r['ratio']:.0%}）"
            )
        # 逐檔（一檔多標籤併列），供手動定位 concepts.yaml
        per_stock = priceless.group_by("stock_id").agg(
            pl.col("sub_industry").sort().str.join("、").alias("labels")
        ).sort("stock_id")
        name_map = (
            dict(industry.select(["stock_id", "stock_name"]).iter_rows())
            if not industry.is_empty() and "stock_name" in industry.columns
            else {}
        )
        console.print(
            "[dim]逐檔（手動從 config/concepts.yaml 移除確認無誤者；本指令不改檔）：[/dim]"
        )
        for r in per_stock.iter_rows(named=True):
            nm = name_map.get(r["stock_id"], "")
            console.print(f"  {r['stock_id']} {nm}　[{r['labels']}]")


def run_sector_flows(week: str, dry: bool, settings: Path) -> None:
    """次產業法人資金流向排名（純讀快取；前 N 流入 + 流出警訊）。"""
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        load_market_history,
        otc_institutional_lag,
        rank_flows,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.data.twse import create_client

    if week != "current":
        console.print("[red]R1 僅支援 --week current（歷史週次查詢隨 R3 落檔後提供）[/red]")
        raise typer.Exit(1)

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    top_n = int(rot.get("top_n", 10))
    rank_by = str(rot.get("rank_by", f"net_flow_{long_w}d"))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    members = list_subindustries()
    if members.is_empty():
        console.print("[red]concepts.yaml 無次產業標記[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]載入法人/價量歷史（{history_days} 交易日）...[/bold]")
    _client = create_client(settings)
    # as_of 對齊價格錨點：避免上櫃法人領先日線（見 load_institutional_history docstring）。
    institutional = _client.load_institutional_history(
        n_days=history_days, as_of=_client.latest_trading_date()
    )
    market = load_market_history(cache_dir, n_days=history_days)
    if institutional.is_empty():
        console.print("[red]無法人快取（請先 make fetch-twse）[/red]")
        raise typer.Exit(1)
    _warn_otc_lag(otc_institutional_lag(cache_dir))

    volume = (
        market.select(["date", "stock_id", "volume"]) if not market.is_empty() else None
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=volume,
        short_window=short_w,
        long_window=long_w,
    )
    ranked = rank_flows(flows, by=rank_by, min_members=min_members)
    latest_date = str(flows["date"].max())
    console.print(
        f"[bold]資金流向排名（{latest_date}・排名鍵 {rank_by}・籃子 ≥{min_members} 檔）[/bold]"
    )

    def _fmt_row(r: dict) -> str:
        lots = r[rank_by] / 1000  # 股 → 張
        conc = r.get(f"flow_concentration_{long_w}d")
        conc_part = f"・力度 {conc * 100:.2f}%" if conc is not None else ""
        delta = r.get("rank_delta")
        delta_part = f"・ΔRank {delta:+d}" if delta is not None else ""
        return (
            f"  #{r['radar_rank']:>2} {r['sub_industry']}（{r['members']} 檔）"
            f" 淨{'流入' if lots >= 0 else '流出'} {abs(lots):,.0f} 張"
            f"・breadth {r[f'flow_breadth_{long_w}d']:.0%}{conc_part}{delta_part}"
        )

    console.print(f"[green]── 資金流入 前 {top_n} ──[/green]")
    for r in ranked.head(top_n).iter_rows(named=True):
        console.print(_fmt_row(r))
    console.print(f"[red]── 資金流出 末 {min(top_n, 5)} ──[/red]")
    for r in ranked.tail(min(top_n, 5)).iter_rows(named=True):
        console.print(_fmt_row(r))
    if dry:
        console.print("[dim]（dry：未落檔；落檔與 ΔRank 快照接續於 R3）[/dim]")


def run_sector_rotation(top: int | None, settings: Path) -> None:
    """R3 生產輪動報表：產 reports/YYYY-Www/sector_rotation.md + .csv（四象限＋校準訊號）。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        compute_subindustry_baskets,
        compute_trend_leaders,
        compute_trend_scores,
        load_market_history,
        otc_institutional_lag,
    )
    from tw_screener.analysis.sector_universe import (
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.data.twse import create_client
    from tw_screener.report.density import data_density_note
    from tw_screener.report.rotation_report import (
        build_participation,
        build_rotation_table,
        load_prev_rotation_snapshot,
        render_rotation_report,
    )
    from tw_screener.screener.runner import derive_week_tag

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    quad = rot.get("quadrant", {})
    cp_ceiling = float(rot.get("cp_score", {}).get("position_ceiling", 60.0))
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    # 多窗鏡頭（含 10d 中端）；short/long 由 compute_fund_flows 自動納入
    windows = tuple(int(x) for x in rot.get("windows", []))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    top_n = top if top is not None else int(rot.get("top_n", 10))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    console.print(f"[bold]載入資料（{history_days} 交易日）...[/bold]")
    members = list_subindustries()
    market = load_market_history(cache_dir, n_days=history_days)
    _client = create_client(settings)
    # as_of 對齊價格錨點：避免上櫃法人領先日線（見 load_institutional_history docstring）。
    institutional = _client.load_institutional_history(
        n_days=history_days, as_of=_client.latest_trading_date()
    )
    if members.is_empty() or market.is_empty() or institutional.is_empty():
        console.print("[red]缺資料：concepts.yaml / daily 快取 / 法人快取[/red]")
        raise typer.Exit(1)
    _warn_otc_lag(otc_institutional_lag(cache_dir))

    baskets = compute_subindustry_baskets(
        members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=short_w,
        long_window=long_w,
        windows=windows,
    )

    # F3：價格趨勢分數（主排序鍵）＋趨勢領頭板（旗標口徑沿 propicks_flags）
    ts_cfg = rot.get("trend_score", {})
    trend = compute_trend_scores(
        baskets,
        members,
        market,
        ma_short=int(ts_cfg.get("ma_short", 20)),
        ma_long=int(ts_cfg.get("ma_long", 60)),
        rs_window=int(ts_cfg.get("rs_window", 20)),
        weights=ts_cfg.get("weights"),
    )
    ld_cfg = rot.get("leaders", {})
    flags_cfg = cfg.get("propicks_flags", {})
    leaders = compute_trend_leaders(
        members,
        market,
        institutional,
        top_n=int(ld_cfg.get("top_n", 15)),
        rs_window=int(ld_cfg.get("rs_window", 20)),
        min_amount_million=float(ld_cfg.get("min_amount_million", 100.0)),
        overheat_ma60_pct=float(flags_cfg.get("overheated_ma60_pct", 40.0)),
        cross_trade_lots=float(flags_cfg.get("cross_trade_lots", 5000.0)),
        cross_trade_rel_pct=float(flags_cfg.get("cross_trade_rel_pct", 4.0)),
    )

    week_tag = derive_week_tag(settings)
    prev = load_prev_rotation_snapshot(reports_dir, week_tag)
    table = build_rotation_table(
        flows,
        baskets,
        short_window=short_w,
        long_window=long_w,
        entry_signal=rot.get("entry_signal", {}),
        position_window=int(quad.get("position_window", 60)),
        position_low_pct=float(quad.get("position_low_pct", 10.0)),
        cp_position_ceiling=cp_ceiling,
        rank_by=rot.get("rank_by"),
        min_members=min_members,
        prev=prev,
        trend=trend,
        next_precision_low_pct=(
            float(quad["next_precision_low_pct"])
            if quad.get("next_precision_low_pct") is not None
            else None
        ),
    )
    if table.is_empty():
        console.print("[red]輪動表為空（資料不足）[/red]")
        raise typer.Exit(1)

    # R4 疊圖：庫存 / 觀察 / 本週命中（任一未維護則略過該來源，不報錯）
    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(read_holdings_csv(wl_dir / "holdings.csv").keys())
    watch_ids = read_watchlist_csv(wl_dir / "watchlist.csv")
    hit_ids: list[str] = []
    for csv_path in sorted((reports_dir / week_tag).glob("screen_result_*.csv")):
        try:
            df = pl.read_csv(csv_path, infer_schema_length=0)
            if "stock_id" in df.columns:
                hit_ids.extend(str(s) for s in df["stock_id"].to_list() if s)
        except Exception:  # noqa: BLE001 — 壞 CSV 不擋疊圖
            continue
    industry = load_industry_mapping(cache_dir)
    names = (
        {
            sid: (nm or "").replace("股份有限公司", "").replace("(股)公司", "").strip()
            for sid, nm in industry.select(["stock_id", "stock_name"]).iter_rows()
        }
        if not industry.is_empty()
        else {}
    )
    participation = build_participation(
        [
            ("庫存（holdings.csv）", holdings_ids, False),
            ("觀察（watchlist.csv）", watch_ids, False),
            (f"本週命中（{week_tag} 篩選聯集）", hit_ids, True),
        ],
        members,
        table,
        long_window=long_w,
        names=names,
    )

    # 大盤 regime 總控（規劃書 03 V2）：重用已載全市場日線＋法人快取
    from tw_screener.analysis.regime import compute_regime, describe_regime

    regime = describe_regime(compute_regime(market, institutional, cfg.get("regime", {})))
    console.print(f"  {regime['line']}")

    md_path = render_rotation_report(
        table,
        week_tag=week_tag,
        output_dir=reports_dir / week_tag,
        short_window=short_w,
        long_window=long_w,
        entry_signal=rot.get("entry_signal", {}),
        position_low_pct=float(quad.get("position_low_pct", 10.0)),
        cp_position_ceiling=cp_ceiling,
        top_n=top_n,
        data_date=str(flows["date"].max()),
        density_note=data_density_note(market["date"].n_unique()),
        participation=participation,
        regime=regime,
        leaders=leaders,
        names=names,
        quadrant_note=str(quad.get("calib_note", "")),
    )
    if not leaders.is_empty():
        leaders.write_csv(reports_dir / week_tag / "trend_leaders.csv")
    n_next = table.filter(pl.col("quadrant") == "下一棒").height
    n_trig = table.filter(pl.col("entry_triggered")).height
    console.print(f"[green]輪動報表 → {md_path}[/green]")
    console.print(
        f"  次產業 {table.height} 個・下一棒候選 {n_next} 個・★訊號觸發 {n_trig} 個"
        f"・趨勢領頭板 {leaders.height} 檔・ΔRank "
        f"{'有上週快照' if prev is not None else '首週（無快照）'}"
    )


def run_sector_calibrate(
    x_pct: float | None,
    n_days: int | None,
    m_days: int | None,
    out_dir: Path,
    settings: Path,
) -> None:
    """R2 起漲點回測校準（研究軌）：掃描資金訊號門檻，產 research/rotation/ 報告。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        compute_subindustry_baskets,
        load_market_history,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest.rotation_calib import (
        detect_breakouts,
        render_calibration_report,
        scan_signals,
    )
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    cal = rot.get("calibration", {})
    p: dict[str, Any] = {
        "x_pct": x_pct if x_pct is not None else float(cal.get("breakout_x_pct", 10.0)),
        "n_days": n_days if n_days is not None else int(cal.get("breakout_n_days", 15)),
        "m_days": m_days if m_days is not None else int(cal.get("low_base_m_days", 60)),
        "low_base_tol_pct": float(cal.get("low_base_tol_pct", 3.0)),
        "cooldown_days": int(cal.get("cooldown_days", 15)),
        "lead_window": int(cal.get("lead_window", 15)),
        "z_window": int(cal.get("z_window", 60)),
        "z_min_periods": int(cal.get("z_min_periods", 30)),
    }
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    console.print(f"[bold]載入資料（{history_days} 交易日）...[/bold]")
    members = list_subindustries()
    market = load_market_history(cache_dir, n_days=history_days)
    _client = create_client(settings)
    # as_of 對齊價格錨點：避免上櫃法人領先日線（見 load_institutional_history docstring）。
    institutional = _client.load_institutional_history(
        n_days=history_days, as_of=_client.latest_trading_date()
    )
    if members.is_empty() or market.is_empty() or institutional.is_empty():
        console.print("[red]缺資料：concepts.yaml / daily 快取 / 法人快取[/red]")
        raise typer.Exit(1)

    # 只校準成員數達標的次產業（避免單檔灌水）
    big_enough = (
        members.group_by("sub_industry")
        .agg(pl.col("stock_id").count().alias("n"))
        .filter(pl.col("n") >= min_members)["sub_industry"]
        .to_list()
    )
    members = members.filter(pl.col("sub_industry").is_in(big_enough))

    baskets = compute_subindustry_baskets(
        members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=short_w,
        long_window=long_w,
    )
    episodes = detect_breakouts(
        baskets,
        x_pct=p["x_pct"],
        n_days=p["n_days"],
        m_days=p["m_days"],
        low_base_tol_pct=p["low_base_tol_pct"],
        cooldown_days=p["cooldown_days"],
    )
    console.print(
        f"次產業 {len(big_enough)} 個・起漲點 {episodes.height} 個"
        f"（X={p['x_pct']}% N={p['n_days']} M={p['m_days']}）"
    )
    if episodes.is_empty():
        console.print("[red]無起漲點樣本——放寬 --x-pct 或 --n-days 後重跑[/red]")
        raise typer.Exit(1)

    console.print("[bold]掃描訊號 × 門檻...[/bold]")
    scan = scan_signals(
        flows,
        episodes,
        z_window=p["z_window"],
        z_min_periods=p["z_min_periods"],
        lead_window=p["lead_window"],
        occupy_days=p["cooldown_days"],
    )

    # R3 四象限可信度實測（前瞻報酬×起漲攔截）——象限語意的每季裁判
    from tw_screener.analysis.rotation import compute_market_index
    from tw_screener.backtest.rotation_calib import evaluate_quadrants

    quad = rot.get("quadrant", {})
    console.print("[bold]四象限可信度實測...[/bold]")
    precision = quad.get("next_precision_low_pct")
    quadrant_stats = evaluate_quadrants(
        flows,
        baskets,
        episodes,
        market_index=compute_market_index(
            market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
        ),
        long_window=long_w,
        position_window=int(quad.get("position_window", 60)),
        position_low_pct=float(quad.get("position_low_pct", 10.0)),
        next_precision_low_pct=float(precision) if precision is not None else None,
        min_members=min_members,
        lead_window=p["lead_window"],
        occupy_days=p["cooldown_days"],
    )

    data_range = (cast(_date, market["date"].min()), cast(_date, market["date"].max()))
    report = render_calibration_report(
        scan,
        episodes,
        p,
        data_range,
        min_triggers=int(cal.get("min_triggers", 8)),
        min_lift=float(cal.get("min_lift", 1.5)),
        quadrant_stats=quadrant_stats,
    )

    # WS-H.4b regime 切片（純增段：既有段落與數字不動；regime 檔缺 → 段落誠實跳過）
    from tw_screener.backtest.regime_slice import load_regime_labels
    from tw_screener.backtest.rotation_calib import entry_regime_slice_section

    regime_path = Path(
        cfg.get("backtest", {})
        .get("regime_history", {})
        .get("output_path", "research/panel/regime_labels.parquet")
    )
    regime_labels = load_regime_labels(regime_path)
    if regime_labels.is_empty():
        console.print(f"[yellow]regime 標籤缺（{regime_path}）——切片段標「未標」/跳過[/yellow]")
    report += "\n".join(
        entry_regime_slice_section(
            quadrant_stats["entry_triggers"],
            episodes,
            flows,
            regime_labels,
            lead_window=p["lead_window"],
            occupy_days=p["cooldown_days"],
        )
    ) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md_path = out_dir / f"calibration_{tag}.md"
    csv_path = out_dir / f"calibration_{tag}.csv"
    md_path.write_text(report, encoding="utf-8")
    scan.write_csv(csv_path)
    console.print(f"[green]報告 → {md_path}[/green]")
    console.print(f"[green]全掃描表 → {csv_path}[/green]")
    console.print("\n[bold]F1 前 8 名：[/bold]")
    for r in scan.head(8).iter_rows(named=True):
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        console.print(
            f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
            f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
        )


