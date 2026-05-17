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

    result: dict[str, tuple[float, int]] = {}
    ph_sorted = price_history.sort("date")

    for stock_id in stock_ids:
        stock_df = ph_sorted.filter(pl.col("stock_id") == stock_id)
        if len(stock_df) < 2:
            continue
        available_gap = len(stock_df) - 1
        gap = min(n, available_gap)
        c_now = stock_df["close"][-1]
        c_back = stock_df["close"][-(gap + 1)]
        if c_now is None or c_back is None or c_back == 0:
            continue
        ret_pct = (c_now - c_back) / c_back * 100
        result[stock_id] = (float(ret_pct), int(gap))

    return result


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
