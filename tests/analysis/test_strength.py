"""analysis/strength.py 共用純函式測試（docs/proposals/04 A5）。"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.analysis.strength import clipped_daily_returns

D0 = date(2026, 6, 1)
D1 = date(2026, 6, 2)
D2 = date(2026, 6, 3)


def _price(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )


def test_clipped_daily_returns_basic_and_null_drop():
    df = clipped_daily_returns(
        _price([("A", D0, 100.0), ("A", D1, 110.0), ("A", D2, 99.0)])
    )
    # 首日（無前一日）報酬 null 被剔除 → 只剩兩列
    assert df.height == 2
    assert df.columns == ["date", "stock_id", "close", "_ret"]
    rets = df.sort("date")["_ret"].to_list()
    assert rets[0] == 0.10  # 100→110
    assert abs(rets[1] - (-0.1)) < 1e-9  # 110→99 = −10%（恰在夾限內）


def test_clipped_daily_returns_clips_extreme():
    # −74% 單日（如減資事件）被夾到 −10%；clip=0 則保留原值
    rows = [("A", D0, 100.0), ("A", D1, 26.0)]
    clipped = clipped_daily_returns(_price(rows))["_ret"].to_list()
    assert clipped == [-0.10]
    raw = clipped_daily_returns(_price(rows), clip_daily_return_pct=0)["_ret"].to_list()
    assert abs(raw[0] - (-0.74)) < 1e-9


def test_clipped_daily_returns_per_stock_shift():
    # shift 以 stock_id 分區：B 首日不沿用 A 的尾值
    df = clipped_daily_returns(
        _price([("A", D0, 100.0), ("A", D1, 105.0), ("B", D0, 50.0), ("B", D1, 55.0)])
    )
    b_first = df.filter((pl.col("stock_id") == "B") & (pl.col("date") == D1))["_ret"].item()
    assert abs(b_first - 0.10) < 1e-9
