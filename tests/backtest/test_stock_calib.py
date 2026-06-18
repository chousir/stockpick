"""個股版起漲事件回測測試（B2，docs/13 §4 Phase B）。

合成離線資料，不打外部。視窗參數用小值（m_days=5 等）以縮小 fixture。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.rotation_calib import compute_base_rate
from tw_screener.backtest.stock_calib import (
    _flow_z_cols,
    compute_cross_window_lead,
    detect_ambush_episodes,
    detect_breakout_episodes,
    detect_reversal_episodes,
    detect_top_episodes,
    scan_stock_signals,
    scan_top_signals,
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


# ── M-MH Phase 2：多窗一般化 + 早偵測閘 + 跨窗配對領先 ────────────────────────


def test_flow_z_cols_discovers_all_windows():
    cols = {"net_flow_1d_z", "net_flow_20d_z", "foreign_flow_5d_z", "close", "date"}
    out = _flow_z_cols(cols)
    wmap = {c: w for c, _, w in out}
    assert wmap == {"net_flow_1d_z": 1, "net_flow_20d_z": 20, "foreign_flow_5d_z": 5}
    # 加速度欄依 prefix 對映（unsuffixed 短窗值）
    assert dict((c, m) for c, m, _ in out)["foreign_flow_5d_z"] == "foreign_momentum"


def _mw_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """A：5d-z idx5 上穿、20d-z 仍 0（長窗未追上）、idx7 起漲；B：稀釋基率。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    rows = []
    for sid in ("A", "B"):
        for i, d in enumerate(days):
            sz = 2.0 if (sid == "A" and i in (5, 6)) else 0.0
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "foreign_flow_5d_z": sz,
                    "foreign_flow_20d_z": 0.0,  # 長窗整段未達標
                    "foreign_momentum": 100.0 if sz > 0 else 0.0,
                    "flow_decel": 1.0,  # 未減速
                    "price_flow_div_5d": -1.0,  # 價未先噴
                    "above_low_60d_pct": 4.0 if sid == "A" else 50.0,
                    "volume_z_5d": sz,
                }
            )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["A"], "start_date": [days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    return panel, episodes


def test_early_gate_adds_variants_only_when_enabled():
    panel, episodes = _mw_fixture()
    early = {"long_z_ceiling": 0.5, "decel_floor": 0.0, "div_ceiling": 0.0}
    on = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,), lead_window=5,
        occupy_days=5, z_min_periods=2, early_gate=early,
    )
    sigs = set(on["signal"].to_list())
    assert "foreign_flow_5d_z (z>1.0) +early" in sigs
    assert "foreign_flow_5d_z (z>1.0) +early+low" in sigs
    assert "foreign_flow_5d_z (z>1.0) +nodiv" in sigs
    # 短窗早閘命中（事件 idx7、觸發 idx5 在領先窗內）
    er = on.filter(pl.col("signal") == "foreign_flow_5d_z (z>1.0) +early").row(0, named=True)
    assert er["hits"] == 1 and er["lift"] is not None and er["lift"] > 1.0
    # 關閉 → 無早閘變體（向後相容）
    off = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,), lead_window=5,
        occupy_days=5, z_min_periods=2,
    )
    assert not any("+early" in s for s in off["signal"].to_list())


def test_cross_window_lead_short_leads_long():
    """5d-z 自 idx5 達標、20d-z 自 idx9 達標（A）；B 短窗達標但 20d 整段未達（short_only）。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    rows = []
    for i, d in enumerate(days):
        rows.append(
            {"date": d, "stock_id": "A",
             "foreign_flow_5d_z": 2.0 if i >= 5 else 0.0,
             "foreign_flow_20d_z": 2.0 if i >= 9 else 0.0}
        )
        rows.append(
            {"date": d, "stock_id": "B",
             "foreign_flow_5d_z": 2.0 if i >= 5 else 0.0,
             "foreign_flow_20d_z": 0.0}
        )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["A", "B"], "start_date": [days[14], days[14]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    lead = compute_cross_window_lead(panel, episodes, threshold=1.0, lookback=20, min_lead_days=2)
    assert not lead.is_empty()
    r = lead.row(0, named=True)
    assert r["short_signal"] == "foreign_flow_5d_z"
    assert r["long_signal"] == "foreign_flow_20d_z"
    assert r["n_short_fired"] == 2 and r["n_paired"] == 1 and r["short_only"] == 1
    assert r["median_lead_days"] == 4  # 20d 首達 idx9 − 5d 首達 idx5
    assert r["pct_short_leads"] == 1.0 and r["pct_lead_ge"] == 1.0


def test_cross_window_lead_empty_inputs():
    panel, episodes = _mw_fixture()
    assert compute_cross_window_lead(pl.DataFrame(), episodes).is_empty()
    assert compute_cross_window_lead(panel, pl.DataFrame()).is_empty()


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


# ── M-MH 精修：L4 頂部/出貨退潮警示校準（前瞻絕對下跌、與 L1 對稱）─────────────


def test_top_detects_high_base_then_drop():
    # 8 日貼高 100，再跌到 80（−20%）——情境貼高、前瞻谷底跌幅過門檻
    closes = [100] * 8 + [97, 92, 86, 80]
    ep = detect_top_episodes(
        _prices({"A": closes}), m_days=5, tol_pct=8.0, drop_pct=10.0, n_days=5, cooldown_days=5
    )
    assert ep.height == 1
    row = ep.row(0, named=True)
    assert row["stock_id"] == "A"
    assert row["base_close"] == 100.0
    assert row["fwd_return_pct"] <= -10.0  # 谷底相對基準的跌幅（負值）


def test_top_no_drop_no_episode():
    # 一直貼高但沒跌 → 非頂部事件
    ep = detect_top_episodes(
        _prices({"A": [100] * 12}), m_days=5, tol_pct=8.0, drop_pct=10.0, n_days=5, cooldown_days=5
    )
    assert ep.is_empty()


def _top_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """A：idx5/6 出現退潮型態（貼高＋短窗減速＋量價背離/量縮）、idx7 出貨下跌（領先 2 日）；
    B：常在低位、無退潮（稀釋基率）。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    rows = []
    for sid in ("A", "B"):
        for i, d in enumerate(days):
            hot = sid == "A" and i in (5, 6)
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "above_high_60d_pct": -1.0 if sid == "A" else -50.0,  # A 一直貼高位
                    "flow_decel": -1.0 if hot else 1.0,  # A 在 idx5/6 短窗買盤減速
                    "price_flow_div_5d": 2.0 if hot else -1.0,  # 量價背離（價漲資金未跟）
                    "volume_z_5d": -1.0 if hot else 0.0,  # 量縮
                    "foreign_flow_5d_z": -2.0 if hot else 0.0,  # 外資短窗賣超
                    "net_flow_5d_z": -2.0 if hot else 0.0,
                }
            )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["A"], "start_date": [days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    return panel, episodes


def test_scan_top_overheat_signal_leads_drop():
    panel, episodes = _top_fixture()
    scan = scan_top_signals(
        panel, episodes, near_high_pct=8.0, lead_window=5, occupy_days=5, z_min_periods=2
    )
    assert not scan.is_empty()
    assert {"signal", "lift", "recall", "median_lead_days", "f1"} <= set(scan.columns)
    oh = scan.filter(pl.col("signal").str.starts_with("★overheat"))
    assert oh.height == 1
    row = oh.row(0, named=True)
    assert row["hits"] == 1
    assert row["median_lead_days"] == 2
    assert row["lift"] is not None and row["lift"] > 1.0


def test_scan_top_builds_baselines_and_components():
    panel, episodes = _top_fixture()
    scan = scan_top_signals(
        panel, episodes, near_high_pct=8.0, lead_window=5, occupy_days=5, z_min_periods=2
    )
    signals = set(scan["signal"].to_list())
    assert any(s.startswith("near_high") for s in signals)  # 純貼高基準
    assert any(s.startswith("★overheat") for s in signals)  # 生產啟發式
    assert "flow_decel (<0)" in signals  # 退潮因子單獨
    assert any("foreign_flow_5d_z" in s and "+high" in s for s in signals)  # 賣超×高位基準


def test_scan_top_empty_inputs_return_empty():
    panel, episodes = _top_fixture()
    assert scan_top_signals(pl.DataFrame(), episodes).is_empty()
    assert scan_top_signals(panel, pl.DataFrame()).is_empty()
