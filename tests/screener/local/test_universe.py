"""build_local_universe 測試：假 client（duck-typed，同 TWSEClient 方法名），全離線不打網。"""

from __future__ import annotations

import polars as pl

from tw_screener.screener.local.universe import build_local_universe


class _FakeClient:
    """只實作 build_local_universe 用到的方法，回傳合成資料。"""

    def __init__(
        self,
        listed_daily: pl.DataFrame,
        otc_daily: pl.DataFrame,
        listed_shares: pl.DataFrame,
        otc_shares: pl.DataFrame,
        valuation: pl.DataFrame,
        revenue: pl.DataFrame,
    ) -> None:
        self._listed_daily = listed_daily
        self._otc_daily = otc_daily
        self._listed_shares = listed_shares
        self._otc_shares = otc_shares
        self._valuation = valuation
        self._revenue = revenue

    def fetch_daily_all(self) -> pl.DataFrame:
        return self._listed_daily

    def fetch_otc_daily_all(self) -> pl.DataFrame:
        return self._otc_daily

    def fetch_listed_shares(self) -> pl.DataFrame:
        return self._listed_shares

    def fetch_otc_shares(self) -> pl.DataFrame:
        return self._otc_shares

    def load_latest_valuation_ratios(self) -> pl.DataFrame:
        return self._valuation

    def fetch_revenue(self) -> pl.DataFrame:
        return self._revenue


def _empty(schema: dict) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def test_build_local_universe_joins_all_sources():
    listed_daily = pl.DataFrame(
        {"stock_id": ["1101"], "name": ["台泥"], "close": [40.0]}
    )
    otc_daily = pl.DataFrame(
        {"stock_id": ["6488"], "name": ["環球晶"], "close": [500.0]}
    )
    listed_shares = pl.DataFrame(
        {"stock_id": ["1101"], "stock_name": ["台泥"], "shares_outstanding": [7_523_181_742]}
    )
    otc_shares = pl.DataFrame(
        {"stock_id": ["6488"], "stock_name": ["環球晶"], "shares_outstanding": [100_000_000]}
    )
    valuation = pl.DataFrame(
        {
            "stock_id": ["1101", "6488"],
            "market": ["上市", "上櫃"],
            "pe": [12.0, 30.0],
            "pbr": [1.0, 2.0],
            "dividend_yield": [5.0, 1.0],
        }
    )
    revenue = pl.DataFrame(
        {
            "stock_id": ["1101", "6488"],
            "company_name": ["台泥", "環球晶"],
            "year_month": ["202607", "202607"],
            "revenue": [100, 200],
            "prev_year_revenue": [90, 180],
            "yoy_pct": [11.1, 11.1],
            "cum_revenue": [1000, 2000],
            "cum_prev_year_revenue": [900, 1800],
            "cum_yoy_pct": [11.1, 11.1],
        }
    )
    client = _FakeClient(
        listed_daily, otc_daily, listed_shares, otc_shares, valuation, revenue
    )
    out = build_local_universe(client)
    assert set(out["stock_id"].to_list()) == {"1101", "6488"}

    row_1101 = out.filter(pl.col("stock_id") == "1101").to_dicts()[0]
    assert row_1101["market"] == "上市"
    assert row_1101["market_cap_billion"] == 7_523_181_742 * 40.0 / 1e8
    assert row_1101["pe_ratio"] == 12.0
    assert row_1101["dividend_yield_pct"] == 5.0
    assert row_1101["cum_rev_yoy_pct"] == 11.1

    row_6488 = out.filter(pl.col("stock_id") == "6488").to_dicts()[0]
    assert row_6488["market"] == "上櫃"


def test_build_local_universe_missing_shares_is_null_not_zero():
    """股數缺快取 → 市值 None，不猜／不補零。"""
    listed_daily = pl.DataFrame({"stock_id": ["1101"], "name": ["台泥"], "close": [40.0]})
    client = _FakeClient(
        listed_daily,
        _empty({"stock_id": pl.Utf8, "name": pl.Utf8, "close": pl.Float64}),
        _empty({"stock_id": pl.Utf8, "stock_name": pl.Utf8, "shares_outstanding": pl.Int64}),
        _empty({"stock_id": pl.Utf8, "stock_name": pl.Utf8, "shares_outstanding": pl.Int64}),
        _empty(
            {
                "stock_id": pl.Utf8, "market": pl.Utf8, "pe": pl.Float64,
                "pbr": pl.Float64, "dividend_yield": pl.Float64,
            }
        ),
        _empty(
            {
                "stock_id": pl.Utf8, "company_name": pl.Utf8, "year_month": pl.Utf8,
                "revenue": pl.Int64, "prev_year_revenue": pl.Int64, "yoy_pct": pl.Float64,
                "cum_revenue": pl.Int64, "cum_prev_year_revenue": pl.Int64,
                "cum_yoy_pct": pl.Float64,
            }
        ),
    )
    out = build_local_universe(client)
    row = out.filter(pl.col("stock_id") == "1101").to_dicts()[0]
    assert row["market_cap_billion"] is None
    assert row["pe_ratio"] is None


def test_build_local_universe_empty_daily_returns_empty():
    empty_daily = _empty({"stock_id": pl.Utf8, "name": pl.Utf8, "close": pl.Float64})
    client = _FakeClient(
        empty_daily, empty_daily,
        _empty({"stock_id": pl.Utf8, "stock_name": pl.Utf8, "shares_outstanding": pl.Int64}),
        _empty({"stock_id": pl.Utf8, "stock_name": pl.Utf8, "shares_outstanding": pl.Int64}),
        _empty(
            {
                "stock_id": pl.Utf8, "market": pl.Utf8, "pe": pl.Float64,
                "pbr": pl.Float64, "dividend_yield": pl.Float64,
            }
        ),
        _empty(
            {
                "stock_id": pl.Utf8, "company_name": pl.Utf8, "year_month": pl.Utf8,
                "revenue": pl.Int64, "prev_year_revenue": pl.Int64, "yoy_pct": pl.Float64,
                "cum_revenue": pl.Int64, "cum_prev_year_revenue": pl.Int64,
                "cum_yoy_pct": pl.Float64,
            }
        ),
    )
    out = build_local_universe(client)
    assert out.is_empty()
