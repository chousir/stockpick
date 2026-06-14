"""個股版起漲事件回測測試（B2，docs/13 §4 Phase B）。

合成離線資料，不打外部。視窗參數用小值（m_days=5 等）以縮小 fixture。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.rotation_calib import compute_base_rate
from tw_screener.backtest.stock_calib import (
    detect_ambush_episodes,
    detect_breakout_episodes,
    detect_reversal_episodes,
    scan_stock_signals,
)


def _prices(closes_by_stock: dict[str, list[float]]) -> pl.DataFrame:
    start = date(2025, 1, 1)
    rows = []
    for sid, closes in closes_by_stock.items():
        for i, c in enumerate(closes):
            rows.append(
                {
                    "date": start + timedelta(days=i),
                    "stock_id": sid,
                    "close": float(c),
                    "volume": 1000,
                }
            )
    return pl.DataFrame(rows)


# ── episode 偵測（三 label）───────────────────────────────────────────────────


def test_ambush_detects_low_base_then_rally():
    # 8 日貼低 100，再衝到 125（+18%）
    closes = [100] * 8 + [105, 110, 118, 125]
    ep = detect_ambush_episodes(
        _prices({"A": closes}), m_days=5, tol_pct=2.0, x_pct=15.0, n_days=5, cooldown_days=5
    )
    assert ep.height == 1
    row = ep.row(0, named=True)
    assert row["stock_id"] == "A"
    assert row["base_close"] == 100.0
    assert row["fwd_return_pct"] > 15.0


def test_breakout_detects_just_left_low_band():
    # 貼低 100 後站上 105（距低 5%，落在 [3,8] 帶），續攻到 122
    closes = [100] * 6 + [105, 108, 113, 118, 122]
    ep = detect_breakout_episodes(
        _prices({"A": closes}),
        m_days=5,
        lo_pct=3.0,
        hi_pct=8.0,
        x_pct=12.0,
        n_days=5,
        cooldown_days=5,
    )
    assert ep.height == 1
    assert ep.row(0, named=True)["base_close"] == 105.0


def test_reversal_detects_deep_drop_then_rebound():
    # 自高點 100 深跌到 72（−26%），再反彈到 96（+33%）
    closes = [100, 98, 95, 90, 82, 72, 78, 85, 92, 96]
    ep = detect_reversal_episodes(
        _prices({"A": closes}),
        l_days=5,
        drawdown_pct=20.0,
        x_pct=15.0,
        n_days=5,
        cooldown_days=5,
    )
    assert ep.height == 1
    assert ep.row(0, named=True)["base_close"] == 72.0


def test_no_rally_no_episode():
    ep = detect_ambush_episodes(
        _prices({"A": [100] * 12}), m_days=5, tol_pct=2.0, x_pct=15.0, n_days=5, cooldown_days=5
    )
    assert ep.is_empty()


def test_empty_price_returns_empty_schema():
    ep = detect_ambush_episodes(pl.DataFrame())
    assert ep.is_empty()
    assert "stock_id" in ep.columns and "fwd_return_pct" in ep.columns


# ── 訊號掃描 ──────────────────────────────────────────────────────────────────


def _scan_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """A：trust z 在 idx5 上穿 1.0、idx7 起漲事件（領先 2 日）；B：無訊號無事件（稀釋基率）。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    rows = []
    for sid in ("A", "B"):
        for i, d in enumerate(days):
            z = 2.0 if (sid == "A" and i in (5, 6)) else 0.0
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "trust_flow_20d_z": z,
                    "trust_momentum": 100.0 if z > 0 else 0.0,
                    "above_low_60d_pct": 4.0 if sid == "A" else 50.0,
                    "volume_z_5d": z,
                }
            )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["A"], "start_date": [days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    return panel, episodes


def test_scan_leading_signal_has_lift_above_one():
    panel, episodes = _scan_fixture()
    scan = scan_stock_signals(
        panel,
        episodes,
        z_thresholds=(1.0,),
        volume_thresholds=(1.0,),
        position_low_pct=15.0,
        lead_window=5,
        occupy_days=5,
        z_min_periods=2,
    )
    assert not scan.is_empty()
    assert {"signal", "lift", "recall", "median_lead_days", "f1"} <= set(scan.columns)
    base = scan.filter(pl.col("signal") == "trust_flow_20d_z (z>1.0)").row(0, named=True)
    assert base["hits"] == 1
    assert base["median_lead_days"] == 2
    assert base["lift"] is not None and base["lift"] > 1.0


def test_scan_builds_low_and_mom_variants():
    panel, episodes = _scan_fixture()
    scan = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,), lead_window=5,
        occupy_days=5, z_min_periods=2,
    )
    signals = set(scan["signal"].to_list())
    assert "trust_flow_20d_z (z>1.0) +mom" in signals
    assert any(s.startswith("trust_flow_20d_z (z>1.0) +low") for s in signals)
    assert "volume_z_5d (z>1.0)" in signals


def test_scan_empty_inputs_return_empty():
    panel, episodes = _scan_fixture()
    assert scan_stock_signals(pl.DataFrame(), episodes).is_empty()
    assert scan_stock_signals(panel, pl.DataFrame()).is_empty()


# ── 基率（全宇宙、一次算好）──────────────────────────────────────────────────


def test_universe_base_rate_dilutes_with_more_stocks():
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    episodes = pl.DataFrame(
        {"stock_id": ["A"], "start_date": [days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    one = compute_base_rate(episodes, days, ["A"], lead_window=5, occupy_days=5, warmup_pos=2,
                            key_col="stock_id")
    two = compute_base_rate(episodes, days, ["A", "B"], lead_window=5, occupy_days=5, warmup_pos=2,
                            key_col="stock_id")
    assert 0 < two < one  # 加入無事件股 B 後基率被稀釋
