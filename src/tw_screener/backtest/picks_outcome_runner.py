"""pick 閉環（picks outcome）編排（規劃書 05 F1 PO2–PO4；自 cli.py 薄殼呼叫）。

載入 pick_store 底帳＋日線/除息快取＋次產業成員 → picks_outcome 純函式 →
research/pick_outcome/ 報告與 CSV。CLI 只保留參數解析。
"""

from datetime import date
from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_picks_outcome(
    settings: Path,
    exit_date: str | None,
    diff: bool,
    hold_weeks: str | None,
) -> None:
    """PO2 分層命中率×α（到期快照＋固定持有窗）＋ PO4 偽陰性 ＋（--diff）PO3 翻轉解剖。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest.picks_outcome import (
        compute_todate_returns,
        counterfactual_summary,
        layer_summary,
        render_outcome_report,
        week_over_week_diff,
        weekly_layer_table,
    )
    from tw_screener.backtest.strategies import compute_forward_returns, strategy_summary
    from tw_screener.data.twse import load_recent_dividends
    from tw_screener.report.pick_store import (
        load_all_excluded,
        load_all_picks,
        weeks_without_picks,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    pk = cfg.get("backtest", {}).get("picks", {})
    history_days = int(pk.get("history_days", 250))
    weeks = (
        [int(w) for w in hold_weeks.split(",") if w.strip()]
        if hold_weeks
        else [int(w) for w in pk.get("hold_weeks", [2, 4, 8, 12])]
    )
    tdpw = int(pk.get("trading_days_per_week", 5))
    clip = float(pk.get("clip_daily_return_pct", 10.0))
    min_sample_warn = int(pk.get("min_sample_warn", 20))
    min_subind = int(pk.get("min_subind_members", 3))
    out_dir = Path(pk.get("output_dir", "research/pick_outcome"))
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    picks = load_all_picks(reports_dir)
    excluded = load_all_excluded(reports_dir)
    missing = weeks_without_picks(reports_dir)
    if picks.is_empty():
        console.print("[red]無任何 picks.csv——先用 `tw-screener picks record` 建底帳[/red]")
        raise typer.Exit(1)

    cut: date | None = None
    if exit_date:
        cut = _date.fromisoformat(exit_date)

    console.print(f"[bold]pick 底帳：{picks.height} 筆／{picks['week'].n_unique()} 週；"
                  f"excluded {excluded.height} 筆[/bold]")
    market = load_market_history(cache_dir, n_days=history_days)
    if market.is_empty():
        console.print("[red]無日線快取——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    since = picks["data_date"].min()
    dividends = (
        load_recent_dividends(cache_dir, since) if isinstance(since, _date) else pl.DataFrame()
    )
    membership = list_subindustries()

    todate = compute_todate_returns(
        picks, market, dividends=dividends, exit_date=cut,
        membership=membership, min_subind_members=min_subind,
    )
    excluded_todate = compute_todate_returns(
        excluded, market, dividends=dividends, exit_date=cut,
    )
    layers = layer_summary(todate)
    weekly_core = weekly_layer_table(todate, layer="core")
    counterfactual = counterfactual_summary(excluded_todate)

    # 固定持有窗：picks → V1 入選快照 schema（layer 當 strategy_id），整套複用
    screens_like = picks.select(
        pl.col("week").alias("week_tag"),
        pl.col("data_date").alias("screened_at"),
        "stock_id",
        "name",
        pl.col("layer").alias("strategy_id"),
    )
    frames = [
        compute_forward_returns(
            screens_like, market, hold_weeks=w, dividends=dividends,
            trading_days_per_week=tdpw, clip_daily_return_pct=clip,
        )
        for w in weeks
    ]
    frames = [f for f in frames if not f.is_empty()]
    hold_summary = strategy_summary(pl.concat(frames) if frames else pl.DataFrame())

    diff_df = None
    if diff:
        enriched_by_week: dict[str, pl.DataFrame] = {}
        for w in picks["week"].unique().to_list():
            p = reports_dir / w / "candidates_enriched.csv"
            if p.exists():
                try:
                    enriched_by_week[w] = pl.read_csv(
                        p, schema_overrides={"stock_id": pl.Utf8}
                    )
                except Exception as e:  # noqa: BLE001 — 單週 enriched 壞掉不擋解剖
                    console.print(f"[yellow]讀 {p} 失敗（{e}），該週訊號留空[/yellow]")
        diff_df = week_over_week_diff(picks, enriched_by_week)

    data_range = (market["date"].min(), market["date"].max())
    report = render_outcome_report(
        todate, layers, weekly_core, hold_summary, counterfactual, excluded_todate,
        missing, cut, data_range, diff=diff_df, min_sample_warn=min_sample_warn,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md_path = out_dir / f"outcome_{tag}.md"
    md_path.write_text(report, encoding="utf-8")
    todate.write_csv(out_dir / f"picks_returns_{tag}.csv")
    if not excluded_todate.is_empty():
        excluded_todate.write_csv(out_dir / f"excluded_returns_{tag}.csv")
    if diff_df is not None and not diff_df.is_empty():
        diff_df.write_csv(out_dir / f"layer_diff_{tag}.csv")
    console.print(f"[green]報告 → {md_path}[/green]")

    if missing:
        console.print(f"[yellow]⚠️ 缺 picks.csv 的週：{'、'.join(missing)}（產物斷供）[/yellow]")
    for r in weekly_core.iter_rows(named=True):
        console.print(
            f"  {r['week']} 核心：平均 {r['avg_return_pct']:+.2f}%・"
            f"市場中位 {r['market_return_pct']:+.2f}%・α {r['avg_alpha_pct']:+.2f}pp・"
            f"勝 {r['wins']}/{r['n']}"
        )
    for r in layers.iter_rows(named=True):
        console.print(
            f"  [bold]{r['layer']}[/bold]：n={r['n']}・勝率 {r['win_rate']:.0%}・"
            f"平均 {r['avg_return_pct']:+.2f}%・α(大盤) {r['avg_alpha_market_pct']:+.2f}pp"
        )
    if not counterfactual.is_empty():
        top = counterfactual.row(0, named=True)
        console.print(
            f"  [bold]偽陰性最大旗標[/bold]：{top['reason']}（n={top['n']}・"
            f"平均 {top['avg_return_pct']:+.2f}%・跑贏大盤 {top['beat_market_rate']:.0%}）"
        )
    if diff_df is not None:
        console.print(f"  翻轉解剖：{diff_df.height} 筆降級（詳報告 §5）")
