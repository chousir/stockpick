"""WS-D laggard_grid 單元測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.laggard_grid import laggard_cell_grid, stock_rs_vs_group

_D0 = date(2026, 1, 5)


def test_stock_rs_vs_group_basket_diff() -> None:
    """rs_subind＝個股窗報酬 − 籃等權窗報酬（pp）。"""
    days = [_D0 + timedelta(days=i) for i in range(3)]
    # A：100→110（+10%）；B：100→120（+20%）→ 籃 +15%；rs_A=−5pp、rs_B=+5pp
    panel = pl.DataFrame(
        {
            "date": days * 2,
            "stock_id": ["1111"] * 3 + ["2222"] * 3,
            "close": [100.0, 105.0, 110.0, 100.0, 110.0, 120.0],
        }
    )
    membership = pl.DataFrame(
        {"sub_industry": ["甲", "甲"], "stock_id": ["1111", "2222"]}
    )
    rs = stock_rs_vs_group(panel, membership, window=2)
    rows = {r["stock_id"]: r for r in rs.filter(pl.col("date") == days[2]).iter_rows(named=True)}
    assert abs(rows["1111"]["rs_subind"] - (-5.0)) < 1e-9
    assert abs(rows["2222"]["rs_subind"] - 5.0) < 1e-9


def test_laggard_cell_grid_dimensions_and_baseline() -> None:
    """cell 維度標籤正確、全體基準列存在、n 加總守恆。"""
    n_days, n_stocks = 30, 30
    days = [_D0 + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(days):
        for s in range(n_stocks):
            rows.append(
                {
                    "date": d,
                    "stock_id": f"s{s:03d}",
                    "sub_industry": "甲" if s < 15 else "乙",
                    "rs_subind": -1.0 if s % 2 == 0 else 1.0,
                    "trend_score": 80.0 if s < 15 else 20.0,
                    "ma60_dist_pct": [5.0, 12.0, 30.0][s % 3],
                    "alpha20": 1.0 * (s % 5) - 1.0 + 0.01 * i,
                }
            )
    df = pl.DataFrame(rows)
    grid = laggard_cell_grid(df, horizons=(20,), tier_edges=(10.0, 15.0), n_boot=100)
    cells = grid.filter(pl.col("group_strength") != "（全體）")
    # 2×2×3＝12 cells 全在
    assert cells.height == 12
    assert set(cells["tier"].to_list()) == {"貼低≤10", "中段10–15", "延伸>15"}
    baseline = grid.filter(pl.col("group_strength") == "（全體）")
    assert baseline.height == 1
    assert int(cells["n"].sum()) == baseline.row(0, named=True)["n"]
    # 前後半段欄有值
    r0 = cells.row(0, named=True)
    assert r0["mean_h1"] is not None and r0["mean_h2"] is not None


def test_laggard_cell_grid_null_inputs_dropped() -> None:
    df = pl.DataFrame(
        {
            "date": [_D0] * 3,
            "rs_subind": [None, -1.0, 1.0],
            "trend_score": [50.0, None, 60.0],
            "ma60_dist_pct": [5.0, 5.0, None],
            "alpha20": [1.0, 1.0, 1.0],
        }
    )
    grid = laggard_cell_grid(df, horizons=(20,), n_boot=100)
    assert grid.is_empty() or grid["n"].sum() == 0  # 全列有 null → 沒有可用樣本
