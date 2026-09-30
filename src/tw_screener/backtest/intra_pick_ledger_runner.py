"""M-Pick3c 前瞻台帳編排（docs/35；自 cli.py 薄殼呼叫）。

讀 reports/<週>/shortlist.csv（真實池）→ 載日線快取（近 history_days 交易日、M-Pick2 同來源
優先序）與 FinMind 財報／月營收快取 → intra_pick 純函式算 F1–F4（point-in-time，只取
shortlist 的 data_date 當日）→ 併入 research/intra_pick_ledger/ledger.csv。純本地檔案、不打網。

make week 在 shortlist 之後容錯呼叫（失敗不擋主流程；week-check 會點名台帳缺週）。
規則（docs/35 §2）：週次早於 start_week 拒寫；對某週的任何寫入（含首次）須在
data_date + rewrite_days 日內，逾期拒寫（不得事後才決定要記哪幾週）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from loguru import logger
from rich.console import Console

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_ledger as il
from tw_screener.report.shortlist import ShortlistConfig, load_shortlist
from tw_screener.report.shortlist_runner import _resolve_week_dir

console = Console()

# 與 M-Pick2 build_stock_weeks 相同的來源優先序：
# stock_day 個股月檔 → daily 上市全市場 → otc_daily 上櫃
_PRICE_PATTERNS = ("stock_day_*.parquet", "daily_*.parquet", "otc_daily_*.parquet")

_PRICE_HINT = (
    "日線快取在窗內有缺日（因子定義：窗內缺日即 null、不補值）。"
    "已知成因：個股月檔月中抓取（R2 前被當永久快取）而殘缺、全市場日檔只在跑流程那幾天才有；"
    "上市股可 make backfill-daily-history START=… END=… 補齊，"
    "暫定月檔月結後會自動重抓（make week 的 fetch-candidates-history，"
    "或 uv run tw-screener data backfill-otc-history 掃上櫃成員；docs/35 §5）"
)
_FACTOR_HINTS = {
    "mom_6_1": f"F1 動能——{_PRICE_HINT}",
    "high52_near": f"F2 52 週高點——{_PRICE_HINT}",
    "eps_accel": "F3 EPS 加速——財報快取缺該季或前四季",
    "rev_accel": "F4 營收加速——月營收快取缺連續 18 個月",
}
_MAX_LISTED = 12


def _refuse(err: il.LedgerFrozenError) -> int:
    """逾期／凍結：底帳不動，回 exit code 1。"""
    logger.warning("intra-pick-ledger：{}", err)
    console.print(f"[red]{err}——底帳未動[/red]")
    return 1


def _ledger_unreadable(path: Path, err: Exception) -> int:
    """既有底帳讀寫失敗（損毀／欄位不符）：不自動修復、不覆寫，回 exit code 1。"""
    logger.warning("intra-pick-ledger：底帳 {} 讀寫失敗：{}", path, err)
    console.print(
        f"[red]底帳 {path} 讀寫失敗（{err}）——檔案損毀或欄位不符，請人工檢查；"
        f"本週台帳未寫、底帳未動[/red]"
    )
    return 1


def _print_summary(
    summary: dict[str, Any],
    rows: pl.DataFrame,
    week: str,
    ledger: pl.DataFrame,
    path: Path,
    min_coverage: float,
) -> None:
    coverage: dict[str, float] = summary["coverage"]
    cov_txt = "・".join(f"{c} {v:.0%}" for c, v in coverage.items()) or "—"
    console.print(
        f"[green]{week} 台帳：池 {summary['n_rows']} 列，"
        f"合格成員（top／alt／capped）{summary['n_main']} 檔／{summary['n_groups']} 組"
        f"（≥2 檔 {summary['n_groups_ge2']} 組），機器 Top {summary['n_top']}[/green]"
    )
    console.print(f"因子覆蓋率（合格成員內非 null）：{cov_txt}")
    main = rows.filter(pl.col("tier").is_in(il.MAIN_TIERS))
    for c in summary["low_coverage"]:
        console.print(
            f"[yellow]⚠️ {c} 覆蓋率 {coverage[c]:.0%} < {min_coverage:.0%}："
            f"{_FACTOR_HINTS[c]}[/yellow]"
        )
        missing = main.filter(pl.col(c).is_null())
        listed = "、".join(
            f"{r['stock_id']} {r['name'] or ''}".strip()
            for r in missing.head(_MAX_LISTED).iter_rows(named=True)
        )
        more = f" 等 {missing.height} 檔" if missing.height > _MAX_LISTED else ""
        console.print(f"[yellow]   {c} 缺值的合格成員：{listed}{more}[/yellow]")
    console.print(
        f"[dim]底帳累積 {ledger['week'].n_unique()} 週、{ledger.height} 列 → {path}[/dim]"
    )


def run_intra_pick_ledger(
    settings: Path, week: str | None = None, *, now: datetime | None = None
) -> int:
    """M-Pick3c：把 reports/<週>/shortlist.csv 的真實池＋凍結因子值 upsert 進底帳；回傳 exit code。

    0＝成功，或依設定跳過（週次早於 start_week）；
    1＝缺輸入／資料日不在日線曆／該週已凍結（底帳不動）。now 僅供測試注入（預設現在的 UTC 時間）。
    """
    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.data.finmind import load_financials_history, load_month_revenue_history

    now = now or datetime.now(UTC)
    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    lc = (cfg.get("backtest") or {}).get("intra_pick_ledger") or {}
    start_week = lc.get("start_week")
    if not start_week:
        console.print(
            "[red]settings backtest.intra_pick_ledger.start_week 未設定"
            "——台帳不寫（起點是乾淨樣本的定義）[/red]"
        )
        return 1
    rewrite_days = int(lc.get("rewrite_days", 7))
    history_days = int(lc.get("history_days", 320))
    ledger_path = Path(lc.get("ledger_path", "research/intra_pick_ledger/ledger.csv"))

    week_dir = _resolve_week_dir(Path(cfg["paths"]["reports_dir"]), week)
    if week_dir is None:
        return 1
    week_tag = week_dir.name
    try:
        before_start = il.is_before_start(week_tag, str(start_week))
    except ValueError as e:
        console.print(f"[red]週次 {week_tag!r} 或 start_week 格式不合（{e}）——本週台帳未寫[/red]")
        return 1
    if before_start:
        console.print(f"[dim]{week_tag} 早於乾淨樣本起點 {start_week}，不寫台帳[/dim]")
        return 0

    shortlist = load_shortlist(week_dir)
    if shortlist is None or shortlist.is_empty():
        logger.warning("intra-pick-ledger：{} 無可用 shortlist.csv", week_dir)
        console.print(f"[red]{week_dir} 無 shortlist.csv——先跑 make shortlist；本週台帳未寫[/red]")
        return 1
    if "week" not in shortlist.columns or "data_date" not in shortlist.columns:
        console.print("[red]shortlist.csv 缺 week／data_date 欄——本週台帳未寫[/red]")
        return 1
    csv_weeks = shortlist["week"].drop_nulls().unique().to_list()
    if csv_weeks != [week_tag]:
        console.print(
            f"[red]shortlist.csv 的 week 欄 {csv_weeks} 與目錄名 {week_tag} 不符"
            f"（檔案被複製到別週？）——本週台帳未寫[/red]"
        )
        return 1
    data_dates = shortlist["data_date"].cast(pl.Date, strict=False).drop_nulls().unique().to_list()
    if len(data_dates) != 1:
        console.print(
            f"[red]shortlist.csv 的 data_date 不唯一或缺（{data_dates}）——本週台帳未寫[/red]"
        )
        return 1
    data_date = data_dates[0]

    # 寫入期限先驗（載入日線快取要 ~1 分鐘，逾期就不必算）；upsert_ledger 內仍會再驗一次
    try:
        il.check_write_window(
            il.read_ledger(ledger_path), week_tag, data_date, now=now, rewrite_days=rewrite_days
        )
    except il.LedgerFrozenError as e:
        return _refuse(e)
    except (OSError, pl.exceptions.PolarsError) as e:
        return _ledger_unreadable(ledger_path, e)

    ip_cfg = ip.IntraPickConfig.from_settings(cfg)
    cache_root = Path(cfg["paths"]["cache_dir"])
    console.print(
        f"[bold]{week_tag} 台帳（資料日 {data_date}）："
        f"載入日線快取近 {history_days} 交易日...[/bold]"
    )
    price = load_market_history(cache_root / "twse", n_days=history_days, patterns=_PRICE_PATTERNS)
    calendar = ip.trading_calendar(price, ip_cfg.calendar_min_names)
    if data_date not in set(calendar):
        console.print(
            f"[red]資料日 {data_date} 不在日線快取的交易日曆內"
            f"（當日有價 ≥ {ip_cfg.calendar_min_names} 檔才算）——日線未抓齊？本週台帳未寫[/red]"
        )
        return 1
    feats = ip.price_features(price.filter(pl.col("date").is_in(calendar)), calendar, ip_cfg)

    finmind_dir = cache_root / "finmind"
    fin = load_financials_history(finmind_dir)
    rev = load_month_revenue_history(finmind_dir)
    for note in il.cache_staleness(fin, rev, data_date, ip_cfg):
        logger.warning("intra-pick-ledger：{}", note)
        deadline = il.rewrite_deadline(data_date, rewrite_days)
        console.print(f"[yellow]⚠️ {note}；補齊後於 {deadline} 前重跑本指令可覆寫本週[/yellow]")
    keys = pl.DataFrame(
        {"date": [data_date] * shortlist.height, "stock_id": shortlist["stock_id"].cast(pl.Utf8)},
        schema={"date": pl.Date, "stock_id": pl.Utf8},
    )
    eps = ip.eps_asof(keys, ip.eps_accel_table(fin, ip.quarter_end_close(feats)), ip_cfg)
    revf = ip.revenue_asof(keys, ip.revenue_accel_table(rev), ip_cfg.revenue_available_day)

    try:
        rows = il.build_ledger_rows(
            shortlist,
            feats,
            eps,
            revf,
            sl_cfg=ShortlistConfig.from_settings(cfg),
            ip_cfg=ip_cfg,
            recorded_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except (ValueError, pl.exceptions.PolarsError) as e:
        logger.warning("intra-pick-ledger：shortlist.csv 與台帳規格不符：{}", e)
        console.print(f"[red]shortlist.csv 缺欄或型別異常（{e}）——本週台帳未寫[/red]")
        return 1
    try:
        ledger = il.upsert_ledger(ledger_path, rows, now=now, rewrite_days=rewrite_days)
    except il.LedgerFrozenError as e:
        return _refuse(e)
    except (OSError, pl.exceptions.PolarsError) as e:
        return _ledger_unreadable(ledger_path, e)
    _print_summary(
        il.week_summary(rows, ip_cfg.min_coverage),
        rows,
        week_tag,
        ledger,
        ledger_path,
        ip_cfg.min_coverage,
    )
    return 0
