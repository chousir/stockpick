"""官方族群指數重測「族群前5」測試（docs/31 §10）。純函式合成資料，不打網、不碰真快取。"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.backtest.official_sector_grid import (
    _INDUSTRY_TO_INDEX_ALIASES,
    _UNMAPPED_INDUSTRIES,
    build_official_sector_baskets,
    build_official_sector_membership,
    official_group_rank_grid,
)


def test_unmapped_industries_not_in_alias_table() -> None:
    """§10.2 排除清單跟映射表互斥——避免文件與程式碼各說各話。"""
    assert set(_UNMAPPED_INDUSTRIES).isdisjoint(_INDUSTRY_TO_INDEX_ALIASES)


def test_build_official_sector_membership_filters_unmapped() -> None:
    official = pl.DataFrame(
        {
            "sub_industry": ["半導體業", "半導體業", "其他製造業", "存託憑證"],
            "stock_id": ["2330", "2454", "9999", "0001"],
        }
    )
    out = build_official_sector_membership(official)
    assert set(out["stock_id"].to_list()) == {"2330", "2454"}
    assert set(out["sub_industry"].to_list()) == {"半導體業"}


def test_build_official_sector_membership_empty_input() -> None:
    empty = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    assert build_official_sector_membership(empty).is_empty()


def test_build_official_sector_baskets_maps_index_name_to_canonical() -> None:
    sector_index = pl.DataFrame(
        {
            "date": [date(2026, 8, 21), date(2026, 8, 21)],
            "index_name": ["半導體類指數", "光電類指數"],
            "close_index": [500.0, 300.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    row = baskets.filter(pl.col("sub_industry") == "半導體業").row(0, named=True)
    assert row["basket_index"] == 500.0
    assert set(baskets["sub_industry"].to_list()) == {"半導體業", "光電業"}


def test_build_official_sector_baskets_rename_fallback() -> None:
    """觀光跨期改名：新名（觀光餐旅類指數）優先，查無時退回舊名（觀光類指數）。"""
    sector_index = pl.DataFrame(
        {
            "date": [date(2021, 7, 1), date(2026, 8, 21)],
            "index_name": ["觀光類指數", "觀光餐旅類指數"],
            "close_index": [100.0, 200.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    got = {
        r["date"]: r["basket_index"]
        for r in baskets.filter(pl.col("sub_industry") == "觀光事業").iter_rows(named=True)
    }
    assert got == {date(2021, 7, 1): 100.0, date(2026, 8, 21): 200.0}


def test_build_official_sector_baskets_prefers_new_name_when_both_present() -> None:
    """同一天新舊名都存在（理論邊界情況）→ 取候選清單優先序較高的新名。"""
    sector_index = pl.DataFrame(
        {
            "date": [date(2026, 8, 21), date(2026, 8, 21)],
            "index_name": ["觀光類指數", "觀光餐旅類指數"],
            "close_index": [999.0, 200.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    row = baskets.filter(pl.col("sub_industry") == "觀光事業").row(0, named=True)
    assert row["basket_index"] == 200.0


def test_build_official_sector_baskets_empty_input() -> None:
    assert build_official_sector_baskets(
        pl.DataFrame(schema={"date": pl.Date, "index_name": pl.Utf8, "close_index": pl.Float64})
    ).is_empty()


def test_official_group_rank_grid_two_cells_only() -> None:
    """刻意只有2個cell（group_top5 True/False），不是G3的16格——確認結構正確。"""
    dates = [date(2026, 1, d) for d in (2, 3, 4, 5, 6)]
    rows = []
    for d in dates:
        for i, (sub, score) in enumerate(
            [("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 60.0), ("E", 50.0), ("F", 40.0)]
        ):
            rows.append(
                {
                    "date": d, "sub_industry": sub, "trend_score": score,
                    "alpha10": 1.0 if i < 5 else -1.0, "alpha20": 2.0 if i < 5 else -2.0,
                }
            )
    stock_rows = pl.DataFrame(rows)
    grid = official_group_rank_grid(stock_rows, horizons=(10, 20), top_n_groups=5, n_boot=50)
    assert set(grid["group_top5"].to_list()) == {True, False}
    assert set(grid["horizon"].to_list()) == {10, 20}
    assert grid.height == 4  # 2 horizons x 2 cells


def test_official_group_rank_grid_empty_when_missing_columns() -> None:
    grid = official_group_rank_grid(pl.DataFrame({"date": [date(2026, 1, 1)]}))
    assert grid.is_empty()
