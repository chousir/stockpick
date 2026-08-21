"""apply_local_filters 純函式測試（全離線，合成小 DataFrame，不用 fixture）。"""

from __future__ import annotations

import polars as pl
import pytest

from tw_screener.screener.goodinfo.url_builder import FilterCondition, StrategyConfig
from tw_screener.screener.local.filter import UnsupportedLocalFilterError, apply_local_filters


def _universe() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": ["1101", "2330", "2317", "9999"],
            "name": ["台泥", "台積電", "鴻海", "無資料股"],
            "market": ["上市", "上市", "上市", "上市"],
            "close": [40.0, 1000.0, 200.0, 50.0],
            "market_cap_billion": [3000.0, 25000.0, 1500.0, None],
            "pe_ratio": [12.0, 20.0, 14.0, None],
            "pb_ratio": [1.0, 5.0, 1.2, None],
            "dividend_yield_pct": [5.0, 1.5, 4.0, None],
            "cum_rev_yoy_pct": [8.0, 25.0, 15.0, None],
        }
    )


def _f_strategy() -> StrategyConfig:
    """對照 config/strategies/f_value_rebound.yaml 的四條件（門檻同真實 yaml）。"""
    return StrategyConfig(
        id="f_value_rebound",
        name="價值反彈",
        description="test",
        market="上市/上櫃",
        filters=[
            FilterCondition(item="市值 (億元)", min=100),
            FilterCondition(item="本益比 (PER)", max=15),
            FilterCondition(item="成交價現金殖利率 (%)", min=3),
            FilterCondition(item="累計月營收年增減率(%)", min=10),
        ],
        rules=[],
        display_sheet="交易狀況",
        display_period="日",
    )


def test_apply_local_filters_basic_thresholds():
    out = apply_local_filters(_universe(), _f_strategy())
    # 1101: PE12≤15、殖利率5≥3、累計YoY8 <10 → 不過
    # 2330: 殖利率1.5<3 → 不過
    # 2317: 市值1500≥100、PE14≤15、殖利率4≥3、累計YoY15≥10 → 過
    # 9999: 全 null → 不過
    assert out["stock_id"].to_list() == ["2317"]


def test_apply_local_filters_null_excluded():
    """null 值一律視為不通過，不補值。"""
    out = apply_local_filters(_universe(), _f_strategy())
    assert "9999" not in out["stock_id"].to_list()


def test_apply_local_filters_boundary_inclusive():
    """門檻邊界值應含（>=/<=，非嚴格不等）。"""
    universe = pl.DataFrame(
        {
            "stock_id": ["0001"],
            "name": ["邊界股"],
            "market": ["上市"],
            "close": [10.0],
            "market_cap_billion": [100.0],  # 恰等於 min
            "pe_ratio": [15.0],  # 恰等於 max
            "pb_ratio": [1.0],
            "dividend_yield_pct": [3.0],  # 恰等於 min
            "cum_rev_yoy_pct": [10.0],  # 恰等於 min
        }
    )
    out = apply_local_filters(universe, _f_strategy())
    assert out["stock_id"].to_list() == ["0001"]


def test_apply_local_filters_unmapped_item_raises():
    """策略含官方資料無法覆蓋的條件（如近四季ROE）→ 拒跑，不悄悄漏篩。"""
    strategy = StrategyConfig(
        id="d_quality_leader",
        name="品質龍頭",
        description="test",
        market="上市/上櫃",
        filters=[FilterCondition(item="近四季–ROE(%)–本季度", min=15)],
        rules=[],
        display_sheet="交易狀況",
        display_period="日",
    )
    with pytest.raises(UnsupportedLocalFilterError, match="d_quality_leader"):
        apply_local_filters(_universe(), strategy)


def test_apply_local_filters_empty_universe():
    empty = pl.DataFrame(
        schema={
            "stock_id": pl.Utf8, "name": pl.Utf8, "market": pl.Utf8, "close": pl.Float64,
            "market_cap_billion": pl.Float64, "pe_ratio": pl.Float64, "pb_ratio": pl.Float64,
            "dividend_yield_pct": pl.Float64, "cum_rev_yoy_pct": pl.Float64,
        }
    )
    out = apply_local_filters(empty, _f_strategy())
    assert out.is_empty()
