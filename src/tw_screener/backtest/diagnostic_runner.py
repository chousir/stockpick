"""診斷編排（M-Diag1「抓太晚」WS1；自 cli.py 薄殼呼叫）。

載入 pick 底帳（取 week→data_date）＋candidates_enriched 候選宇宙＋日線/除息快取 →
diagnostic 純函式 → research/diagnostic/ 報告與 CSV。CLI 只保留參數解析。
CP3（WS2 missed-launch）之後併入同一指令。
"""

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_diagnostic(settings: Path, out_dir: Path | None) -> None:
    """WS1「抓太晚」診斷：延伸度分桶曲線＋排序訊號 IC＋組內名次 skill。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.backtest import diagnostic as D
    from tw_screener.data.twse import load_recent_dividends
    from tw_screener.report.pick_store import load_all_picks

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    dg = cfg.get("backtest", {}).get("diagnostic", {})
    history_days = int(dg.get("history_days", 250))
    horizons = tuple(int(t) for t in dg.get("horizons_td", [5, 10, 20]))
    tdpw = int(dg.get("trading_days_per_week", 5))
    clip = float(dg.get("clip_daily_return_pct", 10.0))
    min_sample_warn = int(dg.get("min_sample_warn", 20))
    ext_gate = float(dg.get("ext_gate_pct", 15.0))
    target = str(dg.get("target", "excess_return_pct"))
    out = out_dir or Path(dg.get("output_dir", "research/diagnostic"))
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    picks = load_all_picks(reports_dir)
    if picks.is_empty():
        console.print("[red]無任何 picks.csv——先用 `tw-screener picks record` 建底帳[/red]")
        raise typer.Exit(1)
    # week → data_date（每週一個資料日；防前視 entry 用次一交易日）
    week_to_date: dict[str, object] = {}
    for w, d in zip(picks["week"].to_list(), picks["data_date"].to_list()):
        week_to_date.setdefault(w, d)

    enriched: dict[str, pl.DataFrame] = {}
    for w in sorted(week_to_date):
        p = reports_dir / w / "candidates_enriched.csv"
        if p.exists():
            try:
                enriched[w] = pl.read_csv(
                    p, schema_overrides={"stock_id": pl.Utf8}, infer_schema_length=2000
                )
            except Exception as e:  # noqa: BLE001 — 單週 enriched 壞掉不擋整批診斷
                console.print(f"[yellow]讀 {p} 失敗（{e}），跳過該週[/yellow]")
    if not enriched:
        console.print("[red]無 candidates_enriched.csv——先跑 make week[/red]")
        raise typer.Exit(1)

    market = load_market_history(cache_dir, n_days=history_days)
    if market.is_empty():
        console.print("[red]無日線快取——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    since = picks["data_date"].min()
    dividends = (
        load_recent_dividends(cache_dir, since) if isinstance(since, _date) else pl.DataFrame()
    )

    screens, feats = D.build_candidate_screens(enriched, week_to_date)
    returns = D.forward_returns_long(
        screens, market, dividends, horizons_td=horizons,
        trading_days_per_week=tdpw, clip_daily_return_pct=clip,
    )
    if returns.is_empty():
        console.print("[red]無到期前瞻報酬（快取太短或全未到期）[/red]")
        raise typer.Exit(1)
    joined = feats.join(
        returns.select(
            "week_tag", "stock_id", "horizon_td",
            "return_pct", "market_return_pct", "excess_return_pct",
        ),
        on=["week_tag", "stock_id"], how="inner",
    )
    uni_n = {
        int(td): int(joined.filter(pl.col("horizon_td") == td).height) for td in horizons
    }

    ext_curve = D.extension_curve(joined, target_col=target)
    ic_table = D.signal_ic_table(joined, target_col=target)
    rank_ic = D.rank_ic_table(joined, target_col=target)
    target_label = {"excess_return_pct": "超額報酬 vs 大盤", "return_pct": "原始報酬"}.get(
        target, target
    )
    report = D.render_late_entry_report(
        ext_curve, ic_table, rank_ic, uni_n, target_label, ext_gate, min_sample_warn
    )

    out.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md = out / f"late_entry_{tag}.md"
    md.write_text(report, encoding="utf-8")
    ext_curve.write_csv(out / f"late_entry_ext_curve_{tag}.csv")
    ic_table.write_csv(out / f"late_entry_ic_{tag}.csv")
    if not rank_ic.is_empty():
        rank_ic.write_csv(out / f"late_entry_rank_ic_{tag}.csv")
    console.print(f"[green]診斷報告 → {md}[/green]")
    console.print(
        "  候選宇宙到期："
        + "・".join(f"r+{td}d n={n}" for td, n in sorted(uni_n.items()))
    )
    for r in ic_table.filter(pl.col("feature") == "ma60_dist_pct").iter_rows(named=True):
        ci = (
            f"[{r['ci_lo']:+.2f},{r['ci_hi']:+.2f}]" if r["ci_lo"] is not None else "—"
        )
        console.print(f"  距季線 IC r+{r['horizon_td']}d：{r['ic']:+.3f} {ci}")

    # ── WS2：漏掉起漲股目錄（A 純無偏路；daily_all 全市場乾淨底料 ≤06-09）────────────────
    ml = dg.get("missed_launch", {})
    grid_cfg = [tuple(int(x) for x in g) for g in ml.get("grid", [[10, 20], [10, 15], [5, 15]])]
    primary_cfg = tuple(int(x) for x in ml.get("primary", [10, 20]))
    min_amt = float(ml.get("min_amount_m", 100.0))
    pb_max = float(ml.get("pullback_max_pct", 0.0))
    ma_ceil = float(ml.get("ma_dist_ceiling_pct", 10.0))
    from tw_screener.report.pick_store import load_all_excluded

    excluded = load_all_excluded(reports_dir)
    market_all = load_market_history(
        cache_dir, n_days=history_days, patterns=("daily_all_*.parquet",)
    )
    if market_all.is_empty():
        console.print("[yellow]無 daily_all 全市場快取——跳過 WS2 漏抓目錄[/yellow]")
        return
    all_weeks = sorted(week_to_date)
    # 全週 enriched 併出 stock_id→name（給 never_surfaced 補名）
    name_map: dict[str, str] = {}
    for enr in enriched.values():
        if "name" in enr.columns:
            for sid, nm in zip(
                enr["stock_id"].cast(pl.Utf8).to_list(), enr["name"].to_list()
            ):
                if nm is not None:
                    name_map.setdefault(str(sid), str(nm))

    # 觀察清單／持股＝手動雷達面（當前快照，非 point-in-time；近似成員）
    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    watchlist_ids: set[str] = set()
    held_ids: set[str] = set()
    wl_csv = wl_dir / "watchlist.csv"
    if wl_csv.exists():
        wl = pl.read_csv(wl_csv, schema_overrides={"stock_id": pl.Utf8})
        watchlist_ids = set(wl["stock_id"].to_list())
        if "note" in wl.columns:  # note 首詞當股名補進 name_map
            for sid, note in zip(wl["stock_id"].to_list(), wl["note"].to_list()):
                if note and str(sid) not in name_map:
                    name_map[str(sid)] = str(note).split()[0]
    hold_csv = wl_dir / "holdings.csv"
    if hold_csv.exists():
        hd = pl.read_csv(hold_csv, schema_overrides={"stock_id": pl.Utf8})
        held_ids = set(hd["stock_id"].to_list())
        if "note" in hd.columns:
            for sid, note in zip(hd["stock_id"].to_list(), hd["note"].to_list()):
                if note and str(sid) not in name_map:
                    name_map[str(sid)] = str(note).split()[0]

    grid_rows: list[dict] = []
    primary_liquid = pl.DataFrame()
    window_note = f"{market_all['date'].min()!s} ~ {market_all['date'].max()!s}"
    for fwd, y in grid_cfg:
        launched = D.detect_missed_launches(
            market_all, week_to_date, all_weeks, forward_td=fwd, launch_pct=float(y),
            pullback_max_pct=pb_max, ma_dist_ceiling_pct=ma_ceil,
            trading_days_per_week=tdpw, clip_daily_return_pct=clip,
        )
        xr = D.crossref_launches(
            launched, picks, enriched, excluded, name_map=name_map,
            watchlist_ids=watchlist_ids, held_ids=held_ids,
        )
        summ = D.missed_launch_summary(xr, min_amount_m=min_amt)
        cnt = {
            c: (int(summ[c].sum()) if not summ.is_empty() else 0)
            for c in ("held", "acted", "considered", "watchlisted",
                      "never_liquid", "never_illiquid")
        }
        grid_rows.append({"config": f"≥{y}%/{fwd}d", "events": int(xr.height), **cnt})
        if (fwd, y) == primary_cfg:
            primary_liquid = D.liquid_missed_table(xr, min_amount_m=min_amt)

    grid_df = pl.DataFrame(grid_rows) if grid_rows else pl.DataFrame()
    ml_report = D.render_missed_launch_report(
        grid_df, primary_liquid, f"≥{primary_cfg[1]}%/{primary_cfg[0]}d",
        min_amt, window_note,
    )
    ml_md = out / f"missed_launch_{tag}.md"
    ml_md.write_text(ml_report, encoding="utf-8")
    if not grid_df.is_empty():
        grid_df.write_csv(out / f"missed_launch_grid_{tag}.csv")
    if not primary_liquid.is_empty():
        primary_liquid.write_csv(out / f"missed_launch_liquid_{tag}.csv")
    console.print(f"[green]漏抓目錄 → {ml_md}[/green]")
    for r in grid_rows:
        console.print(
            f"  {r['config']}：事件 {r['events']}・考慮未選 {r['considered']}・"
            f"觀察沒扣扳機 {r['watchlisted']}・沒撈到(投資級) {r['never_liquid']}・"
            f"微型 {r['never_illiquid']}"
        )
