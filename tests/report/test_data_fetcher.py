"""tests/report/test_data_fetcher.py — data_fetcher 單元測試（全離線）。"""

from datetime import date, timedelta

import polars as pl

from tw_screener.report.data_fetcher import (
    _format_institutional_summary,
    _format_price_summary,
    _format_revenue_summary,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_price_df(n: int = 30) -> pl.DataFrame:
    today = date.today()
    dates = [today - timedelta(days=n - i) for i in range(n)]
    closes = [100.0 + i * 0.5 for i in range(n)]
    return pl.DataFrame({
        "date": dates,
        "stock_id": ["2330"] * n,
        "trade_volume": [1000] * n,
        "trade_value": [100_000] * n,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "change": [0.5] * n,
        "transaction": [500] * n,
    })


def _make_revenue_df() -> pl.DataFrame:
    return pl.DataFrame({
        "stock_id": ["2330"] * 6,
        "company_name": ["台積電"] * 6,
        "year_month": ["202501", "202502", "202503", "202504", "202505", "202506"],
        "revenue": [200_000_000, 210_000_000, 220_000_000, 215_000_000, 225_000_000, 230_000_000],
        "prev_year_revenue": [180_000_000] * 6,
        "yoy_pct": [11.1, 16.7, 22.2, 19.4, 25.0, 27.8],
    })


def _make_institutional_df() -> pl.DataFrame:
    today = date.today()
    dates = [today - timedelta(days=5 - i) for i in range(5)]
    return pl.DataFrame({
        "date": dates,
        "stock_id": ["2330"] * 5,
        "stock_name": ["台積電"] * 5,
        "foreign_net": [1000, -500, 2000, 800, -200],
        "trust_net": [100, 50, -100, 200, 300],
        "dealer_net": [-50, 100, 50, -100, 200],
        "total_net": [1050, -350, 1950, 900, 300],
    })


# ─── _format_price_summary ────────────────────────────────────────────────────

def test_format_price_summary_empty():
    result = _format_price_summary(pl.DataFrame())
    assert "未取得" in result


def test_format_price_summary_contains_key_fields():
    df = _make_price_df(30)
    result = _format_price_summary(df)
    assert "MA20" in result
    assert "MA60" in result or "MA20" in result  # only 30 days, MA60 uses available
    assert "最新收盤" in result
    assert "期間最高" in result
    assert "30 個交易日" in result


def test_format_price_summary_ma20_correct():
    df = _make_price_df(30)
    result = _format_price_summary(df)
    closes = df["close"].to_list()
    # Formatter rounds to 1 decimal place
    expected_ma20 = round(sum(closes[-20:]) / 20, 1)
    assert f"{expected_ma20:.1f}" in result


# ─── _format_revenue_summary ──────────────────────────────────────────────────

def test_format_revenue_summary_empty():
    result = _format_revenue_summary(pl.DataFrame())
    assert "未取得" in result


def test_format_revenue_summary_contains_months():
    df = _make_revenue_df()
    result = _format_revenue_summary(df)
    assert "202501" in result
    assert "202506" in result


def test_format_revenue_summary_shows_yoy():
    df = _make_revenue_df()
    result = _format_revenue_summary(df)
    assert "%" in result
    assert "+" in result  # positive YoY


# ─── _format_institutional_summary ───────────────────────────────────────────

def test_format_institutional_summary_empty():
    result = _format_institutional_summary(pl.DataFrame())
    assert "未取得" in result


def test_format_institutional_summary_shows_cumulative():
    df = _make_institutional_df()
    result = _format_institutional_summary(df)
    assert "累計" in result
    assert "外資" in result
    assert "投信" in result


def test_format_institutional_summary_correct_total():
    df = _make_institutional_df()
    result = _format_institutional_summary(df)
    # foreign_net sum = 1000 + (-500) + 2000 + 800 + (-200) = 3100
    assert "3,100" in result or "+3,100" in result
