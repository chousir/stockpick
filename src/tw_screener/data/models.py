"""Pydantic schema 定義（作為資料合約文件）。"""

from datetime import date

from pydantic import BaseModel


class DailyPrice(BaseModel):
    """全市場每日行情（STOCK_DAY_ALL）。"""

    stock_id: str
    name: str
    trade_volume: int
    trade_value: int
    open: float
    high: float
    low: float
    close: float
    change: float | None
    transaction: int


class InstitutionalTrade(BaseModel):
    """三大法人買賣超（T86）。"""

    date: date
    stock_id: str
    stock_name: str
    foreign_net: int
    trust_net: int
    dealer_net: int
    total_net: int


class MonthlyRevenue(BaseModel):
    """月營收（t187ap05_L）。"""

    stock_id: str
    company_name: str
    year_month: str  # YYYYMM
    revenue: int
    prev_year_revenue: int | None
    yoy_pct: float | None
