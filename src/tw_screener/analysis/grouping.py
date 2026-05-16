"""族群分析：把篩選結果按 TWSE 產業別分組，計算族群強度分數。"""

from __future__ import annotations

import polars as pl
from loguru import logger

_DEFAULT_WEIGHTS: dict[str, float] = {"entry_rate": 0.4, "rs": 0.4, "institutional": 0.2}


def is_etf_or_warrant(stock_id: str) -> bool:
    """Return True if stock_id is an ETF, structured product, or warrant (should skip analysis).

    Filters:
    - starts with "00": ETF codes (0050, 006208, 00400A, 00631L ...)
    - contains non-digit: warrant/bond codes (2330Y, 6592A ...)
    """
    if stock_id.startswith("00"):
        return True
    if not stock_id.isdigit():
        return True
    return False


def _compute_rs_from_history(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,
) -> dict[str, float]:
    """Try to compute 7-day RS from price history. Returns empty dict if not enough data."""
    if price_history.is_empty() or "close" not in price_history.columns:
        return {}

    bench_7d = 0.0
    if not benchmark.is_empty() and "close" in benchmark.columns:
        bench_sorted = benchmark.sort("date")
        if len(bench_sorted) >= 7:
            c_now = bench_sorted["close"][-1]
            c_7d = bench_sorted["close"][-7]
            if c_now is not None and c_7d is not None and c_7d != 0:
                bench_7d = (c_now - c_7d) / c_7d * 100

    rs_map: dict[str, float] = {}
    ph_sorted = price_history.sort("date")
    for stock_id in stock_ids:
        stock_price = ph_sorted.filter(pl.col("stock_id") == stock_id)
        if len(stock_price) >= 7:
            c_now = stock_price["close"][-1]
            c_7d = stock_price["close"][-7]
            if c_now is not None and c_7d is not None and c_7d != 0:
                rs_map[stock_id] = (c_now - c_7d) / c_7d * 100 - bench_7d

    return rs_map


def group_stocks(
    screener_results: dict[str, pl.DataFrame],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,
    industry_df: pl.DataFrame | None = None,
    weights: dict[str, float] | None = None,
    min_group_size: int = 2,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Group screened stocks by industry and compute group strength scores.

    Returns (groups_df, enriched_stocks_df):
    - groups_df: industry-level stats sorted by score desc (filtered by min_group_size)
    - enriched_stocks_df: per-stock data with industry, RS, strategy flags
    """
    if weights is None:
        weights = _DEFAULT_WEIGHTS

    strategy_ids = sorted(screener_results.keys())

    # Build per-stock dict from all strategies (skip ETFs and warrants)
    stock_rows: dict[str, dict] = {}
    skipped_etf = 0
    for sid in strategy_ids:
        df = screener_results.get(sid, pl.DataFrame())
        if df.is_empty():
            continue
        for row in df.iter_rows(named=True):
            stock_id = str(row["stock_id"])
            if is_etf_or_warrant(stock_id):
                skipped_etf += 1
                continue
            if stock_id not in stock_rows:
                stock_rows[stock_id] = {
                    "stock_id": stock_id,
                    "name": str(row.get("name") or ""),
                    "close": float(row.get("close") or 0),
                    "change_pct": float(row.get("change_pct") or 0),
                    "amount_million": float(row.get("amount_million") or 0),
                    "goodinfo_url": str(row.get("goodinfo_url") or ""),
                    **{f"in_{s}": False for s in strategy_ids},
                }
            stock_rows[stock_id][f"in_{sid}"] = True

    if skipped_etf:
        logger.info("group_stocks: 排除 {} 檔 ETF/權證/結構型商品", skipped_etf)

    if not stock_rows:
        logger.warning("group_stocks: 三組 CSV 均為空（或全為 ETF），無法進行族群分析")
        _empty_g: dict[str, type[pl.DataType]] = {
            "industry_code": pl.Utf8,
            "industry_name": pl.Utf8,
            "members_count": pl.Int32,
            "total_in_industry": pl.Int32,
            "entry_rate": pl.Float64,
            "rs_avg": pl.Float64,
            "inst_score": pl.Float64,
            "score": pl.Float64,
        }
        return pl.DataFrame(schema=_empty_g), pl.DataFrame()

    stock_df = pl.DataFrame(list(stock_rows.values()))

    # strategy_count = sum of all in_{sid} flags
    strategy_count_expr: pl.Expr = pl.lit(0, dtype=pl.Int32)
    for sid in strategy_ids:
        strategy_count_expr = strategy_count_expr + pl.col(f"in_{sid}").cast(pl.Int32)
    stock_df = stock_df.with_columns(strategy_count_expr.alias("strategy_count"))

    # RS: prefer 7-day history, fall back to change_pct
    stock_ids = stock_df["stock_id"].to_list()
    rs_from_history = _compute_rs_from_history(stock_ids, price_history, benchmark)
    rs_values = [
        rs_from_history.get(sid, float(row.get("change_pct") or 0))
        for sid, row in zip(stock_ids, stock_df.iter_rows(named=True))
    ]
    stock_df = stock_df.with_columns(pl.Series("rs", rs_values, dtype=pl.Float64))

    # Join with industry classification
    if industry_df is not None and not industry_df.is_empty():
        stock_df = stock_df.join(
            industry_df.select(["stock_id", "industry_code", "industry_name"]),
            on="stock_id",
            how="left",
        )
    else:
        stock_df = stock_df.with_columns(
            [pl.lit("00").alias("industry_code"), pl.lit("未分類").alias("industry_name")]
        )

    stock_df = stock_df.with_columns(
        [
            pl.col("industry_code").fill_null("00"),
            pl.col("industry_name").fill_null("未分類"),
        ]
    )

    # Group-level aggregation
    agg_exprs: list[pl.Expr] = [
        pl.col("stock_id").count().alias("members_count"),
        pl.col("rs").mean().alias("rs_avg"),
    ]
    for sid in strategy_ids:
        agg_exprs.append(
            pl.col(f"in_{sid}").cast(pl.Int32).sum().alias(f"count_{sid}")
        )

    groups = stock_df.group_by(["industry_code", "industry_name"]).agg(agg_exprs)

    # total_in_industry and entry_rate
    if industry_df is not None and not industry_df.is_empty():
        industry_total = industry_df.group_by("industry_code").agg(
            pl.col("stock_id").count().alias("total_in_industry")
        )
        groups = groups.join(industry_total, on="industry_code", how="left")
        groups = groups.with_columns(
            pl.col("total_in_industry").fill_null(pl.col("members_count"))
        )
    else:
        groups = groups.with_columns(pl.col("members_count").alias("total_in_industry"))

    groups = groups.with_columns(
        (
            pl.col("members_count").cast(pl.Float64)
            / pl.col("total_in_industry").cast(pl.Float64)
        ).alias("entry_rate")
    )

    # inst_score: placeholder (would use institutional net in a future enhancement)
    groups = groups.with_columns(pl.lit(0.0).alias("inst_score"))

    # Composite score (normalise RS to 0-1 within this set of groups)
    rs_series = groups["rs_avg"]
    rs_max = rs_series.max() or 0.0
    rs_min = rs_series.min() or 0.0
    rs_range = float(rs_max - rs_min) if rs_max != rs_min else 1.0

    w_er = weights.get("entry_rate", 0.4)
    w_rs = weights.get("rs", 0.4)
    w_inst = weights.get("institutional", 0.2)

    groups = groups.with_columns(
        (
            w_er * pl.col("entry_rate") * 10
            + w_rs * ((pl.col("rs_avg") - rs_min) / rs_range) * 10
            + w_inst * pl.col("inst_score") * 10
        ).alias("score")
    )

    # Filter and sort
    groups = groups.filter(pl.col("members_count") >= min_group_size).sort(
        "score", descending=True
    )

    logger.info(
        "group_stocks: {} 族群（入選率 ≥ {} 的共 {} 族群）",
        len(groups),
        min_group_size,
        len(groups),
    )
    return groups, stock_df
