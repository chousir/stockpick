"""backtest/redesign_dimension_grid.py — docs/31 §22.3 架構：panel-only候選排列組合
研究共用模組（維度依序研究＋結果模組化可組合，見§22.3提案）。

**只新增，不修改** `official_sector_grid.py` 既有的 `_aggregate_rank_velocity_cells`／
`_aggregate_flow_trigger_cells`——那兩個函式的輸出數字已發布在 docs/31 §14.4／§17.4，
任何細節調整都會讓已發布數字不可重現。本模組抽出兩者共同的 cell 聚合邏輯成通用版
`evaluate_signal_cells()`／`evaluate_signal_cells_by_regime()`，供§22.3.4排序的5個
候選維度＋日後的維度間組合共用，不再各自複製一份~150行幾乎相同的程式碼。

正確性回歸測試（§22.3.3要求）：`tests/backtest/test_redesign_dimension_grid.py` 對
合成 cells 資料驗證 `evaluate_signal_cells()` 與 `_aggregate_rank_velocity_cells`／
`_aggregate_flow_trigger_cells` 逐格數字完全一致。
"""

from __future__ import annotations

import math

import polars as pl

from tw_screener.backtest.factor_lab import (
    REGIME_LABELS,
    REGIME_MIN_N,
    Split,
    moving_block_bootstrap_ci,
    walk_forward_splits,
)

_CELL_SCHEMA: dict[str, type[pl.DataType]] = {
    "horizon": pl.Int64, "cell": pl.Utf8, "n": pl.UInt32, "n_dates": pl.UInt32,
    "mean": pl.Float64, "median": pl.Float64, "win_rate": pl.Float64,
    "delta_mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64,
    "mean_h1": pl.Float64, "mean_h2": pl.Float64,
}
_CELL_REGIME_SCHEMA: dict[str, type[pl.DataType]] = {
    "horizon": pl.Int64, "cell": pl.Utf8, "regime": pl.Utf8, "n": pl.Int64,
    "n_dates": pl.Int64, "mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64,
    "thin": pl.Boolean,
}
_ROTATION_WF_SCHEMA: dict[str, type[pl.DataType]] = {
    "horizon": pl.Int64, "cell": pl.Utf8, "split_id": pl.Int64,
    "test_start": pl.Date, "test_end": pl.Date,
    "test_n": pl.UInt32, "test_n_dates": pl.UInt32,
    "test_delta_mean": pl.Float64, "test_ci_lo": pl.Float64, "test_ci_hi": pl.Float64,
}
_ROTATION_CELLS_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date, "sub_industry": pl.Utf8, "stock_id": pl.Utf8, "cell": pl.Utf8,
}

ROTATION_CELLS: tuple[str, ...] = ("hit", "miss")


def _smean(s: pl.Series) -> float | None:
    v = s.mean()
    return float(v) if isinstance(v, (int, float)) else None


def _block_len_snapshots(h: int, snapshot_gap_td: int) -> int:
    return max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))


def evaluate_signal_cells(
    cells: pl.DataFrame,
    cell_names: tuple[str, ...],
    horizons: tuple[int, ...],
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """通用 cell 評分：逐 horizon×cell 算 mean/median/win_rate/delta_mean(vs 當日全樣本)
    /CI95(moving-block bootstrap)/前後半段 mean。

    抽自 `official_sector_grid.py` 的 `_aggregate_rank_velocity_cells`（§14.2）與
    `_aggregate_flow_trigger_cells`（§17.2）——兩者邏輯完全相同，差別只在寫死的 cell
    名稱常數；本函式把該差異換成 `cell_names` 參數，供§22.3排序的5個候選維度＋日後
    維度間組合共用一份聚合邏輯。

    Args:
        cells: 需含 date/cell/alpha{h}（cell 欄為呼叫端已分派好的字串標籤）。
        cell_names: 要輸出哪些 cell（某 cell 若在資料裡完全無列則該格不出現在結果，
            與既有兩個私有函式行為一致）。
    """
    if cells.is_empty():
        return pl.DataFrame(schema=_CELL_SCHEMA)
    mid_date = cells["date"].median()

    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in cells.columns:
            continue
        sub = cells.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for cell_name in cell_names:
            g = sub.filter(pl.col("cell") == cell_name)
            if g.is_empty():
                continue
            vals = [float(v) for v in g[tgt].to_list()]
            h1 = g.filter(pl.col("date") <= mid_date)[tgt]
            h2 = g.filter(pl.col("date") > mid_date)[tgt]
            med = g[tgt].median()
            daily_delta = (
                g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
            )
            delta_vals = [float(v) for v in daily_delta["_m"].to_list() if v is not None]
            ci_lo, ci_hi = (
                moving_block_bootstrap_ci(delta_vals, block_len=block_len, n_boot=n_boot, seed=seed)
                if delta_vals else (None, None)
            )
            rows.append(
                {
                    "horizon": h, "cell": cell_name,
                    "n": len(vals), "n_dates": len(delta_vals),
                    "mean": sum(vals) / len(vals) if vals else None,
                    "median": float(med) if isinstance(med, (int, float)) else None,
                    "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                    "delta_mean": sum(delta_vals) / len(delta_vals) if delta_vals else None,
                    "ci_lo": ci_lo, "ci_hi": ci_hi,
                    "mean_h1": _smean(h1), "mean_h2": _smean(h2),
                }
            )
    return pl.DataFrame(rows, schema=_CELL_SCHEMA).sort(["horizon", "cell"])


def evaluate_signal_cells_by_regime(
    cells: pl.DataFrame,
    cell_names: tuple[str, ...],
    horizons: tuple[int, ...],
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """`evaluate_signal_cells` 的 regime 切片版（結構同 `rank_velocity_by_regime`／
    `flow_trigger_by_regime`，抽成通用版供§22.3排序的候選維度共用）。
    """
    if (
        cells.is_empty()
        or regime_col not in cells.columns
        or cells[regime_col].drop_nulls().is_empty()
    ):
        return pl.DataFrame(schema=_CELL_REGIME_SCHEMA)

    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in cells.columns:
            continue
        sub = cells.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for cell_name in cell_names:
            cell_sub = sub.filter(pl.col("cell") == cell_name)
            for reg in REGIME_LABELS:
                g = cell_sub.filter(pl.col(regime_col) == reg)
                daily = g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
                vals = [float(v) for v in daily["_m"].to_list() if v is not None]
                lo_ci, hi_ci = (
                    moving_block_bootstrap_ci(vals, block_len=block_len, n_boot=n_boot, seed=seed)
                    if vals else (None, None)
                )
                rows.append(
                    {
                        "horizon": h, "cell": cell_name, "regime": reg,
                        "n": g.height, "n_dates": len(vals),
                        "mean": (sum(vals) / len(vals)) if vals else None,
                        "ci_lo": lo_ci, "ci_hi": hi_ci, "thin": len(vals) < REGIME_MIN_N,
                    }
                )
    return pl.DataFrame(rows, schema=_CELL_REGIME_SCHEMA)


# ---------------------------------------------------------------------------
# docs/31 §22.5 維度1：族群輪動（trend_score 當日橫斷面前20%）
# ---------------------------------------------------------------------------


def build_rotation_cells(stock_rows: pl.DataFrame, top_quantile: float = 0.2) -> pl.DataFrame:
    """§22.5：逐日對 trend_score 非 null 的 sub_industry 依降冪排名，
    `_rank ≤ ⌈top_quantile × 當日有效群組數⌉` → `hit`，其餘 → `miss`。

    用「當日有效群組數」動態算分母（非固定次產業總數）——MI_INDEX 部分較新分類
    指數歷史較淺（§10.2），固定分母會在資料缺席的早期日期系統性錯置門檻。

    Args:
        stock_rows: 需含 date/sub_industry/trend_score/stock_id（同
            `official_group_rank_grid` 的 stock_rows 結構）。trend_score 為 null 的
            (date, sub_industry) 不參與排名，也不會出現在輸出（inner join，同
            `_add_group_rank` 慣例：不外插、不用0填）。
    """
    need = {"date", "sub_industry", "trend_score", "stock_id"}
    if stock_rows.is_empty() or not need.issubset(stock_rows.columns):
        return pl.DataFrame(schema=_ROTATION_CELLS_SCHEMA)
    base = stock_rows.drop_nulls(["date", "sub_industry", "trend_score", "stock_id"])
    if base.is_empty():
        return pl.DataFrame(schema=_ROTATION_CELLS_SCHEMA)

    group_rank = (
        base.select("date", "sub_industry", "trend_score")
        .unique(subset=["date", "sub_industry"])
        .with_columns(
            pl.col("trend_score").rank(method="min", descending=True).over("date").alias("_rank"),
            pl.col("trend_score").count().over("date").alias("_n_groups"),
        )
        .with_columns(
            (pl.col("_rank") <= (pl.col("_n_groups") * top_quantile).ceil())
            .alias("_hit")
        )
        .select(
            "date", "sub_industry",
            pl.when(pl.col("_hit")).then(pl.lit("hit")).otherwise(pl.lit("miss")).alias("cell"),
        )
    )
    return base.join(group_rank, on=["date", "sub_industry"], how="inner")


def rotation_grid(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.5 全樣本讀值：`hit`/`miss` 兩格 forward alpha 對照。"""
    cells = build_rotation_cells(stock_rows, top_quantile=top_quantile)
    return evaluate_signal_cells(cells, ROTATION_CELLS, horizons, n_boot, seed, snapshot_gap_td)


def rotation_by_regime(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.5 regime 切片：`hit`/`miss` 兩格 × regime forward alpha。"""
    cells = build_rotation_cells(stock_rows, top_quantile=top_quantile)
    return evaluate_signal_cells_by_regime(
        cells, ROTATION_CELLS, horizons, n_boot, seed, regime_col, snapshot_gap_td
    )


def walk_forward_rotation(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.5 walk-forward：無需選門檻（top_quantile 為預先登記固定值，非可調
    超參數），只需各段獨立算 `hit`/`miss` 兩格的 delta/CI，誠實列出每段結果
    （同 §13.4/§14.2 精神）。最近一段（split_id 最大）即§22.5核准的保留驗證窗，
    呼叫端不得用它調整訊號定義，只能拿它的結果對搜尋階段（其餘段）做同號複核。
    """
    cells = build_rotation_cells(stock_rows, top_quantile=top_quantile)
    if cells.is_empty():
        return pl.DataFrame(schema=_ROTATION_WF_SCHEMA)

    all_dates = sorted(cells["date"].unique().to_list())
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in cells.columns:
            continue
        emb = h + 1
        splits: list[Split] = walk_forward_splits(
            all_dates, n_splits=n_splits, min_train_frac=min_train_frac, embargo_td=emb
        )
        for s in splits:
            test_cells = cells.filter(
                (pl.col("date") >= s.test_start) & (pl.col("date") <= s.test_end)
            )
            test_grid = evaluate_signal_cells(
                test_cells, ROTATION_CELLS, horizons=(h,), n_boot=n_boot, seed=seed,
                snapshot_gap_td=snapshot_gap_td,
            )
            for cell_name in ROTATION_CELLS:
                r_df = test_grid.filter(pl.col("cell") == cell_name)
                if r_df.is_empty():
                    continue
                r = r_df.row(0, named=True)
                rows.append(
                    {
                        "horizon": h, "cell": cell_name, "split_id": s.split_id,
                        "test_start": s.test_start, "test_end": s.test_end,
                        "test_n": r["n"], "test_n_dates": r["n_dates"],
                        "test_delta_mean": r["delta_mean"],
                        "test_ci_lo": r["ci_lo"], "test_ci_hi": r["ci_hi"],
                    }
                )
    return pl.DataFrame(rows, schema=_ROTATION_WF_SCHEMA)
