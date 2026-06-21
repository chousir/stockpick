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


def test_group_stocks_foreign_multiwindow_and_ret10():
    """修法6：外資 20 日累計為正、但近 5 日轉賣，foreign_net_5d/10d 各自揭露；
    ret_10d 為近 10 日報酬（除息還原）→ 報表可區分健康回踩 vs 下跌反彈。"""
    from datetime import date

    dates = [date(2026, 5, d) for d in range(1, 13)]  # 12 個交易日
    # 前 7 日 +10000、後 5 日 −4000 → 20日和 +50000、5日和 −20000、10日和 +30000
    fnet = [10000] * 7 + [-4000] * 5
    inst = pl.DataFrame(
        {
            "date": dates,
            "stock_id": ["3231"] * 12,
            "stock_name": ["緯創"] * 12,
            "foreign_net": fnet,
            "trust_net": [0] * 12,
            "dealer_net": [0] * 12,
            "total_net": fnet,
        }
    )
    # 近 10 日由 176 跌到 162（ret_10d≈−8%）＝下跌反彈型
    closes = [170.0, 176.0, 173.0, 168.0, 165.0, 160.0, 158.0, 156.0, 159.0, 158.0, 163.0, 162.0]
    history = pl.DataFrame({"stock_id": ["3231"] * 12, "date": dates, "close": closes})
    industry = pl.DataFrame(
        {
            "stock_id": ["3231", "3037"],
            "industry_code": ["25", "25"],
            "industry_name": ["電腦及周邊設備業", "電腦及周邊設備業"],
        }
    )
    results = {"a_breakout": _make_screener_df(["3231", "3037"], [0.0, 0.0], "a_breakout")}
    _, members = group_stocks(
        results, history, pl.DataFrame(), industry_df=industry,
        min_group_size=2, institutional=inst,
    )
    row = members.filter(pl.col("stock_id") == "3231").to_dicts()[0]
    assert row["foreign_net"] == pytest.approx(50000.0)       # 20 日累計（正）
    assert row["foreign_net_5d"] == pytest.approx(-20000.0)   # 近 5 日（轉賣・近端真相）
    assert row["foreign_net_10d"] == pytest.approx(30000.0)
    assert row["ret_10d"] == pytest.approx((162.0 - 176.0) / 176.0 * 100, abs=0.1)


def test_group_stocks_range_extrema_windows():
    """M-修法7（7a）：low/high_20d/60d 為近 20/60 日收盤 min/max（絕對價）。
    極值落在最前 5 筆（在 60 日窗內、20 日窗外）→ 兩窗應給不同的低/高。"""
    from datetime import date

    n = 25
    dates = [date(2026, 5, 1 + i) for i in range(n)]
    head = [50.0, 200.0, 60.0, 190.0, 70.0]  # 全域低 50 / 高 200（僅落在 60 日窗）
    tail = [120.0, 130.0, 110.0, 140.0, 150.0, 100.0, 160.0, 170.0, 115.0, 125.0,
            135.0, 145.0, 155.0, 165.0, 175.0, 180.0, 118.0, 128.0, 138.0, 148.0]
    closes = head + tail  # len 25；近 20 日 = tail（低 100 / 高 180）
    history = pl.DataFrame({"stock_id": ["2330"] * n, "date": dates, "close": closes})
    industry = pl.DataFrame(
        {
            "stock_id": ["2330", "2454"],
            "industry_code": ["24", "24"],
            "industry_name": ["半導體業", "半導體業"],
        }
    )
    results = {"a_breakout": _make_screener_df(["2330", "2454"], [0.0, 0.0], "a_breakout")}
    _, members = group_stocks(
        results, history, pl.DataFrame(), industry_df=industry, min_group_size=2,
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row["low_20d"] == pytest.approx(100.0)
    assert row["high_20d"] == pytest.approx(180.0)
    assert row["low_60d"] == pytest.approx(50.0)
    assert row["high_60d"] == pytest.approx(200.0)


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


def test_group_stocks_vol_ratio_window_capped():
    """量比均量視窗截斷到 vol_lookback：餵 40 天歷史也只取最近 20 天均量（與輸入列數脫鉤）。"""
    from datetime import date, timedelta

    base = date(2026, 1, 1)
    # 41 筆：最舊 20 天量=5000、其次 20 天量=1000、最後一天(今日)=2000
    dates = [base + timedelta(days=i) for i in range(41)]
    volumes = [5000] * 20 + [1000] * 20 + [2000]
    volume_history = _make_volume_history(["2330"] * 41, dates, volumes)
    results = {"a_breakout": _make_screener_df(["2330", "2454"], [3.5, 2.8], "a_breakout")}

    # 預設 vol_lookback=20 → 近 20 日均量=1000 → ratio = 2000/1000 = 2.0
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, volume_history=volume_history,
    )
    row = members.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row["vol_ratio"] == pytest.approx(2.0)

    # vol_lookback=40 → 全 40 日均量=(20*5000+20*1000)/40=3000 → ratio≈0.667（證明參數生效）
    _, members40 = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, volume_history=volume_history,
        vol_lookback=40,
    )
    row40 = members40.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row40["vol_ratio"] == pytest.approx(2000 / 3000)


def test_group_stocks_inst_missing_flag():
    """法人 join 不到 → inst_missing=True 且 numeric 仍填 0（評分/排名不變）；有資料 → False。"""
    results = {"a_breakout": _make_screener_df(["2330", "2454"], [3.0, 2.0], "a_breakout")}
    institutional = _make_institutional(["2330"], [1000])  # 只有 2330 有法人快取
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=institutional,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["inst_missing"] is False
    assert m["2330"]["inst_net"] == pytest.approx(1000.0)
    assert m["2454"]["inst_missing"] is True
    assert m["2454"]["inst_net"] == 0.0  # 缺漏仍填 0，下游排名/家數比行為不變


def test_group_stocks_inst_missing_when_no_institutional():
    """完全無法人資料 → 所有成員 inst_missing=True。"""
    results = {"a_breakout": _make_screener_df(["2330", "2454"], [3.0, 2.0], "a_breakout")}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    for r in members.iter_rows(named=True):
        assert r["inst_missing"] is True


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


# ─── 策略 G：MA60 斜率 + 拉回 setup 過濾 ──────────────────────────────────────

from datetime import date as _date, timedelta as _timedelta  # noqa: E402

# 80 日：先漲 70 日（100→169）再拉回 10 日（→149）。
# MA60_today=146.75、MA60_10前=139.50 → 斜率 +5.2%；close 149 → 距季線 +1.53%（帶內）
_PULLBACK_CLOSES = [100.0 + i for i in range(70)] + [169.0 - 2 * (i + 1) for i in range(10)]
# 80 日純漲（100→179）：close 179、MA60_today=149.5 → 距季線 +19.7%（延伸、出帶）
_EXTENDED_CLOSES = [100.0 + i for i in range(80)]


def _ma_price_history(closes_map: dict[str, list[float]]) -> pl.DataFrame:
    rows_sid, rows_date, rows_close = [], [], []
    for sid, closes in closes_map.items():
        for i, c in enumerate(closes):
            rows_sid.append(sid)
            rows_date.append(_date(2026, 1, 1) + _timedelta(days=i))
            rows_close.append(float(c))
    return pl.DataFrame(
        {"stock_id": rows_sid, "date": rows_date, "close": rows_close},
        schema={"stock_id": pl.Utf8, "date": pl.Date, "close": pl.Float64},
    )


def _g_screener(close_map: dict[str, float]) -> pl.DataFrame:
    sids = list(close_map.keys())
    return pl.DataFrame(
        {
            "stock_id": sids,
            "name": [f"公司{s}" for s in sids],
            "close": [close_map[s] for s in sids],
            "change_pct": [0.0] * len(sids),
            "amount_million": [1000.0] * len(sids),
            "goodinfo_url": [f"http://g/{s}" for s in sids],
            "strategy_id": ["g_growth_pullback"] * len(sids),
        }
    )


def _flat_volume(stock_ids: list[str], today_vol: dict[str, int]) -> pl.DataFrame:
    """21 日量：前 20 日皆 1000，最後一日為 today_vol[sid]（控制 vol_ratio）。"""
    rows_sid, rows_date, rows_vol = [], [], []
    for sid in stock_ids:
        for i in range(20):
            rows_sid.append(sid)
            rows_date.append(_date(2026, 4, 1) + _timedelta(days=i))
            rows_vol.append(1000)
        rows_sid.append(sid)
        rows_date.append(_date(2026, 4, 21))
        rows_vol.append(today_vol[sid])
    return pl.DataFrame(
        {"stock_id": rows_sid, "date": rows_date, "trade_volume": rows_vol},
        schema={"stock_id": pl.Utf8, "date": pl.Date, "trade_volume": pl.Int64},
    )


def _eg_results(close_map: dict[str, float]) -> dict:
    """同一批股同時掛 E 與 G：in_e 恆 True → strategy_count≥1 → 不被雜訊過濾丟掉，
    可獨立驗證 G 的拉回收斂（dict key 決定 in_{sid}，strategy_id 欄不影響）。"""
    df = _g_screener(close_map)
    return {"e_growth_momentum": df, "g_growth_pullback": df}


def test_group_stocks_g_pullback_keeps_valid_setup():
    """三條件全中（季線上揚 + 乖離帶內 + 量縮）→ in_g 留 True；延伸股 → False。"""
    price_history = _ma_price_history(
        {"2330": _PULLBACK_CLOSES, "2454": _EXTENDED_CLOSES}
    )
    results = _eg_results({"2330": 149.0, "2454": 179.0})  # 同掛 E 故不被丟
    volume_history = _flat_volume(["2330", "2454"], {"2330": 800, "2454": 800})
    _, members = group_stocks(
        results,
        price_history,
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
        volume_history=volume_history,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["in_g_growth_pullback"] is True   # 回踩季線 + 量縮
    assert m["2454"]["in_g_growth_pullback"] is False  # 距季線 +19.7% 出帶（延伸）


def test_group_stocks_g_pullback_volume_expansion_excluded():
    """價格型態合格但量增（vol_ratio > 1）→ 量縮條件不過 → in_g False。"""
    price_history = _ma_price_history({"2330": _PULLBACK_CLOSES, "2454": _EXTENDED_CLOSES})
    results = _eg_results({"2330": 149.0, "2454": 179.0})
    volume_history = _flat_volume(["2330", "2454"], {"2330": 2000, "2454": 800})  # 2330 量增
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, volume_history=volume_history,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["in_g_growth_pullback"] is False  # 量比 2.0 > 1.0


def test_group_stocks_g_pullback_no_volume_data_excluded():
    """無 volume_history → vol_ratio=0、量縮無法確認 → in_g False（不亂標）。"""
    price_history = _ma_price_history({"2330": _PULLBACK_CLOSES, "2454": _EXTENDED_CLOSES})
    results = _eg_results({"2330": 149.0, "2454": 179.0})
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["in_g_growth_pullback"] is False


def test_group_stocks_drops_pure_g_universe_noise():
    """只從 G 宇宙進來、未過拉回、又不在 D/E/F 的股 → strategy_count=0 被丟出成員池。"""
    # 2330 過拉回（留）、2454 延伸不過（純 G → 丟）、3034 過拉回（留，湊滿 group）
    price_history = _ma_price_history(
        {"2330": _PULLBACK_CLOSES, "2454": _EXTENDED_CLOSES, "3034": _PULLBACK_CLOSES}
    )
    results = {"g_growth_pullback": _g_screener({"2330": 149.0, "2454": 179.0, "3034": 149.0})}
    volume_history = _flat_volume(["2330", "2454", "3034"], {"2330": 800, "2454": 800, "3034": 800})
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, volume_history=volume_history,
    )
    ids = members["stock_id"].to_list()
    assert "2330" in ids and "3034" in ids   # 有效 G，留下
    assert "2454" not in ids                  # 純宇宙雜訊，丟出
    assert (members["strategy_count"] > 0).all()


def test_group_stocks_no_g_column_no_error():
    """results 無 G key → 不產生 in_g 欄、不報錯。"""
    results = {
        "e_growth_momentum": _make_screener_df(["2330", "2454"], [3.5, 2.8], "e_growth_momentum")
    }
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    assert "in_g_growth_pullback" not in members.columns
    assert "in_e_growth_momentum" in members.columns


def test_group_stocks_ma60_slope_sign_and_null():
    """漲勢 80 日 → ma60_slope_pct > 0；歷史不足 70 日 → null。"""
    price_history = _ma_price_history(
        {"2330": _EXTENDED_CLOSES, "2454": [100.0 + i for i in range(40)]}  # 2454 僅 40 日
    )
    results = _eg_results({"2330": 179.0, "2454": 139.0})  # 同掛 E 故不被丟
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    m = {r["stock_id"]: r for r in members.iter_rows(named=True)}
    assert m["2330"]["ma60_slope_pct"] is not None and m["2330"]["ma60_slope_pct"] > 0
    assert m["2454"]["ma60_slope_pct"] is None  # 40 日 < 60+10


# ─── 電子次產業強度排名 ─────────────────────────────────────────────────────────


def test_rank_themes_splits_and_ranks():
    """次產業拆開各自排名；強主題勝過弱主題；不足 min_members 過濾。"""
    members = pl.DataFrame(
        {
            "stock_id": ["A1", "A2", "B1", "B2", "C1"],
            "momentum_5d": [8.0, 6.0, -7.0, -5.0, 20.0],
            "inst_net": [100.0, 50.0, -10.0, -20.0, 30.0],
        }
    )
    themes_long = pl.DataFrame(
        {
            "stock_id": ["A1", "A2", "B1", "B2", "C1"],
            "theme": ["IC設計", "IC設計", "記憶體模組", "記憶體模組", "矽智財"],
            "kind": ["次產業"] * 5,
        }
    )
    from tw_screener.analysis.grouping import rank_themes
    out = rank_themes(members, themes_long, min_members=2)
    themes = out["theme"].to_list()
    assert "矽智財" not in themes             # 只 1 檔 → 過濾
    assert themes[0] == "IC設計"              # 中位 +7 > 記憶體模組 -6 → 排前
    ic = out.filter(pl.col("theme") == "IC設計").to_dicts()[0]
    assert ic["up_count"] == 2               # 兩檔皆漲
    assert ic["kind"] == "次產業"
    mem = out.filter(pl.col("theme") == "記憶體模組").to_dicts()[0]
    assert mem["score"] < ic["score"]        # 弱主題分數較低


def test_rank_themes_multilabel_contributes_to_each():
    """一檔屬兩主題 → 兩主題各算入它（多標籤 long table 各貢獻一列）。"""
    members = pl.DataFrame({"stock_id": ["X", "Y", "Z"], "momentum_5d": [10.0, 10.0, 10.0]})
    themes_long = pl.DataFrame(
        {
            "stock_id": ["X", "Y", "X", "Z"],
            "theme": ["AI", "AI", "散熱", "散熱"],
            "kind": ["概念股"] * 4,
        }
    )
    from tw_screener.analysis.grouping import rank_themes
    out = rank_themes(members, themes_long, concept_min_members=2)
    counts = {r["theme"]: r["members_count"] for r in out.iter_rows(named=True)}
    assert counts["AI"] == 2                  # X, Y
    assert counts["散熱"] == 2                 # X（同時貢獻兩主題）, Z


def test_rank_themes_lead_score_favours_breadth_over_momentum():
    """領先分數 vs 漲幅分數分流：外資/量能 breadth 高但漲幅落後者，lead_score 應勝。"""
    members = pl.DataFrame(
        {
            "stock_id": ["M1", "M2", "M3", "U1", "U2", "U3"],
            "momentum_5d": [-1.0, -2.0, -1.5, 8.0, 7.0, 9.0],
            "inst_net": [100.0, 200.0, 50.0, -10.0, -20.0, 5.0],
            "foreign_net": [100.0, 200.0, 150.0, -10.0, -20.0, -5.0],
            "vol_ratio": [1.8, 2.0, 1.6, 0.5, 0.6, 0.4],
        }
    )
    themes_long = pl.DataFrame(
        {
            "stock_id": ["M1", "M2", "M3", "U1", "U2", "U3"],
            "theme": ["記憶體", "記憶體", "記憶體", "驅動IC", "驅動IC", "驅動IC"],
            "kind": ["次產業"] * 6,
        }
    )
    from tw_screener.analysis.grouping import rank_themes

    out = rank_themes(members, themes_long, vol_surge_ratio=1.5)
    d = {r["theme"]: r for r in out.iter_rows(named=True)}
    assert d["記憶體"]["foreign_buy_count"] == 3      # 外資全買
    assert d["記憶體"]["vol_surge_count"] == 3        # 量比皆 ≥ 1.5
    assert d["記憶體"]["lead_score"] > d["驅動IC"]["lead_score"]  # 領先鏡頭：記憶體勝
    assert d["記憶體"]["score"] < d["驅動IC"]["score"]            # 漲幅鏡頭：驅動IC勝（分流）


def test_attach_rank_delta():
    """ΔRank = 上週 radar_rank − 本週；無上週快照或新題材 → null。"""
    from tw_screener.analysis.grouping import attach_rank_delta

    radar = pl.DataFrame(
        {"theme": ["記憶體", "驅動IC"], "radar_rank": [1, 2], "lead_score": [80.0, 40.0]}
    )
    prev = pl.DataFrame({"theme": ["驅動IC", "記憶體"], "radar_rank": [1, 2]})
    dd = {r["theme"]: r["rank_delta"] for r in attach_rank_delta(radar, prev).iter_rows(named=True)}
    assert dd["記憶體"] == 1       # 上週 2 → 本週 1，升 1
    assert dd["驅動IC"] == -1      # 上週 1 → 本週 2，降 1
    assert attach_rank_delta(radar, None)["rank_delta"].to_list() == [None, None]
    new = pl.DataFrame({"theme": ["新題材"], "radar_rank": [1], "lead_score": [50.0]})
    assert attach_rank_delta(new, prev)["rank_delta"].to_list() == [None]


def test_rank_themes_concept_min_members_filter():
    """門檻依 kind 分流：概念股 2 檔在 concept_min_members=3 下過濾，次產業 2 檔保留。"""
    members = pl.DataFrame({"stock_id": ["A", "B"], "momentum_5d": [5.0, 5.0]})
    themes_long = pl.DataFrame(
        {
            "stock_id": ["A", "B", "A", "B"],
            "theme": ["IC設計", "IC設計", "AI", "AI"],
            "kind": ["次產業", "次產業", "概念股", "概念股"],
        }
    )
    from tw_screener.analysis.grouping import rank_themes
    out = rank_themes(members, themes_long, min_members=2, concept_min_members=3)
    themes = out["theme"].to_list()
    assert "IC設計" in themes                  # 次產業 2 檔 ≥ 2 → 保留
    assert "AI" not in themes                  # 概念股 2 檔 < 3 → 過濾


def test_rank_themes_empty_without_themes():
    from tw_screener.analysis.grouping import rank_themes
    members = pl.DataFrame({"stock_id": ["A"], "momentum_5d": [1.0]})
    assert rank_themes(members, pl.DataFrame()).is_empty()
