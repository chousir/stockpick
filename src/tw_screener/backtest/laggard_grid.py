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

from tw_screener.backtest.factor_lab import bootstrap_mean_ci


def _smean(s: pl.Series) -> float | None:
    v = s.mean()
    return float(v) if isinstance(v, (int, float)) else None


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
    lo, hi = tier_edges
    base = (
        stock_rows.drop_nulls(["rs_subind", "trend_score", "ma60_dist_pct"])
        .with_columns(
            pl.when(pl.col("trend_score") >= pl.col("trend_score").median().over("date"))
            .then(pl.lit("族群強")).otherwise(pl.lit("族群弱")).alias("group_strength"),
            pl.when(pl.col("rs_subind") < 0.0)
            .then(pl.lit("落後")).otherwise(pl.lit("領先")).alias("stock_pos"),
            pl.when(pl.col("ma60_dist_pct") <= lo).then(pl.lit(f"貼低≤{lo:.0f}"))
            .when(pl.col("ma60_dist_pct") <= hi).then(pl.lit(f"中段{lo:.0f}–{hi:.0f}"))
            .otherwise(pl.lit(f"延伸>{hi:.0f}")).alias("tier"),
        )
    )
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
