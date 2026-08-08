"""pick 閉環（picks outcome）編排（規劃書 05 F1 PO2–PO4；自 cli.py 薄殼呼叫）。

載入 pick_store 底帳＋日線/除息快取＋次產業成員 → picks_outcome 純函式 →
research/pick_outcome/ 報告與 CSV。CLI 只保留參數解析。
"""

from datetime import date
from pathlib import Path
from typing import cast

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
        _CONTRARIAN_RECALL_HORIZONS,
        compute_todate_returns,
        contrarian_recall_check,
        counterfactual_summary,
        layer_summary,
        render_outcome_report,
        stop_delay_ledger,
        stop_delay_summary,
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
    def _forward(w: int) -> pl.DataFrame:
        return compute_forward_returns(
            screens_like, market, hold_weeks=w, dividends=dividends,
            trading_days_per_week=tdpw, clip_daily_return_pct=clip,
        )

    by_hold = {w: _forward(w) for w in sorted(set(weeks) | set(_CONTRARIAN_RECALL_HORIZONS))}
    frames = [by_hold[w] for w in weeks if not by_hold[w].is_empty()]
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

    # M3.1 停損延遲帳：條件單語意 vs 週頻覆核的執行價差
    stop_delay = stop_delay_ledger(picks, market)

    # M1.6 左側解禁回收條款：左側票 vs 一般機會層的 r+10/r+20 α 與勝率
    cb = cfg.get("contrarian_base", {}) or {}
    since_raw = cb.get("recall_unblocked_since")
    recall = contrarian_recall_check(
        picks, by_hold,
        prefix=str(cb.get("thesis_prefix", "左側M-BR1")),
        min_picks=int(cb.get("recall_min_picks", 10)),
        min_weeks=int(cb.get("recall_min_weeks", 12)),
        unblocked_since=(
            since_raw if isinstance(since_raw, _date)
            else _date.fromisoformat(str(since_raw)) if since_raw else None
        ),
        as_of=cut,
    )

    data_range = (market["date"].min(), market["date"].max())
    report = render_outcome_report(
        todate, layers, weekly_core, hold_summary, counterfactual, excluded_todate,
        missing, cut, data_range, diff=diff_df, min_sample_warn=min_sample_warn,
        stop_delay=stop_delay, contrarian_recall=recall,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md_path = out_dir / f"outcome_{tag}.md"
    md_path.write_text(report, encoding="utf-8")
    todate.write_csv(out_dir / f"picks_returns_{tag}.csv")
    if not stop_delay.is_empty():
        stop_delay.write_csv(out_dir / f"stop_delay_{tag}.csv")
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
    sd = stop_delay_summary(stop_delay)
    if sd["n_measured"]:
        console.print(
            f"  [bold]停損延遲帳[/bold]：可量測 {sd['n_measured']} 筆・"
            f"平均延遲成本 {float(cast(float, sd['avg_cost_pct'])):+.2f}%・"
            f"最痛 {sd['worst_stock']} {float(cast(float, sd['worst_cost_pct'])):+.2f}%"
        )
    else:
        counts = cast(dict, sd["counts"])
        console.print(
            f"  停損延遲帳：無可量測樣本（未觸發 {counts['not_triggered']}／"
            f"未到覆核點 {counts['pending_review']}／停損價無法解析 {counts['unparsed']}）"
        )
    if recall["warn"]:
        console.print(
            "  [bold red]🔴 左側解禁回收警告[/bold red]：α 與勝率同時劣於一般機會層"
            "（詳報告 §7）——回退＝settings.contrarian_base.picks_unblocked: false"
        )
    else:
        console.print(f"  左側解禁記帳：{recall['n_left']} 筆（詳報告 §7）")


def run_picks_brief(settings: Path, week: str | None = None) -> None:
    """WS-A3：上週 picks r+5 brief 一頁 md → 最新週報目錄（輸入包）。

    評估週＝底帳中「r+5 已到期」的最近一週；無到期週 → 佔位頁（誠實標註）。
    掛 make week 末段（容錯：底帳/快取缺不擋主流程，exit 0）。

    week（WS-L，可選）：指定則改評估該週、輸出寫到 reports/<week>/pick_outcome_brief.md
    （該週不存在 → exit 1）；未指定＝現行預設行為，一個位元組都不變。
    """
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.backtest.picks_outcome import render_weekly_brief
    from tw_screener.backtest.strategies import compute_forward_returns
    from tw_screener.data.twse import load_recent_dividends
    from tw_screener.report.pick_store import load_all_excluded, load_all_picks, week_dirs

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    pk = cfg.get("backtest", {}).get("picks", {})
    history_days = int(pk.get("history_days", 250))
    tdpw = int(pk.get("trading_days_per_week", 5))
    clip = float(pk.get("clip_daily_return_pct", 10.0))
    brief_name = str(pk.get("brief_filename", "pick_outcome_brief.md"))
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    dirs = week_dirs(reports_dir)
    if not dirs:
        console.print("[yellow]無週報目錄——brief 跳過[/yellow]")
        return
    if week is not None and not (reports_dir / week).is_dir():
        console.print(
            f"[red]{reports_dir / week} 不存在——week 應為 reports/ 下的週次目錄名[/red]"
        )
        raise typer.Exit(1)

    picks = load_all_picks(reports_dir)
    if picks.is_empty():
        console.print("[yellow]無 picks 底帳——brief 跳過[/yellow]")
        return
    market = load_market_history(cache_dir, n_days=history_days)
    if market.is_empty():
        console.print("[yellow]無日線快取——brief 跳過[/yellow]")
        return
    since = picks["data_date"].min()
    dividends = (
        load_recent_dividends(cache_dir, since) if isinstance(since, date) else pl.DataFrame()
    )

    def _returns(rows: pl.DataFrame, tag_col: str) -> pl.DataFrame:
        if rows.is_empty():
            return pl.DataFrame()
        screens_like = rows.select(
            pl.col("week").alias("week_tag"),
            pl.col("data_date").alias("screened_at"),
            "stock_id",
            "name",
            pl.col(tag_col).alias("strategy_id"),
        )
        return compute_forward_returns(
            screens_like, market, hold_weeks=1, dividends=dividends,
            trading_days_per_week=tdpw, clip_daily_return_pct=clip,
        )

    def _attach_late_entry(
        returns_df: pl.DataFrame, ledger: pl.DataFrame, week_val: str
    ) -> pl.DataFrame:
        """把底帳（picks.csv／excluded.csv）的 late_entry 併回 compute_forward_returns 輸出。

        compute_forward_returns 只認 week_tag/stock_id/name/strategy_id/screened_at 白名單，
        late_entry 不隨 screens_like 通過，故另外以 (week, stock_id) join 回來（WS-L）。
        """
        if returns_df.is_empty() or ledger.is_empty():
            return returns_df
        late_map = (
            ledger.filter(pl.col("week") == week_val)
            .select("stock_id", "late_entry")
            .unique(subset="stock_id", keep="first")
        )
        return returns_df.join(late_map, on="stock_id", how="left").with_columns(
            pl.col("late_entry").fill_null(False)
        )

    all_r = _returns(picks, "layer")
    matured = (
        all_r.filter((pl.col("status") == "matured") & pl.col("return_pct").is_not_null())
        if not all_r.is_empty()
        else pl.DataFrame()
    )
    excluded_all = load_all_excluded(reports_dir)

    if week is not None:
        target_week = week
        out_path = reports_dir / week / brief_name
        picks_r = (
            matured.filter(pl.col("week_tag") == target_week)
            if not matured.is_empty()
            else pl.DataFrame()
        )
        excl_week = (
            excluded_all.filter(pl.col("week") == target_week)
            if not excluded_all.is_empty()
            else pl.DataFrame()
        )
        excl_r = _returns(excl_week, "reason")
    elif matured.is_empty():
        target_week = str(picks["week"].max())
        out_path = dirs[-1] / brief_name
        picks_r = pl.DataFrame()
        excl_r = pl.DataFrame()
    else:
        target_week = str(matured["week_tag"].max())
        out_path = dirs[-1] / brief_name
        picks_r = matured.filter(pl.col("week_tag") == target_week)
        excl_week = (
            excluded_all.filter(pl.col("week") == target_week)
            if not excluded_all.is_empty()
            else pl.DataFrame()
        )
        excl_r = _returns(excl_week, "reason")

    picks_r = _attach_late_entry(picks_r, picks, target_week)
    excl_r = _attach_late_entry(excl_r, excluded_all, target_week)

    dd = picks.filter(pl.col("week") == target_week)["data_date"]
    data_date = dd[0] if dd.len() else None
    report = render_weekly_brief(picks_r, excl_r, target_week, data_date, horizon_td=tdpw)
    out_path.write_text(report, encoding="utf-8")
    console.print(f"[green]pick brief（{target_week}）→ {out_path}[/green]")
