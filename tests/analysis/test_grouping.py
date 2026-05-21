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


def _make_screener_df(
    stock_ids: list[str], change_pcts: list[float], strategy_id: str
) -> pl.DataFrame:
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
    # 2330+2454 (半導體 24) = 2;
    # 2317 (其他電子 31) = 1 → filtered; 2382 (電腦 25) = 1 → filtered
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
            [5.0, 4.0, 0.1, 0.2],
            "a_breakout",
        )
    }
    # 加上「其他電子 + 電腦」各 2 檔以便兩族群並存
    industry = pl.concat(
        [
            _INDUSTRY_DF,
            pl.DataFrame(
                {
                    "stock_id": ["2376", "6669"],
                    "industry_code": ["31", "25"],
                    "industry_name": ["其他電子業", "電腦及周邊設備業"],
                }
            ),
        ]
    )
    results["a_breakout"] = pl.concat(
        [
            results["a_breakout"],
            _make_screener_df(["2376", "6669"], [0.5, 0.3], "a_breakout"),
        ]
    )
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=industry, min_group_size=2
    )
    scores = groups["score"].to_list()
    assert scores == sorted(scores, reverse=True)


def test_group_stocks_multi_strategy_count():
    """多策略時，count_{sid} 應分別計算。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout"),
        "b_growth_institutional": _make_screener_df(["2330"], [3.5], "b_growth_institutional"),
    }
    groups, _ = group_stocks(
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
    """無 price_history 時，rs（5 日 momentum 欄位）應 fallback 到 change_pct。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    rs_map = {row["stock_id"]: row["rs"] for row in members.iter_rows(named=True)}
    assert rs_map["2330"] == pytest.approx(3.5)
    assert rs_map["2454"] == pytest.approx(2.8)


def test_group_stocks_has_momentum_columns():
    """新欄位 momentum_5d / momentum_5d_days_used 應存在。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    assert "momentum_5d" in groups.columns
    assert "momentum_5d_days_used" in groups.columns
    assert "up_count" in groups.columns
    assert "momentum_5d" in members.columns
    assert "momentum_days_used" in members.columns


def test_group_stocks_momentum_uses_median_not_mean():
    """單檔小型股飆漲不應拉高整族群：momentum_5d 取中位數而非平均。

    情境同 2026-W21 電力供應業：一檔 +30%、另兩檔 -3% / -17%。
    mean = +3.33%（假象轉強），median = -3%（真實偏弱），up_count = 1。
    """
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "3034"], [30.0, -3.0, -17.0], "a_breakout"
        )
    }
    groups, _ = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    row = groups.filter(pl.col("industry_code") == "24")
    # 中位數 = -3.0（非平均 +3.33）
    assert row["momentum_5d"][0] == pytest.approx(-3.0)
    # 上漲家數：3 檔僅 1 檔為正
    assert row["up_count"][0] == 1


def test_group_stocks_uses_5_day_momentum():
    """提供 6 筆 OHLCV → momentum_5d 應為 5 日累計報酬。"""
    from datetime import date

    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 6,
            "date": [date(2026, 5, i) for i in range(11, 17)],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 110.0],
        }
    )
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454"], [0.0, 0.0], "a_breakout"
        )
    }
    _, members = group_stocks(
        results, history, pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row["momentum_5d"] == pytest.approx(10.0)
    assert row["momentum_days_used"] == 5


# ─── 法人接進族群強度（M2）─────────────────────────────────────────────────


def _make_institutional(stock_ids: list[str], total_nets: list[int]) -> pl.DataFrame:
    from datetime import date

    return pl.DataFrame(
        {
            "date": [date(2026, 5, 19)] * len(stock_ids),
            "stock_id": stock_ids,
            "stock_name": [f"公司{sid}" for sid in stock_ids],
            "foreign_net": total_nets,
            "trust_net": [0] * len(stock_ids),
            "dealer_net": [0] * len(stock_ids),
            "total_net": total_nets,
        }
    )


def test_group_stocks_inst_score_is_buy_breadth():
    """inst_score = 族群內法人買超家數 / 成員數；inst_net 帶入個股。"""
    results = {
        "a_breakout": _make_screener_df(
            ["2330", "2454", "3034"], [3.0, 2.0, 1.0], "a_breakout"
        )
    }
    # 3 檔中 2 檔法人買超（2330, 2454），1 檔賣超（3034）
    institutional = _make_institutional(["2330", "2454", "3034"], [1000, 500, -800])
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
        institutional=institutional,
    )
    row = groups.filter(pl.col("industry_code") == "24")
    assert row["inst_buy_count"][0] == 2
    assert row["inst_score"][0] == pytest.approx(2 / 3)
    inst_map = {r["stock_id"]: r["inst_net"] for r in members.iter_rows(named=True)}
    assert inst_map["2330"] == pytest.approx(1000.0)
    assert inst_map["3034"] == pytest.approx(-800.0)


def test_group_stocks_no_institutional_inst_score_zero():
    """未提供法人資料 → inst_score = 0、inst_net/foreign_net/trust_net = 0（不破壞）。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.0, 2.0], "a_breakout")
    }
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    assert groups.filter(pl.col("industry_code") == "24")["inst_score"][0] == 0.0
    for r in members.iter_rows(named=True):
        assert r["inst_net"] == 0.0
        assert r["foreign_net"] == 0.0
        assert r["trust_net"] == 0.0


def test_group_stocks_splits_foreign_and_trust():
    """外資/投信各自近 N 日合計帶入個股；inst_net 仍為三大法人總和。"""
    from datetime import date

    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.0, 2.0], "a_breakout")
    }
    institutional = pl.DataFrame(
        {
            "date": [date(2026, 5, 19), date(2026, 5, 19)],
            "stock_id": ["2330", "2454"],
            "stock_name": ["A", "B"],
            "foreign_net": [1000, -300],
            "trust_net": [200, 50],
            "dealer_net": [-100, 10],
            "total_net": [1100, -240],
        }
    )
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
        institutional=institutional,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["inst_net"] == pytest.approx(1100.0)
    assert m["2330"]["foreign_net"] == pytest.approx(1000.0)
    assert m["2330"]["trust_net"] == pytest.approx(200.0)
    assert m["2454"]["foreign_net"] == pytest.approx(-300.0)
    assert m["2454"]["trust_net"] == pytest.approx(50.0)


# ─── _compute_rs_from_history（back-compat wrapper）─────────────────────────


def test_compute_rs_empty_history():
    assert _compute_rs_from_history(["2330"], pl.DataFrame(), pl.DataFrame()) == {}


def test_compute_rs_full_5_days():
    """6 筆 → 5 日報酬。"""
    from datetime import date

    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 6,
            "date": [date(2026, 5, i) for i in range(1, 7)],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 110.0],
        }
    )
    result = _compute_rs_from_history(["2330"], history, pl.DataFrame())
    assert result["2330"] == pytest.approx(10.0)


def test_compute_rs_partial_data():
    """只有 3 筆 → 2 日累計報酬（partial fallback）。"""
    from datetime import date

    history = pl.DataFrame(
        {
            "stock_id": ["2330"] * 3,
            "date": [date(2026, 5, i) for i in range(1, 4)],
            "close": [100.0, 105.0, 110.0],
        }
    )
    result = _compute_rs_from_history(["2330"], history, pl.DataFrame())
    assert "2330" in result
    assert result["2330"] == pytest.approx(10.0)


# ─── 量比 ──────────────────────────────────────────────────────────────────────


def _make_volume_history(stock_ids: list[str], dates_per_stock: list, volumes: list) -> pl.DataFrame:
    from datetime import date as _date
    rows = []
    for sid, d, v in zip(stock_ids, dates_per_stock, volumes):
        rows.append({"stock_id": sid, "date": d, "trade_volume": v})
    return pl.DataFrame(rows, schema={"stock_id": pl.Utf8, "date": pl.Date, "trade_volume": pl.Int64})


def test_group_stocks_vol_ratio_computed():
    """提供 volume_history → members 含正確 vol_ratio（今日量 / 近 N 日均量）。"""
    from datetime import date

    dates = [date(2026, 5, d) for d in range(12, 18)]  # 5/12 ~ 5/17（6 日）
    # 2330: 前 5 日均量 = (1000+1200+800+1100+900)/5 = 1000; 今日(5/17)=2000 → ratio=2.0
    stock_ids = ["2330"] * 6
    volumes = [1000, 1200, 800, 1100, 900, 2000]
    volume_history = _make_volume_history(stock_ids, dates, volumes)

    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
        volume_history=volume_history,
    )
    row_2330 = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row_2330["vol_ratio"] == pytest.approx(2.0)
    # 2454 無 volume_history → vol_ratio = 0
    row_2454 = members.filter(pl.col("stock_id") == "2454").to_dicts()[0]
    assert row_2454["vol_ratio"] == pytest.approx(0.0)


def test_group_stocks_no_volume_history_vol_ratio_zero():
    """未提供 volume_history → vol_ratio = 0，不破壞現有流程。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    for r in members.iter_rows(named=True):
        assert r["vol_ratio"] == pytest.approx(0.0)


def test_group_stocks_vol_ratio_insufficient_data():
    """歷史天數不足 3 日 → vol_ratio = 0（不輸出不可靠的比值）。"""
    from datetime import date

    # 只有 2 天歷史 + 今日共 3 筆，prior 2 日 < 3 → vol_ratio = 0
    dates = [date(2026, 5, 16), date(2026, 5, 17), date(2026, 5, 18)]
    volume_history = _make_volume_history(
        ["2330"] * 3, dates, [1000, 1200, 3000]
    )
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
        volume_history=volume_history,
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    # prior = 2 days only → below threshold → vol_ratio = 0
    assert row["vol_ratio"] == pytest.approx(0.0)


# ─── MA 距離計算 ──────────────────────────────────────────────────────────────


def _make_price_history(stock_id: str, closes: list[float]) -> pl.DataFrame:
    from datetime import date
    n = len(closes)
    return pl.DataFrame({
        "stock_id": [stock_id] * n,
        "date": [date(2026, 1, i + 1) for i in range(n)],
        "close": closes,
    })


def test_group_stocks_ma20_correct():
    """提供 25 日歷史 → ma20_dist_pct 正確（最後 20 日均價）。"""
    closes = [100.0] * 20 + [110.0, 110.0, 110.0, 110.0, 110.0]  # 25 日
    price_history = _make_price_history("2330", closes)
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    # 最後 20 日：15 日 100 + 5 日 110 → ma20 = (15*100 + 5*110) / 20 = 102.5
    # close (from screener_df) = 100.0 → dist = (100-102.5)/102.5 * 100 ≈ -2.44%
    assert row["ma20_dist_pct"] == pytest.approx((100.0 - 102.5) / 102.5 * 100, rel=1e-3)


def test_group_stocks_ma60_none_when_insufficient():
    """不足 60 日歷史 → ma60_dist_pct 為 None（顯示 '-'）。"""
    closes = [100.0] * 30  # only 30 days
    price_history = _make_price_history("2330", closes)
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row["ma60_dist_pct"] is None


def test_group_stocks_ma_no_history():
    """未提供 price_history → ma20/ma60 均為 None，不破壞現有流程。"""
    results = {
        "a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")
    }
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    for r in members.iter_rows(named=True):
        assert r["ma20_dist_pct"] is None
        assert r["ma60_dist_pct"] is None
