"""動能計算：N 日累計報酬率（從本地 OHLCV 快取算出）。

集中所有 N 日報酬率邏輯，供 grouping.py、leader.py、report 共用。
資料來源優先序由呼叫端決定（已合併好的 price_history DataFrame）；
此模組只做計算、不負責讀檔。
"""

from __future__ import annotations

import polars as pl


def compute_n_day_return(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    n: int = 5,
) -> dict[str, tuple[float, int]]:
    """回傳每檔股票的 N 日累計報酬率（百分比）與實際用到的交易日 gap。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        n: 目標交易日 gap（預設 5 日）。

    Returns:
        {stock_id: (cumulative_return_pct, actual_days_used)}
        - actual_days_used = min(n, 該股可用交易日數 - 1)
        - 若該股可用交易日 < 2 或 close 缺失 → 不出現在 dict 中
    """
    if price_history.is_empty():
        return {}
    required = {"stock_id", "date", "close"}
    if not required.issubset(set(price_history.columns)):
        return {}

    # 向量化：每股一次標出列數(_n)與組內序(_i)，取末列(c_now)與起點列(c_back=index
    # max(0, _n-1-n))，再 join 算報酬。等價於 per-stock 迴圈、但只掃全表一次。
    tagged = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .with_columns(
            pl.len().over("stock_id").cast(pl.Int64).alias("_n"),
            pl.int_range(pl.len()).over("stock_id").alias("_i"),
        )
        .filter(pl.col("_n") >= 2)
    )
    if tagged.is_empty():
        return {}

    back_idx = pl.max_horizontal(pl.col("_n") - 1 - n, pl.lit(0))
    c_now = tagged.filter(pl.col("_i") == pl.col("_n") - 1).select(
        "stock_id", pl.col("close").alias("_c_now"), "_n"
    )
    c_back = tagged.filter(pl.col("_i") == back_idx).select(
        "stock_id", pl.col("close").alias("_c_back")
    )
    merged = (
        c_now.join(c_back, on="stock_id")
        .filter(
            pl.col("_c_now").is_not_null()
            & pl.col("_c_back").is_not_null()
            & (pl.col("_c_back") != 0)
        )
        .with_columns(
            pl.min_horizontal(pl.lit(n), pl.col("_n") - 1).alias("_gap"),
            ((pl.col("_c_now") - pl.col("_c_back")) / pl.col("_c_back") * 100).alias("_ret"),
        )
    )
    return {
        row["stock_id"]: (float(row["_ret"]), int(row["_gap"]))
        for row in merged.iter_rows(named=True)
    }


def compute_rolling_extrema(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    windows: tuple[int, ...] = (20, 60),
) -> dict[str, dict[int, tuple[float, float]]]:
    """回傳每檔股票各視窗「最近 window 個交易日」收盤的 (最低, 最高)。

    供進場階梯的 T3 結構價（前波低）與回檔深度檢核（距區間低/高）使用。
    用原始收盤、不做除息還原——進場/停損是實際成交價，與 close/MA20/MA60 絕對價同口徑
    （近端除息另由 ex_div_cash 旗標揭露）。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        windows: 視窗交易日數集合（預設近 20 與 60 日）。

    Returns:
        {stock_id: {window: (low, high)}}；無可用收盤的股不出現。
        各視窗取最近 window 個交易日的收盤 min/max（不足則用可得全部）。
    """
    if price_history.is_empty():
        return {}
    required = {"stock_id", "date", "close"}
    if not required.issubset(set(price_history.columns)):
        return {}

    # 向量化：丟掉 null 收盤後，每股各視窗取最近 w 列(tail)的 min/max，一次 group_by 算完。
    agg_exprs = []
    for w in windows:
        agg_exprs.append(pl.col("close").tail(w).min().alias(f"_min_{w}"))
        agg_exprs.append(pl.col("close").tail(w).max().alias(f"_max_{w}"))
    grouped = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .filter(pl.col("close").is_not_null())
        .group_by("stock_id", maintain_order=True)
        .agg(agg_exprs)
    )

    result: dict[str, dict[int, tuple[float, float]]] = {}
    for row in grouped.iter_rows(named=True):
        result[row["stock_id"]] = {
            w: (float(row[f"_min_{w}"]), float(row[f"_max_{w}"])) for w in windows
        }
    return result


def compute_dividend_addback(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    dividends: pl.DataFrame,
    n: int = 5,
) -> dict[str, tuple[float, float]]:
    """回傳每檔在 N 日動能視窗內的「現金股利還原加成」。

    除息日股價缺口會讓 compute_n_day_return 的 N 日報酬假負（6-8 月除息季尤甚），
    並把除息股壓到動能排名後段。此函式比照 compute_n_day_return 的視窗（同 gap），
    找出 ex_date 落在視窗內的現金股利，回傳該加回報酬的百分比，讓呼叫端把 momentum
    還原成總報酬（價＋息）。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        dividends: 必須含 stock_id / ex_date / cash_dividend 欄（cash 為元/股）。
        n: 動能視窗交易日數（與 compute_n_day_return 對齊，預設 5）。

    Returns:
        {stock_id: (addback_pct, total_cash)} —— 僅含視窗內有現金股利的檔。
        addback_pct = 視窗內現金股利合計 / 視窗起點收盤 × 100；total_cash = 合計股利（元）。
        僅還原現金股利（息）；配股（權）不在此估算。
    """
    if price_history.is_empty() or not {"stock_id", "date", "close"}.issubset(
        set(price_history.columns)
    ):
        return {}
    if dividends.is_empty() or not {"stock_id", "ex_date", "cash_dividend"}.issubset(
        set(dividends.columns)
    ):
        return {}

    # 向量化：先一次算出每股視窗起點(date_back/c_back)與最新日(latest)，再與現金股利
    # join、篩 ex_date 落在視窗內者合計。等價於 per-stock 迴圈、但只掃全表一次。
    tagged = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .with_columns(
            pl.len().over("stock_id").cast(pl.Int64).alias("_n"),
            pl.int_range(pl.len()).over("stock_id").alias("_i"),
        )
        .filter(pl.col("_n") >= 2)
    )
    if tagged.is_empty():
        return {}

    back_idx = pl.max_horizontal(pl.col("_n") - 1 - n, pl.lit(0))
    back = tagged.filter(pl.col("_i") == back_idx).select(
        "stock_id",
        pl.col("date").alias("_date_back"),  # 視窗起點（c_back 的日期）
        pl.col("close").alias("_c_back"),
    )
    latest = tagged.filter(pl.col("_i") == pl.col("_n") - 1).select(
        "stock_id", pl.col("date").alias("_latest")
    )
    windows = back.join(latest, on="stock_id").filter(
        pl.col("_c_back").is_not_null() & (pl.col("_c_back") != 0)
    )
    if windows.is_empty():
        return {}

    # 只留正現金股利（比照舊版 cash > 0；stock_id 轉字串對齊呼叫端代號）
    divs = dividends.with_columns(pl.col("stock_id").cast(pl.Utf8)).filter(
        pl.col("cash_dividend").is_not_null()
        & (pl.col("cash_dividend") > 0)
        & pl.col("ex_date").is_not_null()
    )
    if divs.is_empty():
        return {}

    # ex_date 嚴格晚於視窗起點收盤、且不晚於最新日 → 缺口落在視窗內，需加回
    joined = (
        windows.join(divs, on="stock_id")
        .filter(
            (pl.col("_date_back") < pl.col("ex_date"))
            & (pl.col("ex_date") <= pl.col("_latest"))
        )
        .group_by("stock_id")
        .agg(
            pl.col("cash_dividend").sum().alias("_total_cash"),
            pl.col("_c_back").first().alias("_c_back"),
        )
        .filter(pl.col("_total_cash") > 0)
    )
    return {
        row["stock_id"]: (
            float(row["_total_cash"] / row["_c_back"] * 100),
            float(row["_total_cash"]),
        )
        for row in joined.iter_rows(named=True)
    }


def aggregate_group_momentum(
    momentum_map: dict[str, tuple[float, int]],
    stock_ids_per_group: dict[str, list[str]],
) -> dict[str, tuple[float, int]]:
    """把個股動能聚合到族群層級。

    Args:
        momentum_map: compute_n_day_return 的回傳。
        stock_ids_per_group: {industry_code: [stock_id, ...]}

    Returns:
        {industry_code: (avg_return_pct, min_days_used)}
        - avg_return_pct: 族群內有資料個股的平均報酬
        - min_days_used: 族群內最差的可用天數（用於顯示「N 日資料」標註）
        - 若族群內無任何個股有資料 → (0.0, 0)
    """
    result: dict[str, tuple[float, int]] = {}
    for industry_code, stock_ids in stock_ids_per_group.items():
        returns: list[float] = []
        days: list[int] = []
        for sid in stock_ids:
            entry = momentum_map.get(sid)
            if entry is None:
                continue
            returns.append(entry[0])
            days.append(entry[1])
        if returns:
            avg = sum(returns) / len(returns)
            min_d = min(days)
            result[industry_code] = (avg, min_d)
        else:
            result[industry_code] = (0.0, 0)
    return result
