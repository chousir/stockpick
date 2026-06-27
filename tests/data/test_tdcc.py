"""tests/data/test_tdcc.py — TDCC 集保戶股權分散表資料層單元測試（全離線）。"""

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.data.tdcc import (
    derive_big_holders,
    latest_big_holders_with_wow,
    parse_distribution,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tdcc" / "distribution_sample.csv"


def _load_fixture() -> pl.DataFrame:
    return parse_distribution(FIXTURE.read_text(encoding="utf-8"))


def test_parse_distribution_schema_and_trim() -> None:
    df = _load_fixture()
    assert df.columns == ["data_date", "stock_id", "level", "holders", "shares", "pct"]
    # BOM 與固定寬度尾端空白都要被吃掉
    assert df["data_date"].dtype == pl.Date
    assert df["stock_id"].dtype == pl.Utf8
    assert set(df["stock_id"].unique().to_list()) == {"2330", "2317", "0050", "6182", "1216"}
    assert df.filter(pl.col("stock_id").str.contains(" ")).is_empty()
    # 5 檔 × 17 級距
    assert df.height == 85
    assert df["data_date"].max() == date(2026, 6, 26)


def test_parse_distribution_levels_present() -> None:
    df = _load_fixture()
    t2330 = df.filter(pl.col("stock_id") == "2330")
    assert sorted(t2330["level"].to_list()) == list(range(1, 18))
    # 級距 17 ＝合計（占比 100）
    total = t2330.filter(pl.col("level") == 17)
    assert total["pct"][0] == 100.0


def test_parse_distribution_empty_input() -> None:
    assert parse_distribution("").is_empty()
    assert parse_distribution("﻿").is_empty()
    assert parse_distribution("資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n").is_empty()


def test_derive_big_holders_matches_hand_computed() -> None:
    # 由原始股數精算（比 TDCC 逐級截斷顯示的占比加總更精確；差 ≤~0.03pp）。
    # 2330：≥400 張（級距12-15）= 87.85%、≥1000 張（級距15）= 85.12%
    derived = derive_big_holders(_load_fixture())
    row = derived.filter(pl.col("stock_id") == "2330").row(0, named=True)
    assert row["big_holder_pct"] == 87.85
    assert row["big_holder_1000_pct"] == 85.12
    row17 = derived.filter(pl.col("stock_id") == "2317").row(0, named=True)
    assert row17["big_holder_1000_pct"] == 67.23


def test_derive_big_holders_custom_levels() -> None:
    # 只取級距 15 當「大戶」→ big_holder_pct 應等於 1000 張口徑
    derived = derive_big_holders(_load_fixture(), levels_400=(15,), levels_1000=(15,))
    row = derived.filter(pl.col("stock_id") == "2330").row(0, named=True)
    assert row["big_holder_pct"] == row["big_holder_1000_pct"] == 85.12


def test_derive_big_holders_zero_total_is_null() -> None:
    dist = pl.DataFrame(
        {
            "data_date": [date(2026, 6, 26)] * 2,
            "stock_id": ["9999", "9999"],
            "level": [15, 17],
            "holders": [0, 0],
            "shares": [0, 0],  # 合計 0 → 占比 null（不補零）
            "pct": [0.0, 0.0],
        }
    )
    derived = derive_big_holders(dist)
    assert derived["big_holder_pct"][0] is None
    assert derived["big_holder_1000_pct"][0] is None


def test_derive_big_holders_empty() -> None:
    out = derive_big_holders(pl.DataFrame())
    assert out.is_empty()
    assert "big_holder_pct" in out.columns


def test_latest_with_wow_single_week_null() -> None:
    derived = derive_big_holders(_load_fixture())
    out = latest_big_holders_with_wow(derived)
    assert out["big_holder_wow"].is_null().all()
    assert out["big_holder_1000_wow"].is_null().all()
    assert set(out.columns) == {
        "stock_id",
        "data_date",
        "big_holder_pct",
        "big_holder_1000_pct",
        "big_holder_wow",
        "big_holder_1000_wow",
    }


def test_latest_with_wow_two_weeks_diff() -> None:
    # 前一週合成：2330 千張大戶占比較低，本週上升 → WoW 為正
    prev = pl.DataFrame(
        {
            "data_date": [date(2026, 6, 19)],
            "stock_id": ["2330"],
            "big_holder_pct": [86.00],
            "big_holder_1000_pct": [84.00],
        }
    )
    latest = pl.DataFrame(
        {
            "data_date": [date(2026, 6, 26)],
            "stock_id": ["2330"],
            "big_holder_pct": [87.83],
            "big_holder_1000_pct": [85.11],
        }
    )
    out = latest_big_holders_with_wow(pl.concat([prev, latest]))
    row = out.row(0, named=True)
    assert row["data_date"] == date(2026, 6, 26)
    assert row["big_holder_wow"] == 1.83
    assert row["big_holder_1000_wow"] == 1.11


def test_latest_with_wow_new_stock_null() -> None:
    # 本週才出現的個股（前週缺）→ WoW null（左 join 自最新週）
    prev = pl.DataFrame(
        {
            "data_date": [date(2026, 6, 19)],
            "stock_id": ["2330"],
            "big_holder_pct": [86.00],
            "big_holder_1000_pct": [84.00],
        }
    )
    latest = pl.DataFrame(
        {
            "data_date": [date(2026, 6, 26), date(2026, 6, 26)],
            "stock_id": ["2330", "9999"],
            "big_holder_pct": [87.83, 50.0],
            "big_holder_1000_pct": [85.11, 30.0],
        }
    )
    out = latest_big_holders_with_wow(pl.concat([prev, latest]))
    new = out.filter(pl.col("stock_id") == "9999").row(0, named=True)
    assert new["big_holder_wow"] is None
    assert new["big_holder_1000_wow"] is None


def test_latest_with_wow_empty() -> None:
    out = latest_big_holders_with_wow(pl.DataFrame())
    assert out.is_empty()
    assert "big_holder_wow" in out.columns
