"""backtest/redesign_prelim_read.py — docs/31 §21.4：G1/G2/G4/G5/L6 初步（非規劃書
§7.4正式驗證）cross-sectional forward alpha讀值。

用`research/g1_g2_g5_watch/ledger.csv`／`research/l6_g4_watch/ledger.csv`已經
逐週累積的快照，接上即時日線快取算forward alpha，看命中列相對當週全樣本的
delta方向。**明確非正式驗證**：樣本量小（幾週、fundamentals同季內不變，同一季內
連續週的旗標值完全相同，只有價格結果在變）、未做regime切片、CI在樣本不足時
（`moving_block_bootstrap_ci`的T<10門檻）會回`(None, None)`——如實印出，不假裝
有效、不隱藏。

重用`backtest/panel.py`的`build_price_panel()`算r{h}/mkt_ew_r{h}/alpha{h}（只傳
price，不傳dividends/institutional等選配輸入——這是初步讀值不是正式面板），
`backtest/factor_lab.py`的`moving_block_bootstrap_ci`。
"""

from __future__ import annotations

import math
from pathlib import Path

import polars as pl

from tw_screener.backtest.factor_lab import moving_block_bootstrap_ci
from tw_screener.backtest.panel import build_price_panel

HORIZONS: tuple[int, ...] = (10, 20, 40)
_SNAPSHOT_GAP_TD = 5  # 週度快照，約5個交易日一次

_READ_SCHEMA: dict[str, type[pl.DataType]] = {
    "flag": pl.Utf8,
    "horizon": pl.Int64,
    "n": pl.Int64,
    "n_dates": pl.Int64,
    "mean": pl.Float64,
    "median": pl.Float64,
    "win_rate": pl.Float64,
    "delta_mean": pl.Float64,
    "ci_lo": pl.Float64,
    "ci_hi": pl.Float64,
}


def _block_len_snapshots(h: int, snapshot_gap_td: int = _SNAPSHOT_GAP_TD) -> int:
    return max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))


def compute_prelim_forward_alpha(
    ledger: pl.DataFrame,
    price_history: pl.DataFrame,
    flag_cols: tuple[str, ...],
    horizons: tuple[int, ...] = HORIZONS,
    n_boot: int = 1000,
    seed: int = 42,
) -> pl.DataFrame:
    """對ledger每個旗標欄算初步forward alpha讀值（docs/31 §21.4）。

    Args:
        ledger: `g1_g2_g5_watch`或`l6_g4_watch`的`ledger.csv`讀入（需`week`/
            `data_date`/`stock_id` + `flag_cols`布林欄，即`LEDGER_SCHEMA`格式）。
        price_history: `analysis.rotation.load_market_history()`輸出
            （`date`/`stock_id`/`close`[/`volume`]），需涵蓋ledger最早`data_date`
            到最晚`data_date`+`max(horizons)`個交易日，否則越晚的快照forward
            alpha會是null（未到期，如實留空不外插）。
        flag_cols: 要算的旗標欄名，如`("g1","g2","g5")`或
            `("g4","l6_2cond","l6_4cond")`。

    Returns:
        每列＝一個(flag, horizon)組合：`n`/`n_dates`/`mean`/`median`/`win_rate`/
        `delta_mean`/`ci_lo`/`ci_hi`——CI在樣本不足時皆為`None`（`n_dates`即
        distinct週次數，同季內重疊週會拉高`n`但不拉高`n_dates`，讀表時務必看
        `n_dates`而非`n`判斷樣本是否夠格）。
    """
    if ledger.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=_READ_SCHEMA)

    fresh_panel = build_price_panel(price_history, horizons=horizons)
    if fresh_panel.is_empty():
        return pl.DataFrame(schema=_READ_SCHEMA)

    joined = ledger.rename({"data_date": "date"}).join(
        fresh_panel.select("date", "stock_id", *[f"alpha{h}" for h in horizons]),
        on=["date", "stock_id"], how="left",
    )

    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in joined.columns:
            continue
        sub = joined.drop_nulls([tgt])
        if sub.is_empty():
            continue
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        block_len = _block_len_snapshots(h)
        for flag in flag_cols:
            if flag not in sub.columns:
                continue
            g = sub.filter(pl.col(flag))
            if g.is_empty():
                continue
            vals = [float(v) for v in g[tgt].to_list()]
            med = g[tgt].median()
            daily_delta = (
                g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
            )
            delta_vals = [float(v) for v in daily_delta["_m"].to_list() if v is not None]
            ci_lo, ci_hi = (
                moving_block_bootstrap_ci(
                    delta_vals, block_len=block_len, n_boot=n_boot, seed=seed
                )
                if delta_vals
                else (None, None)
            )
            rows.append(
                {
                    "flag": flag,
                    "horizon": h,
                    "n": len(vals),
                    "n_dates": len(delta_vals),
                    "mean": sum(vals) / len(vals) if vals else None,
                    "median": float(med) if isinstance(med, (int, float)) else None,
                    "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                    "delta_mean": sum(delta_vals) / len(delta_vals) if delta_vals else None,
                    "ci_lo": ci_lo,
                    "ci_hi": ci_hi,
                }
            )
    return pl.DataFrame(rows, schema=_READ_SCHEMA) if rows else pl.DataFrame(schema=_READ_SCHEMA)


def _read_ledger_csv(path: Path, schema: dict[str, type[pl.DataType]]) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema=schema)
    return pl.read_csv(path, try_parse_dates=True, schema_overrides=schema)


def format_prelim_report(g1g2g5_read: pl.DataFrame, l6g4_read: pl.DataFrame) -> str:
    """把兩份讀值組成一份markdown報告（docs/31 §21.4附產物）。"""
    lines = [
        "# docs/31 §21.4：G1/G2/G4/G5/L6 初步（非驗證）forward alpha讀值",
        "",
        "> **明確非規劃書§7.4正式驗證**——樣本量小、未做regime切片、`n_dates`",
        "> 才是真正獨立觀察數（`n`含同季內大量重疊列）；CI在樣本不足時為空白，",
        "> 不代表訊號無效，只代表現在還測不出來，等每週累積會自然變準。",
        "",
    ]
    for title, df in (("G1/G2/G5（fundamentals衍生）", g1g2g5_read),
                       ("G4/L6（revenue/PE衍生）", l6g4_read)):
        lines.append(f"## {title}")
        lines.append("")
        if df.is_empty():
            lines.append(
                "（無資料——ledger可能為空，或最早快照週次連r+10都還沒滿足"
                "（entry+10個交易日尚未到）；兩份底帳目前僅2026-W34一週"
                "（資料日2026-08-21），r+10最快約2026-09-04才會有第一筆可算，"
                "這是時間累積的必然現象，不是bug）"
            )
            lines.append("")
            continue
        lines.append(
            "| flag | horizon | n | n_dates | mean | median | win_rate | delta_mean | CI95 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in df.sort(["flag", "horizon"]).iter_rows(named=True):
            ci = (
                f"[{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}]"
                if r["ci_lo"] is not None and r["ci_hi"] is not None
                else "（樣本不足，n_dates<10）"
            )
            lines.append(
                f"| {r['flag']} | r+{r['horizon']} | {r['n']} | {r['n_dates']} | "
                f"{r['mean']:+.2f}% | {r['median']:+.2f}% | {r['win_rate']:.0%} | "
                f"{r['delta_mean']:+.2f}% | {ci} |"
            )
        lines.append("")
    return "\n".join(lines)


def run_redesign_prelim_read(settings: Path, out_path: Path | None = None) -> str:
    """docs/31 §21.4：讀兩份ledger＋即時日線快取，算初步forward alpha讀值，
    輸出markdown報告（預設`research/redesign_prelim_read/latest.md`）。
    """
    import yaml as _yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.backtest.g1_g2_g5_watch import LEDGER_SCHEMA as G1G2G5_SCHEMA
    from tw_screener.backtest.l6_g4_watch import LEDGER_SCHEMA as L6G4_SCHEMA

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    g1g2g5_path = Path(
        cfg.get("backtest", {}).get("g1_g2_g5_watch", {}).get(
            "output_path", "research/g1_g2_g5_watch/ledger.csv"
        )
    )
    l6g4_path = Path(
        cfg.get("backtest", {}).get("l6_g4_watch", {}).get(
            "output_path", "research/l6_g4_watch/ledger.csv"
        )
    )
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    g1g2g5_ledger = _read_ledger_csv(g1g2g5_path, G1G2G5_SCHEMA)
    l6g4_ledger = _read_ledger_csv(l6g4_path, L6G4_SCHEMA)
    price_history = load_market_history(cache_dir, n_days=250)

    g1g2g5_read = compute_prelim_forward_alpha(g1g2g5_ledger, price_history, ("g1", "g2", "g5"))
    l6g4_read = compute_prelim_forward_alpha(
        l6g4_ledger, price_history, ("g4", "l6_2cond", "l6_4cond")
    )

    report = format_prelim_report(g1g2g5_read, l6g4_read)
    dest = out_path or Path("research/redesign_prelim_read/latest.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    return report
