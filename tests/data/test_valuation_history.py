"""tests/data/test_valuation_history.py — 台股估值中位數累積管線單元測試（全離線，docs/25 §2.4）。

只測累積管線本身；不測驗證/計分（範圍外，見模組 docstring——資料不夠年限前沒有東西可測）。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.data.valuation_history import (
    accumulation_depth_message,
    append_valuation_history,
    daily_valuation_summary,
)

_SCHEMA = {
    "date": pl.Date,
    "stock_id": pl.Utf8,
    "market": pl.Utf8,
    "pe": pl.Float64,
    "pbr": pl.Float64,
    "dividend_yield": pl.Float64,
}


def _snapshot(day: date, rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"date": day, "market": "上市", **r} for r in rows],
        schema=_SCHEMA,
    )


# ── daily_valuation_summary ─────────────────────────────────────────────────


def test_daily_summary_excludes_nulls_from_median() -> None:
    """虧損股 PE=null 不當 0，中位數只算非 null 那幾檔。"""
    df = _snapshot(
        date(2026, 8, 1),
        [
            {"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0},
            {"stock_id": "1102", "pe": None, "pbr": 1.5, "dividend_yield": None},  # 虧損股
            {"stock_id": "1103", "pe": 20.0, "pbr": 2.0, "dividend_yield": 5.0},
        ],
    )
    summary = daily_valuation_summary(df)
    assert summary is not None
    assert summary["date"] == date(2026, 8, 1)
    assert summary["median_pe"] == 15.0  # (10+20)/2，null 排除不拉低樣本
    assert summary["n_pe"] == 2
    assert summary["median_pbr"] == 1.5  # (1.0,1.5,2.0) 三檔中位數
    assert summary["n_pbr"] == 3
    assert summary["median_dividend_yield"] == 4.0
    assert summary["n_dividend_yield"] == 2


def test_daily_summary_all_null_column_gives_none_not_zero() -> None:
    """整欄全 null（如某天全市場都缺殖利率）→ median=None／n=0，不是 0。"""
    df = _snapshot(
        date(2026, 8, 1),
        [
            {"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": None},
            {"stock_id": "1102", "pe": 20.0, "pbr": 2.0, "dividend_yield": None},
        ],
    )
    summary = daily_valuation_summary(df)
    assert summary is not None
    assert summary["median_dividend_yield"] is None
    assert summary["n_dividend_yield"] == 0


def test_daily_summary_empty_snapshot_returns_none() -> None:
    """整天完全沒抓到資料（端點掛掉）→ None，沒有日期可掛、不產列。"""
    empty = pl.DataFrame(schema=_SCHEMA)
    assert daily_valuation_summary(empty) is None


# ── append_valuation_history ────────────────────────────────────────────────


def test_append_creates_new_history_file(tmp_path) -> None:
    df = _snapshot(
        date(2026, 8, 1), [{"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0}]
    )
    hpath = tmp_path / "tw_valuation_history.parquet"
    result = append_valuation_history(df, hpath)
    assert hpath.exists()
    assert result.height == 1
    assert result["date"][0] == date(2026, 8, 1)


def test_append_accumulates_across_dates_preserving_prior_rows(tmp_path) -> None:
    hpath = tmp_path / "tw_valuation_history.parquet"
    day1 = _snapshot(
        date(2026, 8, 1), [{"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0}]
    )
    day2 = _snapshot(
        date(2026, 8, 2), [{"stock_id": "1101", "pe": 12.0, "pbr": 1.1, "dividend_yield": 2.5}]
    )
    append_valuation_history(day1, hpath)
    result = append_valuation_history(day2, hpath)
    assert result.height == 2
    assert sorted(result["date"].to_list()) == [date(2026, 8, 1), date(2026, 8, 2)]
    # 前一天的列沒被覆蓋
    row1 = result.filter(pl.col("date") == date(2026, 8, 1))
    assert row1["median_pe"][0] == 10.0


def test_append_same_date_twice_is_idempotent(tmp_path) -> None:
    """同一天重跑 fetch-twse（重試/手動多跑）不能造成累積序列裡有重複日期。"""
    hpath = tmp_path / "tw_valuation_history.parquet"
    day1_a = _snapshot(
        date(2026, 8, 1), [{"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0}]
    )
    day1_b = _snapshot(
        date(2026, 8, 1), [{"stock_id": "1101", "pe": 999.0, "pbr": 9.0, "dividend_yield": 9.0}]
    )
    append_valuation_history(day1_a, hpath)
    result = append_valuation_history(day1_b, hpath)
    assert result.height == 1
    # 第一次寫入的值保留，第二次重複日期被跳過（不覆蓋、不新增）
    assert result["median_pe"][0] == 10.0


def test_append_empty_snapshot_does_not_write_row(tmp_path) -> None:
    hpath = tmp_path / "tw_valuation_history.parquet"
    day1 = _snapshot(
        date(2026, 8, 1), [{"stock_id": "1101", "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0}]
    )
    append_valuation_history(day1, hpath)
    empty = pl.DataFrame(schema=_SCHEMA)
    result = append_valuation_history(empty, hpath)
    assert result.height == 1  # 空快照沒有新增任何列，既有那天還在


# ── accumulation_depth_message ──────────────────────────────────────────────


def test_depth_message_reports_remaining_days_not_a_signal() -> None:
    history = pl.DataFrame(
        {
            "date": [date(2026, 6, 12)],
            "median_pe": [15.0],
            "n_pe": [1000],
            "median_pbr": [1.5],
            "n_pbr": [1000],
            "median_dividend_yield": [3.0],
            "n_dividend_yield": [900],
        }
    )
    msg = accumulation_depth_message(history, target_days=750)
    assert "1 個交易日" in msg
    assert "749" in msg  # 還差 749 天
    assert "%" not in msg and "百分位" not in msg  # 不能暗示有任何計分/驗證結果


def test_depth_message_empty_history() -> None:
    empty = pl.DataFrame(
        schema={
            "date": pl.Date,
            "median_pe": pl.Float64,
            "n_pe": pl.Int64,
            "median_pbr": pl.Float64,
            "n_pbr": pl.Int64,
            "median_dividend_yield": pl.Float64,
            "n_dividend_yield": pl.Int64,
        }
    )
    assert "0 個交易日" in accumulation_depth_message(empty, target_days=750)
