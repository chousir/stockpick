"""CP 值補漲股候選清單（cp candidates）生產軌編排（自 cli.py 下沉）。

B3：套 B2 勝出因子於最新快照，產 reports/週次/cp_candidates.*。CLI 只保留參數
解析＋呼叫 run_cp_candidates；純讀快取、屬主 make week 流程的一站。
"""

from pathlib import Path

import typer
from rich.console import Console

from tw_screener.analysis.watchlist import read_holdings_csv, read_watchlist_csv

console = Console()


def run_cp_candidates(settings: Path) -> None:
    """B3 個股 CP 候選清單（生產軌）：套 B2 勝出因子於最新快照，產 reports/週次/cp_candidates.*。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import compute_subindustry_baskets, load_market_history
    from tw_screener.analysis.sector_universe import (
        build_broad_industry_membership,
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.analysis.stock_panel import build_stock_panel, compute_coverage_meta
    from tw_screener.analysis.valuation import build_valuation
    from tw_screener.data.twse import create_client
    from tw_screener.report.cp_candidates import (
        attach_subind_quadrant,
        attach_valuation,
        build_cp_candidates,
        build_early_inflow_watch,
        build_overheat_watch,
        compute_early_inflow,
        compute_overheat_warning,
        latest_snapshot,
        recent_buy_confirm,
        render_cp_candidates_report,
        tag_holding_status,
    )
    from tw_screener.screener.runner import derive_week_tag

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cp = cfg.get("cp_value", {})
    cand = cp.get("candidate", {})
    rot = cfg.get("rotation", {})
    history_days = int(cp.get("history_days", 250))
    z_window = int(cp.get("z_window", 60))
    z_min_periods = int(cp.get("z_min_periods", 30))
    position_low_pct = float(cp.get("position_low_pct", 15.0))
    cp_ceiling = float(rot.get("cp_score", {}).get("position_ceiling", 60.0))
    rules = cand.get("rules", [])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    console.print(f"[bold]載入上市資料（{history_days} 交易日）...[/bold]")
    # 個股層只框上市：只讀 daily_*（同 B2），docs/13 §4 B1
    market = load_market_history(cache_dir, n_days=history_days, patterns=("daily_*.parquet",))
    _client = create_client(settings)
    # as_of 對齊價格錨點：避免上櫃法人領先日線（見 load_institutional_history docstring）。
    institutional = _client.load_institutional_history(
        n_days=history_days, as_of=_client.latest_trading_date()
    )
    members = list_subindustries()
    if market.is_empty():
        console.print("[red]缺上市日線快取（daily_*.parquet）[/red]")
        raise typer.Exit(1)

    baskets = (
        compute_subindustry_baskets(
            members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
        )
        if not members.is_empty()
        else market.head(0)
    )
    console.print("[bold]建個股特徵面板 + 取最新快照...[/bold]")
    panel = build_stock_panel(
        market, institutional, members, baskets, z_window=z_window, z_min_periods=z_min_periods
    )
    if panel.is_empty():
        console.print("[red]面板為空——檢查日線/法人快取[/red]")
        raise typer.Exit(1)
    coverage = compute_coverage_meta(panel, institutional, members, universe="listed")

    snapshot = latest_snapshot(panel)
    confirm_days = int(cand.get("confirm_days", 2))
    confirm = recent_buy_confirm(panel, institutional, confirm_days=confirm_days)
    lag = cand.get("laggard", {})
    lag_on = bool(lag.get("enabled", False))
    candidates = build_cp_candidates(
        snapshot,
        confirm,
        rules,
        position_low_pct=position_low_pct,
        drawdown_pct=float(cand.get("drawdown_pct", 20.0)),
        cp_ceiling=cp_ceiling,
        max_candidates=int(cand.get("max_candidates", 40)),
        laggard_labels=tuple(lag.get("apply_to", [])) if lag_on else (),
        laggard_boost=float(lag.get("boost", 0.0)) if lag_on else 0.0,
        laggard_threshold=float(lag.get("threshold", 0.0)),
    )

    week_tag = derive_week_tag(settings)
    out_dir = reports_dir / week_tag
    rotation_csv = out_dir / "sector_rotation.csv"
    candidates = attach_subind_quadrant(candidates, members, rotation_csv)

    # 疊庫存 / 觀察 / 本週命中（任一未維護則該來源空，不報錯）
    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(read_holdings_csv(wl_dir / "holdings.csv").keys())
    watch_ids = read_watchlist_csv(wl_dir / "watchlist.csv")
    hit_ids: list[str] = []
    for fp in sorted(out_dir.glob("screen_result_*.csv")):
        try:
            df = pl.read_csv(fp, infer_schema_length=0)
            if "stock_id" in df.columns:
                hit_ids.extend(str(s) for s in df["stock_id"].to_list() if s)
        except Exception:  # noqa: BLE001 — 壞 CSV 不擋疊圖
            continue
    candidates = tag_holding_status(candidates, holdings_ids, watch_ids, hit_ids)

    # M-MH Phase 3：短窗早訊號加值欄（庫存/觀察的 20d 未確認低信心早訊；補 20d 漏掉的覆蓋）
    early_cfg = cp.get("early_gate", {})
    ew_cfg = cp.get("early_watch", {})
    early_watch = None
    if bool(ew_cfg.get("enabled", False)):  # 研究中・預設關（規劃書 04 A3）
        early = compute_early_inflow(
            snapshot,
            prefixes=tuple(ew_cfg.get("prefixes", ["foreign_flow", "net_flow"])),
            z_threshold=float(early_cfg.get("z_threshold", 1.0)),
            long_z_ceiling=float(early_cfg.get("long_z_ceiling", 0.5)),
            decel_floor=float(early_cfg.get("decel_floor", 0.0)),
        )
        cand_ids = set(candidates["stock_id"].to_list()) if not candidates.is_empty() else set()
        early_watch = build_early_inflow_watch(early, holdings_ids, watch_ids, cand_ids)

    # 精修・點 5：過熱-退潮警示（庫存/觀察已漲到高位但短窗退潮＝停利提醒；未校準啟發式）
    oh_cfg = cp.get("overheat_watch", {})
    overheat_watch = None
    if bool(oh_cfg.get("enabled", False)):  # 研究中・預設關（規劃書 04 A3）
        overheat = compute_overheat_warning(
            snapshot,
            near_high_pct=float(oh_cfg.get("near_high_pct", 8.0)),
            div_floor=float(oh_cfg.get("div_floor", 0.0)),
            vol_contract_floor=float(oh_cfg.get("vol_contract_floor", 0.0)),
        )
        overheat_watch = build_overheat_watch(overheat, holdings_ids, watch_ids)

    # C2 三重濾網：疊 C1 次產業相對估值（官方 trailing PE 主 / PB 補虧損股，橫斷面取最新一份）
    industry = load_industry_mapping(cache_dir)
    val_cfg = cp.get("valuation", {})
    cheap_pctile = float(val_cfg.get("cheap_pctile", 30.0))
    ratios = create_client(settings).load_latest_valuation_ratios()
    # 估值同儕：手標次產業優先、未標上市股以 TWSE 產業別兜底（members 仍純手標供型態/籃子用）
    peer_members = build_peer_membership(members, industry)
    valuation = build_valuation(
        ratios,
        peer_members,
        min_peers=int(val_cfg.get("min_peers", 5)),
        cheap_pctile=cheap_pctile,
        # 手標次產業樣本 <min_peers 退用TWSE粗產業別（2026-08-29，docs/31 §20.8）
        broad_membership=build_broad_industry_membership(industry),
    )
    candidates = attach_valuation(candidates, valuation, cheap_pctile=cheap_pctile)

    names = (
        {
            sid: (nm or "").replace("股份有限公司", "").replace("(股)公司", "").strip()
            for sid, nm in industry.select(["stock_id", "stock_name"]).iter_rows()
        }
        if not industry.is_empty()
        else {}
    )
    if candidates.is_empty() and int(coverage.get("n_trading_days", 0)) < z_min_periods:
        console.print(
            f"[yellow]⚠️ 暖機期：面板僅 {coverage.get('n_trading_days', 0)} 交易日 "
            f"< z 最短視窗 {z_min_periods}——資金 z 未成熟、候選必為 0（快取逐日累積中）[/yellow]"
        )
    md_path = render_cp_candidates_report(
        candidates,
        week_tag=week_tag,
        output_dir=out_dir,
        params={"drawdown_pct": float(cand.get("drawdown_pct", 20.0)),
                "confirm_days": int(cand.get("confirm_days", 2)),
                "z_min_periods": z_min_periods},
        coverage=coverage,
        rules=rules,
        names=names,
        data_date=str(panel["date"].max()),
        early_watch=early_watch,
        overheat_watch=overheat_watch,
    )
    n_triple = (
        candidates.filter(pl.col("triple_filter") == "✓三重").height
        if "triple_filter" in candidates.columns
        else 0
    )
    console.print(f"[green]CP 候選清單 → {md_path}[/green]")
    n_ew = early_watch.height if early_watch is not None else 0
    n_oh = overheat_watch.height if overheat_watch is not None else 0
    console.print(
        f"  候選 {candidates.height} 檔（三重濾網全過 {n_triple}）・"
        f"短窗早訊（庫存/觀察）{n_ew} 檔・過熱-退潮（庫存/觀察）{n_oh} 檔・"
        f"面板 {coverage['n_stocks']} 檔 × {coverage['n_trading_days']} 日・"
        f"法人覆蓋 {coverage['inst_coverage_pct']}%"
    )

