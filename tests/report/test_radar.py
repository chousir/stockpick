"""輪動雷達快照 I/O ＋ 跨週 ΔRank 讀檔測試。"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from tw_screener.report.group_report import (
    _load_prev_theme_snapshot,
    _week_key,
    _write_theme_snapshot,
)


def _snap() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "theme": ["證券", "記憶體"],
            "kind": ["次產業", "次產業"],
            "radar_rank": [1, 2],
            "lead_score": [88.0, 45.0],
            "score": [90.0, 52.0],
            "momentum_5d": [21.0, -1.0],
            "members_count": [4, 4],
            "foreign_score": [0.75, 0.75],
            "vol_surge_score": [1.0, 0.0],
            "rank_delta": [None, None],
        }
    )


def test_week_key():
    assert _week_key("2026-W23") == (2026, 23)
    assert _week_key("2026-W09") == (2026, 9)
    assert _week_key("not-a-week") is None


def test_snapshot_roundtrip_and_prev_lookup(tmp_path: Path):
    """寫上週快照 → 本週能讀回（取週序 < 本週的最近一份）。"""
    w22 = tmp_path / "2026-W22"
    w22.mkdir()
    _write_theme_snapshot(_snap(), w22)
    assert (w22 / "theme_strength.csv").exists()

    w23 = tmp_path / "2026-W23"
    w23.mkdir()
    prev = _load_prev_theme_snapshot(w23, "2026-W23")
    assert prev is not None
    assert "radar_rank" in prev.columns
    assert set(prev["theme"].to_list()) == {"證券", "記憶體"}


def test_prev_lookup_none_when_no_earlier_week(tmp_path: Path):
    """沒有更早的週快照（首次跑）→ None，不報錯。"""
    w23 = tmp_path / "2026-W23"
    w23.mkdir()
    assert _load_prev_theme_snapshot(w23, "2026-W23") is None


def test_rotation_overlay_load_and_graceful_missing(tmp_path: Path):
    """R5：sector_rotation.csv 並列對照；檔不存在/壞檔 → 空 dict 優雅降級。"""
    from tw_screener.report.group_report import _load_rotation_overlay

    assert _load_rotation_overlay(None) == {}
    assert _load_rotation_overlay(tmp_path) == {}  # 無檔

    pl.DataFrame(
        {
            "sub_industry": ["記憶體", "PCB"],
            "radar_rank": [2, 9],
            "quadrant": ["主升續勢", "出貨警訊"],
            "entry_triggered": [True, False],
        }
    ).write_csv(tmp_path / "sector_rotation.csv")
    out = _load_rotation_overlay(tmp_path)
    assert out["記憶體"] == {"rank": 2, "quadrant": "主升續勢", "triggered": True}
    assert out["PCB"]["triggered"] is False

    # 欄位不齊的壞快照 → 空 dict
    (tmp_path / "bad").mkdir()
    pl.DataFrame({"foo": [1]}).write_csv(tmp_path / "bad" / "sector_rotation.csv")
    assert _load_rotation_overlay(tmp_path / "bad") == {}
