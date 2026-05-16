"""領頭羊判斷：在每個族群內對個股打分、標出領頭羊候選。"""

from __future__ import annotations

import polars as pl
from loguru import logger


def find_leaders(
    group_members: pl.DataFrame,
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
) -> pl.DataFrame:
    """
    For each industry group in group_members, rank stocks and mark the leader.

    leader_score = 0.35 * rs_rank + 0.30 * amount_rank + 0.20 * inst_rank + 0.15 * strategy_rank

    Returns group_members augmented with:
      inst_net, rs_rank, amount_rank, inst_rank, strategy_rank,
      group_size, rs_rank_norm, amount_rank_norm, inst_rank_norm, strategy_rank_norm,
      leader_score, is_leader
    """
    if group_members.is_empty():
        logger.warning("find_leaders: group_members 為空")
        return pl.DataFrame(
            schema={
                "stock_id": pl.Utf8,
                "industry_code": pl.Utf8,
                "leader_score": pl.Float64,
                "is_leader": pl.Boolean,
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

    # Rank within industry_code group (descending: rank 1 = best)
    df = df.with_columns(
        [
            pl.col("rs")
            .rank(method="average", descending=True)
            .over("industry_code")
            .alias("rs_rank"),
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
            (1.0 - (pl.col("rs_rank") - 1.0) / denom).alias("rs_rank_norm"),
            (1.0 - (pl.col("amount_rank") - 1.0) / denom).alias("amount_rank_norm"),
            (1.0 - (pl.col("inst_rank") - 1.0) / denom).alias("inst_rank_norm"),
            (1.0 - (pl.col("strategy_rank") - 1.0) / denom).alias("strategy_rank_norm"),
        ]
    )

    df = df.with_columns(
        (
            0.35 * pl.col("rs_rank_norm")
            + 0.30 * pl.col("amount_rank_norm")
            + 0.20 * pl.col("inst_rank_norm")
            + 0.15 * pl.col("strategy_rank_norm")
        ).alias("leader_score")
    )

    # Mark the stock with the highest leader_score in each group as the leader
    df = df.with_columns(
        (
            pl.col("leader_score") == pl.col("leader_score").max().over("industry_code")
        ).alias("is_leader")
    )

    logger.info(
        "find_leaders: {} 族群，共 {} 個領頭羊候選",
        df["industry_code"].n_unique(),
        df["is_leader"].sum(),
    )
    return df
