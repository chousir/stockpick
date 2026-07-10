"""ground-truth 面板編排（WS-A2；自 cli.py 薄殼呼叫）。

載入 日線/除息/三大法人 快取＋concepts.yaml 次產業 → panel 純函式 →
research/panel/panel.parquet＋build 報告＋核價抽查（vs 該週 Goodinfo screen_result 收盤）。
CLI 只保留參數解析。
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def _load_institutional_all(cache_dir: Path) -> pl.DataFrame:
    """讀全部 institutional_*.parquet（含 _otc_）→ 法人日淨額長表（無檔回空表）。"""
    files = sorted(cache_dir.glob("institutional_*.parquet"))
    if not files:
        return pl.DataFrame()
    need = ["date", "stock_id", "foreign_net", "trust_net", "dealer_net"]
    frames = []
    for f in files:
        try:
            lf = pl.scan_parquet(f).select(need)
            frames.append(lf)
        except Exception as e:  # noqa: BLE001 — 單日法人檔壞掉不擋面板
            console.print(f"[yellow]法人快取 {f.name} 讀取失敗（{e}），跳過[/yellow]")
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed").collect()


def _goodinfo_close_reference(reports_dir: Path) -> pl.DataFrame:
    """各週 screen_result_*.csv（Goodinfo 快照）→ 獨立核價來源（date/stock_id/close_ref）。"""
    from tw_screener.backtest.strategies import load_historical_screens

    screens = load_historical_screens(reports_dir)
    if screens.is_empty():
        return pl.DataFrame()
    return (
        screens.drop_nulls("screened_at")
        .drop_nulls("close")
        .select(
            pl.col("screened_at").alias("date"),
            "stock_id",
            pl.col("close").alias("close_ref"),
        )
        .unique(subset=["date", "stock_id"], keep="first")
    )


def run_build_panel(settings: Path, out_dir: Path | None) -> None:
    """建 ground-truth 面板 parquet＋build 報告＋核價抽查。"""
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest.panel import (
        build_price_panel,
        panel_summary,
        reconcile_close,
    )
    from tw_screener.data.twse import load_recent_dividends

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    pn = cfg.get("backtest", {}).get("panel", {})
    history_days = int(pn.get("history_days", 300))
    horizons = tuple(int(h) for h in pn.get("horizons_td", [5, 10, 20, 40]))
    ma_windows = tuple(int(w) for w in pn.get("ma_windows", [20, 60]))
    vol_lookback = int(pn.get("vol_lookback", 20))
    tol_pct = float(pn.get("reconcile_tol_pct", 0.5))
    pass_rate = float(pn.get("reconcile_pass_rate", 0.995))
    n_samples = int(pn.get("reconcile_samples", 10))
    out = out_dir or Path(pn.get("output_dir", "research/panel"))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    console.print(f"[bold]載入日線快取（近 {history_days} 交易日、三來源）...[/bold]")
    # 面板優先序（與生產 load 預設相反）：STOCK_DAY 月檔＝事後修訂的官方歷史，
    # 準確度優於當日快照 daily_all（實測 09-19 兩檔快照值未含盤後修訂）
    price = load_market_history(
        cache_dir,
        n_days=history_days,
        patterns=("stock_day_*.parquet", "daily_*.parquet", "otc_daily_*.parquet"),
    )
    if price.is_empty():
        console.print("[red]無日線快取——先跑 make fetch-twse / backfill-universe-history[/red]")
        raise typer.Exit(1)
    since = price["date"].min()
    dividends = load_recent_dividends(cache_dir, since) if isinstance(since, date) else None
    institutional = _load_institutional_all(cache_dir)
    membership = list_subindustries()

    n_inst = institutional.height if not institutional.is_empty() else 0
    n_div = dividends.height if dividends is not None and not dividends.is_empty() else 0
    console.print(f"  價格 {price.height} 列・法人 {n_inst} 列・除息 {n_div} 筆")
    panel = build_price_panel(
        price,
        dividends=dividends,
        institutional=institutional,
        membership=membership,
        horizons=horizons,
        ma_windows=ma_windows,
        vol_lookback=vol_lookback,
    )
    if panel.is_empty():
        console.print("[red]面板為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    out.mkdir(parents=True, exist_ok=True)
    pq = out / "panel.parquet"
    panel.write_parquet(pq)
    summary = panel_summary(panel, horizons=horizons)

    # 主核價：TWSE 兩獨立端點交叉（面板採 STOCK_DAY 修訂值；ref＝daily_all 當日快照）——
    # 分歧＝快照 vs 事後修訂，面板採修訂值；一致率過低才代表合併/解析有 bug
    rng = random.Random(20260710)
    px_snap = load_market_history(cache_dir, n_days=history_days, patterns=("daily_*.parquet",))
    recon = pl.DataFrame()
    if not px_snap.is_empty():
        ids = sorted(set(px_snap["stock_id"].to_list()) & set(panel["stock_id"].to_list()))
        sample_ids = rng.sample(ids, min(n_samples, len(ids))) if ids else []
        if sample_ids:
            ref = (
                px_snap.filter(pl.col("stock_id").is_in(sample_ids))
                .select("date", "stock_id", pl.col("close").alias("close_ref"))
                .drop_nulls("close_ref")
                .filter(pl.col("close_ref") > 0)
            )
            recon = reconcile_close(panel, ref, tol_pct=tol_pct)

    # 次要對照：Goodinfo screen_result 收盤（已知語義：收盤可能是 Goodinfo 抓檔日、
    # 晚於 screened_at 標籤——不當 pass/fail，只回報同日/次日一致率）
    gi = _goodinfo_close_reference(reports_dir)
    gi_same = gi_next = gi_total = 0
    if not gi.is_empty():
        nxt = panel.sort("stock_id", "date").with_columns(
            pl.col("close").shift(-1).over("stock_id").alias("close_next")
        )
        cmp_ = gi.join(
            nxt.select("date", "stock_id", "close", "close_next"),
            on=["date", "stock_id"],
            how="inner",
        ).with_columns(
            ((pl.col("close") - pl.col("close_ref")).abs() / pl.col("close_ref") * 100 < tol_pct)
            .alias("_same"),
            (
                (pl.col("close_next") - pl.col("close_ref")).abs() / pl.col("close_ref") * 100
                < tol_pct
            ).alias("_next"),
        )
        gi_total = cmp_.height
        gi_same = int(cmp_["_same"].sum())
        gi_next = int(cmp_.filter(~pl.col("_same"))["_next"].sum())

    tag = date.today().strftime("%Y%m%d")
    lines = [
        "# ground-truth 面板 build 報告（WS-A2）",
        "",
        f"- 建置日：{date.today()}；輸出：`{pq}`",
        f"- 前瞻窗：{list(horizons)}（交易日）；entry＝次一交易日收盤；除息線性加回；"
        "未到期/下市＝null。",
        "- 基準：mkt_ew_r{h}＝同日全面板中位（等權）；alpha{h}＝r{h}−基準。",
        "",
        "## 體檢",
        "",
        "| 指標 | 值 |",
        "|---|---|",
        *[f"| {r['metric']} | {r['value']} |" for r in summary.iter_rows(named=True)],
        "",
        "## 核價抽查（主：TWSE 兩獨立端點交叉；面板＝STOCK_DAY 修訂值、ref＝daily_all 快照）",
        "",
    ]
    if recon.is_empty():
        lines.append("> 無可比對樣本（無 daily_all 快取或無交集）——核價未執行，如實標註。")
        console.print("[yellow]核價抽查無樣本[/yellow]")
    else:
        n = recon.height
        n_ok = int(recon["within_tol"].sum())
        rate = n_ok / n if n else 0.0
        worst = recon.row(0, named=True)
        lines += [
            f"- 樣本 {n} 筆（{recon['stock_id'].n_unique()} 檔）；"
            f"差 <{tol_pct}%：{n_ok}/{n}（{rate:.2%}，通過線 {pass_rate:.1%}）；"
            f"最大差 {worst['diff_pct']:.3f}%（{worst['stock_id']} @ {worst['date']}）。",
            "- 分歧筆＝daily_all 當日快照 vs STOCK_DAY 事後修訂；面板採修訂值（逐筆如下）。",
            "",
            "| date | stock_id | panel(修訂) | ref(快照) | diff% |",
            "|---|---|---|---|---|",
            *[
                f"| {r['date']} | {r['stock_id']} | {r['close_panel']} | {r['close_ref']} "
                f"| {r['diff_pct']:.3f} |"
                for r in recon.filter(~pl.col("within_tol")).head(15).iter_rows(named=True)
            ],
        ]
        status = "[green]PASS[/green]" if rate >= pass_rate else "[red]FAIL[/red]"
        console.print(
            f"  主核價（TWSE 端點交叉）：{n_ok}/{n}（{rate:.2%}）在 {tol_pct}% 內 {status}"
        )
    lines += [
        "",
        "## 次要對照（Goodinfo screen_result 收盤・僅揭露不裁決）",
        "",
    ]
    if gi_total == 0:
        lines.append("> 無 screen_result 快照可比。")
    else:
        lines.append(
            f"- 同 (screened_at, stock) 共 {gi_total} 筆：同日收盤一致 {gi_same}、"
            f"**次一交易日收盤一致 {gi_next}**、皆不合 {gi_total - gi_same - gi_next}。"
            "次日一致＝screen_result 收盤實為 Goodinfo 抓檔日（晚於 screened_at 標籤）之價，"
            "屬既知快照語義，非面板價格錯誤。"
        )
        console.print(
            f"  Goodinfo 對照：同日 {gi_same}／次日 {gi_next}／其他 "
            f"{gi_total - gi_same - gi_next}（共 {gi_total}）"
        )

    md = out / f"panel_build_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]面板 → {pq}（{panel.height} 列）；報告 → {md}[/green]")
