"""策略回測閉環測試（規劃書 03 V1）。

合成 reports/ 入選快照 + 價格序列，驗算 entry/exit/除息/大盤超額/未到期/下市分流。
不打網、不依賴真實快取。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.backtest.strategies import (
    compute_forward_returns,
    load_historical_screens,
    render_backtest_report,
    strategy_summary,
)


def _write_screen(tmp_path, week_tag: str, screened_at: str, rows: list[dict]) -> None:
    d = tmp_path / week_tag
    d.mkdir(parents=True, exist_ok=True)
    for strat in {r["strategy_id"] for r in rows}:
        sub = [r for r in rows if r["strategy_id"] == strat]
        pl.DataFrame(
            [
                {
                    "stock_id": r["stock_id"],
                    "name": r.get("name", r["stock_id"]),
                    "market": "上市",
                    "close": r.get("close", 100.0),
                    "change_pct": 0.0,
                    "strategy_id": r["strategy_id"],
                    "screened_at": screened_at,
                    "goodinfo_url": "x",
                }
                for r in sub
            ]
        ).write_csv(d / f"screen_result_{strat}.csv")


def _price_series(stock_id: str, start: date, closes: list[float]) -> pl.DataFrame:
    # 連續工作日（含週末也無妨，只是當交易日用）的日線
    import datetime as _dt

    dates = []
    d = start
    while len(dates) < len(closes):
        if d.weekday() < 5:  # 跳過週末，模擬交易日
            dates.append(d)
        d += _dt.timedelta(days=1)
    return pl.DataFrame(
        {"date": dates, "stock_id": [stock_id] * len(closes), "close": closes}
    )


# ─── load_historical_screens ──────────────────────────────────────────────────


def test_load_historical_screens_merges_weeks_with_tags(tmp_path):
    _write_screen(tmp_path, "2026-W21", "2026-05-22", [
        {"stock_id": "2330", "strategy_id": "d_quality_leader"},
    ])
    _write_screen(tmp_path, "2026-W22", "2026-05-28", [
        {"stock_id": "2330", "strategy_id": "d_quality_leader"},
        {"stock_id": "1477", "strategy_id": "f_value_rebound"},
    ])
    out = load_historical_screens(tmp_path)
    assert set(out["week_tag"].unique()) == {"2026-W21", "2026-W22"}
    assert out.schema["stock_id"] == pl.Utf8
    assert out.schema["screened_at"] == pl.Date
    assert out.height == 3


def test_load_historical_screens_keeps_leading_zero_ids(tmp_path):
    _write_screen(tmp_path, "2026-W21", "2026-05-22", [
        {"stock_id": "0050", "strategy_id": "d_quality_leader"},
    ])
    out = load_historical_screens(tmp_path)
    assert out["stock_id"].to_list() == ["0050"]


def test_load_historical_screens_empty_dir(tmp_path):
    out = load_historical_screens(tmp_path)
    assert out.is_empty()
    assert "week_tag" in out.columns


# ─── compute_forward_returns ──────────────────────────────────────────────────


def test_forward_return_basic_entry_next_day_no_lookahead():
    # screened_at=週一；entry 應為週二（次一交易日），不可用入選當日
    screens = pl.DataFrame(
        {
            "week_tag": ["2026-W21"],
            "screened_at": [date(2026, 5, 4)],  # 週一
            "stock_id": ["2330"],
            "name": ["台積電"],
            "close": [100.0],
            "change_pct": [0.0],
            "strategy_id": ["d_quality_leader"],
        }
    )
    # 週一100 → 週二110(entry) → ... → 第2交易日後(hold_weeks=0? 用1週=5日)
    px = _price_series("2330", date(2026, 5, 4), [100, 110, 111, 112, 113, 114, 121])
    out = compute_forward_returns(screens, px, hold_weeks=1, trading_days_per_week=5)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["entry_date"] == date(2026, 5, 5)  # 次一交易日
    assert row["entry_price"] == 110.0
    # entry(週二, idx1=110) + 5 交易日 = idx6 = 121
    assert row["exit_price"] == 121.0
    assert abs(row["return_pct"] - (121 - 110) / 110 * 100) < 1e-6
    assert row["status"] == "matured"


def test_forward_return_immature_excluded():
    # 市場資料在 entry 後不足 hold_days → 未到期 → 排除（不入表）
    screens = pl.DataFrame(
        {
            "week_tag": ["2026-W26"],
            "screened_at": [date(2026, 6, 1)],
            "stock_id": ["2330"],
            "name": ["台積電"],
            "close": [100.0],
            "change_pct": [0.0],
            "strategy_id": ["d_quality_leader"],
        }
    )
    px = _price_series("2330", date(2026, 6, 1), [100, 101, 102])  # 只有 3 日
    out = compute_forward_returns(screens, px, hold_weeks=2, trading_days_per_week=5)
    assert out.is_empty()


def test_forward_return_delisted_marks_null_not_zero():
    # 大盤夠長（用另一檔撐市場日曆），但目標股序列提早結束 → 下市 → exit null
    target = _price_series("9999", date(2026, 5, 4), [100, 110, 111])  # 早停
    market_filler = _price_series("0050", date(2026, 5, 4), [50] * 20)  # 撐滿日曆
    px = pl.concat([target, market_filler])
    screens = pl.DataFrame(
        {
            "week_tag": ["2026-W21"],
            "screened_at": [date(2026, 5, 4)],
            "stock_id": ["9999"],
            "name": ["下市股"],
            "close": [100.0],
            "change_pct": [0.0],
            "strategy_id": ["d_quality_leader"],
        }
    )
    out = compute_forward_returns(screens, px, hold_weeks=1, trading_days_per_week=5)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["status"] == "delisted"
    assert row["exit_price"] is None
    assert row["return_pct"] is None


def test_forward_return_dividend_addback():
    # ex_date 落在 (entry, exit] → 現金股利加回；缺口本身壓低價、加回還原總報酬
    screens = pl.DataFrame(
        {
            "week_tag": ["2026-W21"],
            "screened_at": [date(2026, 5, 4)],
            "stock_id": ["2330"],
            "name": ["台積電"],
            "close": [100.0],
            "change_pct": [0.0],
            "strategy_id": ["d_quality_leader"],
        }
    )
    # entry=週二 idx1=100；持有 5 日 → exit idx6=95（除息缺口 5 元壓低）
    px = _price_series("2330", date(2026, 5, 4), [100, 100, 99, 98, 97, 96, 95])
    divs = pl.DataFrame(
        {"stock_id": ["2330"], "ex_date": [date(2026, 5, 7)], "cash_dividend": [5.0]}
    )
    out = compute_forward_returns(
        screens, px, hold_weeks=1, dividends=divs, trading_days_per_week=5
    )
    row = out.row(0, named=True)
    # 純價報酬 (95-100)/100 = -5%；加回 5/100=+5% → 約 0%
    assert abs(row["return_pct"] - 0.0) < 1e-6


def test_forward_return_excess_vs_market():
    # 目標股漲 10%；眾多持平股稀釋等權大盤 → 大盤微漲、超額 = 報酬 − 大盤（恆等式）。
    target = _price_series("2330", date(2026, 5, 4), [100, 100, 101, 102, 103, 104, 110])
    px = pl.concat(
        [target]
        + [_price_series(f"flat{i}", date(2026, 5, 4), [50] * 7) for i in range(20)]
    )
    screens = pl.DataFrame(
        {
            "week_tag": ["2026-W21"],
            "screened_at": [date(2026, 5, 4)],
            "stock_id": ["2330"],
            "name": ["台積電"],
            "close": [100.0],
            "change_pct": [0.0],
            "strategy_id": ["d_quality_leader"],
        }
    )
    out = compute_forward_returns(screens, px, hold_weeks=1, trading_days_per_week=5)
    row = out.row(0, named=True)
    assert abs(row["return_pct"] - 10.0) < 1e-6
    # 等權大盤含目標股本身 → 微漲、但遠小於個股；超額為兩者之差（恆等式）
    assert 0.0 < row["market_return_pct"] < row["return_pct"]
    assert abs(row["excess_return_pct"] - (row["return_pct"] - row["market_return_pct"])) < 1e-6


# ─── strategy_summary ─────────────────────────────────────────────────────────


def test_strategy_summary_stats():
    returns = pl.DataFrame(
        {
            "week_tag": ["w"] * 4,
            "stock_id": ["a", "b", "c", "d"],
            "name": ["a", "b", "c", "d"],
            "strategy_id": ["f_value_rebound"] * 4,
            "hold_weeks": [2] * 4,
            "screened_at": [date(2026, 5, 4)] * 4,
            "entry_date": [date(2026, 5, 5)] * 4,
            "exit_date": [date(2026, 5, 12)] * 4,
            "entry_price": [100.0] * 4,
            "exit_price": [110.0, 90.0, 120.0, 105.0],
            "return_pct": [10.0, -10.0, 20.0, 5.0],
            "market_return_pct": [2.0, 2.0, 2.0, 2.0],
            "excess_return_pct": [8.0, -12.0, 18.0, 3.0],
            "status": ["matured"] * 4,
        }
    )
    s = strategy_summary(returns).row(0, named=True)
    assert s["sample_count"] == 4
    assert abs(s["win_rate"] - 0.75) < 1e-9  # 3/4 正
    assert abs(s["avg_return_pct"] - 6.25) < 1e-9
    assert abs(s["median_return_pct"] - 7.5) < 1e-9
    assert abs(s["max_drawdown_pct"] - (-10.0)) < 1e-9  # 最差單檔
    assert abs(s["win_rate_vs_market"] - 0.75) < 1e-9  # 3/4 超額為正


def test_strategy_summary_delisted_counted_not_in_stats():
    returns = pl.DataFrame(
        {
            "week_tag": ["w", "w"],
            "stock_id": ["a", "b"],
            "name": ["a", "b"],
            "strategy_id": ["d_quality_leader"] * 2,
            "hold_weeks": [2] * 2,
            "screened_at": [date(2026, 5, 4)] * 2,
            "entry_date": [date(2026, 5, 5)] * 2,
            "exit_date": [date(2026, 5, 12), None],
            "entry_price": [100.0, 100.0],
            "exit_price": [110.0, None],
            "return_pct": [10.0, None],
            "market_return_pct": [2.0, None],
            "excess_return_pct": [8.0, None],
            "status": ["matured", "delisted"],
        }
    )
    s = strategy_summary(returns).row(0, named=True)
    assert s["sample_count"] == 1  # 下市不計入勝率
    assert s["n_delisted"] == 1
    assert abs(s["win_rate"] - 1.0) < 1e-9


def test_strategy_summary_empty():
    out = strategy_summary(pl.DataFrame())
    assert out.is_empty()
    assert "win_rate" in out.columns


# ─── render_backtest_report ───────────────────────────────────────────────────


def test_render_report_flags_small_sample():
    summary = pl.DataFrame(
        {
            "strategy_id": ["f_value_rebound"],
            "hold_weeks": [2],
            "sample_count": pl.Series([5], dtype=pl.UInt32),
            "n_delisted": pl.Series([0], dtype=pl.UInt32),
            "win_rate": [0.8],
            "avg_return_pct": [5.0],
            "median_return_pct": [4.0],
            "max_drawdown_pct": [-3.0],
            "avg_excess_pct": [2.0],
            "median_excess_pct": [1.5],
            "win_rate_vs_market": [0.6],
        }
    )
    md = render_backtest_report(
        summary, pl.DataFrame(), (date(2026, 1, 1), date(2026, 6, 26)), 6, min_sample_warn=20
    )
    assert "F 價值回升" in md
    assert "⚠️" in md  # 樣本 5 < 20 → 警示
    assert "方向性" in md


def test_render_report_empty_summary():
    md = render_backtest_report(
        pl.DataFrame(), pl.DataFrame(), (None, None), 0, min_sample_warn=20
    )
    assert "無可統計樣本" in md
