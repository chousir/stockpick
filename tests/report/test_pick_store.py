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
    core_extension_violation,
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


# ── F2 位階紀律：core 距季線乖離硬上限（裁決點 #2 拍板 +15% 嚴）──────────────


def test_core_extension_gate_blocks_historical_extended():
    """§1.3 主虧損剖面歷史重跑：創見/華碩/W27 三檔延伸股在 +15% 門檻下全數擋於核心之外。"""
    historical_extended = {
        "創見": 24.8,       # §1.3：入選 +24.8% → −29.9%
        "華碩": 26.3,       # §1.3：入選 +26.3% → −21.5%
        "華航(W27)": 23.7,  # W27 enriched 距季線
        "長榮航(W27)": 23.5,
        "矽創(W27)": 27.5,
    }
    for name, ext in historical_extended.items():
        msg = core_extension_violation("core", ext, 15.0)
        assert msg is not None, f"{name}（+{ext}%）應被 F2 位階紀律擋下"
        assert "15" in msg


def test_core_extension_gate_passes_clean_positions():
    # §1.3 贏家層（≤15%）：彰銀 +8.6、遠東銀 +4.4 → 通過
    assert core_extension_violation("core", 8.6, 15.0) is None
    assert core_extension_violation("core", 4.4, 15.0) is None
    assert core_extension_violation("core", 15.0, 15.0) is None  # 邊界含


def test_core_extension_gate_only_applies_to_core():
    # 延伸股仍可入 opportunity/pool（買強勢僅存在趨勢領頭板，F3 前先降層）
    assert core_extension_violation("opportunity", 40.0, 15.0) is None
    assert core_extension_violation("pool", 40.0, 15.0) is None


def test_core_extension_gate_disabled_or_unknown():
    assert core_extension_violation("core", 40.0, None) is None  # 未設定門檻＝不啟用
    assert core_extension_violation("core", 40.0, 0.0) is None
    assert core_extension_violation("core", None, 15.0) is None  # 乖離未知→runner 警告後如實記


# ── WS-L：同週 data_date 一致性衛生 ──────────────────────────────────────────


def test_upsert_pick_rejects_inconsistent_data_date_same_week(tmp_path):
    """W26 型事故的預防性衛生：同週第二檔 data_date 與既有列不同 → 預設擋，訊息含既有/新值/週次。"""
    week_dir = tmp_path / "2026-W27"
    upsert_pick(week_dir, _ROW)  # 2610, data_date 2026-06-30
    other = {**_ROW, "stock_id": "3293", "data_date": date(2026, 7, 1)}
    with pytest.raises(ValueError) as exc:
        upsert_pick(week_dir, other)
    msg = str(exc.value)
    assert "2026-06-30" in msg  # 既有值
    assert "2026-07-01" in msg  # 新值
    assert "2026-W27" in msg  # 週次
    assert "late_entry" in msg
    # 擋下後未落帳
    out = load_week_picks(week_dir)
    assert out.height == 1


def test_upsert_pick_late_entry_true_allows_and_persists(tmp_path):
    """late_entry=True 明示覆寫：放行寫入且該欄落地。"""
    week_dir = tmp_path / "2026-W27"
    upsert_pick(week_dir, _ROW)
    other = {**_ROW, "stock_id": "3293", "data_date": date(2026, 7, 1), "late_entry": True}
    upsert_pick(week_dir, other)
    out = load_week_picks(week_dir)
    assert out.height == 2
    row = out.filter(pl.col("stock_id") == "3293").row(0, named=True)
    assert row["late_entry"] is True
    # 原本那筆未指定 late_entry → 預設 False 落地
    first = out.filter(pl.col("stock_id") == "2610").row(0, named=True)
    assert first["late_entry"] is False


def test_upsert_excluded_rejects_inconsistent_data_date_same_week(tmp_path):
    week_dir = tmp_path / "2026-W27"
    row = {
        "week": "2026-W27",
        "data_date": date(2026, 6, 30),
        "stock_id": "2327",
        "name": "國巨",
        "reason": "過熱",
        "detail": None,
    }
    upsert_excluded(week_dir, row)
    other = {**row, "stock_id": "3293", "data_date": date(2026, 7, 1)}
    with pytest.raises(ValueError, match="late_entry"):
        upsert_excluded(week_dir, other)
    upsert_excluded(week_dir, {**other, "late_entry": True})
    out = load_week_excluded(week_dir)
    assert out.height == 2


def test_upsert_pick_first_of_week_never_blocked_regardless_of_late_entry(tmp_path):
    """該週第一筆沒有既有列可比對，不論 late_entry 為何都放行。"""
    week_dir = tmp_path / "2026-W27"
    upsert_pick(week_dir, _ROW)
    assert load_week_picks(week_dir).row(0, named=True)["late_entry"] is False


def test_load_legacy_csv_without_late_entry_column_backfills_false(tmp_path):
    """舊 CSV 無 late_entry 欄 → 載入時補 False，不得爆炸。"""
    week_dir = tmp_path / "2026-W27"
    week_dir.mkdir(parents=True)
    legacy_cols = [c for c in PICKS_SCHEMA if c != "late_entry"]
    pl.DataFrame([{c: _ROW[c] for c in legacy_cols}]).write_csv(week_dir / "picks.csv")
    out = load_week_picks(week_dir)
    assert dict(out.schema) == dict(PICKS_SCHEMA)
    assert out.row(0, named=True)["late_entry"] is False
    # load_all_picks 也要能吃舊檔不爆炸
    assert load_all_picks(tmp_path).height == 1


# ── M1.5 左側票 F2 行為鎖測（委託書 M1.5）────────────────────────────────


def test_f2_passes_negative_ma60_dist_for_contrarian_picks():
    """左側票 ma60_dist 天然為負（趨勢仍破）——F2 只擋「延伸太遠」那側，須自動通過。

    委託書 M1.5 的兩條要求逐條鎖住，防日後有人把 F2 改成雙側門檻而悄悄封死左側腿。
    """
    for ext in (-0.4, -6.0, -24.9, -40.0):
        assert core_extension_violation("core", ext, 15.0) is None
        assert core_extension_violation("opportunity", ext, 15.0) is None


def test_f2_missing_ma60_dist_never_blocks_write():
    """乖離缺值不得擋寫（M1.5）——由呼叫端警告「位階無法查核」後如實記錄。"""
    assert core_extension_violation("core", None, 15.0) is None
    assert core_extension_violation("opportunity", None, 15.0) is None
