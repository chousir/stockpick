"""tests/analysis/test_momentum.py — momentum.compute_n_day_return 單元測試。"""

from datetime import date

import polars as pl
import pytest

from tw_screener.analysis.momentum import (
    aggregate_group_momentum,
    compute_dividend_addback,
    compute_n_day_return,
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
