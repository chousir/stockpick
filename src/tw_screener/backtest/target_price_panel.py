"""backtest/target_price_panel.py — docs/31 §20.13 Phase 1：實驗性機械式「目標價」的
歷史類比面板 ＋ forward-return 分位數原語。

機械式目標價的定位（§20.13 開頭，誠實劃界）：**不是基本面估值、不是擇時訊號**，
而是「profile（位階 × 族群內相對強弱）相似的股票，過去 N 個交易日報酬的歷史分位
數」——天氣預報式的機率陳述。本模組只負責 (1) 重建歷史類比面板、(2) 逐 cell 算
forward 報酬分位數查表；fit/test 切分、投射、校準裁決在 `target_price_read.py`。

設計要點（2026-09-01，advisor 複核後定案）：
- **2 軸 9 格**：`ma60_dist_pct`（位階）× `rs_subind`（族群內相對強弱，20 日窗）各
  切 3 bin——只 2 軸避免動能類指標共線、避免小格。regime **不進 cell key**，當切片
  （比照 `laggard_cell_grid` / `g3_cell_grid` 各配一支 `_by_regime`）。
- **價格腿 only**（forward 報酬不加回股利）：fit/test 雙邊同口徑、系統性偏誤抵銷；
  6 個月窗跨台股 7–8 月除息季 → 高殖利率股的機械數字偏**保守**（單向偏誤，§20.13）。
- 重用 `panel.build_price_panel`（forward r{h}、防前視 shift(-1) entry）、
  `laggard_grid.stock_rs_vs_group`（rs_subind 口徑）。

純函式；IO（parquet 載入/落地）由 runner / `target_price_read` 負責。
"""

from __future__ import annotations

import polars as pl

from tw_screener.backtest.laggard_grid import stock_rs_vs_group
from tw_screener.backtest.panel import build_price_panel

HORIZONS: tuple[int, ...] = (20, 60, 120)
PCTILES: tuple[int, ...] = (10, 25, 50, 60, 75, 90)
CELL_DIMS: tuple[str, ...] = ("pos_bin", "rs_bin")
POOLED_CELL = "_pooled"

# cell bin 切點（事前鎖定；runner 從 config/settings.yaml 覆寫）
POS_EDGES: tuple[float, float] = (-8.0, 8.0)   # ma60_dist_pct：貼低 / 中段 / 延伸
RS_EDGES: tuple[float, float] = (-5.0, 5.0)    # rs_subind pp：落後 / 均勢 / 領先

_PANEL_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "stock_id": pl.Utf8,
    "close": pl.Float64,
    "ma60_dist_pct": pl.Float64,
    "rs_subind": pl.Float64,
    "regime": pl.Utf8,
    "pos_bin": pl.Utf8,
    "rs_bin": pl.Utf8,
    "cell": pl.Utf8,
}

_PCTILE_SCHEMA: dict[str, type[pl.DataType]] = {
    "cell": pl.Utf8,
    "horizon": pl.Int64,
    "n": pl.Int64,
    "n_dates": pl.Int64,
    **{f"p{k}": pl.Float64 for k in PCTILES},
    "iqr": pl.Float64,
}


def _pos_bin_expr(edges: tuple[float, float]) -> pl.Expr:
    lo, hi = edges
    return (
        pl.when(pl.col("ma60_dist_pct") <= lo)
        .then(pl.lit(f"位階貼低≤{lo:.0f}"))
        .when(pl.col("ma60_dist_pct") <= hi)
        .then(pl.lit(f"位階中段{lo:.0f}~{hi:.0f}"))
        .otherwise(pl.lit(f"位階延伸>{hi:.0f}"))
        .alias("pos_bin")
    )


def _rs_bin_expr(edges: tuple[float, float]) -> pl.Expr:
    lo, hi = edges
    return (
        pl.when(pl.col("rs_subind") <= lo)
        .then(pl.lit(f"族群內落後≤{lo:.0f}"))
        .when(pl.col("rs_subind") <= hi)
        .then(pl.lit(f"族群內均勢{lo:.0f}~{hi:.0f}"))
        .otherwise(pl.lit(f"族群內領先>{hi:.0f}"))
        .alias("rs_bin")
    )


def assign_analog_cells(
    profile_rows: pl.DataFrame,
    pos_edges: tuple[float, float] = POS_EDGES,
    rs_edges: tuple[float, float] = RS_EDGES,
) -> pl.DataFrame:
    """加 `pos_bin` / `rs_bin` / `cell` 三欄——兩維任一 null 的列丟掉（如實劃界）。

    需含 `ma60_dist_pct` / `rs_subind`。`cell` ＝ `pos_bin` + "｜" + `rs_bin`（9 種）。
    """
    need = {"ma60_dist_pct", "rs_subind"}
    if profile_rows.is_empty() or not need.issubset(profile_rows.columns):
        return profile_rows
    base = profile_rows.drop_nulls(["ma60_dist_pct", "rs_subind"])
    if base.is_empty():
        return base
    return base.with_columns(
        _pos_bin_expr(pos_edges), _rs_bin_expr(rs_edges)
    ).with_columns(
        (pl.col("pos_bin") + pl.lit("｜") + pl.col("rs_bin")).alias("cell")
    )


def build_analog_panel(
    price: pl.DataFrame,
    membership: pl.DataFrame,
    regime: pl.DataFrame | None = None,
    horizons: tuple[int, ...] = HORIZONS,
    pos_edges: tuple[float, float] = POS_EDGES,
    rs_edges: tuple[float, float] = RS_EDGES,
    rs_window: int = 20,
    min_rows_per_day: int = 900,
) -> pl.DataFrame:
    """歷史類比面板：(date, stock_id) × [close, r{h}, ma60_dist_pct, rs_subind, regime,
    pos_bin, rs_bin, cell]。

    Args:
        price: 日線長表（date/stock_id/close[/volume]），須涵蓋 2022 起至今——
            forward r{h} 由 `build_price_panel` 算（防前視：entry＝次一交易日收盤）。
        membership: `list_subindustries()` 輸出（sub_industry/stock_id）——多標籤股
            在各次產業各算一次 rs_subind，這裡取 (date, stock_id) 平均折成單列。
        regime: `regime_labels.parquet` 讀入（date/regime_label）；None → regime 全 null。
        min_rows_per_day: 該交易日普通股列數下限——低於此視為部分抓取日、整日剔除
            （與 grouping 篩選層 fill 0 的降級慣例不同：這是 ground truth，寧缺勿濫）。

    Returns:
        欄位見 `_PANEL_SCHEMA`；輸入不足 → 空表。
    """
    if price.is_empty() or membership.is_empty():
        return pl.DataFrame(schema=_PANEL_SCHEMA)

    fresh = build_price_panel(price, horizons=horizons, regime=regime)
    if fresh.is_empty():
        return pl.DataFrame(schema=_PANEL_SCHEMA)

    # 部分抓取日剔除
    ok_dates = (
        fresh.group_by("date")
        .agg(pl.len().alias("_n"))
        .filter(pl.col("_n") >= min_rows_per_day)
        .select("date")
    )
    fresh = fresh.join(ok_dates, on="date", how="inner")
    if fresh.is_empty():
        return pl.DataFrame(schema=_PANEL_SCHEMA)

    rs = stock_rs_vs_group(fresh, membership, window=rs_window)
    if rs.is_empty():
        return pl.DataFrame(schema=_PANEL_SCHEMA)
    rs_one = rs.group_by("date", "stock_id").agg(pl.col("rs_subind").mean())

    keep = ["date", "stock_id", "close", "ma60_dist_pct"]
    keep += [f"r{h}" for h in horizons]
    keep += ["regime"] if "regime" in fresh.columns else []
    joined = fresh.select(keep).join(rs_one, on=["date", "stock_id"], how="inner")
    if "regime" not in joined.columns:
        joined = joined.with_columns(pl.lit(None, dtype=pl.Utf8).alias("regime"))

    celled = assign_analog_cells(joined, pos_edges, rs_edges)
    if celled.is_empty():
        return pl.DataFrame(schema=_PANEL_SCHEMA)
    out_cols = list(_PANEL_SCHEMA) + [f"r{h}" for h in horizons]
    return celled.select([c for c in out_cols if c in celled.columns]).sort(
        "stock_id", "date"
    )


def forward_return_percentiles(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    pctiles: tuple[int, ...] = PCTILES,
    cell_col: str = "cell",
) -> pl.DataFrame:
    """逐 (cell, horizon) 算 forward 報酬 r{h} 的分位數查表（＋ `_pooled` 全樣本列）。

    n＝非 null r{h} 列數；n_dates＝distinct 交易日數（讀表判樣本是否夠格看 n_dates
    而非 n——同一 cell 相鄰日高度重疊）；iqr＝p75−p25。

    分位用 linear interpolation。輸入空或缺 r{h} → 空表。
    """
    if panel.is_empty() or cell_col not in panel.columns:
        return pl.DataFrame(schema=_PCTILE_SCHEMA)

    rows: list[dict] = []
    for h in horizons:
        tgt = f"r{h}"
        if tgt not in panel.columns:
            continue
        sub = panel.drop_nulls([tgt, cell_col])
        if sub.is_empty():
            continue

        agg_exprs = [
            pl.len().alias("n"),
            pl.col("date").n_unique().alias("n_dates"),
            *[
                pl.col(tgt).quantile(k / 100, interpolation="linear").alias(f"p{k}")
                for k in pctiles
            ],
        ]
        per_cell = sub.group_by(cell_col).agg(agg_exprs).rename({cell_col: "cell"})

        pooled = sub.select(
            pl.lit(POOLED_CELL).alias("cell"),
            pl.len().alias("n"),
            pl.col("date").n_unique().alias("n_dates"),
            *[
                pl.col(tgt).quantile(k / 100, interpolation="linear").alias(f"p{k}")
                for k in pctiles
            ],
        )

        combined = pl.concat([per_cell, pooled], how="diagonal")
        for r in combined.iter_rows(named=True):
            p25, p75 = r.get("p25"), r.get("p75")
            rows.append(
                {
                    "cell": r["cell"],
                    "horizon": h,
                    "n": int(r["n"]),
                    "n_dates": int(r["n_dates"]),
                    **{f"p{k}": r.get(f"p{k}") for k in pctiles},
                    "iqr": (p75 - p25) if p25 is not None and p75 is not None else None,
                }
            )
    return pl.DataFrame(rows, schema=_PCTILE_SCHEMA) if rows else pl.DataFrame(
        schema=_PCTILE_SCHEMA
    )
