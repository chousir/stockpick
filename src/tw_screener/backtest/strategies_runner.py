"""個股策略回測閉環（backtest strategies）研究軌編排（自 cli.py 下沉）。

V1：量化 D/E/F/G 入選後各持有窗的勝率/報酬/回撤 vs 大盤，產 research/strategy_backtest/。
CLI 只保留參數解析＋呼叫 run_backtest_strategies。
"""

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_backtest_strategies(hold_weeks: str | None, out_dir: Path, settings: Path) -> None:
    """V1 個股策略回測閉環：量化 D/E/F/G 入選後各持有窗的勝率/報酬/回撤 vs 大盤。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.backtest.strategies import (
        compute_forward_returns,
        load_historical_screens,
        render_backtest_report,
        strategy_summary,
    )
    from tw_screener.data.twse import load_recent_dividends

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    bt = cfg.get("backtest", {}).get("strategies", {})
    weeks = (
        [int(w) for w in hold_weeks.split(",") if w.strip()]
        if hold_weeks
        else [int(w) for w in bt.get("hold_weeks", [2, 4, 8, 12])]
    )
    history_days = int(bt.get("history_days", 250))
    clip = float(bt.get("clip_daily_return_pct", 10.0))
    tdpw = int(bt.get("trading_days_per_week", 5))
    min_sample_warn = int(bt.get("min_sample_warn", 20))
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    console.print(f"[bold]載入入選快照（{reports_dir}）...[/bold]")
    screens = load_historical_screens(reports_dir)
    if screens.is_empty():
        console.print("[red]無 screen_result_*.csv 入選快照——先跑 make week 累積週報[/red]")
        raise typer.Exit(1)
    n_weeks = screens["week_tag"].n_unique()
    market = load_market_history(cache_dir, n_days=history_days)
    if market.is_empty():
        console.print("[red]無日線快取（daily_*.parquet）——先跑 data fetch-twse[/red]")
        raise typer.Exit(1)
    since = screens["screened_at"].min()
    dividends = (
        load_recent_dividends(cache_dir, since)
        if isinstance(since, _date)
        else pl.DataFrame()
    )
    console.print(
        f"入選 {screens.height} 筆・{n_weeks} 週・{screens['strategy_id'].n_unique()} 策略；"
        f"持有窗 {weeks} 週"
    )

    frames = [
        compute_forward_returns(
            screens, market, hold_weeks=w, dividends=dividends,
            trading_days_per_week=tdpw, clip_daily_return_pct=clip,
        )
        for w in weeks
    ]
    returns = pl.concat([f for f in frames if not f.is_empty()]) if frames else pl.DataFrame()
    summary = strategy_summary(returns)

    data_range = (market["date"].min(), market["date"].max())
    report = render_backtest_report(
        summary, returns, data_range, n_weeks, min_sample_warn=min_sample_warn
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md_path = out_dir / f"summary_{tag}.md"
    md_path.write_text(report, encoding="utf-8")
    if not returns.is_empty():
        returns.write_csv(out_dir / f"returns_{tag}.csv")
        summary.write_csv(out_dir / f"summary_{tag}.csv")
    console.print(f"[green]報告 → {md_path}[/green]")
    if summary.is_empty():
        console.print("[yellow]無可統計樣本（多數未到期，屬正常——樣本仍薄）。[/yellow]")
        return
    console.print("\n[bold]各策略 × 持有窗：[/bold]")
    for r in summary.iter_rows(named=True):
        wr = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        avg = f"{r['avg_return_pct']:+.1f}%" if r["avg_return_pct"] is not None else "—"
        exc = f"{r['avg_excess_pct']:+.1f}%" if r["avg_excess_pct"] is not None else "—"
        warn = " ⚠️樣本小" if (r["sample_count"] or 0) < min_sample_warn else ""
        console.print(
            f"  {r['strategy_id']} / {r['hold_weeks']}週：勝率 {wr}・平均 {avg}"
            f"・超額 {exc}・n={r['sample_count']}{warn}"
        )
