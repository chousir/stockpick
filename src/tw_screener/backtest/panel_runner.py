"""ground-truth 面板編排（WS-A2＋WS-H.2/K.1；自 cli.py 薄殼呼叫）。

載入 日線/除息/三大法人/融資融券/TDCC 集保/regime 標籤 快取＋concepts.yaml 次產業 →
panel 純函式（panel_start 裁切＋chip/div_coverage 旗標）→ research/panel/panel.parquet
＋build 報告（含逐年覆蓋率段）＋核價抽查（vs 該週 Goodinfo screen_result 收盤）。
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


def _load_institutional_all(cache_dir: Path) -> tuple[pl.DataFrame, frozenset[str]]:
    """讀全部 institutional_*.parquet（含 _otc_）→ 法人日淨額長表 ＋ OTC 股清單（無檔回空）。

    OTC 股清單＝曾出現在 institutional_otc_*.parquet 的 stock_id 聯集——chip_coverage
    市場段判定用；不另開資料源，沿用既有法人快取的檔名切分（minimal path）。
    """
    files = sorted(cache_dir.glob("institutional_*.parquet"))
    if not files:
        return pl.DataFrame(), frozenset()
    need = ["date", "stock_id", "foreign_net", "trust_net", "dealer_net", "total_net"]
    frames = []
    for f in files:
        try:
            lf = pl.scan_parquet(f).select(need).with_columns(
                pl.lit("_otc_" in f.name).alias("_is_otc")
            )
            frames.append(lf)
        except Exception as e:  # noqa: BLE001 — 單日法人檔壞掉不擋面板
            console.print(f"[yellow]法人快取 {f.name} 讀取失敗（{e}），跳過[/yellow]")
    if not frames:
        return pl.DataFrame(), frozenset()
    merged = pl.concat(frames, how="vertical_relaxed").collect()
    otc_ids = frozenset(merged.filter(pl.col("_is_otc"))["stock_id"].unique().to_list())
    return merged.drop("_is_otc"), otc_ids


def _load_margin_all(cache_dir: Path) -> pl.DataFrame:
    """讀全部 margin_*.parquet（僅上市；排除舊版 margin_otc_/5 欄遺留檔）→ 融資融券長表。

    schema 防禦同 twse.py load_margin_signals：排除欄位不符本版 _MARGIN_SCHEMA 的舊快取，
    避免 concat 衝突（無檔／全不符回空表，呼叫端降級為 margin_balance_lots 全 null）。
    """
    need = ["date", "stock_id", "margin_balance"]
    files = sorted(
        f for f in cache_dir.glob("margin_*.parquet") if not f.name.startswith("margin_otc_")
    )
    if not files:
        return pl.DataFrame()
    frames = []
    for f in files:
        try:
            df = pl.read_parquet(f)
        except Exception as e:  # noqa: BLE001 — 單日融資檔壞掉不擋面板
            console.print(f"[yellow]融資快取 {f.name} 讀取失敗（{e}），跳過[/yellow]")
            continue
        if set(need).issubset(df.columns):
            frames.append(df.select(need))
        else:
            console.print(f"[yellow]略過 schema 不符的舊版融資快取 {f.name}[/yellow]")
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _load_tdcc_all(cache_dir: Path) -> pl.DataFrame:
    """讀全部 tdcc_distribution_*.parquet（原始級距長表）→ derive_big_holders() 週度大戶占比。"""
    from tw_screener.data.tdcc import derive_big_holders

    files = sorted(cache_dir.glob("tdcc_distribution_*.parquet"))
    if not files:
        return pl.DataFrame()
    frames = []
    for f in files:
        try:
            frames.append(pl.read_parquet(f))
        except Exception as e:  # noqa: BLE001 — 單週 TDCC 檔壞掉不擋面板
            console.print(f"[yellow]TDCC 快取 {f.name} 讀取失敗（{e}），跳過[/yellow]")
    if not frames:
        return pl.DataFrame()
    return derive_big_holders(pl.concat(frames, how="vertical_relaxed"))


def _load_regime_labels(path: Path) -> pl.DataFrame:
    """讀 regime_labels.parquet（WS-H.3 另一模組產出）；不存在／讀失敗 → 空表如實揭露未產。"""
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001 — 檔壞不擋面板本體
        console.print(f"[yellow]regime 標籤讀取失敗（{e}），視同未產[/yellow]")
        return pl.DataFrame()


def _load_delisted_ids(path: Path) -> list[str]:
    """讀 WS-J.3 官方下市清單 csv（stock_id 欄）；缺檔/壞檔 → 空清單如實揭露未標。"""
    if not path.exists():
        return []
    try:
        return pl.read_csv(path, schema_overrides={"stock_id": pl.Utf8})["stock_id"].to_list()
    except Exception as e:  # noqa: BLE001 — 清單壞不擋面板本體
        console.print(f"[yellow]下市清單讀取失敗（{e}），delisted 全 False[/yellow]")
        return []


def apply_delisted_flag(panel: pl.DataFrame, delisted_ids: list[str]) -> pl.DataFrame:
    """面板加 delisted 欄（WS-J.3）：官方下市清單成員＝True。

    語義：True 列＝該股其後（或清單日）已下市——r+k 尾端 null 屬「下市給 null 不給 0」
    邊界（playbook/20 §6.3），研究端可據此欄分離生存者。清單空＝全 False（未標非無）。
    """
    return panel.with_columns(pl.col("stock_id").is_in(delisted_ids).alias("delisted"))


def _coverage_report_lines(panel: pl.DataFrame, otc_stock_ids: frozenset[str]) -> list[str]:
    """逐年覆蓋率表（籌碼/除息/融資/TDCC）＋基準口徑警語（build 報告用，純揭露不裁決）。"""
    if panel.is_empty():
        return ["> 面板為空，無覆蓋率可報。"]
    p = panel.with_columns(
        pl.col("date").dt.year().alias("_year"),
        pl.when(pl.col("stock_id").is_in(list(otc_stock_ids)))
        .then(pl.lit("otc"))
        .otherwise(pl.lit("listed"))
        .alias("_market"),
    )
    lines = [
        "| 年 | 面板列數 | 宇宙檔數 | chip_coverage(上市) | chip_coverage(上櫃) "
        "| margin join(僅上市) | TDCC 週數 | div_coverage |",
        "|---|---|---|---|---|---|---|---|",
    ]
    def _rate(s: pl.Series) -> str:
        m = s.mean()
        return f"{float(m):.1%}" if isinstance(m, (int, float)) else "—"

    for y in sorted(p["_year"].unique().to_list()):
        yp = p.filter(pl.col("_year") == y)
        listed = yp.filter(pl.col("_market") == "listed")
        otc = yp.filter(pl.col("_market") == "otc")
        chip_listed = _rate(listed["chip_coverage"]) if listed.height else "—"
        chip_otc = _rate(otc["chip_coverage"]) if otc.height else "—"
        margin_rate = _rate(listed["margin_balance_lots"].is_not_null()) if listed.height else "—"
        tdcc_weeks = yp.filter(pl.col("big_holder_pct").is_not_null())["date"].n_unique()
        div_rate = _rate(yp["div_coverage"])
        lines.append(
            f"| {y} | {yp.height} | {yp['stock_id'].n_unique()} | {chip_listed} | {chip_otc} "
            f"| {margin_rate} | {tdcc_weeks} | {div_rate} |"
        )
    lines += [
        "",
        "> **基準口徑警語（W28 二輪 bulk 回補後）**：**上市段全年皆全市場**"
        "（2022-2024 走 MI_INDEX bulk 逐日回補、2025+ 為日快照）；**上櫃段 2022-2024 僅"
        "次產業成員**（今日 concepts∩OTC，TPEX 無 bulk 歷史端點、只能逐檔補成員），"
        "2025+ 才全市場。故 mkt_ew 等權基準在 2022-2024 相對少計上櫃小型股、口徑偏上市，"
        "跨段比較 alpha 要看此段（純多頭 2025+ 全市場 vs 2022-2024 上市全+上櫃成員）。",
    ]
    return lines


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
    panel_start_raw = pn.get("panel_start")
    panel_start = date.fromisoformat(str(panel_start_raw)) if panel_start_raw else None
    regime_path_raw = cfg.get("backtest", {}).get("regime_history", {}).get("output_path")
    regime_path = Path(regime_path_raw) if regime_path_raw else None

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
    institutional, otc_stock_ids = _load_institutional_all(cache_dir)
    margin = _load_margin_all(cache_dir)
    tdcc = _load_tdcc_all(cache_dir)
    regime = _load_regime_labels(regime_path) if regime_path is not None else pl.DataFrame()
    membership = list_subindustries()

    n_inst = institutional.height if not institutional.is_empty() else 0
    n_div = dividends.height if dividends is not None and not dividends.is_empty() else 0
    console.print(
        f"  價格 {price.height} 列・法人 {n_inst} 列（OTC {len(otc_stock_ids)} 檔）・"
        f"除息 {n_div} 筆・融資 {margin.height} 列・TDCC {tdcc.height} 檔・"
        f"regime {regime.height} 列"
    )
    panel = build_price_panel(
        price,
        dividends=dividends,
        institutional=institutional,
        membership=membership,
        horizons=horizons,
        ma_windows=ma_windows,
        vol_lookback=vol_lookback,
        otc_stock_ids=otc_stock_ids,
        panel_start=panel_start,
        margin=margin,
        tdcc=tdcc,
        regime=regime,
    )
    if panel.is_empty():
        console.print("[red]面板為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    delisted_raw = pn.get("delisted_list")
    delisted_ids = _load_delisted_ids(Path(str(delisted_raw))) if delisted_raw else []
    panel = apply_delisted_flag(panel, delisted_ids)
    n_delisted_in_panel = (
        panel.filter(pl.col("delisted"))["stock_id"].n_unique() if delisted_ids else 0
    )
    console.print(
        f"  delisted 標記：清單 {len(delisted_ids)} 檔、面板內有價者 {n_delisted_in_panel} 檔"
        if delisted_ids
        else "  delisted 標記：清單未產（research/survivorship/），全 False"
    )

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
        "## 覆蓋率（逐年；WS-H.2/K.1）",
        "",
        *_coverage_report_lines(panel, otc_stock_ids),
        "",
    ]
    if regime_path is None or regime.is_empty():
        lines.append("> regime 標籤未產（先跑 make regime-history）。")
        lines.append("")
    lines += [
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
        verdict = "PASS" if rate >= pass_rate else "FAIL"
        lines += [
            f"- **核價判定：{verdict}**——樣本 {n} 筆（{recon['stock_id'].n_unique()} 檔）；"
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
