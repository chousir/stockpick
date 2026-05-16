"""url_builder.py — 將策略 YAML 轉成 Goodinfo 自訂篩選 URL。"""

from pathlib import Path
from urllib.parse import urlencode

import yaml
from pydantic import BaseModel


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


def build_screener_url(strategy: StrategyConfig, base_url: str) -> str:
    """將策略設定轉成 Goodinfo 自訂篩選 URL。

    filters 對應 FL_ITEM{i} / FL_VAL_S{i} / FL_VAL_E{i}（0-indexed）。
    rules 對應 FL_RULE{i}（0-indexed）。
    period 若存在，附加在 item name 後以「–」連接（Goodinfo 慣例）。
    """
    params: list[tuple[str, str]] = [
        ("MARKET_CAT", "自訂篩選"),
        ("INDUSTRY_CAT", "我的條件"),
    ]

    for i, f in enumerate(strategy.filters):
        item_name = f"{f.item}–{f.period}" if f.period else f.item
        params.append((f"FL_ITEM{i}", item_name))
        params.append((f"FL_VAL_S{i}", str(int(f.min)) if f.min is not None else ""))
        params.append((f"FL_VAL_E{i}", str(int(f.max)) if f.max is not None else ""))

    for i, rule in enumerate(strategy.rules):
        params.append((f"FL_RULE{i}", rule))

    params.extend([
        ("FL_SHEET", strategy.display_sheet),
        ("FL_SHEET2", strategy.display_period),
        ("FL_MARKET", strategy.market),
        ("FL_QRY", "查 詢"),
    ])

    query = urlencode(params, encoding="utf-8")
    return f"{base_url}/StockList.asp?{query}"
