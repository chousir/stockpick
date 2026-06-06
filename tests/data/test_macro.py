"""總經行事曆 macro_calendar 載入／過濾測試（比照 dividend calendar）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.data.macro import filter_macro_calendar, load_macro_calendar

_COLS = {"date", "name", "category", "severity", "verified", "note"}


def _sample() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2026, 6, 10), date(2026, 6, 17), date(2026, 7, 28)],
            "name": ["CPI", "FOMC", "FOMC2"],
            "category": ["CPI", "FOMC", "FOMC"],
            "severity": ["高", "高", "高"],
            "verified": [False, False, False],
            "note": ["", "", ""],
        },
        schema={
            "date": pl.Date,
            "name": pl.Utf8,
            "category": pl.Utf8,
            "severity": pl.Utf8,
            "verified": pl.Boolean,
            "note": pl.Utf8,
        },
    )


def test_filter_macro_window():
    """30 天窗只留窗內事件、依日期排序；窗外（7/28）剔除。"""
    out = filter_macro_calendar(_sample(), date(2026, 6, 6), 30)
    assert out["date"].to_list() == [date(2026, 6, 10), date(2026, 6, 17)]


def test_filter_macro_excludes_past():
    """today 之前的事件不入窗（6/10 排除；60 天窗涵蓋 6/17 與 7/28）。"""
    out = filter_macro_calendar(_sample(), date(2026, 6, 12), 60)
    assert out["date"].to_list() == [date(2026, 6, 17), date(2026, 7, 28)]


def test_filter_macro_empty():
    out = filter_macro_calendar(pl.DataFrame(), date(2026, 6, 6), 30)
    assert out.is_empty()


def test_load_macro_missing_file():
    """檔不存在 → 回空 DataFrame（不報錯），欄位齊全。"""
    df = load_macro_calendar(Path("config/__no_such_macro__.yaml"))
    assert df.is_empty()
    assert _COLS <= set(df.columns)


def test_load_macro_real_file():
    """專案內建 config/macro_calendar.yaml 可解析、日期為 pl.Date、欄位齊全。"""
    df = load_macro_calendar()
    assert not df.is_empty()
    assert _COLS <= set(df.columns)
    assert df.schema["date"] == pl.Date
    # 排序遞增
    dates = df["date"].to_list()
    assert dates == sorted(dates)
