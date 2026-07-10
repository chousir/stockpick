"""WS-A2 ground-truth 面板單元測試（全離線合成資料）。

驗防偏誤慣例：entry=次一交易日、除息 (entry,exit] 加回、未到期/下市=null、
ETF 排除、位階/量比窗不足=null、等權中位基準與 alpha。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.panel import (
    build_price_panel,
    panel_summary,
    reconcile_close,
)

# 連續 10 個「交易日」（測試不需要真日曆，序列順序即交易日序）
_DAYS = [date(2026, 1, 5) + timedelta(days=i) for i in range(10)]


def _px(stock_id: str, closes: list[float], volumes: list[float] | None = None) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame(
        {
            "date": _DAYS[:n],
            "stock_id": [stock_id] * n,
            "close": closes,
            "volume": volumes if volumes is not None else [1000.0] * n,
        }
    )


def test_forward_return_entry_next_day() -> None:
    """r{h}＝(close[i+1+h]−close[i+1])/close[i+1]；entry 用次一交易日、非當日。"""
    px = _px("2330", [100.0, 110.0, 121.0, 133.1, 146.41])
    panel = build_price_panel(px, horizons=(2,), ma_windows=(2,), vol_lookback=2)
    r2_day0 = panel.filter(pl.col("date") == _DAYS[0])["r2"][0]
    # entry=110（day1）、exit=133.1（day3）→ +21%
    assert r2_day0 == pytest.approx(21.0, abs=1e-6)


def test_forward_return_immature_and_delisted_null() -> None:
    """序列尾端（未到期）與提早結束（下市）→ null，不是 0。"""
    live = _px("2330", [100.0] * 10)
    dead = _px("9999", [50.0] * 4)  # 只活 4 天＝中途下市
    panel = build_price_panel(
        pl.concat([live, dead]), horizons=(5,), ma_windows=(2,), vol_lookback=2
    )
    dead_rows = panel.filter(pl.col("stock_id") == "9999")
    assert dead_rows["r5"].null_count() == dead_rows.height  # 全 null
    live_tail = panel.filter((pl.col("stock_id") == "2330") & (pl.col("date") == _DAYS[9]))
    assert live_tail["r5"][0] is None  # 未到期


def test_dividend_addback_in_window_only() -> None:
    """ex_date ∈ (entry, exit] 才加回；ex_date=entry 當日不加。"""
    px = _px("2330", [100.0] * 6)
    div = pl.DataFrame(
        {
            "stock_id": ["2330", "2330"],
            "ex_date": [_DAYS[1], _DAYS[3]],  # day1=entry 當日（不加）、day3 ∈ 窗內（加）
            "cash_dividend": [7.0, 2.0],
        }
    )
    panel = build_price_panel(px, dividends=div, horizons=(2,), ma_windows=(2,), vol_lookback=2)
    r2_day0 = panel.filter(pl.col("date") == _DAYS[0])["r2"][0]
    assert r2_day0 == pytest.approx(2.0, abs=1e-6)  # 價差 0％＋2/100×100


def test_market_baseline_median_and_alpha() -> None:
    """mkt_ew_r{h}＝同日中位；alpha＝r−中位。"""
    a = _px("1111", [100.0, 100.0, 110.0])  # r1@day0 = +10%
    b = _px("2222", [100.0, 100.0, 120.0])  # r1@day0 = +20%
    panel = build_price_panel(pl.concat([a, b]), horizons=(1,), ma_windows=(2,), vol_lookback=2)
    day0 = panel.filter(pl.col("date") == _DAYS[0]).sort("stock_id")
    assert day0["mkt_ew_r1"].to_list() == pytest.approx([15.0, 15.0])
    assert day0["alpha1"].to_list() == pytest.approx([-5.0, 5.0])


def test_etf_and_warrant_excluded() -> None:
    """00 開頭（ETF）與含非數字（權證）整檔排除。"""
    stocks = pl.concat(
        [_px("2330", [100.0] * 3), _px("0050", [100.0] * 3), _px("2330Y", [1.0] * 3)]
    )
    panel = build_price_panel(stocks, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    assert set(panel["stock_id"].unique().to_list()) == {"2330"}


def test_ma_dist_null_until_window() -> None:
    """均線窗不足 → null；足窗才有值。"""
    px = _px("2330", [100.0, 100.0, 100.0, 130.0])
    panel = build_price_panel(px, horizons=(1,), ma_windows=(3,), vol_lookback=2)
    vals = panel.sort("date")["ma3_dist_pct"].to_list()
    assert vals[0] is None and vals[1] is None
    assert vals[2] == pytest.approx(0.0, abs=1e-9)
    # day3：MA3=(100+100+130)/3=110 → 130/110−1 = +18.18%
    assert vals[3] == pytest.approx(18.1818, abs=1e-3)


def test_vol_ratio_prior_window_excludes_today() -> None:
    """量比＝今日量/前 N 日均量（不含今日）；窗不足 null 不填 0。"""
    px = _px("2330", [100.0] * 4, volumes=[1000.0, 2000.0, 1500.0, 7000.0])
    panel = build_price_panel(px, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    vals = panel.sort("date")["vol_ratio"].to_list()
    assert vals[0] is None and vals[1] is None  # 前兩日窗不足
    assert vals[2] == pytest.approx(1500.0 / 1500.0, abs=1e-9)
    assert vals[3] == pytest.approx(7000.0 / 1750.0, abs=1e-9)


def test_institutional_join_and_missing_null() -> None:
    """法人淨額按 (date, stock_id) join；無資料日 → null。"""
    px = _px("2330", [100.0] * 3)
    inst = pl.DataFrame(
        {
            "date": [_DAYS[0]],
            "stock_id": ["2330"],
            "foreign_net": [5000],
            "trust_net": [-100],
            "dealer_net": [0],
        }
    )
    panel = build_price_panel(
        px, institutional=inst, horizons=(1,), ma_windows=(2,), vol_lookback=2
    )
    day0 = panel.filter(pl.col("date") == _DAYS[0])
    assert day0["foreign_net"][0] == 5000 and day0["trust_net"][0] == -100
    assert panel.filter(pl.col("date") == _DAYS[1])["foreign_net"][0] is None


def test_sub_industry_first_membership() -> None:
    """一檔多次產業 → 取 membership 表第一筆。"""
    px = _px("2330", [100.0] * 3)
    membership = pl.DataFrame(
        {"sub_industry": ["晶圓代工", "IC 設計"], "stock_id": ["2330", "2330"]}
    )
    panel = build_price_panel(
        px, membership=membership, horizons=(1,), ma_windows=(2,), vol_lookback=2
    )
    assert panel["sub_industry"].unique().to_list() == ["晶圓代工"]


def test_reconcile_close_diff() -> None:
    """核價：diff_pct 與 within_tol 正確。"""
    px = _px("2330", [100.0, 200.0])
    panel = build_price_panel(px, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    ref = pl.DataFrame(
        {"date": [_DAYS[0], _DAYS[1]], "stock_id": ["2330", "2330"], "close_ref": [100.0, 202.0]}
    )
    recon = reconcile_close(panel, ref, tol_pct=0.5)
    assert recon.height == 2
    worst = recon.row(0, named=True)
    assert worst["diff_pct"] == pytest.approx(2.0 / 202.0 * 100, abs=1e-6)
    assert not worst["within_tol"]
    assert recon.row(1, named=True)["within_tol"]


def test_empty_input_returns_empty() -> None:
    assert build_price_panel(pl.DataFrame()).is_empty()
    assert panel_summary(pl.DataFrame()).is_empty()
