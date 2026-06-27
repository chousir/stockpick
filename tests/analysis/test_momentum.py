"""tests/analysis/test_momentum.py — momentum.compute_n_day_return 單元測試。"""

import random
import time
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.analysis.momentum import (
    aggregate_group_momentum,
    compute_dividend_addback,
    compute_n_day_return,
    compute_rolling_extrema,
)


def _make_history(
    stock_id: str,
    closes: list[float],
    start: date = date(2026, 5, 1),
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": [stock_id] * len(closes),
            "date": [date(start.year, start.month, start.day + i) for i in range(len(closes))],
            "close": closes,
        }
    )


def test_empty_history_returns_empty_dict():
    assert compute_n_day_return(["2330"], pl.DataFrame()) == {}


def test_missing_columns_returns_empty():
    df = pl.DataFrame({"stock_id": ["2330"], "close": [100.0]})  # missing date
    assert compute_n_day_return(["2330"], df) == {}


def test_full_5_day_return():
    """6 筆收盤 → 5 日 gap → 完整 5 日報酬。"""
    history = _make_history("2330", [100.0, 101.0, 102.0, 103.0, 104.0, 110.0])
    result = compute_n_day_return(["2330"], history, n=5)
    assert "2330" in result
    ret, days = result["2330"]
    assert ret == pytest.approx(10.0)  # (110-100)/100*100
    assert days == 5


def test_partial_data_uses_available_gap():
    """只有 3 筆 → gap=2 → 2 日報酬，actual_days_used=2。"""
    history = _make_history("2330", [100.0, 105.0, 110.0])
    result = compute_n_day_return(["2330"], history, n=5)
    assert "2330" in result
    ret, days = result["2330"]
    assert ret == pytest.approx(10.0)  # (110-100)/100*100
    assert days == 2


def test_one_row_skipped():
    """只有 1 筆 → 無法算 gap，該股不在結果中。"""
    history = _make_history("2330", [100.0])
    assert "2330" not in compute_n_day_return(["2330"], history, n=5)


def test_multiple_stocks_partial_data():
    """混合：A 有 7 筆（取最近 5 日 gap）、B 有 3 筆（partial）、C 沒資料。"""
    df_a = _make_history("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 110.0])
    df_b = _make_history("B", [50.0, 51.0, 53.0])
    history = pl.concat([df_a, df_b])
    result = compute_n_day_return(["A", "B", "C"], history, n=5)
    assert "A" in result and "B" in result and "C" not in result
    # A: gap=5 → (110 - closes[1]=101) / 101 * 100 ≈ 8.91
    assert result["A"][0] == pytest.approx(8.9108910891, rel=1e-4)
    assert result["A"][1] == 5
    # B: only 3 closes, gap=2 → (53-50)/50*100 = 6.0
    assert result["B"] == (pytest.approx(6.0), 2)


def test_unsorted_dates_still_correct():
    """date 順序亂的資料應該被自動排序。"""
    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 6,
            "date": [
                date(2026, 5, 6),  # latest
                date(2026, 5, 1),
                date(2026, 5, 3),
                date(2026, 5, 2),
                date(2026, 5, 5),
                date(2026, 5, 4),
            ],
            "close": [110.0, 100.0, 102.0, 101.0, 104.0, 103.0],
        }
    )
    result = compute_n_day_return(["2330"], history, n=5)
    ret, days = result["2330"]
    assert ret == pytest.approx(10.0)
    assert days == 5


def test_zero_back_close_skipped():
    """過去某天收盤為 0 → 不能算（分母 0）。"""
    history = _make_history("2330", [0.0, 0.0, 0.0, 0.0, 0.0, 110.0])
    assert "2330" not in compute_n_day_return(["2330"], history, n=5)


# ─── compute_rolling_extrema（M-修法7 7a）─────────────────────────────────────


def test_rolling_extrema_windows():
    """各視窗取最近 N 個交易日收盤的 (min, max)。"""
    history = _make_history("2330", [100.0, 90.0, 120.0, 95.0, 105.0, 98.0, 130.0, 110.0])
    result = compute_rolling_extrema(["2330"], history, windows=(3, 5))
    assert result["2330"][3] == (98.0, 130.0)  # 最近 3 日 [98, 130, 110]
    assert result["2330"][5] == (95.0, 130.0)  # 最近 5 日 [95, 105, 98, 130, 110]


def test_rolling_extrema_short_history_uses_available():
    """可用天數 < 視窗 → 用可得全部，不報錯。"""
    history = _make_history("2330", [100.0, 120.0])
    result = compute_rolling_extrema(["2330"], history, windows=(60,))
    assert result["2330"][60] == (100.0, 120.0)


def test_rolling_extrema_empty_and_missing_cols():
    assert compute_rolling_extrema(["2330"], pl.DataFrame()) == {}
    df = pl.DataFrame({"stock_id": ["2330"], "close": [100.0]})  # 缺 date
    assert compute_rolling_extrema(["2330"], df) == {}


# ─── compute_dividend_addback ─────────────────────────────────────────────────


def _divs(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": [r[0] for r in rows],
            "ex_date": [r[1] for r in rows],
            "cash_dividend": [r[2] for r in rows],
        }
    )


def test_dividend_addback_in_window():
    """除息日落在 5 日視窗內 → 加回 cash/c_back*100。"""
    # 6/6..6/11，c_back=140（6/6）；6/8 除息 7.0 → addback = 7/140*100 = 5.0
    history = _make_history("3036", [140.0, 138.0, 130.0, 131.0, 132.0, 133.0],
                            start=date(2026, 6, 6))
    divs = _divs([("3036", date(2026, 6, 8), 7.0)])
    result = compute_dividend_addback(["3036"], history, divs, n=5)
    assert "3036" in result
    addback_pct, total_cash = result["3036"]
    assert addback_pct == pytest.approx(5.0)
    assert total_cash == pytest.approx(7.0)


def test_dividend_ex_date_at_window_start_excluded():
    """ex_date == 視窗起點日（c_back 之收盤已除息後）→ 缺口不在視窗，不加回。"""
    history = _make_history("3036", [140.0, 138.0, 130.0, 131.0, 132.0, 133.0],
                            start=date(2026, 6, 6))
    divs = _divs([("3036", date(2026, 6, 6), 7.0)])
    assert "3036" not in compute_dividend_addback(["3036"], history, divs, n=5)


def test_dividend_before_window_excluded():
    """ex_date 早於視窗起點 → 不在視窗。"""
    history = _make_history("3036", [140.0, 138.0, 130.0, 131.0, 132.0, 133.0],
                            start=date(2026, 6, 6))
    divs = _divs([("3036", date(2026, 6, 1), 7.0)])
    assert "3036" not in compute_dividend_addback(["3036"], history, divs, n=5)


def test_stock_dividend_zero_cash_excluded():
    """純配股（cash=0）不在現金還原範圍。"""
    history = _make_history("1312", [50.0, 49.0, 48.0, 49.0, 50.0, 51.0],
                            start=date(2026, 6, 6))
    divs = _divs([("1312", date(2026, 6, 8), 0.0)])
    assert "1312" not in compute_dividend_addback(["1312"], history, divs, n=5)


def test_multiple_dividends_in_window_summed():
    """視窗內多筆現金股利合計加回。"""
    history = _make_history("2327", [100.0, 98.0, 96.0, 97.0, 98.0, 99.0],
                            start=date(2026, 6, 6))
    divs = _divs([("2327", date(2026, 6, 8), 3.0), ("2327", date(2026, 6, 10), 1.0)])
    addback_pct, total_cash = compute_dividend_addback(["2327"], history, divs, n=5)["2327"]
    assert total_cash == pytest.approx(4.0)
    assert addback_pct == pytest.approx(4.0)  # (3+1)/100*100


def test_empty_dividends_returns_empty():
    history = _make_history("3036", [140.0, 138.0, 133.0], start=date(2026, 6, 6))
    assert compute_dividend_addback(["3036"], history, pl.DataFrame()) == {}


def test_dividend_addback_missing_columns_returns_empty():
    history = _make_history("3036", [140.0, 138.0, 133.0], start=date(2026, 6, 6))
    bad = pl.DataFrame({"stock_id": ["3036"], "ex_date": [date(2026, 6, 8)]})  # no cash
    assert compute_dividend_addback(["3036"], history, bad) == {}


# ─── aggregate_group_momentum ─────────────────────────────────────────────────


def test_aggregate_group_momentum_mean():
    momentum = {"A": (10.0, 5), "B": (4.0, 5), "C": (-2.0, 3)}
    groups = {"semi": ["A", "B"], "panel": ["C"]}
    result = aggregate_group_momentum(momentum, groups)
    assert result["semi"][0] == pytest.approx(7.0)  # (10+4)/2
    assert result["semi"][1] == 5
    assert result["panel"][0] == pytest.approx(-2.0)
    assert result["panel"][1] == 3


def test_aggregate_group_momentum_skips_missing():
    momentum = {"A": (10.0, 5)}
    groups = {"semi": ["A", "missing_stock"]}
    result = aggregate_group_momentum(momentum, groups)
    assert result["semi"] == (pytest.approx(10.0), 5)


def test_aggregate_group_momentum_min_days():
    momentum = {"A": (5.0, 5), "B": (3.0, 2)}
    groups = {"semi": ["A", "B"]}
    result = aggregate_group_momentum(momentum, groups)
    _avg, days = result["semi"]
    assert days == 2  # min of (5, 2)


def test_aggregate_group_momentum_empty_group():
    momentum = {"A": (10.0, 5)}
    groups = {"empty": ["X", "Y"]}
    result = aggregate_group_momentum(momentum, groups)
    assert result["empty"] == (0.0, 0)


# ─── 大宇宙向量化等價性基準（規劃書 01 P3）─────────────────────────────────────
# 把舊的 per-stock 迴圈實作逐字搬進測試當「黃金基準」，向量化版必須逐值相同。
# 同時量測耗時，斷言向量化不慢於迴圈（1800 檔下應快數量級）。


def _naive_n_day_return(stock_ids, price_history, n=5):
    if price_history.is_empty():
        return {}
    if not {"stock_id", "date", "close"}.issubset(set(price_history.columns)):
        return {}
    result = {}
    ph_sorted = price_history.sort("date")
    for stock_id in stock_ids:
        stock_df = ph_sorted.filter(pl.col("stock_id") == stock_id)
        if len(stock_df) < 2:
            continue
        gap = min(n, len(stock_df) - 1)
        c_now = stock_df["close"][-1]
        c_back = stock_df["close"][-(gap + 1)]
        if c_now is None or c_back is None or c_back == 0:
            continue
        result[stock_id] = (float((c_now - c_back) / c_back * 100), int(gap))
    return result


def _naive_rolling_extrema(stock_ids, price_history, windows=(20, 60)):
    if price_history.is_empty():
        return {}
    if not {"stock_id", "date", "close"}.issubset(set(price_history.columns)):
        return {}
    result = {}
    ph_sorted = price_history.sort("date")
    for stock_id in stock_ids:
        closes = ph_sorted.filter(pl.col("stock_id") == stock_id)["close"].drop_nulls().to_list()
        if not closes:
            continue
        result[stock_id] = {w: (float(min(closes[-w:])), float(max(closes[-w:]))) for w in windows}
    return result


def _naive_dividend_addback(stock_ids, price_history, dividends, n=5):
    if price_history.is_empty() or not {"stock_id", "date", "close"}.issubset(
        set(price_history.columns)
    ):
        return {}
    if dividends.is_empty() or not {"stock_id", "ex_date", "cash_dividend"}.issubset(
        set(dividends.columns)
    ):
        return {}
    div_by_stock = {}
    for r in dividends.iter_rows(named=True):
        cash, ex = r.get("cash_dividend"), r.get("ex_date")
        if cash is None or cash <= 0 or ex is None:
            continue
        div_by_stock.setdefault(str(r["stock_id"]), []).append((ex, float(cash)))
    result = {}
    ph_sorted = price_history.sort("date")
    for stock_id in stock_ids:
        events = div_by_stock.get(stock_id)
        if not events:
            continue
        stock_df = ph_sorted.filter(pl.col("stock_id") == stock_id)
        if len(stock_df) < 2:
            continue
        gap = min(n, len(stock_df) - 1)
        date_back = stock_df["date"][-(gap + 1)]
        latest = stock_df["date"][-1]
        c_back = stock_df["close"][-(gap + 1)]
        if c_back is None or c_back == 0:
            continue
        total_cash = sum(cash for ex, cash in events if date_back < ex <= latest)
        if total_cash <= 0:
            continue
        result[stock_id] = (float(total_cash / c_back * 100), float(total_cash))
    return result


def _make_universe(n_stocks=1800, n_days=80, seed=42):
    """合成大宇宙：含長短不一的歷史、零星 null 收盤、隨機除息。"""
    rng = random.Random(seed)
    start = date(2026, 3, 2)
    rows_sid, rows_date, rows_close = [], [], []
    div_sid, div_ex, div_cash = [], [], []
    stock_ids = [f"{4000 + i}" for i in range(n_stocks)]
    for sid in stock_ids:
        length = rng.choice([1, 2, 3, 25, n_days])  # 含 1 筆(應被略過)與部分資料
        price = rng.uniform(20, 500)
        for d in range(length):
            # 偶發 null 收盤 / 偶發 0 收盤，逼出邊界
            if rng.random() < 0.01:
                close = None
            elif rng.random() < 0.005:
                close = 0.0
            else:
                price *= 1 + rng.uniform(-0.05, 0.05)
                close = round(price, 2)
            rows_sid.append(sid)
            rows_date.append(start + timedelta(days=d))
            rows_close.append(close)
        if rng.random() < 0.3 and length >= 3:  # 30% 有除息，落在歷史區間內某天
            div_sid.append(sid)
            div_ex.append(start + timedelta(days=rng.randint(0, length - 1)))
            div_cash.append(round(rng.uniform(0.5, 8.0), 2))
    price_history = pl.DataFrame(
        {"stock_id": rows_sid, "date": rows_date, "close": rows_close}
    )
    dividends = pl.DataFrame(
        {"stock_id": div_sid, "ex_date": div_ex, "cash_dividend": div_cash}
    )
    return stock_ids, price_history, dividends


def test_vectorized_matches_naive_large_universe():
    """1800 檔合成資料：向量化版與迴圈版逐值相同，且耗時不增。"""
    stock_ids, price_history, dividends = _make_universe()

    t0 = time.perf_counter()
    naive_ret = _naive_n_day_return(stock_ids, price_history, n=5)
    naive_ext = _naive_rolling_extrema(stock_ids, price_history, windows=(20, 60))
    naive_div = _naive_dividend_addback(stock_ids, price_history, dividends, n=5)
    naive_time = time.perf_counter() - t0

    t1 = time.perf_counter()
    vec_ret = compute_n_day_return(stock_ids, price_history, n=5)
    vec_ext = compute_rolling_extrema(stock_ids, price_history, windows=(20, 60))
    vec_div = compute_dividend_addback(stock_ids, price_history, dividends, n=5)
    vec_time = time.perf_counter() - t1

    # 逐值相同（dict 相等忽略順序）
    assert vec_ret == naive_ret
    assert vec_ext == naive_ext
    assert vec_div == naive_div
    # 確認測資真的有打到各分支
    assert len(naive_ret) > 100
    assert len(naive_div) > 10
    # 向量化在大宇宙下應更快（留充裕餘裕、避免 CI 抖動誤殺）
    assert vec_time < naive_time
