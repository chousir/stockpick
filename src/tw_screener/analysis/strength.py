"""次產業／全市場強度計算共用純函式（docs/proposals/04 A5）。

雙宇宙（精選候選宇宙 × 全市場無偏宇宙）設計保留，只把兩邊**真正重複**的
計算抽成共用純函式。首位住戶：等權指數建構前置的個股日報酬——
rotation 的全市場等權指數（compute_market_index）與次產業籃子
（compute_subindustry_baskets）皆「先算個股日報酬、再分組累乘」，前置完全相同。
"""

from __future__ import annotations

import polars as pl


def clipped_daily_returns(
    price_history: pl.DataFrame,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """個股日報酬（close 對前一交易日），漲跌停外夾限 ±clip%。

    Args:
        price_history: (date, stock_id, close, ...) 全市場日線
        clip_daily_return_pct: 日報酬夾限（%）。台股漲跌停 ±10%，逾此＝
            減資/分割/除權息/停牌補跳等未還原事件，夾住避免毒化下游累乘指數。設 0 停用。

    Returns:
        (date, stock_id, close, _ret) 依 (stock_id, date) 排序、首日 null 報酬剔除。
        供 compute_market_index（全市場等權）與 compute_subindustry_baskets（次產業籃子）共用。

    close ≤ 0 的列（停牌/髒列，daily_all 自 2026-06 起混入 0 收盤的權證型代號）先剔除：
    連續兩個 0 收盤會產生 0/0=NaN，經 cum_prod 毒化下游等權指數與籃子（regime「廣度 nan」
    ＋輪動位階 nan 的共同根因）。尾端再守 is_finite 擋任何殘餘 NaN/inf。
    """
    ret = pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1.0
    if clip_daily_return_pct > 0:
        bound = clip_daily_return_pct / 100
        ret = ret.clip(-bound, bound)
    return (
        price_history.select(["date", "stock_id", "close"])
        .filter(pl.col("close") > 0)
        .sort(["stock_id", "date"])
        .with_columns(ret.alias("_ret"))
        .drop_nulls("_ret")
        .filter(pl.col("_ret").is_finite())
    )
