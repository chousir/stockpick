"""pick 底帳持久化測試（規劃書 05 F1-PO1）。

tmp_path 上驗 upsert 冪等／schema 驗證／跨週合併／斷供偵測，不碰真 reports/。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from tw_screener.report.pick_store import (
    EXCLUDED_SCHEMA,
    PICKS_SCHEMA,
    load_all_excluded,
    load_all_picks,
    load_week_excluded,
    load_week_picks,
    upsert_excluded,
    upsert_pick,
    weeks_without_picks,
)

_ROW = {
    "week": "2026-W27",
    "data_date": date(2026, 6, 30),
    "stock_id": "2610",
    "name": "華航",
    "layer": "core",
    "sub_industry": "航空",
    "entry_zone": "回測23不破分批",
    "stop": "收盤破季線19.16",
    "ext_ma60_pct": 23.7,
    "thesis_tag": "F 主升續勢",
}


def test_upsert_pick_roundtrip(tmp_path):
    week_dir = tmp_path / "2026-W27"
    upsert_pick(week_dir, _ROW)
    out = load_week_picks(week_dir)
    assert out.height == 1
    assert dict(out.schema) == dict(PICKS_SCHEMA)
    row = out.row(0, named=True)
    assert row["stock_id"] == "2610"
    assert row["data_date"] == date(2026, 6, 30)
    assert abs(row["ext_ma60_pct"] - 23.7) < 1e-9


def test_upsert_pick_idempotent_replaces_same_stock(tmp_path):
    week_dir = tmp_path / "2026-W27"
    upsert_pick(week_dir, _ROW)
    upsert_pick(week_dir, {**_ROW, "layer": "opportunity"})
    out = load_week_picks(week_dir)
    assert out.height == 1
    assert out.row(0, named=True)["layer"] == "opportunity"


def test_upsert_pick_rejects_bad_layer(tmp_path):
    with pytest.raises(ValueError, match="layer"):
        upsert_pick(tmp_path / "2026-W27", {**_ROW, "layer": "watch"})


def test_upsert_excluded_requires_reason(tmp_path):
    row = {
        "week": "2026-W27",
        "data_date": date(2026, 6, 30),
        "stock_id": "2327",
        "name": "國巨",
        "reason": "",
        "detail": None,
    }
    with pytest.raises(ValueError, match="reason"):
        upsert_excluded(tmp_path / "2026-W27", row)
    upsert_excluded(tmp_path / "2026-W27", {**row, "reason": "過熱"})
    out = load_week_excluded(tmp_path / "2026-W27")
    assert dict(out.schema) == dict(EXCLUDED_SCHEMA)
    assert out.row(0, named=True)["reason"] == "過熱"


def test_load_all_merges_weeks_and_missing_weeks_flagged(tmp_path):
    upsert_pick(tmp_path / "2026-W26", {**_ROW, "week": "2026-W26"})
    upsert_pick(tmp_path / "2026-W27", _ROW)
    upsert_excluded(
        tmp_path / "2026-W27",
        {
            "week": "2026-W27",
            "data_date": date(2026, 6, 30),
            "stock_id": "2327",
            "name": "國巨",
            "reason": "過熱",
            "detail": None,
        },
    )
    # W25：有篩選產物但沒 picks.csv → 斷供如實標
    w25 = tmp_path / "2026-W25"
    w25.mkdir()
    pl.DataFrame({"stock_id": ["2330"], "screened_at": ["2026-06-18"]}).write_csv(
        w25 / "screen_result_d_quality_leader.csv"
    )
    assert load_all_picks(tmp_path).height == 2
    assert load_all_excluded(tmp_path).height == 1
    assert weeks_without_picks(tmp_path) == ["2026-W25"]


def test_load_week_picks_missing_file_empty_schema(tmp_path):
    out = load_week_picks(tmp_path / "2026-W99")
    assert out.is_empty()
    assert dict(out.schema) == dict(PICKS_SCHEMA)
