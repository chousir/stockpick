"""F5 揭露欄測試（規劃書 05 F5，沿舊 06 NF1＋07 TR1）：全離線合成/實錄數字。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.analysis.grouping import classify_risk_kind, near_flow_state
from tw_screener.analysis.trajectory import compute_trajectories, trajectory_metrics

# ── NF1 近端籌碼狀態：舊 06 三案例（W27 實錄張數，×1000＝股）──────────────────


def test_flow_state_stall_taishin():
    # 台新新光金：外資 20日 +162,168 張、近5日 +2,720 張＝佔比 1.7% → 熄火
    # （投信 +364,368 張雖為主導邊，警示邊優先揭露）
    state, share = near_flow_state(
        162_168_000, 2_720_000, 364_368_000, 100_000_000
    )
    assert state is not None and "熄火" in state and "外資" in state
    assert share is not None and share < 5


def test_flow_state_accel_cal():
    # 華航：外資 20日 +194,189、近5 +104,005＝54% → 加速
    state, share = near_flow_state(194_189_000, 104_005_000, 28_040_000, 5_000_000)
    assert state is not None and "加速" in state
    assert share is not None and share > 40


def test_flow_state_sell_reversal_and_none():
    # 20 日大買、近 5 日轉負 → 轉賣
    state, _ = near_flow_state(100_000_000, -5_000_000, None, None)
    assert state is not None and "轉賣" in state
    # 20 日賣超主導（無大額買超邊）→ 無可揭露
    assert near_flow_state(-100_000_000, -5_000_000, -2_000_000, -1_000_000) == (None, None)
    # 量太小（< min_lots）→ 無可揭露
    assert near_flow_state(500_000, 100_000, None, None) == (None, None)


def test_risk_kind_three_cases():
    # 台新新光金：熄火（距季線 +26.1% 也延伸，但籌碼熄火優先）
    assert classify_risk_kind("熄火(外資)", 26.1, 11.9, 0.5) == "籌碼熄火"
    # 華航：籌碼加速＋距季線 +23.7% → 價格延伸（非熄火）
    assert classify_risk_kind("加速(外資)", 23.7, 13.6, 5.0) == "價格延伸"
    # 國泰金：5日 −7.03% 且破月線（−0.8%）→ 價格已跌（最高優先）
    assert classify_risk_kind("轉賣(投信)", 10.0, -0.8, -7.03) == "價格已跌"
    # 皆無 → 空字串
    assert classify_risk_kind("平穩(外資)", 8.0, 2.0, 1.0) == ""


# ── TR1 回踩品質軌跡：止穩 vs 破線 合成樣本 ──────────────────────────────────


def _closes_uptrend_pullback() -> tuple[list[float], list[float]]:
    """40 日：緩漲到 130 後小回 1 天、收在 MA20 上；回踩 5 日量縮到 0.5x。"""
    closes = [100 + i for i in range(31)]  # 100→130
    closes += [130.5, 131, 130.8, 131.5, 131.2, 132, 131.8, 132.5, 131.9]  # 高檔整理、末日小跌
    vols = [1000.0] * (len(closes) - 5) + [500.0] * 5  # 近 5 日縮量
    return closes, vols


def _closes_breakdown() -> tuple[list[float], list[float]]:
    """40 日：漲到 130 後連跌 6 天破月線；下殺放量 1.5x。"""
    closes = [100 + i for i in range(31)]
    closes += [128, 124, 120, 116, 112, 108, 104, 100, 96]  # 連跌深破 MA20
    vols = [1000.0] * (len(closes) - 5) + [1500.0] * 5
    return closes, vols


def test_trajectory_calm_pullback_is_stable():
    closes, vols = _closes_uptrend_pullback()
    m = trajectory_metrics(closes, vols)
    assert m["above_ma20_days"] is not None and m["above_ma20_days"] > 0  # 守月線
    assert m["down_days_streak"] <= 1
    assert m["pullback_vol_ratio"] is not None and m["pullback_vol_ratio"] <= 0.8
    assert m["pullback_quality"] == "止穩"


def test_trajectory_volume_breakdown_is_broken():
    closes, vols = _closes_breakdown()
    m = trajectory_metrics(closes, vols)
    assert m["above_ma20_days"] is not None and m["above_ma20_days"] < 0  # 破月線
    assert m["down_days_streak"] >= 3
    assert m["pullback_quality"] == "破線"


def test_trajectory_insufficient_history_all_null():
    m = trajectory_metrics([100.0, 101.0, 102.0], [1000.0] * 3)
    assert all(v is None for v in m.values())


def test_trajectory_no_volume_cannot_confirm_stable():
    # 守月線、未連跌，但無量資料 → 無法確認縮量 → 觀察（不臆造健康）
    closes, _ = _closes_uptrend_pullback()
    m = trajectory_metrics(closes, [])
    assert m["pullback_vol_ratio"] is None
    assert m["pullback_quality"] == "觀察"


def test_compute_trajectories_batch_and_missing_volume():
    d0 = date(2026, 5, 1)
    closes, vols = _closes_uptrend_pullback()
    rows = [
        {"date": d0 + timedelta(days=i), "stock_id": "A", "close": c, "volume": v}
        for i, (c, v) in enumerate(zip(closes, vols))
    ]
    # B：只有 3 天 → 全 null
    rows += [
        {"date": d0 + timedelta(days=i), "stock_id": "B", "close": 50.0 + i, "volume": 100.0}
        for i in range(3)
    ]
    out = compute_trajectories(pl.DataFrame(rows))
    a = out.filter(pl.col("stock_id") == "A").row(0, named=True)
    b = out.filter(pl.col("stock_id") == "B").row(0, named=True)
    assert a["pullback_quality"] == "止穩"
    assert b["pullback_quality"] is None and b["down_days_streak"] is None
