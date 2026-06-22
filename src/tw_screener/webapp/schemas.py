"""Pydantic 回應模型。

enriched 表跨週演進（docs/17 §2.3）：以最新週 W25 為超集、所有欄位 Optional，
舊週缺欄回 null。`extra="allow"` 容忍未列出的欄位（schema 再演進不會炸）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WeeksResponse(BaseModel):
    weeks: list[str]
    latest: str | None


class EnrichedRow(BaseModel):
    """candidates / holdings / watchlist 共用列（W25 超集，全 Optional）。"""

    model_config = ConfigDict(extra="allow")

    # 識別
    stock_id: str | None = None
    name: str | None = None
    industry: str | None = None
    theme: str | None = None
    strategy: str | None = None
    rank_in_group: int | None = None
    flags: str | None = None
    goodinfo_url: str | None = None
    # 動能/報酬
    momentum_5d_pct: float | None = None
    ret_10d_pct: float | None = None
    change_pct: float | None = None
    # 技術
    close: float | None = None
    vol_ratio: float | None = None
    ma20_dist_pct: float | None = None
    ma60_dist_pct: float | None = None
    ma20_price: float | None = None
    ma60_price: float | None = None
    low_20d: float | None = None
    high_20d: float | None = None
    low_60d: float | None = None
    high_60d: float | None = None
    # 估值
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield_pct: float | None = None
    val_metric: str | None = None
    val_pctile: float | None = None
    cheap_flag: str | None = None  # 實際值："" 或 "相對便宜"（非 bool）
    # 基本面
    rev_yoy_pct: float | None = None
    gross_margin_pct: float | None = None
    eps_q: float | None = None
    # 籌碼
    volume_lots_today: float | None = None
    amount_million: float | None = None
    inst_net_lots: float | None = None
    inst_pct20d: float | None = None
    foreign_net_lots: float | None = None
    foreign_net_5d_lots: float | None = None
    foreign_net_10d_lots: float | None = None
    trust_net_lots: float | None = None
    # 除權息
    ex_div_cash: float | None = None
    div_addback_pct: float | None = None
    # holdings 專屬
    buy_price: float | None = None
    return_pct: float | None = None
    market_value_k: float | None = None
