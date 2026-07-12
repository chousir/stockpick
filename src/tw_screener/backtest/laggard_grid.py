"""backtest/laggard_grid.py — WS-D 族群內強弱正式化（2×2×位階 forward 報酬格）。

WS5-② 的正式驗證：把 cp 副表的 laggard 讀法（族群強 × 個股落後 × 位階貼低）
攤成完整格——**所有 cell 全列**（不是只看 ambush 那格），森 n＋bootstrap CI。

維度定義（全部沿用生產既有量尺，不發明新口徑）：
- 族群強弱：WS-C 重建 trend_score 同日跨族群中位切（強/弱）；
- 個股領先/落後：rs_subind＝個股 20 日報酬 − 次產業籃 20 日等權報酬（pp），
  <0＝落後（docs/18 生產切點 threshold=0.0）；
- 位階：ma60_dist_pct 三層——貼低 ≤ base_zone（10）／中段／延伸 > F2 gate（15）。

證據門檻（主排序權重提案的先決條件，寫死在報告不是程式）：
ambush cell（強×落後×貼低）須同時 (a) CI 與全體分離 (b) 前後半段同向，
否則結論落「需更多樣本」。純函式；IO 由 runner 負責。
"""

from __future__ import annotations

import polars as pl

from tw_screener.backtest.factor_lab import (
    REGIME_LABELS,
    REGIME_MIN_N,
    bootstrap_mean_ci,
    moving_block_bootstrap_ci,
)


def _smean(s: pl.Series) -> float | None:
    v = s.mean()
    return float(v) if isinstance(v, (int, float)) else None


def _tier_labels(tier_edges: tuple[float, float]) -> tuple[str, str, str]:
    lo, hi = tier_edges
    return (f"貼低≤{lo:.0f}", f"中段{lo:.0f}–{hi:.0f}", f"延伸>{hi:.0f}")


def _assign_cells(stock_rows: pl.DataFrame, tier_edges: tuple[float, float]) -> pl.DataFrame:
    """三維 cell 標籤（group_strength/stock_pos/tier）——laggard_cell_grid 與
    regime 切片版共用同一份切法，防兩處口徑漂移。丟三維任一 null 的列。"""
    lo, hi = tier_edges
    tiers = _tier_labels(tier_edges)
    return (
        stock_rows.drop_nulls(["rs_subind", "trend_score", "ma60_dist_pct"])
        .with_columns(
            pl.when(pl.col("trend_score") >= pl.col("trend_score").median().over("date"))
            .then(pl.lit("族群強")).otherwise(pl.lit("族群弱")).alias("group_strength"),
            pl.when(pl.col("rs_subind") < 0.0)
            .then(pl.lit("落後")).otherwise(pl.lit("領先")).alias("stock_pos"),
            pl.when(pl.col("ma60_dist_pct") <= lo).then(pl.lit(tiers[0]))
            .when(pl.col("ma60_dist_pct") <= hi).then(pl.lit(tiers[1]))
            .otherwise(pl.lit(tiers[2])).alias("tier"),
        )
    )


def stock_rs_vs_group(
    panel: pl.DataFrame,
    membership: pl.DataFrame,
    window: int = 20,
) -> pl.DataFrame:
    """(date, stock_id, sub_industry, rs_subind)：個股 vs 次產業籃同窗報酬差（pp）。

    籃報酬＝成員同窗報酬等權平均（含自身；多標籤股在各籃各算一列）。
    個股窗報酬不足（新股/停牌）→ 該列不出。
    """
    if panel.is_empty() or membership.is_empty():
        return pl.DataFrame()
    px = (
        panel.select("date", "stock_id", "close")
        .sort("stock_id", "date")
        .with_columns(
            ((pl.col("close") / pl.col("close").shift(window).over("stock_id")) - 1)
            .alias("_ret_w")
        )
        .drop_nulls("_ret_w")
    )
    rows = membership.join(px, on="stock_id", how="inner")
    return (
        rows.with_columns(
            pl.col("_ret_w").mean().over("sub_industry", "date").alias("_basket_ret")
        )
        .select(
            "date", "stock_id", "sub_industry",
            ((pl.col("_ret_w") - pl.col("_basket_ret")) * 100).alias("rs_subind"),
        )
    )


def laggard_cell_grid(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    tier_edges: tuple[float, float] = (10.0, 15.0),
    n_boot: int = 1000,
) -> pl.DataFrame:
    """2（族群強弱）×2（領先/落後）×3（位階）forward 報酬格——所有 cell 全列。

    stock_rows 需含：date / rs_subind / trend_score（該股所屬族群當日）/
    ma60_dist_pct / alpha{h}。另附前後半段平均（同向性檢查）。
    """
    schema = {
        "horizon": pl.Int64, "group_strength": pl.Utf8, "stock_pos": pl.Utf8,
        "tier": pl.Utf8, "n": pl.UInt32, "mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "median": pl.Float64,
        "win_rate": pl.Float64, "mean_h1": pl.Float64, "mean_h2": pl.Float64,
    }
    need = {"date", "rs_subind", "trend_score", "ma60_dist_pct"}
    if stock_rows.is_empty() or not need.issubset(stock_rows.columns):
        return pl.DataFrame(schema=schema)
    base = _assign_cells(stock_rows, tier_edges)
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    mid_date = base["date"].median()
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        for key, g in sub.group_by(
            ["group_strength", "stock_pos", "tier"], maintain_order=True
        ):
            vals = [float(v) for v in g[tgt].to_list()]
            ci_lo, ci_hi = bootstrap_mean_ci(vals, n_boot=n_boot)
            h1 = g.filter(pl.col("date") <= mid_date)[tgt]
            h2 = g.filter(pl.col("date") > mid_date)[tgt]
            med = g[tgt].median()
            rows.append(
                {
                    "horizon": h,
                    "group_strength": str(key[0]),
                    "stock_pos": str(key[1]),
                    "tier": str(key[2]),
                    "n": len(vals),
                    "mean": sum(vals) / len(vals) if vals else None,
                    "ci_lo": ci_lo,
                    "ci_hi": ci_hi,
                    "median": float(med) if isinstance(med, (int, float)) else None,
                    "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                    "mean_h1": _smean(h1),
                    "mean_h2": _smean(h2),
                }
            )
        # 全體基準列（該窗）
        vals = [float(v) for v in sub[tgt].to_list()]
        ci_lo, ci_hi = bootstrap_mean_ci(vals, n_boot=n_boot)
        med = sub[tgt].median()
        h1 = sub.filter(pl.col("date") <= mid_date)[tgt]
        h2 = sub.filter(pl.col("date") > mid_date)[tgt]
        rows.append(
            {
                "horizon": h, "group_strength": "（全體）", "stock_pos": "—", "tier": "—",
                "n": len(vals), "mean": sum(vals) / len(vals) if vals else None,
                "ci_lo": ci_lo, "ci_hi": ci_hi,
                "median": float(med) if isinstance(med, (int, float)) else None,
                "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                "mean_h1": _smean(h1),
                "mean_h2": _smean(h2),
            }
        )
    return pl.DataFrame(rows, schema=schema).sort(
        ["horizon", "group_strength", "stock_pos", "tier"]
    )


def laggard_cell_grid_by_regime(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    tier_edges: tuple[float, float] = (10.0, 15.0),
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
) -> pl.DataFrame:
    """cell×regime forward 報酬格（WS-H.4a）：12 cell × 3 regime **全列舉**，含空
    cell（n=0、統計欄 null）——樣本薄是預期，照列不藏格。

    CI＝per-date mean 序列的 moving-block bootstrap（block=horizon+1；快照雖週頻，
    塊長沿 WS-H.4a 規格以日頻 horizon 記）；mean 欄＝該序列等權平均（與 CI 同一
    統計量，非 pooled mean）。thin＝n_dates < REGIME_MIN_N（30），呼叫端據此把該
    cell 排除於跨 regime 裁決分母（factor_lab.regime_alignment_verdict）。

    regime_col 缺席或全 null（WS-H 標籤未產）→ 空表，呼叫端誠實跳過整段。
    """
    schema = {
        "horizon": pl.Int64, "regime": pl.Utf8, "group_strength": pl.Utf8,
        "stock_pos": pl.Utf8, "tier": pl.Utf8, "n": pl.Int64, "n_dates": pl.Int64,
        "mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64, "thin": pl.Boolean,
    }
    need = {"date", "rs_subind", "trend_score", "ma60_dist_pct"}
    if (
        stock_rows.is_empty()
        or not need.issubset(stock_rows.columns)
        or regime_col not in stock_rows.columns
        or stock_rows[regime_col].drop_nulls().is_empty()
    ):
        return pl.DataFrame(schema=schema)
    base = _assign_cells(stock_rows, tier_edges)
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    tiers = _tier_labels(tier_edges)
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        for reg in REGIME_LABELS:
            for gs in ("族群強", "族群弱"):
                for sp in ("落後", "領先"):
                    for tier in tiers:
                        g = sub.filter(
                            (pl.col(regime_col) == reg)
                            & (pl.col("group_strength") == gs)
                            & (pl.col("stock_pos") == sp)
                            & (pl.col("tier") == tier)
                        )
                        daily = (
                            g.group_by("date").agg(pl.col(tgt).mean().alias("_m")).sort("date")
                        )
                        vals = [float(v) for v in daily["_m"].to_list() if v is not None]
                        lo_ci, hi_ci = (
                            moving_block_bootstrap_ci(
                                vals, block_len=h + 1, n_boot=n_boot, seed=seed
                            )
                            if vals
                            else (None, None)
                        )
                        rows.append(
                            {
                                "horizon": h, "regime": reg, "group_strength": gs,
                                "stock_pos": sp, "tier": tier, "n": g.height,
                                "n_dates": len(vals),
                                "mean": (sum(vals) / len(vals)) if vals else None,
                                "ci_lo": lo_ci, "ci_hi": hi_ci,
                                "thin": len(vals) < REGIME_MIN_N,
                            }
                        )
    return pl.DataFrame(rows, schema=schema)
