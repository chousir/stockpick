"""backtest/g3_grid.py — docs/31 §4.1 G3 候選 filter 驗證（族群前5×個股貼落後×位階不極端×
資金非負）。

docs/31 §9 milestone：把 G3（族群已轉強、個股還沒領先）從規劃書裡的設計假說變成有沒有用的
答案。四維全部沿用生產既有量尺，不發明新口徑（G3 定義見 docs/31 §4.1）：

- 族群強弱：`trend_score` 當日跨**次產業**（非跨個股列）排名前 N（預設5，非 laggard_grid
  的中位切）；
- 個股領先/落後：`rs_subind`（同 laggard_grid.stock_rs_vs_group 口徑）落在區間
  [rs_lo, rs_hi]（預設 [-15,0]，非 laggard_grid 的單純 <0）；
- 位階：`ma60_dist_pct` 落在區間 [ma60_lo, ma60_hi]（預設 [-10,10]，**有下界**——單邊
  ≤10 會把「破線股」與「健康未延伸股」混在同一格，2026-08-22 opus 複核抓到的實作缺陷）；
- 資金：`net_flow_{w}d_z`（個股自身滾動 z，同 analysis/stock_panel.py 口徑）≥ 0。

**不修改 laggard_grid.py 既有函式**（避免動到已驗證上線的 WS-D 管線引入迴歸）——四維定義
不同，刻意寫成平行模組，共用的只有 factor_lab 的 bootstrap／regime 判準函式，純函式；
IO 由 runner 負責。

**CI 方法論（2026-08-22 修正）**：`alpha{h}` 右偏（大樣本格幾乎必然 mean>0、CI 不跨0），
對原始 alpha 做「CI 是否跨0」檢定在此右偏分布下結構上幾乎不可能失敗，等於沒檢定——改成對
**delta{h}＝alpha{h} − 當日全樣本(sub)均值** 做 CI，才是回答「這格有沒有比同一天的市場整體
更好」。CI 用 moving-block bootstrap（非 iid pooled bootstrap，因相鄰週快照前瞻窗重疊、
同日同族群列高度相關），block 長度以「快照步數」計（`ceil((h+1)/snapshot_gap_td)`），
非原始交易日數——週頻抽樣本身就是「快照」單位，用交易日數當 block 長度單位不一致
（2026-08-22 opus 複核抓到）。

**升 gate 硬門檻**（docs/31 §7.4 預先登記，抄自 docs/24 M-BR1 先例，非本次發明）：方向
≥4/5 walk-forward 分段一致（此處以前後半段h1/h2同向近似，見docstring說明非完整5段）＋
block-bootstrap CI 排除0 ＋ 跨≥2 regime（尤其須含2022空頭）。三缺一 → 停在揭露欄+人工觀察，
按docs/24格式記錄「已否證」或「未過關」，不得寫成「未驗證」。
"""

from __future__ import annotations

import math

import polars as pl

from tw_screener.backtest.factor_lab import (
    REGIME_LABELS,
    REGIME_MIN_N,
    moving_block_bootstrap_ci,
)

_CELL_DIMS = ("group_strength", "stock_pos", "tier", "flow")


def _smean(s: pl.Series) -> float | None:
    v = s.mean()
    return float(v) if isinstance(v, (int, float)) else None


def assign_g3_cells(
    stock_rows: pl.DataFrame,
    rs_range: tuple[float, float] = (-15.0, 0.0),
    ma60_range: tuple[float, float] = (-10.0, 10.0),
    top_n_groups: int = 5,
) -> pl.DataFrame:
    """四維 G3 cell 標籤——group_strength/stock_pos/tier/flow。丟四維任一 null 的列。

    需含：date / sub_industry / rs_subind / trend_score / ma60_dist_pct / net_flow_z。
    排名分母修正（2026-08-22）：先在 (date, sub_industry) 去重取 trend_score，對**次產業**
    排名，再 join 回個股列——舊版直接對個股列 rank 會被單一大族群的成員數灌爆名次分母。
    """
    rs_lo, rs_hi = rs_range
    ma_lo, ma_hi = ma60_range
    need = {"date", "sub_industry", "rs_subind", "trend_score", "ma60_dist_pct", "net_flow_z"}
    base = stock_rows.drop_nulls(list(need))
    if base.is_empty():
        return base
    group_rank = (
        base.select("date", "sub_industry", "trend_score")
        .unique(subset=["date", "sub_industry"])
        .with_columns(
            pl.col("trend_score").rank(method="min", descending=True).over("date").alias("_rank")
        )
        .select("date", "sub_industry", "_rank")
    )
    base = base.join(group_rank, on=["date", "sub_industry"], how="left")
    return base.with_columns(
        pl.when(pl.col("_rank") <= top_n_groups)
        .then(pl.lit("族群前5")).otherwise(pl.lit("族群非前5")).alias("group_strength"),
        pl.when((pl.col("rs_subind") >= rs_lo) & (pl.col("rs_subind") <= rs_hi))
        .then(pl.lit("貼落後")).otherwise(pl.lit("非貼落後")).alias("stock_pos"),
        pl.when((pl.col("ma60_dist_pct") >= ma_lo) & (pl.col("ma60_dist_pct") <= ma_hi))
        .then(pl.lit("位階中段")).otherwise(pl.lit("破線或延伸")).alias("tier"),
        pl.when(pl.col("net_flow_z") >= 0)
        .then(pl.lit("資金非負")).otherwise(pl.lit("資金為負")).alias("flow"),
    ).drop("_rank")


def _block_len_snapshots(h: int, snapshot_gap_td: int) -> int:
    """horizon（交易日）換算成「快照步數」的 block 長度，至少1。"""
    return max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))


def g3_cell_grid(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    rs_range: tuple[float, float] = (-15.0, 0.0),
    ma60_range: tuple[float, float] = (-10.0, 10.0),
    top_n_groups: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """2×2×2×2（族群/個股/位階/資金）forward 報酬格——所有 cell 全列，含 G3 本身
    （四維皆命中目標格）與其餘15格對照。

    `mean`/`median`/`win_rate` 為原始 alpha（供人讀取量級參考）；`ci_lo`/`ci_hi` 是對
    **delta（cell per-date mean − 當日全樣本 per-date mean）**的 moving-block bootstrap，
    不是對原始 alpha——原始 alpha 右偏，CI 幾乎必然不跨0，不是有意義的檢定（見模組
    docstring）。`delta_mean` 額外附上，方便對照 ci 是否真的圍著它。
    """
    schema = {
        "horizon": pl.Int64, "group_strength": pl.Utf8, "stock_pos": pl.Utf8,
        "tier": pl.Utf8, "flow": pl.Utf8, "is_g3": pl.Boolean, "n": pl.UInt32,
        "n_dates": pl.UInt32, "mean": pl.Float64, "median": pl.Float64,
        "win_rate": pl.Float64, "delta_mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64,
        "mean_h1": pl.Float64, "mean_h2": pl.Float64,
    }
    need = {"date", "sub_industry", "rs_subind", "trend_score", "ma60_dist_pct", "net_flow_z"}
    if stock_rows.is_empty() or not need.issubset(stock_rows.columns):
        return pl.DataFrame(schema=schema)
    base = assign_g3_cells(stock_rows, rs_range, ma60_range, top_n_groups)
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    mid_date = base["date"].median()
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for key, g in sub.group_by(list(_CELL_DIMS), maintain_order=True):
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
            is_g3 = (
                key[0] == "族群前5" and key[1] == "貼落後"
                and key[2] == "位階中段" and key[3] == "資金非負"
            )
            rows.append(
                {
                    "horizon": h,
                    "group_strength": str(key[0]), "stock_pos": str(key[1]),
                    "tier": str(key[2]), "flow": str(key[3]), "is_g3": is_g3,
                    "n": len(vals), "n_dates": len(delta_vals),
                    "mean": sum(vals) / len(vals) if vals else None,
                    "median": float(med) if isinstance(med, (int, float)) else None,
                    "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                    "delta_mean": sum(delta_vals) / len(delta_vals) if delta_vals else None,
                    "ci_lo": ci_lo, "ci_hi": ci_hi,
                    "mean_h1": _smean(h1), "mean_h2": _smean(h2),
                }
            )
        vals = [float(v) for v in sub[tgt].to_list()]
        med = sub[tgt].median()
        h1 = sub.filter(pl.col("date") <= mid_date)[tgt]
        h2 = sub.filter(pl.col("date") > mid_date)[tgt]
        rows.append(
            {
                "horizon": h, "group_strength": "（全體）", "stock_pos": "—",
                "tier": "—", "flow": "—", "is_g3": False,
                "n": len(vals), "n_dates": None,
                "mean": sum(vals) / len(vals) if vals else None,
                "median": float(med) if isinstance(med, (int, float)) else None,
                "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                "delta_mean": 0.0, "ci_lo": None, "ci_hi": None,
                "mean_h1": _smean(h1), "mean_h2": _smean(h2),
            }
        )
    return pl.DataFrame(rows, schema=schema).sort(
        ["horizon", "group_strength", "stock_pos", "tier", "flow"]
    )


def g3_cell_grid_by_regime(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    rs_range: tuple[float, float] = (-15.0, 0.0),
    ma60_range: tuple[float, float] = (-10.0, 10.0),
    top_n_groups: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """G3 cell×regime forward 報酬格（對delta做CI，理由同g3_cell_grid）——只列 G3 目標格
    vs 全體基準（不像 laggard_grid 全16格×regime全列，因裁決只在乎 G3 格本身跨regime是否
    同向；其餘15格對照見 g3_cell_grid）。

    regime 缺席或全 null（WS-H 標籤未產）→ 空表，呼叫端誠實跳過整段。
    """
    schema = {
        "horizon": pl.Int64, "regime": pl.Utf8, "n": pl.Int64, "n_dates": pl.Int64,
        "mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64, "thin": pl.Boolean,
    }
    need = {"date", "sub_industry", "rs_subind", "trend_score", "ma60_dist_pct", "net_flow_z"}
    if (
        stock_rows.is_empty()
        or not need.issubset(stock_rows.columns)
        or regime_col not in stock_rows.columns
        or stock_rows[regime_col].drop_nulls().is_empty()
    ):
        return pl.DataFrame(schema=schema)
    base = assign_g3_cells(stock_rows, rs_range, ma60_range, top_n_groups)
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        g3_only = sub.filter(
            (pl.col("group_strength") == "族群前5")
            & (pl.col("stock_pos") == "貼落後")
            & (pl.col("tier") == "位階中段")
            & (pl.col("flow") == "資金非負")
        )
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for reg in REGIME_LABELS:
            g = g3_only.filter(pl.col(regime_col) == reg)
            daily = g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
            vals = [float(v) for v in daily["_m"].to_list() if v is not None]
            lo_ci, hi_ci = (
                moving_block_bootstrap_ci(vals, block_len=block_len, n_boot=n_boot, seed=seed)
                if vals else (None, None)
            )
            rows.append(
                {
                    "horizon": h, "regime": reg, "n": g.height, "n_dates": len(vals),
                    "mean": (sum(vals) / len(vals)) if vals else None,
                    "ci_lo": lo_ci, "ci_hi": hi_ci, "thin": len(vals) < REGIME_MIN_N,
                }
            )
    return pl.DataFrame(rows, schema=schema)
