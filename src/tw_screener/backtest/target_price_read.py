"""backtest/target_price_read.py — docs/31 §20.13 Phase 1：機械式「目標價」校準回測。

**pre-registration（跑前鎖定，全文見 docs/31 §20.13）**：
- fit 窗 2022-01-01 ~ 2024-12-31：**全交易日** anchor，建 cell（位階×族群內強弱 9 格）
  → forward 報酬 r{h} 分位查表（`forward_return_percentiles`）。fit 尾端再剪掉
  `EMBARGO_TD`（=max horizon+5）個交易日，防其 forward 標籤（r120 ≈ entry+121td）
  落進 test 窗。
- test 窗 2025-01-01 ~ 資料末：**週頻** anchor（每 ISO 週最後一交易日，降前瞻窗重疊），
  用 fit 查表投射、比對實際 r{h}。
- fit/test 依日期硬切 + embargo，test 完全不參與建桶（`_assert_no_leak`）。

**主問題（#1）**：profile 分桶有沒有帶「超出全市場（`_pooled`）基準」的資訊？——
不是「目標價準不準」，那近乎同義反覆（target = close×(1+P60) ⇒「達標」≡「r{h}>P60」，
是所 fit 分布的重述）。#2–#5 為描述性，非裁決。

**事前預期（§20.13）**：資料止於 2026-08 → r60/r120 的 test 獨立塊數個位數、結構性
無裁決；只有 r20 會有正式裁決，而 r20 正是「最接近雜訊」的 horizon——本研究大概率
落在「機械式目標價不可用 / 只報全市場區間」。

輸出 `research/target_price/calibration_<date>.md`。純函式；IO 由 `run_...` 負責。
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.backtest.diagnostic import _spearman as spearman
from tw_screener.backtest.factor_lab import (
    REGIME_LABELS,
    REGIME_MIN_N,
    moving_block_bootstrap_ci,
)
from tw_screener.backtest.rotation_efficacy import weekly_snapshot_dates
from tw_screener.backtest.target_price_panel import (
    HORIZONS,
    PCTILES,
    POOLED_CELL,
    build_analog_panel,
    forward_return_percentiles,
)


def _fval(v: object) -> float | None:
    """polars 聚合結果安全轉 float（None/非數 → None）。"""
    return float(v) if isinstance(v, (int, float)) else None


FIT_START = date(2022, 1, 1)
FIT_END = date(2024, 12, 31)
TEST_START = date(2025, 1, 1)

CENTRAL_PCTILE = 50   # 投射中心（target = close×(1+P50/100)）
RANK_PCTILE = 60      # cell 排序 & 方向命中門檻
_SNAPSHOT_GAP_TD = 5  # test 週頻 anchor ≈ 5 交易日/步

# fit/test 之間的 embargo（交易日）：fit 窗最後 max(horizon)+buffer 個交易日的 anchor
# 其 forward 標籤（r120 ≈ entry+121td）會落進 test 窗——剪掉，防前視洩漏。
EMBARGO_TD = max(HORIZONS) + 5

_COND_SCHEMA: dict[str, type[pl.DataType]] = {
    "horizon": pl.Int64,
    "n_cells": pl.Int64,
    "spearman_rho": pl.Float64,
    "verdict_1a": pl.Utf8,
}
_POOLED_SCHEMA: dict[str, type[pl.DataType]] = {
    "horizon": pl.Int64,
    "n_dates": pl.Int64,
    "block_len": pl.Int64,
    "n_blocks": pl.Int64,
    "paired_diff_mean": pl.Float64,
    "ci_lo": pl.Float64,
    "ci_hi": pl.Float64,
    "verdict_1b": pl.Utf8,
}

# 塊數下限：moving-block bootstrap 在 n_blocks<此值時退化（單/雙塊重抽），不足以裁決。
MIN_BLOCKS = 8


# ── fit/test 切分 ──────────────────────────────────────────────────────────────
def split_fit_test(
    panel: pl.DataFrame,
    fit_start: date = FIT_START,
    fit_end: date = FIT_END,
    test_start: date = TEST_START,
    embargo_td: int = EMBARGO_TD,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """fit＝[fit_start, fit_end] 全交易日（再剪掉尾端 `embargo_td` 個交易日，防其
    forward 標籤洩漏進 test 窗）；test＝[test_start, ∞) 週頻 anchor。"""
    if panel.is_empty():
        return panel, panel
    fit = panel.filter(
        (pl.col("date") >= fit_start) & (pl.col("date") <= fit_end)
    )
    if embargo_td > 0 and not fit.is_empty():
        fit_dates = sorted(fit["date"].unique().to_list())
        if len(fit_dates) > embargo_td:
            cutoff = fit_dates[-(embargo_td + 1)]
            fit = fit.filter(pl.col("date") <= cutoff)
    test_all = panel.filter(pl.col("date") >= test_start)
    if test_all.is_empty():
        return fit, test_all
    weekly = set(weekly_snapshot_dates(test_all["date"].unique().to_list()))
    test = test_all.filter(pl.col("date").is_in(list(weekly)))
    return fit, test


def _assert_no_leak(fit: pl.DataFrame, test: pl.DataFrame) -> None:
    """fit 與 test 日期不得有交集（pre-registration 硬約束）。"""
    if fit.is_empty() or test.is_empty():
        return
    overlap = set(fit["date"].to_list()) & set(test["date"].to_list())
    if overlap:
        raise ValueError(
            f"fit/test 日期洩漏：{len(overlap)} 個重疊日期（如 {sorted(overlap)[:3]}）"
        )


# ── 投射 ──────────────────────────────────────────────────────────────────────
def project_from_lookup(
    test_rows: pl.DataFrame,
    fit_lookup: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
) -> pl.DataFrame:
    """test 每列 join 其 cell（與 `_pooled`）的 fit 分位 → 投射報酬 & 誤差（return-%
    空間，scale-free）。

    產欄：realized（r{h}）、proj_cell_p{k}、proj_pooled_p{k}、err_cell、err_pooled
    （＝|realized − proj_*_P50|）。丟 realized 為 null 的列（未到期/下市）。
    """
    if test_rows.is_empty() or fit_lookup.is_empty():
        return pl.DataFrame()

    pool = fit_lookup.filter(pl.col("cell") == POOLED_CELL)
    cellwise = fit_lookup.filter(pl.col("cell") != POOLED_CELL)
    frames: list[pl.DataFrame] = []
    for h in horizons:
        tgt = f"r{h}"
        if tgt not in test_rows.columns:
            continue
        sub = test_rows.drop_nulls([tgt, "cell"]).select(
            "date", "stock_id", "regime", "cell", pl.col(tgt).alias("realized")
        )
        if sub.is_empty():
            continue
        cl = cellwise.filter(pl.col("horizon") == h).select(
            "cell", *[pl.col(f"p{k}").alias(f"proj_cell_p{k}") for k in PCTILES]
        )
        pl_row = pool.filter(pl.col("horizon") == h)
        if pl_row.is_empty():
            continue
        pl_vals = {k: pl_row[f"p{k}"][0] for k in PCTILES}
        merged = sub.join(cl, on="cell", how="inner")
        if merged.is_empty():
            continue
        merged = merged.with_columns(
            pl.lit(h).alias("horizon"),
            *[pl.lit(pl_vals[k]).alias(f"proj_pooled_p{k}") for k in PCTILES],
        ).with_columns(
            (pl.col("realized") - pl.col(f"proj_cell_p{CENTRAL_PCTILE}"))
            .abs()
            .alias("err_cell"),
            (pl.col("realized") - pl.col(f"proj_pooled_p{CENTRAL_PCTILE}"))
            .abs()
            .alias("err_pooled"),
        )
        frames.append(merged)
    return pl.concat(frames, how="diagonal") if frames else pl.DataFrame()


# ── #1(a) 條件校準：cell 排序單調性 ───────────────────────────────────────────
def conditional_calibration(
    projected: pl.DataFrame,
    fit_lookup: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
) -> pl.DataFrame:
    """逐 horizon：fit 各 cell 的 P60 vs test 該 cell 實際 r{h} 均值，跨 cell Spearman。

    rho 顯著為正（且 n_cells≥5）＝「fit 排出的貴賤序在 test 站得住」。
    """
    if projected.is_empty() or fit_lookup.is_empty():
        return pl.DataFrame(schema=_COND_SCHEMA)
    rows: list[dict] = []
    for h in horizons:
        ph = projected.filter(pl.col("horizon") == h)
        fl = fit_lookup.filter(
            (pl.col("horizon") == h) & (pl.col("cell") != POOLED_CELL)
        )
        if ph.is_empty() or fl.is_empty():
            continue
        test_by_cell = ph.group_by("cell").agg(
            pl.col("realized").mean().alias("test_mean")
        )
        pair = fl.select("cell", pl.col(f"p{RANK_PCTILE}").alias("fit_p60")).join(
            test_by_cell, on="cell", how="inner"
        )
        rho, n = spearman(pair, "fit_p60", "test_mean")
        if rho is None:
            verdict = "無法判定（cell 數不足或無變異）"
        elif n < 5:
            verdict = f"cell 數 {n}<5，不裁決"
        elif rho >= 0.5:
            verdict = f"排序單調（rho={rho:+.2f}）——fit 貴賤序在 test 站得住"
        elif rho <= -0.3:
            verdict = f"排序反轉（rho={rho:+.2f}）——機械式目標價不可用"
        else:
            verdict = f"排序無關（rho={rho:+.2f}）——profile 分桶無鑑別力"
        rows.append(
            {
                "horizon": h,
                "n_cells": n,
                "spearman_rho": rho,
                "verdict_1a": verdict,
            }
        )
    return pl.DataFrame(rows, schema=_COND_SCHEMA) if rows else pl.DataFrame(
        schema=_COND_SCHEMA
    )


# ── #1(b) pooled null：per-date 配對差序列 → moving-block bootstrap ────────────
def pooled_null_ci(
    projected: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    snapshot_gap_td: int = _SNAPSHOT_GAP_TD,
    n_boot: int = 1000,
    seed: int = 42,
) -> pl.DataFrame:
    """逐 horizon：`(err_cell − err_pooled)` 逐 test anchor date 取均值成序列，對此
    序列做 `moving_block_bootstrap_ci`（block=ceil((h+1)/gap)）。

    CI 整段 <0 ＝ cell 版顯著較準（profile 分桶有增益）；跨 0 或 T<10 → 無鑑別力/
    樣本不足，只報全市場區間。
    """
    if projected.is_empty():
        return pl.DataFrame(schema=_POOLED_SCHEMA)
    rows: list[dict] = []
    for h in horizons:
        ph = projected.filter(pl.col("horizon") == h)
        if ph.is_empty():
            continue
        daily = (
            ph.with_columns((pl.col("err_cell") - pl.col("err_pooled")).alias("_d"))
            .group_by("date")
            .agg(pl.col("_d").mean().alias("_m"))
            .sort("date")
        )
        vals = [float(v) for v in daily["_m"].to_list() if v is not None]
        block_len = max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))
        n_blocks = math.ceil(len(vals) / block_len) if vals else 0
        ci_lo, ci_hi = (
            moving_block_bootstrap_ci(
                vals, block_len=block_len, n_boot=n_boot, seed=seed
            )
            if vals
            else (None, None)
        )
        mean_d = sum(vals) / len(vals) if vals else None
        if ci_lo is None or ci_hi is None:
            verdict = f"T={len(vals)}<10，無 CI——樣本結構性不足，只報全市場區間"
        elif n_blocks < MIN_BLOCKS:
            verdict = (
                f"n_blocks={n_blocks}<{MIN_BLOCKS}（前瞻窗重疊、獨立塊不足）——"
                "**無裁決**，CI 不可信"
            )
        elif ci_hi < 0:
            verdict = "CI 整段 <0——cell 版顯著較準，profile 分桶有增益"
        elif ci_lo > 0:
            verdict = "CI 整段 >0——cell 版顯著較差（反例）"
        else:
            verdict = "CI 跨 0——profile 分桶無鑑別力，只報全市場區間"
        rows.append(
            {
                "horizon": h,
                "n_dates": len(vals),
                "block_len": block_len,
                "n_blocks": n_blocks,
                "paired_diff_mean": mean_d,
                "ci_lo": ci_lo,
                "ci_hi": ci_hi,
                "verdict_1b": verdict,
            }
        )
    return pl.DataFrame(rows, schema=_POOLED_SCHEMA) if rows else pl.DataFrame(
        schema=_POOLED_SCHEMA
    )


# ── #2–#5 描述性 ─────────────────────────────────────────────────────────────
def descriptive_metrics(
    projected: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    ks: tuple[int, ...] = (25, 50, 60, 75),
) -> pl.DataFrame:
    """#2 邊際校準（實際 r{h} ≤ fit cell P_k 的比例 vs 名目 k%）、#3 中位偏誤、
    #4 區間寬度、#5 方向命中率（terminal，明文非 test）。"""
    schema: dict[str, type[pl.DataType]] = {
        "horizon": pl.Int64,
        "n": pl.Int64,
        **{f"cover_p{k}": pl.Float64 for k in ks},
        "median_bias": pl.Float64,
        "interval_width_pct": pl.Float64,
        "direction_hit_p60": pl.Float64,
    }
    if projected.is_empty():
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for h in horizons:
        ph = projected.filter(pl.col("horizon") == h)
        if ph.is_empty():
            continue
        n = ph.height
        cover = {
            f"cover_p{k}": _fval((ph["realized"] <= ph[f"proj_cell_p{k}"]).mean())
            for k in ks
        }
        bias = _fval((ph["realized"] - ph[f"proj_cell_p{CENTRAL_PCTILE}"]).mean())
        width = _fval((ph["proj_cell_p75"] - ph["proj_cell_p25"]).median())
        hit = _fval((ph["realized"] >= ph[f"proj_cell_p{RANK_PCTILE}"]).mean())
        rows.append(
            {
                "horizon": h,
                "n": n,
                **cover,
                "median_bias": bias,
                "interval_width_pct": width,
                "direction_hit_p60": hit,
            }
        )
    return pl.DataFrame(rows, schema=schema)


# ── #6 regime 切片 ───────────────────────────────────────────────────────────
def calibration_by_regime(
    projected: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
) -> pl.DataFrame:
    """cell 版 vs pooled 版誤差差、逐 regime——樣本薄是預期，全列（含 thin 標記）。"""
    schema: dict[str, type[pl.DataType]] = {
        "horizon": pl.Int64,
        "regime": pl.Utf8,
        "n": pl.Int64,
        "n_dates": pl.Int64,
        "mean_err_cell": pl.Float64,
        "mean_err_pooled": pl.Float64,
        "thin": pl.Boolean,
    }
    if projected.is_empty() or "regime" not in projected.columns:
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for h in horizons:
        ph = projected.filter(pl.col("horizon") == h)
        for reg in REGIME_LABELS:
            g = ph.filter(pl.col("regime") == reg)
            nd = g["date"].n_unique() if not g.is_empty() else 0
            rows.append(
                {
                    "horizon": h,
                    "regime": reg,
                    "n": g.height,
                    "n_dates": nd,
                    "mean_err_cell": _fval(g["err_cell"].mean())
                    if not g.is_empty()
                    else None,
                    "mean_err_pooled": _fval(g["err_pooled"].mean())
                    if not g.is_empty()
                    else None,
                    "thin": nd < REGIME_MIN_N,
                }
            )
    return pl.DataFrame(rows, schema=schema)


# ── 信賴度評級（事前公式）─────────────────────────────────────────────────────
def confidence_tier(
    n: int,
    iqr: float | None,
    regime_thin: bool,
    horizon: int,
    n_min_mid: int = 200,
    n_min_high: int = 800,
    iqr_max_high: float = 25.0,
    iqr_max_mid: float = 45.0,
) -> str:
    """高/中/低＝f(cell n、IQR、regime thin、horizon)。門檻事前設、runner 從 settings
    覆寫。horizon>60 或 regime_thin 一律封頂到「中」。"""
    if iqr is None or n < 30:
        return "低"
    long_or_thin = horizon > 60 or regime_thin
    if n >= n_min_high and iqr <= iqr_max_high and not long_or_thin:
        return "高"
    if n >= n_min_mid and iqr <= iqr_max_mid:
        return "中"
    return "低"


# ── 覆蓋率表（事前預期對照）─────────────────────────────────────────────────
def coverage_table(
    test_rows: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    snapshot_gap_td: int = _SNAPSHOT_GAP_TD,
) -> pl.DataFrame:
    schema: dict[str, type[pl.DataType]] = {
        "horizon": pl.Int64,
        "n_rows": pl.Int64,
        "n_dates": pl.Int64,
        "block_len": pl.Int64,
        "n_blocks": pl.Int64,
        "verdict_eligible": pl.Boolean,
    }
    if test_rows.is_empty():
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for h in horizons:
        tgt = f"r{h}"
        if tgt not in test_rows.columns:
            continue
        sub = test_rows.drop_nulls([tgt])
        nd = sub["date"].n_unique()
        block_len = max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))
        n_blocks = math.ceil(nd / block_len) if nd else 0
        rows.append(
            {
                "horizon": h,
                "n_rows": sub.height,
                "n_dates": nd,
                "block_len": block_len,
                "n_blocks": n_blocks,
                "verdict_eligible": nd >= 10 and n_blocks >= MIN_BLOCKS,
            }
        )
    return pl.DataFrame(rows, schema=schema)


# ── 報告 ─────────────────────────────────────────────────────────────────────
def _f(v: object, suf: str = "", nd: int = 2) -> str:
    return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"


def format_report(
    coverage: pl.DataFrame,
    cond: pl.DataFrame,
    pooled: pl.DataFrame,
    desc: pl.DataFrame,
    by_regime: pl.DataFrame,
    fit_span: str,
    test_span: str,
    n_fit_rows: int,
    n_test_rows: int,
) -> str:
    lines = [
        "# docs/31 §20.13 Phase 1：機械式「目標價」校準回測",
        "",
        "> **實驗性・非投資建議・不進 pick.csv**。機械式目標價＝profile（位階×族群內",
        "> 相對強弱 9 格）相似股票過去 N 交易日報酬的**歷史分位數**，非基本面估值、",
        "> 非擇時訊號。主問題＝profile 分桶有沒有帶超出全市場（`_pooled`）基準的資訊；",
        "> **不是「目標價準不準」**（target=close×(1+P60) ⇒「達標」≡「r{h}>P60」，同義",
        "> 反覆）。#2–#5 描述性、非裁決。",
        "",
        f"- fit 窗：{fit_span}（全交易日 anchor，{n_fit_rows} 列）→ 建 9 格 + `_pooled` 查表。",
        f"- test 窗：{test_span}（週頻 anchor，{n_test_rows} 列）→ 投射 + 校準。",
        "  fit/test 日期硬切、無洩漏。",
        "",
        "## 覆蓋率 vs 事前預期",
        "",
        "> 事前預期（pre-registration）：資料止於 2026-08 → r60/r120 的 test 獨立塊數",
        "> 個位數、**結構性無裁決**；只有 r20 會有正式裁決，而 r20 正是誠實定位裡",
        "> 「最接近雜訊」的 horizon。",
        "",
        f"> 可裁決門檻：n_dates≥10 且 n_blocks≥{MIN_BLOCKS}。",
        "",
        "| horizon | test 列 | n_dates | block_len | n_blocks | 可裁決 |",
        "|---|---|---|---|---|---|",
    ]
    for r in coverage.iter_rows(named=True):
        lines.append(
            f"| r+{r['horizon']} | {r['n_rows']} | {r['n_dates']} | {r['block_len']} "
            f"| {r['n_blocks']} | {'是' if r['verdict_eligible'] else '否（塊數不足）'} |"
        )
    lines += [
        "",
        "## #1(a) 條件校準——cell 排序單調性（fit P60 序 vs test 實際 r{h} 均值）",
        "",
        "| horizon | n_cells | Spearman rho | 判定 |",
        "|---|---|---|---|",
    ]
    for r in cond.iter_rows(named=True):
        lines.append(
            f"| r+{r['horizon']} | {r['n_cells']} | {_f(r['spearman_rho'])} "
            f"| {r['verdict_1a']} |"
        )
    lines += [
        "",
        "## #1(b) pooled null——`(err_cell − err_pooled)` per-date 序列 moving-block bootstrap",
        "",
        "> err_* ＝ |實際 r{h} − 投射 P50|（return-% 空間）。CI 整段 <0 ＝ cell 版顯著",
        f"> 較準。block_len = ceil((h+1)/5)；n_blocks<{MIN_BLOCKS} → CI 不可信、無裁決。",
        "",
        "| horizon | n_dates | block_len | n_blocks | 配對差 mean | CI95 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in pooled.iter_rows(named=True):
        ci = (
            f"[{_f(r['ci_lo'])}, {_f(r['ci_hi'])}]"
            if r["ci_lo"] is not None
            else "（T<10，不給）"
        )
        lines.append(
            f"| r+{r['horizon']} | {r['n_dates']} | {r['block_len']} | {r['n_blocks']} "
            f"| {_f(r['paired_diff_mean'], 'pp')} | {ci} | {r['verdict_1b']} |"
        )
    lines += [
        "",
        "## #2–#5 描述性（非裁決）",
        "",
        "| horizon | n | 覆蓋P25 | 覆蓋P50 | 覆蓋P60 | 覆蓋P75 | 中位偏誤 "
        "| 區間寬度P75−P25 | 方向命中P60 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in desc.iter_rows(named=True):
        lines.append(
            f"| r+{r['horizon']} | {r['n']} | {r['cover_p25']:.0%} | {r['cover_p50']:.0%} "
            f"| {r['cover_p60']:.0%} | {r['cover_p75']:.0%} | {_f(r['median_bias'], 'pp')} "
            f"| {_f(r['interval_width_pct'], 'pp')} | {r['direction_hit_p60']:.0%} |"
        )
    lines += [
        "",
        "> 方向命中率**明文非顯著性證據**——見上方同義反覆說明"
        "（同 §20.11「gap% 變化率」明文不測邏輯）。",
        "",
        "## #6 regime 切片（cell 版 vs pooled 版誤差，全列含 thin）",
        "",
        "| horizon | regime | n | n_dates | 平均 err_cell | 平均 err_pooled | 樣本 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in by_regime.iter_rows(named=True):
        lines.append(
            f"| r+{r['horizon']} | {r['regime']} | {r['n']} | {r['n_dates']} "
            f"| {_f(r['mean_err_cell'], 'pp')} | {_f(r['mean_err_pooled'], 'pp')} "
            f"| {'樣本不足' if r['thin'] else '可判'} |"
        )
    lines += [
        "",
        "## 結論（照 §20.13 裁決句）",
        "",
        _overall_verdict(cond, pooled, desc),
        "",
    ]
    return "\n".join(lines)


def _overall_verdict(
    cond: pl.DataFrame, pooled: pl.DataFrame, desc: pl.DataFrame
) -> str:
    """r20 為焦點 horizon 的整體裁決句（§20.13 停損句法）。"""
    def _pick(df: pl.DataFrame, h: int) -> dict | None:
        s = df.filter(pl.col("horizon") == h)
        return s.row(0, named=True) if not s.is_empty() else None

    c20, p20, d20 = _pick(cond, 20), _pick(pooled, 20), _pick(desc, 20)
    if c20 is None or p20 is None:
        return "- r+20 無讀值（test 樣本不足）——**本輪無裁決**，等資料累積後重跑。"
    rho = c20["spearman_rho"]
    ci_hi = p20["ci_hi"]
    ci_lo = p20["ci_lo"]
    n_blocks = p20["n_blocks"]
    width = d20["interval_width_pct"] if d20 else None

    if ci_lo is None or n_blocks < MIN_BLOCKS:
        return (
            f"- r+20 pooled null 樣本結構性不足（T<10 或 n_blocks={n_blocks}<{MIN_BLOCKS}）"
            "——**本輪無正式裁決**，機械式目標價只報全市場 regime-conditional 區間、"
            "不給 per-stock 單一數字，等資料累積後重跑（門檻見 §20.13）。"
        )
    # 9-cell Spearman 無解析 CI（Fisher-z 需 n≥11）——rho 僅方向性參考、不當顯著性。
    if rho is not None and rho <= -0.3:
        return (
            f"- r+20 cell 排序反轉（rho={rho:+.2f}）——**機械式目標價不可用**，"
            "連全市場區間都需標低信心。"
        )
    if rho is not None and rho >= 0.5 and ci_hi is not None and ci_hi < 0:
        base = (
            f"- r+20 排序單調（rho={rho:+.2f}）且 pooled null CI 整段 <0——"
            "**profile 分桶帶額外資訊**，機械式目標價可作 r+20、高 tier 的實驗性參考。"
        )
        if width is not None and width > 25:
            base += f"（惟區間寬度 {width:+.1f}pp>±25pp，投射區間仍須連同標示寬度呈現。）"
        return base
    return (
        f"- r+20 條件校準未過關（rho={_f(rho)}、pooled null CI="
        f"[{_f(ci_lo)}, {_f(ci_hi)}] 跨/未達 0）——**profile 分桶無鑑別力**，"
        "機械式目標價只報全市場 regime-conditional 區間、不給 per-stock 單一數字。"
    )


def run_target_price_read(
    settings: Path,
    out_path: Path | None = None,
    horizons: tuple[int, ...] = HORIZONS,
) -> str:
    """docs/31 §20.13 Phase 1：讀日線+regime 快取 → 建歷史類比面板 → fit/test 校準
    回測 → markdown 報告（預設 `research/target_price/calibration_<date>.md`）。"""
    import yaml as _yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)
    tpc = cfg.get("backtest", {}).get("target_price", {})
    pos_edges = tuple(float(x) for x in tpc.get("pos_edges", (-8.0, 8.0)))
    rs_edges = tuple(float(x) for x in tpc.get("rs_edges", (-5.0, 5.0)))
    rs_window = int(tpc.get("rs_window", 20))
    min_rows_per_day = int(tpc.get("min_rows_per_day", 900))
    n_boot = int(tpc.get("n_boot", 1000))
    seed = int(tpc.get("seed", 42))
    market_days = int(tpc.get("market_history_days", 1300))
    out_dir = Path(tpc.get("output_dir", "research/target_price"))

    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    regime_path = Path(
        cfg.get("backtest", {})
        .get("regime_history", {})
        .get("output_path", "research/panel/regime_labels.parquet")
    )

    price = load_market_history(cache_dir, n_days=market_days)
    membership = list_subindustries()
    regime = (
        pl.read_parquet(regime_path) if regime_path.exists() else None
    )

    panel = build_analog_panel(
        price,
        membership,
        regime=regime,
        horizons=horizons,
        pos_edges=(pos_edges[0], pos_edges[1]),
        rs_edges=(rs_edges[0], rs_edges[1]),
        rs_window=rs_window,
        min_rows_per_day=min_rows_per_day,
    )

    dest = out_path or out_dir / f"calibration_{date.today():%Y%m%d}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if panel.is_empty():
        report = (
            "# docs/31 §20.13 Phase 1：機械式「目標價」校準回測\n\n"
            "（面板為空——日線快取不足以涵蓋 2022 起的 warmup，或 membership 缺）\n"
        )
        dest.write_text(report, encoding="utf-8")
        return report

    fit, test = split_fit_test(panel)
    _assert_no_leak(fit, test)
    fit_lookup = forward_return_percentiles(fit, horizons=horizons)
    projected = project_from_lookup(test, fit_lookup, horizons=horizons)

    coverage = coverage_table(test, horizons=horizons)
    cond = conditional_calibration(projected, fit_lookup, horizons=horizons)
    pooled = pooled_null_ci(projected, horizons=horizons, n_boot=n_boot, seed=seed)
    desc = descriptive_metrics(projected, horizons=horizons)
    by_regime = calibration_by_regime(projected, horizons=horizons)

    def _span(df: pl.DataFrame) -> str:
        if df.is_empty():
            return "—"
        return f"{df['date'].min()!s}~{df['date'].max()!s}"

    report = format_report(
        coverage,
        cond,
        pooled,
        desc,
        by_regime,
        fit_span=_span(fit),
        test_span=_span(test),
        n_fit_rows=fit.height,
        n_test_rows=test.height,
    )
    dest.write_text(report, encoding="utf-8")

    # 附：分位查表 & 投射明細（研究用，gitignored research/ 下）
    fit_lookup.write_csv(out_dir / f"fit_lookup_{date.today():%Y%m%d}.csv")
    if not projected.is_empty():
        projected.write_parquet(out_dir / f"projected_{date.today():%Y%m%d}.parquet")
    return report
