"""Pydantic 回應模型。

enriched 表跨週演進（docs/17 §2.3）：以最新週 W25 為超集、所有欄位 Optional，
舊週缺欄回 null。`extra="allow"` 容忍未列出的欄位（schema 再演進不會炸）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WeeksResponse(BaseModel):
    weeks: list[str]
    latest: str | None


class SectorRow(BaseModel):
    """sector_rotation.csv 列（族群資金雷達，docs/17 §2.1）。全 Optional、extra 容忍。"""

    model_config = ConfigDict(extra="allow")

    radar_rank: int | None = None
    sub_industry: str | None = None
    date: str | None = None
    members: int | None = None
    members_priced: int | None = None
    quadrant: str | None = None
    freshness: str | None = None
    flow_turn: str | None = None
    entry_triggered: bool | None = None
    confirm_triggered: bool | None = None
    rank_delta: int | None = None
    cp_score: float | None = None
    basket_ret_5d_pct: float | None = None
    above_low_pct: float | None = None
    # 資金 z（熱力圖欄；10d 為分析層補窗加的中端鏡頭；其餘 *_z 由 extra 帶過）
    net_flow_5d_z: float | None = None
    net_flow_10d_z: float | None = None
    net_flow_20d_z: float | None = None
    foreign_flow_5d_z: float | None = None
    foreign_flow_10d_z: float | None = None
    foreign_flow_20d_z: float | None = None
    trust_flow_5d_z: float | None = None
    trust_flow_10d_z: float | None = None
    trust_flow_20d_z: float | None = None
    flow_breadth_5d: float | None = None
    flow_breadth_20d: float | None = None


class ThemeRow(BaseModel):
    """theme_strength.csv 列（主題/次產業強度，docs/17 §2.1）。全 Optional。"""

    model_config = ConfigDict(extra="allow")

    theme: str | None = None
    kind: str | None = None
    radar_rank: int | None = None
    lead_score: float | None = None
    score: float | None = None
    momentum_5d: float | None = None
    members_count: int | None = None
    foreign_score: float | None = None
    vol_surge_score: float | None = None
    rank_delta: int | None = None


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
    inst_net_5d_lots: float | None = None
    inst_net_10d_lots: float | None = None
    inst_pct20d: float | None = None
    foreign_net_lots: float | None = None
    foreign_net_5d_lots: float | None = None
    foreign_net_10d_lots: float | None = None
    trust_net_lots: float | None = None
    trust_net_5d_lots: float | None = None
    trust_net_10d_lots: float | None = None
    # 除權息
    ex_div_cash: float | None = None
    div_addback_pct: float | None = None
    # holdings 專屬
    buy_price: float | None = None
    return_pct: float | None = None
    market_value_k: float | None = None


class StockDetail(BaseModel):
    """個股彙整（docs/17 §4/§5.4）：跨表合併列 ＋ 命中策略 ＋ 所屬族群位置。"""

    stock_id: str
    week: str
    # 命中來源表（holdings/candidates/watchlist 子集，優先序 holdings>candidates>watchlist）
    sources: list[str]
    # 命中的篩選策略代號（d/e/f/g 子集）
    screens: list[str]
    # 合併後的 enriched 列（W25 超集、全 Optional）
    row: EnrichedRow
    # 所屬次產業在 sector_rotation 的位置：None＝本週無 sector_rotation 檔；[]＝有檔但無對應次產業
    sectors: list[SectorRow] | None
    goodinfo_url: str | None = None
