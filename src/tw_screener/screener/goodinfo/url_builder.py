"""url_builder.py — 將策略 YAML 轉成 Goodinfo 自訂篩選 URL。"""

from pathlib import Path
from urllib.parse import urlencode

import yaml
from pydantic import BaseModel

# CLAUDE.md §3.3 固定模板；全專案個股 Goodinfo 連結的單一來源
_STOCK_DETAIL_TMPL = "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={}"


def stock_detail_url(stock_id: str) -> str:
    """個股 Goodinfo 頁連結。"""
    return _STOCK_DETAIL_TMPL.format(stock_id)


class FilterCondition(BaseModel):
    item: str
    period: str | None = None
    min: float | None = None
    max: float | None = None


class PostFilterSort(BaseModel):
    field: str
    order: str


class StrategyConfig(BaseModel):
    id: str
    name: str
    description: str
    market: str
    filters: list[FilterCondition]
    rules: list[str]
    display_sheet: str
    display_period: str
    holding_period: str | None = None
    post_filter_sort: list[PostFilterSort] | None = None


def load_strategy(path: Path) -> StrategyConfig:
    """讀取並驗證策略 YAML 檔。"""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return StrategyConfig.model_validate(data)


def build_data_url(strategy: StrategyConfig, base_url: str) -> str:
    """建立 Goodinfo AJAX 資料端點 URL（STEP=DATA）。

    此 URL 直接回傳 HTML fragment 含股票結果表，可傳給 parser.parse_screener_result()。
    與 build_screener_url() 的差異：加了 STEP=DATA、SHEET、RPT_TIME、RANK_RANGE。
    驗證日期：2026-05-16
    """
    params: list[tuple[str, str]] = [
        ("STEP", "DATA"),
        ("MARKET_CAT", "自訂篩選"),
        ("INDUSTRY_CAT", "我的條件"),
        ("SHEET", strategy.display_sheet),
        ("RPT_TIME", "最新資料"),
        ("RANK_RANGE", "300"),
        ("FL_SHEET", strategy.display_sheet),
        ("FL_SHEET2", strategy.display_period),
        ("FL_MARKET", strategy.market),
    ]

    for i, f in enumerate(strategy.filters):
        params.append((f"FL_ITEM{i}", f.item))
        params.append((f"FL_VAL_S{i}", str(int(f.min)) if f.min is not None else ""))
        params.append((f"FL_VAL_E{i}", str(int(f.max)) if f.max is not None else ""))

    for i, rule in enumerate(strategy.rules):
        params.append((f"FL_RULE{i}", rule))

    query = urlencode(params, encoding="utf-8")
    return f"{base_url}/StockList.asp?{query}"


def build_screener_url(strategy: StrategyConfig, base_url: str) -> str:
    """將策略設定轉成 Goodinfo 自訂篩選 URL。

    filters 對應 FL_ITEM{i} / FL_VAL_S{i} / FL_VAL_E{i}（0-indexed）。
    rules 對應 FL_RULE{i}（0-indexed）。
    period 欄位是說明用途，不附加到 item name（Goodinfo 的篩選項目名稱為完整字串）。
    """
    params: list[tuple[str, str]] = [
        ("MARKET_CAT", "自訂篩選"),
        ("INDUSTRY_CAT", "我的條件"),
    ]

    for i, f in enumerate(strategy.filters):
        params.append((f"FL_ITEM{i}", f.item))
        params.append((f"FL_VAL_S{i}", str(int(f.min)) if f.min is not None else ""))
        params.append((f"FL_VAL_E{i}", str(int(f.max)) if f.max is not None else ""))

    for i, rule in enumerate(strategy.rules):
        params.append((f"FL_RULE{i}", rule))

    params.extend(
        [
            ("FL_SHEET", strategy.display_sheet),
            ("FL_SHEET2", strategy.display_period),
            ("FL_MARKET", strategy.market),
            ("FL_QRY", "查 詢"),
        ]
    )

    query = urlencode(params, encoding="utf-8")
    return f"{base_url}/StockList.asp?{query}"
