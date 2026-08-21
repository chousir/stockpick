"""Goodinfo `FL_ITEM` 標籤 → 本地欄名對照表（單一真相來源：門檻只在策略 YAML 改一次，
Goodinfo 查詢與本地篩選兩條路徑都吃同一個數字，不會漂移）。

只收錄目前確認可由官方 API 覆蓋的條件（見 docs/02、playbook/90 市值/累計YoY 落地記錄）。
任何策略若含表外條件（如近四季ROE、連續配息次數——官方 API 無歷史查詢，物理上無法覆蓋），
`apply_local_filters` 會直接 raise，不會悄悄漏篩。
"""

from __future__ import annotations

GOODINFO_ITEM_TO_LOCAL_COL: dict[str, str] = {
    "市值 (億元)": "market_cap_billion",
    "本益比 (PER)": "pe_ratio",
    "成交價現金殖利率 (%)": "dividend_yield_pct",
    "累計月營收年增減率(%)": "cum_rev_yoy_pct",
}


def unmapped_items(items: list[str]) -> list[str]:
    """回傳 items 中沒有本地欄位對應的項目（空 list＝策略可完全本地化）。"""
    return [item for item in items if item not in GOODINFO_ITEM_TO_LOCAL_COL]
