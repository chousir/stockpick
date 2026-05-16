"""tests/analysis/test_grouping.py — group_stocks 單元測試（全離線）。"""

import polars as pl
import pytest

from tw_screener.analysis.grouping import _compute_rs_from_history, group_stocks

# ─── Fixtures ─────────────────────────────────────────────────────────────────

_INDUSTRY_DF = pl.DataFrame(
    {
        "stock_id": ["2330", "2454", "3034", "2317", "2382"],
        "industry_code": ["24", "24", "24", "31", "25"],
        "industry_name": ["半導體業", "半導體業", "半導體業", "其他電子業", "電腦及周邊設備業"],
    }
)


def _make_screener_df(stock_ids: list[str], change_pcts: list[float], strategy_id: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": stock_ids,
            "name": [f"公司{sid}" for sid in stock_ids],
            "close": [100.0] * len(stock_ids),
            "change_pct": change_pcts,
            "amount_million": [1000.0] * len(stock_ids),
            "goodinfo_url": [f"http://goodinfo/{sid}" for sid in stock_ids],
            "strategy_id": [strategy_id] * len(stock_ids),
        }
    )


# ─── group_stocks ─────────────────────────────────────────────────────────────


def test_group_stocks_basic_groups():
    """兩個半導體股 + 其他電子 + 電腦股 → 最多三個族群（依 min_group_size 過濾）。"""
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "2317", "2382"], [3.5, 2.8, 1.2, 0.8], "a_breakout"
        )
    }
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    # 2330+2454 (半導體 code=24) = 2; 2317 (其他電子 code=31) = 1 → filtered; 2382 (電腦 code=25) = 1 → filtered
    assert len(groups) == 1
    assert groups["industry_name"][0] == "半導體業"


def test_group_stocks_min_group_size_filter():
    """只有 1 股的族群應被過濾掉。"""
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "2317"],  # 2330+2454 = 半導體×2, 2317 = 電腦×1
            [3.5, 2.8, 1.2],
            "a_breakout",
        )
    }
    groups, _ = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    assert len(groups) == 1
    assert groups["industry_name"][0] == "半導體業"


def test_group_stocks_members_count():
    """members_count 正確計算。"""
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "3034"], [3.5, 2.8, 4.0], "a_breakout"
        )
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    sc = groups.filter(pl.col("industry_code") == "24")["members_count"][0]
    assert sc == 3


def test_group_stocks_total_in_industry():
    """total_in_industry 應為 industry_df 中該產業的總數。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    row = groups.filter(pl.col("industry_code") == "24")
    assert row["total_in_industry"][0] == 3  # 2330, 2454, 3034 in industry_df
    assert row["members_count"][0] == 2  # only 2 screened


def test_group_stocks_entry_rate():
    """entry_rate = members_count / total_in_industry。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    row = groups.filter(pl.col("industry_code") == "24")
    assert row["entry_rate"][0] == pytest.approx(2 / 3, rel=1e-3)


def test_group_stocks_sorted_by_score():
    """族群應按 score 降序排列。"""
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "2317", "2382"],
            [5.0, 4.0, 0.1, 0.2],  # 半導體 RS 高，電腦 RS 低
            "a_breakout",
        )
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    scores = groups["score"].to_list()
    assert scores == sorted(scores, reverse=True)


def test_group_stocks_multi_strategy_count():
    """多策略時，count_{sid} 應分別計算。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout"),
        "b_growth_institutional": _make_screener_df(["2330"], [3.5], "b_growth_institutional"),
    }
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    sc = groups.filter(pl.col("industry_code") == "24")
    assert sc["count_a_breakout"][0] == 2
    assert sc["count_b_growth_institutional"][0] == 1


def test_group_stocks_empty_results():
    """三組 CSV 均為空 → 回傳空 DataFrame pair。"""
    groups, members = group_stocks(
        {"a_breakout": pl.DataFrame()},
        pl.DataFrame(),
        pl.DataFrame(),
    )
    assert groups.is_empty()
    assert members.is_empty()


def test_group_stocks_no_industry_df():
    """不提供 industry_df 時應回傳「未分類」群組。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=None, min_group_size=2
    )
    assert len(groups) == 1
    assert groups["industry_name"][0] == "未分類"


def test_group_stocks_rs_from_change_pct():
    """無 price_history 時，RS 應等於 change_pct。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    rs_map = {row["stock_id"]: row["rs"] for row in members.iter_rows(named=True)}
    assert rs_map["2330"] == pytest.approx(3.5)
    assert rs_map["2454"] == pytest.approx(2.8)


# ─── _compute_rs_from_history ─────────────────────────────────────────────────


def test_compute_rs_empty_history():
    assert _compute_rs_from_history(["2330"], pl.DataFrame(), pl.DataFrame()) == {}


def test_compute_rs_not_enough_days():
    """少於 7 天資料時應跳過。"""
    from datetime import date

    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 5,
            "date": [date(2026, 5, i) for i in range(1, 6)],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
        }
    )
    result = _compute_rs_from_history(["2330"], history, pl.DataFrame())
    assert "2330" not in result


def test_compute_rs_seven_days():
    """7 天以上的資料應計算 RS。"""
    from datetime import date

    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 110.0]
    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 7,
            "date": [date(2026, 5, i) for i in range(1, 8)],
            "close": closes,
        }
    )
    result = _compute_rs_from_history(["2330"], history, pl.DataFrame())
    # RS = (110 - 100) / 100 * 100 - 0 = 10.0
    assert "2330" in result
    assert result["2330"] == pytest.approx(10.0)
