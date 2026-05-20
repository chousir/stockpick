"""族群分析：把篩選結果按 TWSE 產業別分組，計算族群強度分數。

強度公式（2026-W21 起改為動能主導）：
  score = 50 * sigmoid(momentum_5d / 5)   # 動能主導（momentum_5d = 族群中位數，抗單檔灌水）
        + 25 * entry_rate                 # 入選率（仍保留訊號）
        + 15 * inst_score                 # 法人買超家數比（inst_net > 0 家數 / 成員數）
        + 10 * (log1p(members) / log1p(max))   # 規模

另回傳 up_count（族群內 5 日漲幅 > 0 的家數），供報告計算上漲家數比（breadth），
一眼看穿「整族群轉強」是真廣度還是單檔小型股獨拉。
"""

from __future__ import annotations

import math

import polars as pl
from loguru import logger

from tw_screener.analysis.momentum import compute_n_day_return

_DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.50,
    "entry_rate": 0.25,
    "institutional": 0.15,
    "size": 0.10,
}

_MOMENTUM_DAYS = 5


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
    benchmark: pl.DataFrame,  # noqa: ARG001 — kept for backward compatibility
    n: int = _MOMENTUM_DAYS,
) -> dict[str, float]:
    """回傳 {stock_id: n_day_return_pct}（純絕對動能，不再減大盤）。

    用 momentum.compute_n_day_return；若該股可用天數不足 n，仍會回傳實際可算的報酬。
    無資料的股票不在回傳 dict 中。
    """
    momentum_map = compute_n_day_return(stock_ids, price_history, n=n)
    return {sid: ret for sid, (ret, _days) in momentum_map.items()}


def _sigmoid(x: float) -> float:
    """logistic sigmoid; saturates to [0, 1]."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def group_stocks(
    screener_results: dict[str, pl.DataFrame],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,  # noqa: ARG001 — kept for backward compatibility
    industry_df: pl.DataFrame | None = None,
    weights: dict[str, float] | None = None,
    min_group_size: int = 2,
    institutional: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Group screened stocks by industry and compute group strength scores (動能主導).

    institutional: 近 N 日三大法人（含 stock_id / total_net）；提供時計入族群法人強度。
        個股層聚合為 inst_net（近 N 日合計淨買超股數），族群層 inst_score =
        法人買超家數比（inst_net > 0 的家數 / 成員數）。未提供 → inst_net = 0、inst_score = 0。

    Returns (groups_df, enriched_stocks_df):
    - groups_df: industry-level stats sorted by score desc (filtered by min_group_size)
        欄位：industry_code / industry_name / members_count / total_in_industry /
              entry_rate / momentum_5d（族群中位數）/ up_count / momentum_5d_days_used /
              rs_avg(alias) / inst_buy_count / inst_score / count_{sid} / score
    - enriched_stocks_df: per-stock data with industry, rs (= n_day_return), strategy flags,
        momentum_5d, momentum_days_used, inst_net
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
            "momentum_5d": pl.Float64,
            "up_count": pl.Int32,
            "momentum_5d_days_used": pl.Int32,
            "rs_avg": pl.Float64,
            "inst_buy_count": pl.Int32,
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

    # 5-day momentum: prefer price_history; fallback to change_pct
    stock_ids = stock_df["stock_id"].to_list()
    momentum_map = compute_n_day_return(stock_ids, price_history, n=_MOMENTUM_DAYS)

    rs_values: list[float] = []
    days_values: list[int] = []
    for sid, row in zip(stock_ids, stock_df.iter_rows(named=True)):
        entry = momentum_map.get(sid)
        if entry is not None:
            rs_values.append(entry[0])
            days_values.append(entry[1])
        else:
            rs_values.append(float(row.get("change_pct") or 0))
            days_values.append(1)
    stock_df = stock_df.with_columns(
        [
            pl.Series("rs", rs_values, dtype=pl.Float64),  # 5-day return (alias kept)
            pl.Series("momentum_5d", rs_values, dtype=pl.Float64),
            pl.Series("momentum_days_used", days_values, dtype=pl.Int32),
        ]
    )

    # 法人淨買超：近 N 日 total_net 合計 → 個股層 inst_net（只算一次，下游共用）
    if (
        institutional is not None
        and not institutional.is_empty()
        and {"stock_id", "total_net"}.issubset(institutional.columns)
    ):
        inst_agg = institutional.group_by("stock_id").agg(
            pl.col("total_net").sum().alias("inst_net")
        )
        stock_df = stock_df.join(inst_agg, on="stock_id", how="left").with_columns(
            pl.col("inst_net").fill_null(0.0).cast(pl.Float64)
        )
    else:
        stock_df = stock_df.with_columns(pl.lit(0.0).alias("inst_net"))

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
        pl.col("momentum_5d").median().alias("momentum_5d"),  # 中位數：抗單檔小型股灌水
        (pl.col("momentum_5d") > 0).cast(pl.Int32).sum().alias("up_count"),  # 上漲家數
        (pl.col("inst_net") > 0).cast(pl.Int32).sum().alias("inst_buy_count"),  # 法人買超家數
        pl.col("momentum_days_used").min().alias("momentum_5d_days_used"),
    ]
    for sid in strategy_ids:
        agg_exprs.append(
            pl.col(f"in_{sid}").cast(pl.Int32).sum().alias(f"count_{sid}")
        )

    groups = stock_df.group_by(["industry_code", "industry_name"]).agg(agg_exprs)

    # alias for backward compatibility (rs_avg now = 5-day median return)
    groups = groups.with_columns(pl.col("momentum_5d").alias("rs_avg"))

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

    # inst_score = 法人買超家數比（族群內 inst_net > 0 家數 / 成員數），落在 [0,1]
    groups = groups.with_columns(
        (
            pl.col("inst_buy_count").cast(pl.Float64)
            / pl.col("members_count").cast(pl.Float64)
        ).alias("inst_score")
    )

    # Composite score (動能主導)
    max_members = float(groups["members_count"].max() or 1)
    log_max = math.log1p(max_members)

    w_mom = weights.get("momentum", _DEFAULT_WEIGHTS["momentum"])
    w_er = weights.get("entry_rate", _DEFAULT_WEIGHTS["entry_rate"])
    w_inst = weights.get("institutional", _DEFAULT_WEIGHTS["institutional"])
    w_sz = weights.get("size", _DEFAULT_WEIGHTS["size"])

    # sigmoid: 5 日漲 5% → 0.5；漲 10% → 0.73；漲 20% → 0.88（避免大漲撞天花板）
    momentum_score_vals = [_sigmoid(v / 5.0) for v in groups["momentum_5d"].to_list()]
    groups = groups.with_columns(
        pl.Series("_momentum_score", momentum_score_vals, dtype=pl.Float64)
    )

    groups = groups.with_columns(
        (
            (pl.col("members_count").cast(pl.Float64) + 1.0).log(base=math.e) / log_max
        ).alias("_size_score")
    )

    groups = groups.with_columns(
        (
            w_mom * 100.0 * pl.col("_momentum_score")
            + w_er * 100.0 * pl.col("entry_rate")
            + w_inst * 100.0 * pl.col("inst_score")
            + w_sz * 100.0 * pl.col("_size_score")
        ).alias("score")
    ).drop(["_momentum_score", "_size_score"])

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
