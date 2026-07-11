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
    """00 開頭（ETF）、含字母（權證）、6 位數字（權證/TDR）整檔排除；只留 4 位數普通股。"""
    stocks = pl.concat(
        [
            _px("2330", [100.0] * 3),
            _px("0050", [100.0] * 3),
            _px("2330Y", [1.0] * 3),
            _px("708855", [1.0] * 3),  # 上櫃權證（6 位數字，實測 daily_all 混入）
            _px("910861", [10.0] * 3),  # TDR
        ]
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


def test_panel_start_crops_output_but_keeps_warmup_computed() -> None:
    """panel_start 只裁切輸出範圍；指標仍在完整（含暖身段）窗上算——
    裁切日首日 ma60_dist 非 null（若裁切發生在指標計算前，暖身段被砍、此值必為 null）。"""
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(70)]
    closes = [100.0 + i * 0.5 for i in range(70)]
    px = pl.DataFrame(
        {"date": days, "stock_id": ["2330"] * 70, "close": closes, "volume": [1000.0] * 70}
    )
    start = days[62]  # 裁切日在第 60 根之後 → 完整窗下 ma60 已可算
    panel_full = build_price_panel(px, horizons=(1,), ma_windows=(60,), vol_lookback=2)
    panel_cropped = build_price_panel(
        px, horizons=(1,), ma_windows=(60,), vol_lookback=2, panel_start=start
    )
    assert panel_cropped["date"].min() == start
    assert panel_cropped.height == panel_full.filter(pl.col("date") >= start).height
    first_row = panel_cropped.sort("date").row(0, named=True)
    full_row = panel_full.filter(pl.col("date") == start).row(0, named=True)
    assert first_row["ma60_dist_pct"] is not None
    assert first_row["ma60_dist_pct"] == pytest.approx(full_row["ma60_dist_pct"])


def test_panel_start_absent_equals_current_behavior() -> None:
    """panel_start 缺席（None，預設）→ 輸出與不傳這個參數完全等價（既有行為不變）。"""
    px = _px("2330", [100.0, 102.0, 101.0, 105.0, 108.0])
    baseline = build_price_panel(px, horizons=(1,), ma_windows=(3,), vol_lookback=2)
    explicit_none = build_price_panel(
        px, horizons=(1,), ma_windows=(3,), vol_lookback=2, panel_start=None
    )
    assert baseline.equals(explicit_none)


def test_chip_coverage_market_segment_and_null_out() -> None:
    """chip_coverage＝該日「該市場段」（otc_stock_ids 判定）有無發布法人資料；上市/上櫃互不影響；
    False 的列 foreign/trust/dealer_net 強制 null（區分「該市場段未發布」vs「淨買超 0」）。"""
    listed, otc = "2330", "5388"
    px = pl.concat([_px(listed, [100.0, 101.0, 102.0]), _px(otc, [50.0, 51.0, 52.0])])
    inst = pl.DataFrame(
        {
            "date": [_DAYS[0], _DAYS[0], _DAYS[1], _DAYS[1], _DAYS[2]],
            "stock_id": [listed, otc, listed, otc, listed],  # day2：OTC 市場段整段缺席
            "foreign_net": [100, 200, 110, 210, 120],
            "trust_net": [10, 20, 11, 21, 12],
            "dealer_net": [1, 2, 1, 2, 1],
        }
    )
    panel = build_price_panel(
        px,
        institutional=inst,
        otc_stock_ids=frozenset({otc}),
        horizons=(1,),
        ma_windows=(2,),
        vol_lookback=2,
    )
    day2 = panel.filter(pl.col("date") == _DAYS[2])
    otc_day2 = day2.filter(pl.col("stock_id") == otc).row(0, named=True)
    listed_day2 = day2.filter(pl.col("stock_id") == listed).row(0, named=True)
    assert otc_day2["chip_coverage"] is False
    assert otc_day2["foreign_net"] is None and otc_day2["trust_net"] is None
    assert listed_day2["chip_coverage"] is True  # 上市股不受 OTC 缺席影響
    assert listed_day2["foreign_net"] == 120

    day0_otc = panel.filter(
        (pl.col("date") == _DAYS[0]) & (pl.col("stock_id") == otc)
    ).row(0, named=True)
    assert day0_otc["chip_coverage"] is True
    assert day0_otc["foreign_net"] == 200


def test_div_coverage_boundary_from_min_ex_date() -> None:
    """div_coverage 起日＝dividends 聯集 min(ex_date)；起日前 False、起日（含）後 True；
    無股利輸入 → 全 False。"""
    px = _px("2330", [100.0] * 5)
    div = pl.DataFrame(
        {
            "stock_id": ["2330", "2330"],
            "ex_date": [_DAYS[2], _DAYS[4]],
            "cash_dividend": [1.0, 2.0],
        }
    )
    panel = build_price_panel(px, dividends=div, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    cov = panel.sort("date")["div_coverage"].to_list()
    assert cov == [False, False, True, True, True]  # 起日＝min(ex_date)＝_DAYS[2]

    panel_no_div = build_price_panel(px, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    assert panel_no_div["div_coverage"].to_list() == [False] * 5


def test_margin_and_tdcc_join_null_not_zero() -> None:
    """margin_balance_lots／big_holder_pct：無資料日 null，不填 0（僅提供日有值）。"""
    px = _px("2330", [100.0] * 4)
    margin = pl.DataFrame({"date": [_DAYS[0]], "stock_id": ["2330"], "margin_balance": [1500]})
    tdcc = pl.DataFrame(
        {
            "data_date": [_DAYS[1]],
            "stock_id": ["2330"],
            "big_holder_pct": [42.5],
            "big_holder_1000_pct": [10.1],
        }
    )
    panel = build_price_panel(
        px, margin=margin, tdcc=tdcc, horizons=(1,), ma_windows=(2,), vol_lookback=2
    )
    p = panel.sort("date")
    assert p["margin_balance_lots"].to_list() == [1500.0, None, None, None]
    assert p["big_holder_pct"].to_list() == [None, 42.5, None, None]
    assert p["big_holder_1000_pct"].to_list() == [None, 10.1, None, None]

    panel_none = build_price_panel(px, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    assert panel_none["margin_balance_lots"].null_count() == panel_none.height
    assert panel_none["big_holder_pct"].null_count() == panel_none.height


def test_regime_join_by_date() -> None:
    """regime：純 date join（輸入 regime_label 欄 → 面板 regime 欄）；輸入缺席 → 全 null。"""
    px = _px("2330", [100.0] * 4)
    regime = pl.DataFrame(
        {
            "date": [_DAYS[0], _DAYS[2]],
            "regime_label": ["bull", "bear"],
            "regime_score": [0.8, -0.5],
        }
    )
    panel = build_price_panel(px, regime=regime, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    p = panel.sort("date")
    assert p["regime"].to_list() == ["bull", None, "bear", None]

    panel_none = build_price_panel(px, horizons=(1,), ma_windows=(2,), vol_lookback=2)
    assert panel_none["regime"].null_count() == panel_none.height


def test_empty_input_returns_empty() -> None:
    assert build_price_panel(pl.DataFrame()).is_empty()
    assert panel_summary(pl.DataFrame()).is_empty()
