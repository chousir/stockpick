"""族群內個股排名：依 5 日漲幅 + 成交金額 + 法人 + 策略數綜合打分。

排名公式（2026-W21 起改為動能主導）：
  rank_score = 0.50 * momentum_5d_norm
             + 0.25 * amount_norm
             + 0.15 * inst_norm
             + 0.10 * strategy_count_norm

每個族群取分數最高者 rank_in_group = 1，其次 = 2，依此類推。
"""

from __future__ import annotations

import polars as pl
from loguru import logger


def rank_within_groups(
    group_members: pl.DataFrame,
    price_history: pl.DataFrame,  # noqa: ARG001 — kept for backward compatibility
    institutional: pl.DataFrame,
) -> pl.DataFrame:
    """
    對每個 industry_code 內的個股排名，回傳附 rank_in_group 與相關欄位的 DataFrame。

    leader_score 公式（動能主導）：
        0.50 * momentum_5d_norm + 0.25 * amount_norm
      + 0.15 * inst_norm        + 0.10 * strategy_count_norm

    回傳欄位（在 group_members 既有欄位上補充）：
      inst_net, momentum_rank, amount_rank, inst_rank, strategy_rank,
      group_size, momentum_rank_norm, amount_rank_norm, inst_rank_norm, strategy_rank_norm,
      leader_score, rank_in_group
    """
    if group_members.is_empty():
        logger.warning("rank_within_groups: group_members 為空")
        return pl.DataFrame(
            schema={
                "stock_id": pl.Utf8,
                "industry_code": pl.Utf8,
                "leader_score": pl.Float64,
                "rank_in_group": pl.Int32,
            }
        )

    df = group_members.clone()

    # Add institutional net buy (sum across cached dates)
    if (
        not institutional.is_empty()
        and "stock_id" in institutional.columns
        and "total_net" in institutional.columns
    ):
        inst_agg = institutional.group_by("stock_id").agg(
            pl.col("total_net").sum().alias("inst_net")
        )
        df = df.join(inst_agg, on="stock_id", how="left")
        df = df.with_columns(pl.col("inst_net").fill_null(0.0).cast(pl.Float64))
    else:
        df = df.with_columns(pl.lit(0.0).alias("inst_net"))

    # 排名 key：優先 momentum_5d；缺欄位則 fallback 到 rs
    momentum_col = "momentum_5d" if "momentum_5d" in df.columns else "rs"

    # Rank within industry_code group (descending: rank 1 = best)
    df = df.with_columns(
        [
            pl.col(momentum_col)
            .rank(method="average", descending=True)
            .over("industry_code")
            .alias("momentum_rank"),
            pl.col("amount_million")
            .rank(method="average", descending=True)
            .over("industry_code")
            .alias("amount_rank"),
            pl.col("inst_net")
            .rank(method="average", descending=True)
            .over("industry_code")
            .alias("inst_rank"),
            pl.col("strategy_count")
            .rank(method="average", descending=True)
            .over("industry_code")
            .alias("strategy_rank"),
            pl.len().over("industry_code").alias("group_size"),
        ]
    )

    # Normalise ranks to 0-1: rank 1 → 1.0, rank N → 0.0
    denom = (pl.col("group_size") - 1).cast(pl.Float64).clip(lower_bound=1.0)
    df = df.with_columns(
        [
            (1.0 - (pl.col("momentum_rank") - 1.0) / denom).alias("momentum_rank_norm"),
            (1.0 - (pl.col("amount_rank") - 1.0) / denom).alias("amount_rank_norm"),
            (1.0 - (pl.col("inst_rank") - 1.0) / denom).alias("inst_rank_norm"),
            (1.0 - (pl.col("strategy_rank") - 1.0) / denom).alias("strategy_rank_norm"),
        ]
    )

    df = df.with_columns(
        (
            0.50 * pl.col("momentum_rank_norm")
            + 0.25 * pl.col("amount_rank_norm")
            + 0.15 * pl.col("inst_rank_norm")
            + 0.10 * pl.col("strategy_rank_norm")
        ).alias("leader_score")
    )

    # rank_in_group: 1 = highest leader_score within industry; ordinal so ties get distinct ranks
    df = df.with_columns(
        pl.col("leader_score")
        .rank(method="ordinal", descending=True)
        .over("industry_code")
        .cast(pl.Int32)
        .alias("rank_in_group")
    )

    logger.info(
        "rank_within_groups: {} 族群，{} 檔個股",
        df["industry_code"].n_unique(),
        len(df),
    )
    return df


# Backward-compatible alias — keep callers working while we migrate
find_leaders = rank_within_groups
