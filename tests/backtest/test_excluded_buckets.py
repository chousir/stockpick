"""M6 excluded 分桶回饋帳＋決策卡「上週帳」一行測試（委託書 M6）。"""

from __future__ import annotations

import polars as pl

from tw_screener.backtest.picks_outcome import (
    excluded_bucket_ledger,
    render_excluded_buckets,
    weekly_ledger_line,
    worst_bucket,
)


def _ret(rows: list[dict]) -> pl.DataFrame:
    """最小化的 compute_forward_returns 輸出（只留本模組讀的欄）。"""
    base = {"status": "matured", "return_pct": 0.0, "excess_return_pct": 0.0}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_gap_is_measured_against_same_window_picks():
    """桶平均 +2% 看似「漏掉」，但同窗 picks +5% → gap 為負＝剔除是對的。"""
    picks = _ret([{"strategy_id": "core", "return_pct": 5.0}])
    excl = _ret([
        {"strategy_id": "價格已跌", "return_pct": 2.0},
        {"strategy_id": "土洋對作", "return_pct": 9.0},
    ])
    led = excluded_bucket_ledger({5: (picks, excl)})
    got = {r["reason"]: r["gap_pp"] for r in led.iter_rows(named=True)}
    assert got["價格已跌"] == -3.0
    assert got["土洋對作"] == 4.0


def test_worst_bucket_is_largest_gap_not_largest_return():
    picks = _ret([{"strategy_id": "core", "return_pct": 5.0}])
    excl = _ret([
        {"strategy_id": "價格已跌", "return_pct": 8.0},
        {"strategy_id": "高PE", "return_pct": 7.0},
    ])
    led = excluded_bucket_ledger({5: (picks, excl)})
    assert worst_bucket(led, 5)["reason"] == "價格已跌"


def test_worst_bucket_none_when_no_picks_baseline():
    """picks 無到期樣本 → gap 無從計算，必須回 None（不可回 gap=桶平均）。"""
    excl = _ret([{"strategy_id": "價格已跌", "return_pct": 8.0}])
    led = excluded_bucket_ledger({5: (pl.DataFrame(), excl)})
    assert led.height == 1
    assert led["gap_pp"][0] is None
    assert worst_bucket(led, 5) is None


def test_ledger_counts_beat_market_separately():
    picks = _ret([{"strategy_id": "core", "return_pct": 1.0}])
    excl = _ret([
        {"strategy_id": "價格已跌", "return_pct": 8.0, "excess_return_pct": 3.0},
        {"strategy_id": "價格已跌", "return_pct": -2.0, "excess_return_pct": -5.0},
    ])
    led = excluded_bucket_ledger({5: (picks, excl)})
    assert led["n"][0] == 2 and led["n_beat_market"][0] == 1


def test_only_matured_rows_count():
    picks = _ret([{"strategy_id": "core", "return_pct": 1.0}])
    excl = _ret([
        {"strategy_id": "價格已跌", "return_pct": 8.0},
        {"strategy_id": "價格已跌", "return_pct": 99.0, "status": "pending"},
    ])
    led = excluded_bucket_ledger({5: (picks, excl)})
    assert led["n"][0] == 1 and led["avg_return_pct"][0] == 8.0


def test_two_horizons_produce_two_row_sets():
    picks = _ret([{"strategy_id": "core", "return_pct": 1.0}])
    excl = _ret([{"strategy_id": "價格已跌", "return_pct": 4.0}])
    led = excluded_bucket_ledger({5: (picks, excl), 20: (picks, excl)})
    assert sorted(led["horizon_td"].to_list()) == [5, 20]


# ── 決策卡「上週帳」一行 ──────────────────────────────────────────────────


def test_weekly_ledger_line_has_all_three_cells():
    picks = _ret([
        {"strategy_id": "core", "return_pct": 3.0},
        {"strategy_id": "opportunity", "return_pct": 1.0},
    ])
    excl = _ret([{"strategy_id": "價格已跌", "return_pct": 9.0, "excess_return_pct": 4.0}])
    led = excluded_bucket_ledger({5: (picks, excl)})
    line = weekly_ledger_line(picks, led, {"avg_cost_pct": -1.5, "n_measured": 3})
    assert line.startswith("**上週帳**：")
    assert "picks r+5 中位 +2.0%" in line
    assert "「價格已跌」" in line and "漏 1/1 檔" in line
    assert "停損延遲成本 -1.50%" in line


def test_weekly_ledger_line_degrades_each_cell_independently():
    """三格各自降級為「未取得」——缺資料不編數字，也不讓整行消失。"""
    line = weekly_ledger_line(pl.DataFrame(), pl.DataFrame(), None)
    assert line.count("未取得") == 3
    # 有 picks 但無 excluded、無停損帳 → 只有後兩格未取得
    picks = _ret([{"strategy_id": "core", "return_pct": 3.0}])
    line2 = weekly_ledger_line(picks, pl.DataFrame(), {"avg_cost_pct": None})
    assert "picks r+5 中位 +3.0%" in line2
    assert line2.count("未取得") == 2


def test_render_states_absence_explicitly():
    """缺席要明寫「回饋帳缺席」，不可整段消失（消失沒人會發現）。"""
    body = "\n".join(render_excluded_buckets(pl.DataFrame()))
    assert "回饋帳缺席" in body


def test_render_lists_both_horizons_with_baseline_column():
    picks = _ret([{"strategy_id": "core", "return_pct": 1.0}])
    excl = _ret([{"strategy_id": "價格已跌", "return_pct": 4.0}])
    led = excluded_bucket_ledger({5: (picks, excl), 20: (picks, excl)})
    body = "\n".join(render_excluded_buckets(led, (5, 20)))
    assert "| r+5 | 價格已跌 |" in body
    assert "| r+20 | 價格已跌 |" in body
    assert "同窗 picks" in body


def test_render_names_horizons_that_have_not_matured():
    """r+20 還沒到期時整窗會消失——必須明寫「尚無到期樣本」而非默默少一段。"""
    picks = _ret([{"strategy_id": "core", "return_pct": 1.0}])
    excl = _ret([{"strategy_id": "價格已跌", "return_pct": 4.0}])
    led = excluded_bucket_ledger({5: (picks, excl), 20: (pl.DataFrame(), pl.DataFrame())})
    body = "\n".join(render_excluded_buckets(led, (5, 20)))
    assert "r+20 該窗尚無到期樣本" in body
    assert "非「無偽陰性」" in body


def test_ledger_line_says_why_stop_cost_is_absent():
    """0 筆可量測多半是停損欄寫成「跌破季線」——不寫出來沒人會去改（M3／patch-2）。"""
    line = weekly_ledger_line(
        pl.DataFrame(), pl.DataFrame(),
        {"avg_cost_pct": None, "counts": {"unparsed": 9, "not_triggered": 4}},
    )
    assert "13 筆中 0 筆可量測" in line
