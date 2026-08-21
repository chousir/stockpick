"""本地篩選：對 build_local_universe() 產出的寬表套用策略門檻（純 Polars 函式，全離線可測）。
"""

from __future__ import annotations

import polars as pl
from loguru import logger

from tw_screener.screener.goodinfo.url_builder import StrategyConfig
from tw_screener.screener.local.field_map import GOODINFO_ITEM_TO_LOCAL_COL, unmapped_items


class UnsupportedLocalFilterError(Exception):
    """策略含官方資料無法覆蓋的篩選條件（見 field_map），不得本地化執行——寧可拒跑，不悄悄漏篩。"""


def apply_local_filters(universe: pl.DataFrame, strategy: StrategyConfig) -> pl.DataFrame:
    """逐條套用 strategy.filters 的門檻，回傳通過全部條件的子集。

    任一 filter.item 對不到 field_map 就直接 raise（拒跑該策略，不當「跳過」處理）。
    null 值一律視為不通過（不補值、不猜——同本專案「沒抓到不要編」的一貫原則）。
    每條門檻先印非空值筆數，方便分辨「篩出 N 檔」是真的篩出來、還是因為大半候選這欄是
    null 才剩 N 檔。
    """
    bad = unmapped_items([f.item for f in strategy.filters])
    if bad:
        raise UnsupportedLocalFilterError(
            f"策略 {strategy.id} 含官方資料無法覆蓋的條件 {bad}，不得本地化執行"
        )

    out = universe
    for f in strategy.filters:
        col = GOODINFO_ITEM_TO_LOCAL_COL[f.item]
        non_null = out.filter(pl.col(col).is_not_null()).height
        logger.info(
            "本地篩選 {}：條件 {}（{}）非空值 {}/{} 筆",
            strategy.id, f.item, col, non_null, out.height,
        )
        cond = pl.col(col).is_not_null()
        if f.min is not None:
            cond = cond & (pl.col(col) >= f.min)
        if f.max is not None:
            cond = cond & (pl.col(col) <= f.max)
        out = out.filter(cond)
    return out
