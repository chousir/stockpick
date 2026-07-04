"""族群分析（analysis group）的編排層（自 cli.py 下沉）。

CLI 只保留參數解析＋呼叫 run_group_analysis；資料載入、enrich、組合體檢段與
報表/CSV 產出都在這裡，行為與搬出前一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from tw_screener.analysis.watchlist import (
    enrich_named_list,
    load_latest_screener_results,
    read_holdings_csv,
    read_watchlist_csv,
)

if TYPE_CHECKING:
    import polars as pl

console = Console()


def run_group_analysis(settings: Path) -> None:
    """讀最新一週的篩選 CSV + TWSE 快取，產出 group_analysis.md。"""
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    import yaml as _yaml

    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import find_leaders
    from tw_screener.data.twse import (
        create_client,
        filter_dividend_calendar,
        load_recent_dividends,
    )
    from tw_screener.report.group_report import render_group_report

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    ga_cfg = cfg.get("group_analysis", {})
    weights = ga_cfg.get(
        "weights",
        {"momentum": 0.50, "entry_rate": 0.25, "institutional": 0.15, "size": 0.10},
    )
    min_group_size = int(ga_cfg.get("min_group_size", 2))
    top_groups = int(ga_cfg.get("top_groups", 10))
    top_stocks = int(ga_cfg.get("top_stocks", 10))
    dividend_lookahead = int(ga_cfg.get("dividend_lookahead_days", 14))
    macro_lookahead = int(ga_cfg.get("macro_lookahead_days", 30))
    vol_lookback = int(ga_cfg.get("vol_lookback_days", 20))

    week_tag, screener_results = load_latest_screener_results(settings)
    if not screener_results:
        console.print("[red]找不到篩選 CSV，請先執行 make screen-all[/red]")
        raise typer.Exit(1)

    total_rows = sum(len(df) for df in screener_results.values())
    console.print(f"[bold]族群分析：{week_tag}，共 {total_rows} 筆篩選結果[/bold]")

    import polars as _pl

    client = create_client(settings)

    console.print("  載入產業別資料（TWSE 上市 + 上櫃）...")
    listed_df = client.fetch_listed_industry()
    otc_df = client.fetch_otc_industry()
    if not listed_df.is_empty() and not otc_df.is_empty():
        industry_df = _pl.concat([listed_df, otc_df])
    elif not listed_df.is_empty():
        industry_df = listed_df
    elif not otc_df.is_empty():
        industry_df = otc_df
    else:
        industry_df = _pl.DataFrame()
    if industry_df.is_empty():
        console.print("[yellow]  產業別資料無法取得，以「未分類」處理[/yellow]")
    else:
        console.print(f"  上市 {len(listed_df)} 檔、上櫃 {len(otc_df)} 檔")

    console.print("  合併候選股 OHLCV（stock_day + daily）...")
    candidate_ids: list[str] = []
    seen: set[str] = set()
    for df in screener_results.values():
        if df.is_empty() or "stock_id" not in df.columns:
            continue
        for sid in df["stock_id"].cast(_pl.Utf8).to_list():
            sid = str(sid).strip()
            if sid not in seen:
                seen.add(sid)
                candidate_ids.append(sid)

    price_history = client.load_candidate_history(candidate_ids, n_days=90)
    if price_history.is_empty():
        console.print(
            "[yellow]  無 stock_day / daily 快取，5 日動能將 fallback 到當日漲跌幅[/yellow]"
            "  （建議先跑 make fetch-candidates-history 補抓歷史）"
        )
    else:
        # 顯示資料覆蓋情況
        per_stock_days = price_history.group_by("stock_id").len().get_column("len")
        if len(per_stock_days) > 0:
            cov_min, cov_med = str(per_stock_days.min()), str(per_stock_days.median())
            console.print(
                f"  候選股 {len(candidate_ids)} 檔，歷史覆蓋 min={cov_min}、median={cov_med} 日"
            )

    institutional = client.load_institutional_history(n_days=20)
    if institutional.is_empty():
        console.print(
            "[yellow]  無法人快取，族群法人強度將為 0[/yellow]"
            "（建議先跑 make fetch-institutional-history）"
        )
    else:
        console.print(f"  法人快取：{institutional['date'].n_unique()} 個交易日")

    # 量窗：量比需 vol_lookback+1；F5 軌跡量比需 回踩窗+前段窗+1（取大；量比 tail 不受多載影響）
    _traj_cfg = cfg.get("trajectory", {})
    _vol_days = max(
        vol_lookback + 1,
        int(_traj_cfg.get("pullback_vol_window", 5))
        + int(_traj_cfg.get("base_vol_window", 20))
        + 1,
    )
    volume_history = client.load_volume_history(candidate_ids, n_days=_vol_days)
    if volume_history.is_empty():
        console.print(
            "[yellow]  無 trade_volume 快取，量比欄位將顯示 '-'[/yellow]"
        )
    else:
        console.print(f"  量比資料：{volume_history['stock_id'].n_unique()} 檔")

    # 資料日期一致性檢查（安全網）：三個來源若非同一交易日，量價/籌碼可能來自不同快照
    _src_dates: dict[str, object] = {}
    for _label, _df in (
        ("OHLCV", price_history),
        ("量", volume_history),
        ("法人", institutional),
    ):
        if not _df.is_empty() and "date" in _df.columns:
            _src_dates[_label] = _df["date"].max()
    if len(set(_src_dates.values())) > 1:
        console.print(
            "[yellow]  ⚠ 資料來源最新日期不一致："
            + "、".join(f"{k}={v}" for k, v in _src_dates.items())
            + "；量價/籌碼可能來自不同快照，建議重抓對齊[/yellow]"
        )

    dividends = filter_dividend_calendar(
        client.fetch_dividend_calendar(), _date.today(), dividend_lookahead, candidate_ids
    )
    if dividends.is_empty():
        console.print(f"  本週除權息：候選股未來 {dividend_lookahead} 天內無除權息")
    else:
        console.print(f"  本週除權息：{len(dividends)} 檔候選股（未來 {dividend_lookahead} 天）")

    # 除息還原：聯集近日除權息快照，取近 20 天 ex_date（涵蓋 5 交易日動能視窗），把視窗內
    # 現金股利加回 momentum_5d，修正 6-8 月除息季的假負與排名失真。
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    recent_dividends = load_recent_dividends(cache_dir, _date.today() - _timedelta(days=20))
    if not recent_dividends.is_empty():
        n_exdiv = recent_dividends.filter(
            _pl.col("stock_id").is_in(candidate_ids)
        )["stock_id"].n_unique()
        if n_exdiv:
            console.print(f"  除息還原：近 20 天 {n_exdiv} 檔候選股除權息，動能加回現金股利")

    from tw_screener.data.macro import filter_macro_calendar, load_macro_calendar

    macro_events = filter_macro_calendar(load_macro_calendar(), _date.today(), macro_lookahead)
    if macro_events.is_empty():
        console.print(
            f"  未來總經事件：未來 {macro_lookahead} 天內無"
            "（或 config/macro_calendar.yaml 未建/窗內無事件）"
        )
    else:
        console.print(f"  未來總經事件：{len(macro_events)} 筆（未來 {macro_lookahead} 天）")

    g_pullback = cfg.get("g_pullback")

    console.print("  計算族群強度分數...")
    groups, enriched_stocks = group_stocks(
        screener_results,
        price_history,
        _pl.DataFrame(),  # benchmark: skip for now
        industry_df=industry_df if not industry_df.is_empty() else None,
        weights=weights,
        min_group_size=min_group_size,
        institutional=institutional,
        volume_history=volume_history,
        g_pullback=g_pullback,
        vol_lookback=vol_lookback,
        dividends=recent_dividends,
        trajectory_cfg=cfg.get("trajectory", {}),  # F5 軌跡欄（沿舊 07 TR1）
    )

    if groups.is_empty():
        console.print("[yellow]無符合條件的族群（需 ≥ 2 檔同族群），產出空報告[/yellow]")

    console.print("  計算族群內排名...")
    leaders = find_leaders(enriched_stocks, price_history, institutional)

    # 主題（多標籤）：手標電子次產業 + Yahoo 概念股 → long table。不 join 進 leaders
    # （會把每檔複製成多列、炸掉逐股表），改交報表內 rank_themes / 顯示字串處理。
    from tw_screener.analysis.concepts import load_themes, unmapped_electronics

    themes_long = load_themes()
    if not themes_long.is_empty() and not leaders.is_empty():
        unmapped = unmapped_electronics(leaders, themes_long)
        if unmapped:
            console.print(
                f"[yellow]  次產業未標（電子股 {len(unmapped)} 檔，可補 config/concepts.yaml）："
                f"{', '.join(unmapped[:20])}{' …' if len(unmapped) > 20 else ''}[/yellow]"
            )
        else:
            console.print("  次產業：電子候選股全數已標")

    output_path = Path(cfg["paths"]["reports_dir"]) / week_tag / "group_analysis.md"
    # 大盤 regime 總控（規劃書 03 V2）：全市場日線＋已載法人快取 → 進攻/中性/防禦姿態
    from tw_screener.analysis.regime import compute_market_regime, describe_regime
    from tw_screener.report.density import data_density_note

    console.print("  計算大盤 regime（趨勢/廣度/資金）...")
    regime_result = compute_market_regime(cfg, settings, institutional=institutional)
    regime = describe_regime(regime_result)
    console.print(f"  {regime['line']}")

    # 組合層風控（規劃書 03 V3）：持股標籤集中度＋因子簇曝險（價格無關、render 期即可得）。
    # 報酬相關簇需全市場日線，留給 `portfolio check` CLI；報告段只揭露集中度/因子簇。
    portfolio = _portfolio_section_for_report(cfg, industry_df, themes_long)

    _hist_days = price_history["date"].n_unique() if not price_history.is_empty() else 0
    render_group_report(
        groups, leaders, screener_results, week_tag, output_path, top_groups, top_stocks,
        dividend_events=dividends, themes_long=themes_long, macro_events=macro_events,
        radar_cfg=ga_cfg.get("radar"),
        density_note=data_density_note(_hist_days),
        regime=regime, portfolio=portfolio,
    )

    from tw_screener.report.group_report import write_candidates_enriched_csv

    rev_df = client.fetch_revenue()
    rev_yoy_map: dict[str, object] = {}
    name_map: dict[str, str] = {}
    if not rev_df.is_empty() and "stock_id" in rev_df.columns:
        rdf = rev_df
        if "year_month" in rev_df.columns:
            rdf = rev_df.sort("year_month", descending=True)
        for rr in rdf.iter_rows(named=True):
            sid = str(rr["stock_id"])
            if "yoy_pct" in rev_df.columns:
                rev_yoy_map.setdefault(sid, rr.get("yoy_pct"))
            if "company_name" in rev_df.columns:
                name_map.setdefault(sid, str(rr.get("company_name") or ""))

    # 單季基本面（毛利率/EPS）：純讀快取，由 make fetch-twse 累積
    fund_df = client.load_latest_fundamentals()
    fundamentals_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in fund_df.iter_rows(named=True)}
        if not fund_df.is_empty()
        else {}
    )

    # 官方日估值比（PE/PB/殖利率）：純讀快取，由 make fetch-twse 累積（BWIBBU）。candidates 估值欄
    # 以此為主、Goodinfo 兜底（官方覆蓋 ~97%、口徑一致 trailing）。再過 build_valuation 算次產業
    # 相對位階（PE 主、PB 補虧損股）→ 每檔候選 inline 帶「次位/相對便宜」，免另跑 cp_valuation。
    from tw_screener.analysis.sector_universe import build_peer_membership, list_subindustries
    from tw_screener.analysis.valuation import build_valuation

    val_df = client.load_latest_valuation_ratios()
    val_cfg = cfg.get("cp_value", {}).get("valuation", {})
    valuation = build_valuation(
        val_df,
        build_peer_membership(list_subindustries(), industry_df),
        min_peers=int(val_cfg.get("min_peers", 5)),
        cheap_pctile=float(val_cfg.get("cheap_pctile", 30.0)),
    )
    valuation_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in valuation.iter_rows(named=True)}
        if not valuation.is_empty()
        else {}
    )

    # D3 集保大戶持股比（≥400張 / ≥1000張＋WoW）：純讀快取（make week 的 fetch-tdcc 累積）。
    # TDCC 異常時回空表 → 大戶欄誠實 null，不擋報告。
    from tw_screener.data.tdcc import create_tdcc_client

    bh_df = create_tdcc_client(settings).load_big_holders()
    big_holder_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in bh_df.iter_rows(named=True)}
        if not bh_df.is_empty()
        else {}
    )

    # 上市融資融券（D4）：純讀快取（make week 的 fetch-twse 累積）。上櫃缺→該股 margin 欄 null。
    margin_df = client.load_margin_signals()
    margin_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in margin_df.iter_rows(named=True)}
        if not margin_df.is_empty()
        else {}
    )

    csv_path = output_path.parent / "candidates_enriched.csv"
    cand_rows = write_candidates_enriched_csv(
        leaders, themes_long, screener_results, csv_path,
        flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
        fundamentals_map=fundamentals_map, valuation_map=valuation_map,
        big_holder_map=big_holder_map, margin_map=margin_map,
        near_flow_cfg=cfg.get("near_flow", {}),  # F5 近端籌碼揭露欄（沿舊 06 NF1）
    )
    # 重疊股重用：庫存/觀察清單同檔一律沿用 candidates 那筆，避免跨 CSV 量比/集中度/成交額分岔
    canonical_rows = {row["stock_id"]: row for row in cand_rows}
    n_cand = len(cand_rows)

    console.print(f"[green]報告輸出：{output_path}[/green]")
    console.print(f"  全候選股完整欄位 CSV：{csv_path}（{n_cand} 檔，供 ProPicks 全宇宙挑股）")

    # 庫存與觀察清單（必分析）→ enrich 成 reports 下 2 個 CSV
    from tw_screener.report.group_report import write_named_list_csv

    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_map = read_holdings_csv(wl_dir / "holdings.csv")
    watch_ids = read_watchlist_csv(wl_dir / "watchlist.csv")
    for label, ids, hmap in [
        ("holdings", list(holdings_map), holdings_map),
        ("watchlist", watch_ids, None),
    ]:
        if not ids:
            continue
        console.print(f"  enrich {label}（{len(ids)} 檔，無快取會抓網）...")
        wl_members, wl_synth = enrich_named_list(
            client,
            ids,
            industry_df if not industry_df.is_empty() else None,
            institutional,
            g_pullback,
            name_map=name_map,
            vol_lookback=vol_lookback,
            dividends=recent_dividends,
        )
        out_csv = output_path.parent / f"{label}_enriched.csv"
        n = write_named_list_csv(
            wl_members, themes_long, wl_synth, out_csv,
            flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
            fundamentals_map=fundamentals_map, valuation_map=valuation_map,
            big_holder_map=big_holder_map, margin_map=margin_map,
            holdings_map=hmap, canonical_rows=canonical_rows,
            near_flow_cfg=cfg.get("near_flow", {}),
        )
        console.print(f"[green]  {label}_enriched.csv：{n} 檔 → {out_csv}[/green]")

    console.print(f"  族群數：{len(groups)}，推薦分析：前 {top_stocks} 檔")


def _portfolio_section_for_report(
    cfg: dict, industry_df: pl.DataFrame | None, themes_long: pl.DataFrame | None
) -> dict | None:
    """group_analysis.md 組合體檢段：持股 ids ＋ industry_df ＋ themes_long → 標籤集中度/因子簇。

    純本地、不抓網、不需價格（相關簇留給 `portfolio check` CLI）。無持股回 None（不渲染該段）。
    """
    import polars as _pl

    from tw_screener.analysis.portfolio import (
        compute_portfolio_check,
        describe_portfolio_check,
    )

    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(read_holdings_csv(wl_dir / "holdings.csv"))
    if not holdings_ids:
        return None
    base = _pl.DataFrame({"stock_id": [str(s) for s in holdings_ids]})
    if (
        industry_df is not None
        and not industry_df.is_empty()
        and {"stock_id", "industry_name"}.issubset(industry_df.columns)
    ):
        ind = industry_df.select(
            _pl.col("stock_id").cast(_pl.Utf8),
            _pl.col("industry_name").alias("industry"),
        ).unique(subset=["stock_id"])
        base = base.join(ind, on="stock_id", how="left")
    if themes_long is not None and not themes_long.is_empty():
        th = (
            themes_long.with_columns(_pl.col("stock_id").cast(_pl.Utf8))
            .group_by("stock_id")
            .agg(_pl.col("theme"))
            .with_columns(_pl.col("theme").list.join("、"))
        )
        base = base.join(th, on="stock_id", how="left")
    # 價格史傳空：報告段只取 label/factor（價格無關）；相關簇由 CLI 提供
    result = compute_portfolio_check(base, _pl.DataFrame(), cfg.get("portfolio", {}))
    return describe_portfolio_check(result)
