"""docs/31 §20.13 Phase 1：歷史類比面板 + forward_return_percentiles（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.target_price_panel import (
    PCTILES,
    POOLED_CELL,
    assign_analog_cells,
    build_analog_panel,
    forward_return_percentiles,
)

_DAYS = [date(2022, 1, 3) + timedelta(days=i) for i in range(400)]


def _price(stock_id: str, closes: list[float], days: list[date] | None = None) -> pl.DataFrame:
    d = days or _DAYS[: len(closes)]
    return pl.DataFrame(
        {
            "date": d[: len(closes)],
            "stock_id": [stock_id] * len(closes),
            "close": closes,
            "volume": [1_000.0] * len(closes),
        }
    )


def test_assign_analog_cells_drops_nulls_and_builds_9_cells() -> None:
    rows = pl.DataFrame(
        {
            "ma60_dist_pct": [-20.0, 0.0, 20.0, None, 5.0],
            "rs_subind": [-10.0, 0.0, 10.0, 3.0, None],
        }
    )
    out = assign_analog_cells(rows, pos_edges=(-8.0, 8.0), rs_edges=(-5.0, 5.0))
    assert out.height == 3  # 兩列有 null 被丟
    assert set(out.columns) >= {"pos_bin", "rs_bin", "cell"}
    # cell = pos_bin｜rs_bin
    assert out["cell"].str.contains("｜").all()
    r0 = out.row(0, named=True)
    assert "貼低" in r0["pos_bin"] and "落後" in r0["rs_bin"]


def test_forward_return_percentiles_matches_known_distribution() -> None:
    # 單一 cell、r20 = 0..99 → P50≈49.5、P25≈24.75、P75≈74.25（linear interp）
    n = 100
    panel = pl.DataFrame(
        {
            "date": [_DAYS[i % 50] for i in range(n)],
            "stock_id": [f"{1000 + i}" for i in range(n)],
            "cell": ["A｜B"] * n,
            "r20": [float(i) for i in range(n)],
        }
    )
    out = forward_return_percentiles(panel, horizons=(20,), pctiles=PCTILES)
    cell_row = out.filter(pl.col("cell") == "A｜B").row(0, named=True)
    assert abs(cell_row["p50"] - 49.5) < 1e-6
    assert abs(cell_row["p25"] - 24.75) < 1e-6
    assert abs(cell_row["p75"] - 74.25) < 1e-6
    assert abs(cell_row["iqr"] - (74.25 - 24.75)) < 1e-6
    assert cell_row["n"] == 100
    assert cell_row["n_dates"] == 50
    # _pooled 列同分布（只有一個 cell）
    pooled_row = out.filter(pl.col("cell") == POOLED_CELL).row(0, named=True)
    assert abs(pooled_row["p50"] - cell_row["p50"]) < 1e-6


def test_forward_return_percentiles_pooled_spans_all_cells() -> None:
    panel = pl.DataFrame(
        {
            "date": [_DAYS[i % 20] for i in range(40)],
            "stock_id": [f"{2000 + i}" for i in range(40)],
            "cell": (["lo｜lo"] * 20) + (["hi｜hi"] * 20),
            "r20": ([0.0] * 20) + ([100.0] * 20),
        }
    )
    out = forward_return_percentiles(panel, horizons=(20,))
    pooled_row = out.filter(pl.col("cell") == POOLED_CELL).row(0, named=True)
    # pooled 混兩格 → P50 落中間附近，兩個 cell 各自 P50 是 0 / 100
    assert 0.0 < pooled_row["p50"] < 100.0
    assert out.filter(pl.col("cell") == "lo｜lo").row(0, named=True)["p50"] == 0.0


def test_build_analog_panel_end_to_end_shape() -> None:
    # 3 檔、同一次產業、300 天；一檔強一檔弱一檔中
    days = _DAYS[:300]
    strong = _price("1101", [100.0 * (1.003**i) for i in range(300)], days)
    weak = _price("1102", [100.0 * (0.999**i) for i in range(300)], days)
    mid = _price("1103", [100.0 * (1.0005**i) for i in range(300)], days)
    price = pl.concat([strong, weak, mid])
    membership = pl.DataFrame(
        {"sub_industry": ["水泥"] * 3, "stock_id": ["1101", "1102", "1103"]}
    )
    panel = build_analog_panel(
        price, membership, regime=None, horizons=(20,), min_rows_per_day=1
    )
    assert not panel.is_empty()
    assert {"cell", "pos_bin", "rs_bin", "r20", "rs_subind", "ma60_dist_pct"}.issubset(
        panel.columns
    )
    # 強股 rs_subind 應 > 弱股（同日）
    d = panel["date"].max()
    day = panel.filter(pl.col("date") == d)
    if day.height == 3:
        rs = {r["stock_id"]: r["rs_subind"] for r in day.iter_rows(named=True)}
        assert rs["1101"] > rs["1102"]


def test_build_analog_panel_min_rows_per_day_excludes_partial() -> None:
    days = _DAYS[:200]
    a = _price("1101", [100.0 + i for i in range(200)], days)
    # 1102 只有前 100 天有資料 → 後 100 天該日列數 = 1（<門檻 2）
    b = _price("1102", [50.0 + i for i in range(100)], days[:100])
    price = pl.concat([a, b])
    membership = pl.DataFrame({"sub_industry": ["水泥", "水泥"], "stock_id": ["1101", "1102"]})
    panel = build_analog_panel(
        price, membership, regime=None, horizons=(20,), min_rows_per_day=2
    )
    # 只有兩檔都在的日子才留下
    if not panel.is_empty():
        assert panel["date"].max() <= days[99]


def test_empty_inputs_return_empty_schema() -> None:
    out = forward_return_percentiles(pl.DataFrame(), horizons=(20,))
    assert out.is_empty()
    panel = build_analog_panel(pl.DataFrame(), pl.DataFrame(), regime=None)
    assert panel.is_empty()
