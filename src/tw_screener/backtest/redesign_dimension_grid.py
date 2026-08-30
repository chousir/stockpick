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


def walk_forward_cells(
    cells: pl.DataFrame,
    cell_names: tuple[str, ...],
    horizons: tuple[int, ...] = (10, 20, 40),
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """通用 walk-forward：已分派好 cell 的資料 → 各段獨立算各 cell 的 delta/CI，
    誠實列出每段結果（同 §13.4/§14.2 精神）。最近一段（split_id 最大）即§22.3.1
    核准的保留驗證窗，呼叫端不得用它調整訊號定義，只能拿它的結果對搜尋階段
    （其餘段）做同號複核。供§22.5維度1、§22.7維度1×維度2組合共用，避免各自
    複製一份 walk-forward 迴圈（同 evaluate_signal_cells 的抽出理由）。
    """
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
                test_cells, cell_names, horizons=(h,), n_boot=n_boot, seed=seed,
                snapshot_gap_td=snapshot_gap_td,
            )
            for cell_name in cell_names:
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
    """§22.5 walk-forward：`build_rotation_cells` ＋通用 `walk_forward_cells`。"""
    cells = build_rotation_cells(stock_rows, top_quantile=top_quantile)
    return walk_forward_cells(
        cells, ROTATION_CELLS, horizons, n_splits, min_train_frac, n_boot, seed, snapshot_gap_td
    )


# ---------------------------------------------------------------------------
# docs/31 §22.7 維度1×維度2組合：rotation(hit/miss) × flow_trigger(triggered/untriggered)
# ---------------------------------------------------------------------------

ROTATION_FLOW_CELLS: tuple[str, ...] = (
    "hit_triggered", "hit_untriggered", "miss_triggered", "miss_untriggered",
)


def build_rotation_flow_cells(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    top_quantile: float = 0.2,
    lookback_window: int = 15,
) -> pl.DataFrame:
    """§22.7：`build_rotation_cells` 的 hit/miss 交叉★投信流「近期觸發」，共4格。

    「近期觸發」判定邏輯（交易日索引 join_asof、`lookback_window`個交易日內）
    獨立重寫一份，同`official_sector_grid.py`的`_attach_flow_trigger_cell`——
    不匯入該私有函式，避免耦合到§17已發布程式碼路徑（同全案duplicate-small-
    helper慣例）。

    Args:
        daily_dates: 觸發序列所在的完整交易日曆（非`stock_rows`本身的週頻日期）
            ——同`_attach_flow_trigger_cell`要求，必須用完整交易日曆建索引。
    """
    rotation_cells = build_rotation_cells(stock_rows, top_quantile=top_quantile)
    if rotation_cells.is_empty():
        return pl.DataFrame(schema=_ROTATION_CELLS_SCHEMA)
    if triggers.is_empty():
        return rotation_cells.with_columns(
            (pl.col("cell") + pl.lit("_untriggered")).alias("cell")
        )

    day_index = pl.DataFrame(
        {"date": sorted(set(daily_dates)), "_day_idx": range(len(set(daily_dates)))}
    )
    pop_idx = (
        rotation_cells.join(day_index, on="date", how="left")
        .sort(["sub_industry", "_day_idx"])
    )
    trig_idx = (
        triggers.join(
            day_index.rename({"date": "_trig_date"}),
            left_on="date", right_on="_trig_date", how="inner",
        )
        .rename({"_day_idx": "_trig_idx"})
        .select("sub_industry", "_trig_idx")
        .sort(["sub_industry", "_trig_idx"])
    )
    joined = pop_idx.join_asof(
        trig_idx, left_on="_day_idx", right_on="_trig_idx", by="sub_industry",
        strategy="backward", check_sortedness=False,
    )
    joined = joined.with_columns(
        (
            pl.col("_trig_idx").is_not_null()
            & ((pl.col("_day_idx") - pl.col("_trig_idx")) <= lookback_window)
        ).fill_null(False).alias("_triggered_recent")
    )
    return joined.with_columns(
        (
            pl.col("cell")
            + pl.when(pl.col("_triggered_recent"))
            .then(pl.lit("_triggered"))
            .otherwise(pl.lit("_untriggered"))
        ).alias("cell")
    ).drop(["_day_idx", "_trig_idx", "_triggered_recent"])


def build_flow_cells(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    lookback_window: int = 15,
) -> pl.DataFrame:
    """§22.17：法人流向「近期觸發」單獨抽成hit/miss cell（非附掛在rotation cell
    字串上），供跟`build_margin_cells`／`build_momentum_cells`直接餵
    `build_pairwise_combo_cells()`做2×2組合。

    觸發判定邏輯（次產業層級join_asof、`lookback_window`個交易日內）**刻意重新
    獨立寫一份**，不重用`build_rotation_flow_cells()`內部邏輯——同該函式docstring
    已述的duplicate-small-helper慣例，避免耦合到§22.7/§17已發布程式碼路徑（那兩處
    輸出數字已發布，不可被本函式的任何調整波及）。

    population＝`stock_rows`本身的母體（同`build_rotation_cells`：需date/
    sub_industry/stock_id皆非null）——法人流向本質是次產業層級訊號，沿用§22.7
    既有母體宣告，非本函式新增限制。

    Args:
        daily_dates: 觸發序列所在的完整交易日曆（同`build_rotation_flow_cells`
            要求，非`stock_rows`本身的週頻日期）。
    """
    need = {"date", "sub_industry", "stock_id"}
    if stock_rows.is_empty() or not need.issubset(stock_rows.columns):
        return pl.DataFrame(schema=_ROTATION_CELLS_SCHEMA)
    base = stock_rows.drop_nulls(["date", "sub_industry", "stock_id"])
    if base.is_empty():
        return pl.DataFrame(schema=_ROTATION_CELLS_SCHEMA)
    if triggers.is_empty():
        return base.with_columns(pl.lit("miss").alias("cell"))

    day_index = pl.DataFrame(
        {"date": sorted(set(daily_dates)), "_day_idx": range(len(set(daily_dates)))}
    )
    sub_ind_days = (
        base.select("date", "sub_industry").unique()
        .join(day_index, on="date", how="left")
        .sort(["sub_industry", "_day_idx"])
    )
    trig_idx = (
        triggers.join(
            day_index.rename({"date": "_trig_date"}),
            left_on="date", right_on="_trig_date", how="inner",
        )
        .rename({"_day_idx": "_trig_idx"})
        .select("sub_industry", "_trig_idx")
        .sort(["sub_industry", "_trig_idx"])
    )
    joined = sub_ind_days.join_asof(
        trig_idx, left_on="_day_idx", right_on="_trig_idx", by="sub_industry",
        strategy="backward", check_sortedness=False,
    )
    joined = joined.with_columns(
        (
            pl.col("_trig_idx").is_not_null()
            & ((pl.col("_day_idx") - pl.col("_trig_idx")) <= lookback_window)
        ).fill_null(False).alias("_triggered_recent")
    ).with_columns(
        pl.when(pl.col("_triggered_recent"))
        .then(pl.lit("hit"))
        .otherwise(pl.lit("miss"))
        .alias("cell")
    ).select("date", "sub_industry", "cell")
    return base.join(joined, on=["date", "sub_industry"], how="inner")


def rotation_flow_grid(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    lookback_window: int = 15,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.7 全樣本讀值：4格（hit/miss × triggered/untriggered）forward alpha對照。"""
    cells = build_rotation_flow_cells(
        stock_rows, triggers, daily_dates, top_quantile=top_quantile,
        lookback_window=lookback_window,
    )
    return evaluate_signal_cells(
        cells, ROTATION_FLOW_CELLS, horizons, n_boot, seed, snapshot_gap_td
    )


def rotation_flow_by_regime(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    lookback_window: int = 15,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.7 regime切片：4格 × regime forward alpha。"""
    cells = build_rotation_flow_cells(
        stock_rows, triggers, daily_dates, top_quantile=top_quantile,
        lookback_window=lookback_window,
    )
    return evaluate_signal_cells_by_regime(
        cells, ROTATION_FLOW_CELLS, horizons, n_boot, seed, regime_col, snapshot_gap_td
    )


def walk_forward_rotation_flow(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    lookback_window: int = 15,
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.7 walk-forward：`build_rotation_flow_cells` ＋通用 `walk_forward_cells`。"""
    cells = build_rotation_flow_cells(
        stock_rows, triggers, daily_dates, top_quantile=top_quantile,
        lookback_window=lookback_window,
    )
    return walk_forward_cells(
        cells, ROTATION_FLOW_CELLS, horizons, n_splits, min_train_frac, n_boot, seed,
        snapshot_gap_td,
    )


# ---------------------------------------------------------------------------
# docs/31 §22.10 維度4：融資水位（個股層級，非次產業層級——見§22.10說明）
# ---------------------------------------------------------------------------

MARGIN_CELLS: tuple[str, ...] = ("hit", "miss")
_MARGIN_CELLS_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date, "stock_id": pl.Utf8, "cell": pl.Utf8,
}


def build_margin_cells(
    panel: pl.DataFrame,
    weekly_dates: set,
    chg_window: int = 5,
    top_quantile: float = 0.2,
    min_prev_lots: float = 50.0,
) -> pl.DataFrame:
    """§22.10：個股`margin_balance_lots`的`chg_window`交易日%變化，當週橫斷面
    前`top_quantile`（最快速增加）→`hit`，其餘→`miss`。

    **計算順序刻意在完整日頻`panel`上先算差分，才篩到`weekly_dates`**——若
    先篩週頻再差分，`chg_window`個交易日的窗會被誤算成`chg_window`個週快照
    （≈5倍天數），是§22.2已踩過的同一種尺度誤用風險，此處在計算順序上防範。

    `min_prev_lots`（預設50張）：分母（`chg_window`日前的水位）低於此值的列
    直接排除——避免融資餘額接近0的個股（如1張變2張＝+100%）的雜訊放大主導
    排名，非事後調整的超參數，見§22.10 pre-registration。

    Args:
        panel: 需含 date/stock_id/margin_balance_lots/alpha{h}（同panel.parquet
            原始結構，不套次產業membership——本維度是個股層級訊號）。
        weekly_dates: 週頻快照日期集合（同`weekly_snapshot_dates()`輸出）。
    """
    need = {"date", "stock_id", "margin_balance_lots"}
    if panel.is_empty() or not need.issubset(panel.columns):
        return pl.DataFrame(schema=_MARGIN_CELLS_SCHEMA)

    with_chg = (
        panel.sort(["stock_id", "date"])
        .with_columns(
            pl.col("margin_balance_lots").shift(chg_window).over("stock_id").alias("_prev")
        )
        .with_columns(
            pl.when(pl.col("_prev") >= min_prev_lots)
            .then((pl.col("margin_balance_lots") - pl.col("_prev")) / pl.col("_prev") * 100)
            .otherwise(None)
            .alias("_chg_pct")
        )
    )
    weekly = with_chg.filter(pl.col("date").is_in(list(weekly_dates))).drop_nulls(["_chg_pct"])
    if weekly.is_empty():
        return pl.DataFrame(schema=_MARGIN_CELLS_SCHEMA)

    ranked = weekly.with_columns(
        pl.col("_chg_pct").rank(method="min", descending=True).over("date").alias("_rank"),
        pl.col("_chg_pct").count().over("date").alias("_n"),
    ).with_columns(
        (pl.col("_rank") <= (pl.col("_n") * top_quantile).ceil()).alias("_hit")
    )
    return ranked.with_columns(
        pl.when(pl.col("_hit")).then(pl.lit("hit")).otherwise(pl.lit("miss")).alias("cell")
    ).drop(["_prev", "_chg_pct", "_rank", "_n", "_hit"])


def margin_grid(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window: int = 5,
    top_quantile: float = 0.2,
    min_prev_lots: float = 50.0,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.10 全樣本讀值：`hit`/`miss` 兩格 forward alpha 對照。"""
    cells = build_margin_cells(
        panel, weekly_dates, chg_window=chg_window, top_quantile=top_quantile,
        min_prev_lots=min_prev_lots,
    )
    return evaluate_signal_cells(cells, MARGIN_CELLS, horizons, n_boot, seed, snapshot_gap_td)


def margin_by_regime(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window: int = 5,
    top_quantile: float = 0.2,
    min_prev_lots: float = 50.0,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.10 regime切片：`hit`/`miss` 兩格 × regime forward alpha。"""
    cells = build_margin_cells(
        panel, weekly_dates, chg_window=chg_window, top_quantile=top_quantile,
        min_prev_lots=min_prev_lots,
    )
    return evaluate_signal_cells_by_regime(
        cells, MARGIN_CELLS, horizons, n_boot, seed, regime_col, snapshot_gap_td
    )


def walk_forward_margin(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window: int = 5,
    top_quantile: float = 0.2,
    min_prev_lots: float = 50.0,
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.10 walk-forward：`build_margin_cells` ＋通用 `walk_forward_cells`。"""
    cells = build_margin_cells(
        panel, weekly_dates, chg_window=chg_window, top_quantile=top_quantile,
        min_prev_lots=min_prev_lots,
    )
    return walk_forward_cells(
        cells, MARGIN_CELLS, horizons, n_splits, min_train_frac, n_boot, seed, snapshot_gap_td
    )


# ---------------------------------------------------------------------------
# docs/31 §22.12 維度5：價格動能（個股層級，ma60_dist_pct水位——非rank_velocity
# 的群組排名爬升速度，見§22.12說明）
# ---------------------------------------------------------------------------

MOMENTUM_CELLS: tuple[str, ...] = ("hit", "miss")
_MOMENTUM_CELLS_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date, "stock_id": pl.Utf8, "cell": pl.Utf8,
}


def build_momentum_cells(
    panel: pl.DataFrame,
    weekly_dates: set,
    top_quantile: float = 0.2,
) -> pl.DataFrame:
    """§22.12：個股`ma60_dist_pct`（收盤距自身60日均線%，panel既有欄位，不衍生
    新計算）當週橫斷面前`top_quantile`（距均線最遠／最強勢）→`hit`，其餘→`miss`。

    Args:
        panel: 需含 date/stock_id/ma60_dist_pct/alpha{h}。
        weekly_dates: 週頻快照日期集合（同`weekly_snapshot_dates()`輸出）。
    """
    need = {"date", "stock_id", "ma60_dist_pct"}
    if panel.is_empty() or not need.issubset(panel.columns):
        return pl.DataFrame(schema=_MOMENTUM_CELLS_SCHEMA)

    weekly = panel.filter(pl.col("date").is_in(list(weekly_dates))).drop_nulls(["ma60_dist_pct"])
    if weekly.is_empty():
        return pl.DataFrame(schema=_MOMENTUM_CELLS_SCHEMA)

    ranked = weekly.with_columns(
        pl.col("ma60_dist_pct").rank(method="min", descending=True).over("date").alias("_rank"),
        pl.col("ma60_dist_pct").count().over("date").alias("_n"),
    ).with_columns(
        (pl.col("_rank") <= (pl.col("_n") * top_quantile).ceil()).alias("_hit")
    )
    return ranked.with_columns(
        pl.when(pl.col("_hit")).then(pl.lit("hit")).otherwise(pl.lit("miss")).alias("cell")
    ).drop(["_rank", "_n", "_hit"])


def momentum_grid(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.12 全樣本讀值：`hit`/`miss` 兩格 forward alpha 對照。"""
    cells = build_momentum_cells(panel, weekly_dates, top_quantile=top_quantile)
    return evaluate_signal_cells(cells, MOMENTUM_CELLS, horizons, n_boot, seed, snapshot_gap_td)


def momentum_by_regime(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.12 regime切片：`hit`/`miss` 兩格 × regime forward alpha。"""
    cells = build_momentum_cells(panel, weekly_dates, top_quantile=top_quantile)
    return evaluate_signal_cells_by_regime(
        cells, MOMENTUM_CELLS, horizons, n_boot, seed, regime_col, snapshot_gap_td
    )


def walk_forward_momentum(
    panel: pl.DataFrame,
    weekly_dates: set,
    horizons: tuple[int, ...] = (10, 20, 40),
    top_quantile: float = 0.2,
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.12 walk-forward：`build_momentum_cells` ＋通用 `walk_forward_cells`。"""
    cells = build_momentum_cells(panel, weekly_dates, top_quantile=top_quantile)
    return walk_forward_cells(
        cells, MOMENTUM_CELLS, horizons, n_splits, min_train_frac, n_boot, seed, snapshot_gap_td
    )


# ---------------------------------------------------------------------------
# docs/31 §22.19 維度3：大戶集中度（個股層級，big_holder_pct 週對週 Δpp——非水位
# 本身；big_holder_pct 只有 TDCC 公布日有值，故差分在「TDCC 公布日序列」上算，
# 不在完整日頻 panel 上算，見§22.19說明）
# ---------------------------------------------------------------------------

BIGHOLDER_CELLS: tuple[str, ...] = ("hit", "miss")
_BIGHOLDER_CELLS_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date, "stock_id": pl.Utf8, "cell": pl.Utf8,
}


def build_bigholder_cells(
    panel: pl.DataFrame,
    chg_window_weeks: int = 1,
    top_quantile: float = 0.2,
    min_prev_pct: float = 1.0,
    metric_col: str = "big_holder_pct",
) -> pl.DataFrame:
    """§22.19：個股 `metric_col`（`big_holder_pct`＝≥400 張大戶占集保庫存 %，或
    `big_holder_1000_pct`＝千張大戶）的**週對週變化，單位為百分點 Δpp**（非相對
    %——集中度本身已是 %，Δpp 是自然單位），當 TDCC 公布日橫斷面前 `top_quantile`
    （集中度上升最快）→ `hit`，其餘 → `miss`。

    **計算順序刻意先 `drop_nulls([metric_col])` 才 `shift`**——`big_holder_pct` 只有
    TDCC 公布日有值（`panel.py` 用 exact `data_date`→`date` join、不 forward-fill），
    先篩非 null 得到「TDCC 公布日序列」，`shift(chg_window_weeks)` 才是「上一個 TDCC
    週」；若在完整日頻 panel 上 `shift` 會抓到相鄰日曆日的 null。與維度4（`build_
    margin_cells` 在完整日頻上先差分）的順序相反，因兩者原始資料頻率不同。

    `min_prev_pct`（預設 1.0%）：`chg_window_weeks` 週前的 `metric_col` 水位低於此值
    的列排除在排名外——**排除集保庫存占比極小、大戶欄位本身意義薄弱的個股**（非
    「防雜訊放大」：Δpp 不會在小分母上爆掉，與維度4 的 `min_prev_lots` 動機不同）。
    預先寫死，非事後調整，見§22.19 pre-registration。

    觸發／差分邏輯**刻意獨立重寫一份**，不重用 `data/tdcc.py` 的 `big_holder_wow`
    ——那是「左 join 自最新週」的 production 最新快照 helper、非歷史序列（同全案
    duplicate-small-helper 慣例，避免耦合到 production 程式碼路徑）。

    Args:
        panel: 需含 date/stock_id/`metric_col`/alpha{h}（同 panel.parquet 原始結構，
            不套次產業 membership——本維度是個股層級訊號）。
    """
    need = {"date", "stock_id", metric_col}
    if panel.is_empty() or not need.issubset(panel.columns):
        return pl.DataFrame(schema=_BIGHOLDER_CELLS_SCHEMA)

    snap = (
        panel.drop_nulls([metric_col])
        .sort(["stock_id", "date"])
        .with_columns(
            pl.col(metric_col).shift(chg_window_weeks).over("stock_id").alias("_prev")
        )
        .with_columns(
            pl.when(pl.col("_prev") >= min_prev_pct)
            .then(pl.col(metric_col) - pl.col("_prev"))
            .otherwise(None)
            .alias("_chg")
        )
        .drop_nulls(["_chg"])
    )
    if snap.is_empty():
        return pl.DataFrame(schema=_BIGHOLDER_CELLS_SCHEMA)

    ranked = snap.with_columns(
        pl.col("_chg").rank(method="min", descending=True).over("date").alias("_rank"),
        pl.col("_chg").count().over("date").alias("_n"),
    ).with_columns(
        (pl.col("_rank") <= (pl.col("_n") * top_quantile).ceil()).alias("_hit")
    )
    return ranked.with_columns(
        pl.when(pl.col("_hit")).then(pl.lit("hit")).otherwise(pl.lit("miss")).alias("cell")
    ).drop(["_prev", "_chg", "_rank", "_n", "_hit"])


def bigholder_grid(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window_weeks: int = 1,
    top_quantile: float = 0.2,
    min_prev_pct: float = 1.0,
    metric_col: str = "big_holder_pct",
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.19 全樣本讀值：`hit`/`miss` 兩格 forward alpha 對照。"""
    cells = build_bigholder_cells(
        panel, chg_window_weeks=chg_window_weeks, top_quantile=top_quantile,
        min_prev_pct=min_prev_pct, metric_col=metric_col,
    )
    return evaluate_signal_cells(cells, BIGHOLDER_CELLS, horizons, n_boot, seed, snapshot_gap_td)


def bigholder_by_regime(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window_weeks: int = 1,
    top_quantile: float = 0.2,
    min_prev_pct: float = 1.0,
    metric_col: str = "big_holder_pct",
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.19 regime 切片：`hit`/`miss` 兩格 × regime forward alpha。"""
    cells = build_bigholder_cells(
        panel, chg_window_weeks=chg_window_weeks, top_quantile=top_quantile,
        min_prev_pct=min_prev_pct, metric_col=metric_col,
    )
    return evaluate_signal_cells_by_regime(
        cells, BIGHOLDER_CELLS, horizons, n_boot, seed, regime_col, snapshot_gap_td
    )


def walk_forward_bigholder(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20, 40),
    chg_window_weeks: int = 1,
    top_quantile: float = 0.2,
    min_prev_pct: float = 1.0,
    metric_col: str = "big_holder_pct",
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """§22.19 walk-forward：`build_bigholder_cells` ＋通用 `walk_forward_cells`。"""
    cells = build_bigholder_cells(
        panel, chg_window_weeks=chg_window_weeks, top_quantile=top_quantile,
        min_prev_pct=min_prev_pct, metric_col=metric_col,
    )
    return walk_forward_cells(
        cells, BIGHOLDER_CELLS, horizons, n_splits, min_train_frac, n_boot, seed, snapshot_gap_td
    )


_PAIRWISE_COMBO_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date, "stock_id": pl.Utf8, "cell": pl.Utf8, "regime": pl.Utf8,
}

PAIRWISE_COMBO_CELLS: tuple[str, ...] = ("both_hit", "a_only_hit", "b_only_hit", "neither")


def build_pairwise_combo_cells(cells_a: pl.DataFrame, cells_b: pl.DataFrame) -> pl.DataFrame:
    """§22.15：任兩個既有hit/miss cell表（`build_rotation_cells`／`build_margin_cells`／
    `build_momentum_cells`輸出）在(date, stock_id)上inner join成2x2交叉，4格
    （both_hit/a_only_hit/b_only_hit/neither）。

    population＝兩表交集（inner join，不補值）——見§22.15各組合的覆蓋率聲明，
    不同維度的個股母體本來就不同，交集即為組合可測的實際母體。`alpha{h}`/
    `regime`等非cell欄位取自`cells_b`（兩者皆源自同一份panel，數值理應一致，
    只是避免join後重複欄位），呼叫端若要用進攻regime前提過濾，對回傳結果的
    `regime`欄`.filter()`即可（本函式本身不做任何regime過濾，維持通用）。
    """
    need = {"date", "stock_id", "cell"}
    if cells_a.is_empty() or cells_b.is_empty():
        return pl.DataFrame(schema=_PAIRWISE_COMBO_SCHEMA)
    if not need.issubset(cells_a.columns) or not need.issubset(cells_b.columns):
        return pl.DataFrame(schema=_PAIRWISE_COMBO_SCHEMA)

    a = cells_a.select("date", "stock_id", pl.col("cell").alias("_cell_a"))
    b_cols = [c for c in cells_b.columns if c != "cell"]
    b = cells_b.select(*b_cols, pl.col("cell").alias("_cell_b"))
    joined = a.join(b, on=["date", "stock_id"], how="inner")
    if joined.is_empty():
        return pl.DataFrame(schema=_PAIRWISE_COMBO_SCHEMA)

    return joined.with_columns(
        pl.when((pl.col("_cell_a") == "hit") & (pl.col("_cell_b") == "hit"))
        .then(pl.lit("both_hit"))
        .when((pl.col("_cell_a") == "hit") & (pl.col("_cell_b") == "miss"))
        .then(pl.lit("a_only_hit"))
        .when((pl.col("_cell_a") == "miss") & (pl.col("_cell_b") == "hit"))
        .then(pl.lit("b_only_hit"))
        .otherwise(pl.lit("neither"))
        .alias("cell")
    ).drop(["_cell_a", "_cell_b"])
