"""個股版起漲事件回測測試（B2，docs/13 §4 Phase B）。

合成離線資料，不打外部。視窗參數用小值（m_days=5 等）以縮小 fixture。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.rotation_calib import compute_base_rate
from tw_screener.backtest.stock_calib import (
    _flow_z_cols,
    _laggard_lift_significance,
    compute_cross_window_lead,
    detect_ambush_episodes,
    detect_breakout_episodes,
    detect_reversal_episodes,
    detect_top_episodes,
    dom_monotonicity_spearman,
    dom_monotonicity_table,
    factor_monotonicity_spearman,
    factor_monotonicity_table,
    holdout_table,
    interaction_2x2_table,
    laggard_filter_precision,
    liquidity_table,
    payoff_decay_table,
    render_dom_monotonicity_report,
    render_interaction_report,
    render_laggard_filter_report,
    render_laggard_monotonicity_report,
    render_robustness_report,
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


def test_zero_close_skipped_no_zerodivision():
    # 髒資料：中間插一筆 close=0（停牌）。修前 rolling_min 被 0 毒化＋前瞻基準除以 0 會
    # ZeroDivisionError；修後 _prep 在算 ctx 前剔非正收盤 → 不崩，仍以有效價偵到貼低起漲。
    closes = [100] * 6 + [0] + [105, 110, 118, 125]
    ep = detect_ambush_episodes(
        _prices({"A": closes}), m_days=5, tol_pct=2.0, x_pct=15.0, n_days=5, cooldown_days=5
    )
    assert ep.height >= 1
    assert all(b > 0 for b in ep["base_close"].to_list())


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


# ── B-P1：穩健度四件套（payoff／decay／holdout／流動性硬化；docs/15 T3）────────────


def _robust_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """A：trust z idx5/6 上穿 1.0（觸發 idx5、close=100）、之後續漲（前瞻報酬為正）、量大；
    B：z 恆 0、價平、量極小（稀釋基率＋流動性硬化會被剔）。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(20)]
    rows = []
    for sid in ("A", "B"):
        for i, d in enumerate(days):
            if sid == "A":
                close = 100.0 if i <= 5 else 100.0 * (1 + 0.03 * (i - 5))
                z = 2.0 if i in (5, 6) else 0.0
                vol, low = 100_000, 4.0
            else:
                close, z, vol, low = 100.0, 0.0, 10, 50.0
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "close": close,
                    "volume": vol,
                    "trust_flow_20d_z": z,
                    "trust_momentum": 100.0 if z > 0 else 0.0,
                    "above_low_60d_pct": low,
                    "volume_z_5d": z,
                }
            )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["A"], "start_date": [days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    return panel, episodes


def test_payoff_decay_table_measures_forward_returns():
    panel, _ = _robust_fixture()
    pd_tab = payoff_decay_table(
        panel,
        horizons=(3, 5),
        signals={"trust_flow_20d_z (z>1.0)"},
        z_thresholds=(1.0,),
        volume_thresholds=(1.0,),
    )
    assert not pd_tab.is_empty()
    assert {"signal", "horizon_d", "win_rate", "payoff_ratio", "excess_median_pct"} <= set(
        pd_tab.columns
    )
    r3 = pd_tab.filter(
        (pl.col("signal") == "trust_flow_20d_z (z>1.0)") & (pl.col("horizon_d") == 3)
    ).row(0, named=True)
    assert r3["n"] == 1  # A 在 idx5 觸發
    assert abs(r3["median_ret_pct"] - 9.0) < 1e-6  # close[idx8]/100−1 = +9%
    assert r3["win_rate"] == 1.0
    assert r3["payoff_ratio"] is None  # 無虧損樣本 → 賠率不可算


def test_payoff_decay_empty_or_no_close_returns_empty():
    panel, _ = _robust_fixture()
    assert payoff_decay_table(pl.DataFrame()).is_empty()
    assert payoff_decay_table(panel.drop("close")).is_empty()


def test_holdout_table_splits_train_test():
    panel, episodes = _robust_fixture()
    ho = holdout_table(
        panel,
        episodes,
        split_frac=0.5,  # cut＝idx10：事件 idx7、觸發 idx5 落前段（lead=2 留前瞻空間）
        signals={"trust_flow_20d_z (z>1.0)"},
        z_thresholds=(1.0,),
        volume_thresholds=(1.0,),
        lead_window=2,
        occupy_days=2,
        z_min_periods=2,
    )
    assert not ho.is_empty()
    assert {
        "signal",
        "lift_train",
        "n_triggers_train",
        "lift_test",
        "n_triggers_test",
    } <= set(ho.columns)
    r = ho.filter(pl.col("signal") == "trust_flow_20d_z (z>1.0)").row(0, named=True)
    assert r["lift_train"] is not None and r["lift_train"] > 1.0  # 前段命中
    assert r["lift_test"] is None  # 後段無事件


def test_liquidity_hardening_filters_low_turnover():
    panel, episodes = _robust_fixture()
    kw = {
        "signals": {"trust_flow_20d_z (z>1.0)"},
        "z_thresholds": (1.0,),
        "volume_thresholds": (1.0,),
        "lead_window": 5,
        "occupy_days": 5,
        "z_min_periods": 2,
    }
    # A 的 ADV ≈ 100×100,000 = 10 百萬：門檻 1 百萬 → 留存；門檻 1000 百萬 → 剔除
    keep = liquidity_table(panel, episodes, adv_window=5, adv_min_amount=1.0, **kw)
    drop = liquidity_table(panel, episodes, adv_window=5, adv_min_amount=1000.0, **kw)
    rk = keep.filter(pl.col("signal") == "trust_flow_20d_z (z>1.0)").row(0, named=True)
    rd = drop.filter(pl.col("signal") == "trust_flow_20d_z (z>1.0)").row(0, named=True)
    assert rk["n_raw"] == rk["n_hardened"]  # 低門檻：A 全留
    assert rd["n_hardened"] == 0  # 高門檻：A 被剔（lift_hardened=0）


def test_holdout_liquidity_empty_inputs_return_empty():
    panel, episodes = _robust_fixture()
    assert holdout_table(panel, pl.DataFrame()).is_empty()
    assert liquidity_table(panel, pl.DataFrame()).is_empty()
    assert liquidity_table(panel.drop("volume"), episodes).is_empty()


def test_render_robustness_report_has_sections_and_placeholder():
    panel, episodes = _robust_fixture()
    sig = {"trust_flow_20d_z (z>1.0)"}
    payoff = payoff_decay_table(
        panel, horizons=(3, 5), signals=sig, z_thresholds=(1.0,), volume_thresholds=(1.0,)
    )
    params = {
        "top_k": 6,
        "horizons": [3, 5],
        "holdout_frac": 0.5,
        "adv_window": 5,
        "adv_min_amount": 1.0,
    }
    md = render_robustness_report(
        payoff, pl.DataFrame(), pl.DataFrame(), "ambush", list(sig), params, {}
    )
    assert "payoff" in md and "holdout" in md and "流動性" in md
    assert "trust_flow_20d_z (z>1.0)" in md
    # 空表段落給誠實佔位、不爆
    assert "無法做樣本外切分" in md


# ── B-P2：買方主導度單調性（dom 分位 × 控制位階；docs/15 T1）─────────────────────


def _mono_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """5 檔 dom 由低到高（每檔 dom 定值＝一個分位桶），前瞻報酬隨 dom 單調遞增；
    起漲事件落在高 dom 兩檔（S3/S4）→ 高桶 lift 高、低桶 0。位階交錯使兩層各含 dom 範圍。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    specs = [  # (sid, dom, 每日成長率 g, above_low)
        ("S0", -0.8, -0.02, 4.0),
        ("S1", -0.4, -0.01, 50.0),
        ("S2", 0.0, 0.00, 4.0),
        ("S3", 0.4, 0.01, 50.0),
        ("S4", 0.8, 0.02, 4.0),
    ]
    rows = []
    for sid, dom, g, low in specs:
        for i, d in enumerate(days):
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "close": 100.0 * (1 + g) ** i,
                    "dom_20d": dom,
                    "above_low_60d_pct": low,
                }
            )
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(
        {"stock_id": ["S3", "S4"], "start_date": [days[7], days[7]]},
        schema={"stock_id": pl.Utf8, "start_date": pl.Date},
    )
    return panel, episodes


_MONO_KW = dict(n_buckets=5, fwd_window=5, lead_window=5, occupy_days=5, z_min_periods=2)


def test_dom_monotonicity_table_lift_rises_with_bucket():
    panel, episodes = _mono_fixture()
    tab = dom_monotonicity_table(panel, episodes, **_MONO_KW)
    assert not tab.is_empty()
    assert {"stratum", "bucket", "lift", "median_fwd_ret_pct", "n_stock_days"} <= set(tab.columns)
    # 三層皆有（全體＋控制位階兩層）
    assert {"全體", "貼低", "非貼低"} == set(tab["stratum"].unique().to_list())
    overall = tab.filter(pl.col("stratum") == "全體").sort("bucket")
    assert overall.height == 5
    lifts = [r if r is not None else 0.0 for r in overall["lift"].to_list()]
    assert lifts[-1] > lifts[0]  # 高 dom 桶 lift > 低 dom 桶（起漲集中高 dom）
    # 前瞻報酬中位亦隨桶遞增（dom 越高、續漲越強）
    frs = overall["median_fwd_ret_pct"].to_list()
    assert frs[-1] > frs[0]


def test_dom_spearman_positive_and_significant():
    panel, _ = _mono_fixture()
    sp = dom_monotonicity_spearman(panel, fwd_window=5, z_sig=1.96)
    assert not sp.is_empty()
    assert {"stratum", "spearman_rho", "z", "significant"} <= set(sp.columns)
    allrow = sp.filter(pl.col("stratum") == "全體").row(0, named=True)
    assert allrow["spearman_rho"] is not None and allrow["spearman_rho"] > 0
    assert allrow["significant"] is True  # dom 越高、前瞻報酬越強，方向對且大樣本顯著


def test_dom_monotonicity_empty_or_missing_cols_return_empty():
    panel, episodes = _mono_fixture()
    assert dom_monotonicity_table(pl.DataFrame(), episodes).is_empty()
    assert dom_monotonicity_table(panel, pl.DataFrame()).is_empty()
    assert dom_monotonicity_table(panel.drop("dom_20d"), episodes).is_empty()
    assert dom_monotonicity_table(panel.drop("close"), episodes).is_empty()
    assert dom_monotonicity_spearman(panel.drop("dom_20d")).is_empty()


def test_render_dom_monotonicity_report_has_sections_and_verdict():
    panel, episodes = _mono_fixture()
    tab = dom_monotonicity_table(panel, episodes, **_MONO_KW)
    sp = dom_monotonicity_spearman(panel, fwd_window=5)
    params = {"n_buckets": 5, "fwd_window": 5, "dom_window": 20, "position_low_pct": 15.0,
              "z_sig": 1.96}
    md = render_dom_monotonicity_report(tab, sp, "ambush", params, {})
    assert "分位桶" in md and "Spearman" in md and "裁決" in md
    assert "建議升級" in md or "維持" in md  # 必出一種裁決
    # 空輸入給誠實佔位、不爆
    empty_md = render_dom_monotonicity_report(pl.DataFrame(), pl.DataFrame(), "ambush", params, {})
    assert "無法分桶" in empty_md or "無法檢定" in empty_md


# ── B-P3：個股×族群 2×2 交互（S 資金進+貼低 × G 個股相對次產業領先；docs/15 T2）──────


def _interaction_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """4 格各 3 檔：S 高＝flow_z>0.5 且 above_low≤15、G 高＝rs_subind>0。起漲事件只落 S+G+
    （資金進+貼低且族群領先）→ S+G+ lift 遠高、超加性成立、S+ 內 G高 vs G低 命中率顯著。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    cells = [  # (前綴, flow_z, above_low, rs_subind, 有起漲)
        ("PP", 2.0, 4.0, 5.0, True),    # S+G+
        ("PN", 2.0, 4.0, -5.0, False),  # S+G−
        ("NP", 0.0, 50.0, 5.0, False),  # S−G+
        ("NN", 0.0, 50.0, -5.0, False),  # S−G−
    ]
    rows, ep_rows = [], []
    for prefix, fz, low, rs, has_ep in cells:
        for k in range(3):
            sid = f"{prefix}{k}"
            for d in days:
                rows.append({"date": d, "stock_id": sid, "foreign_flow_20d_z": fz,
                             "above_low_60d_pct": low, "rs_subind_20d": rs})
            if has_ep:
                ep_rows.append({"stock_id": sid, "start_date": days[7]})
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(ep_rows, schema={"stock_id": pl.Utf8, "start_date": pl.Date})
    return panel, episodes


_INTER_KW = dict(lead_window=5, occupy_days=5, z_min_periods=2)


def test_interaction_2x2_superadditive():
    panel, episodes = _interaction_fixture()
    tab = interaction_2x2_table(panel, episodes, **_INTER_KW)
    assert not tab.is_empty()
    assert set(tab["cell"].to_list()) == {"S+G+", "S+G−", "S−G+", "S−G−"}
    by = {r["cell"]: r for r in tab.iter_rows(named=True)}
    # 起漲集中 S+G+：其 lift 為四格最高、且明顯 > S+G−（族群領先在資金訊號上加分）
    assert by["S+G+"]["lift"] is not None and by["S+G+"]["lift"] > 1.0
    assert by["S+G+"]["lift"] > (by["S+G−"]["lift"] or 0.0)
    assert by["S+G+"]["hit_rate"] > by["S+G−"]["hit_rate"]


def test_interaction_empty_or_missing_cols_return_empty():
    panel, episodes = _interaction_fixture()
    assert interaction_2x2_table(pl.DataFrame(), episodes).is_empty()
    assert interaction_2x2_table(panel, pl.DataFrame()).is_empty()
    assert interaction_2x2_table(panel.drop("rs_subind_20d"), episodes).is_empty()
    assert interaction_2x2_table(panel.drop("above_low_60d_pct"), episodes).is_empty()
    assert interaction_2x2_table(panel.drop("foreign_flow_20d_z"), episodes).is_empty()


_INTER_PARAMS = {"s_flow_col": "foreign_flow_20d_z", "s_z_threshold": 0.5, "s_low_pct": 15.0,
                 "g_threshold": 0.0}


def test_render_interaction_report_verdict_upgrade():
    panel, episodes = _interaction_fixture()
    tab = interaction_2x2_table(panel, episodes, **_INTER_KW)
    md = render_interaction_report(tab, "ambush", _INTER_PARAMS, {}, min_triggers=8, z_sig=1.96)
    assert "2×2" in md and "超加性" in md and "裁決" in md
    assert "族群確認" in md  # 超加+G高顯著提升 → 建議設計族群確認加分
    # 空輸入給誠實佔位、不爆
    empty_md = render_interaction_report(pl.DataFrame(), "ambush", _INTER_PARAMS, {})
    assert "無法分格" in empty_md


def _interaction_g_harmful_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """同 _interaction_fixture 但起漲事件落 S+G−（G低）→ S+ 內 G高顯著「降低」命中（z<−界）。"""
    panel, _ = _interaction_fixture()
    ep_rows = [{"stock_id": f"PN{k}", "start_date": date(2025, 1, 1) + timedelta(days=7)}
               for k in range(3)]
    episodes = pl.DataFrame(ep_rows, schema={"stock_id": pl.Utf8, "start_date": pl.Date})
    return panel, episodes


def test_render_interaction_g_high_harmful_verdict():
    # 起漲集中 S+G−（個股落後族群）→ 裁決應否證交互、給反向發現「G低才是補漲訊號」
    panel, episodes = _interaction_g_harmful_fixture()
    tab = interaction_2x2_table(panel, episodes, **_INTER_KW)
    by = {r["cell"]: r for r in tab.iter_rows(named=True)}
    assert by["S+G−"]["hit_rate"] > by["S+G+"]["hit_rate"]  # G低命中 > G高
    md = render_interaction_report(tab, "ambush", _INTER_PARAMS, {}, min_triggers=8, z_sig=1.96)
    assert "否證" in md and "落後" in md  # 反向發現＝落後其族群才是補漲訊號
    assert "建議設計" not in md  # 不得誤判為升級


# ── M-Part C / C-P1：族群內落後度單調（rs_subind direction=decreasing；docs/16）────────


def _laggard_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """5 檔 rs_subind 由低（落後族群）到高（領先），前瞻報酬隨 rs_subind 遞減（越落後越強）；
    起漲事件落最落後兩檔 → 低桶 lift 高、ρ<0（direction=decreasing 顯著）。位階交錯使兩層各含範圍。
    """
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    specs = [  # (sid, rs_subind, 成長率 g, above_low)
        ("L0", -8.0, 0.02, 4.0),   # 最落後・貼低・有起漲
        ("L1", -4.0, 0.01, 50.0),  # 落後・非貼低・有起漲
        ("L2", 0.0, 0.00, 4.0),
        ("L3", 4.0, -0.01, 50.0),
        ("L4", 8.0, -0.02, 4.0),   # 最領先・前瞻最弱
    ]
    rows, ep_rows = [], []
    for sid, rs, g, low in specs:
        for i, d in enumerate(days):
            rows.append({"date": d, "stock_id": sid, "close": 100.0 * (1 + g) ** i,
                         "rs_subind_20d": rs, "above_low_60d_pct": low})
        if sid in ("L0", "L1"):
            ep_rows.append({"stock_id": sid, "start_date": days[7]})
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(ep_rows, schema={"stock_id": pl.Utf8, "start_date": pl.Date})
    return panel, episodes


_LAG_KW = dict(n_buckets=5, fwd_window=5, lead_window=5, occupy_days=5, z_min_periods=2)


def test_factor_monotonicity_decreasing_significant():
    panel, episodes = _laggard_fixture()
    tab = factor_monotonicity_table(panel, episodes, "rs_subind_20d", **_LAG_KW)
    assert not tab.is_empty()
    assert {"stratum", "bucket", "factor_median", "lift"} <= set(tab.columns)
    overall = tab.filter(pl.col("stratum") == "全體").sort("bucket")
    lifts = [v if v is not None else 0.0 for v in overall["lift"].to_list()]
    assert lifts[0] > lifts[-1]  # 低桶（最落後）lift > 高桶（領先）

    sp = factor_monotonicity_spearman(panel, "rs_subind_20d", fwd_window=5, direction="decreasing")
    allrow = sp.filter(pl.col("stratum") == "全體").row(0, named=True)
    assert allrow["spearman_rho"] is not None and allrow["spearman_rho"] < 0  # 落後預測起漲 → ρ<0
    assert allrow["significant"] is True  # direction=decreasing：ρ<0 且 z<−界


def test_factor_monotonicity_direction_flag_matters():
    # 同資料：預設 increasing 不認 ρ<0 為顯著，decreasing 才認（驗 direction 旗有效）
    panel, _ = _laggard_fixture()
    inc = factor_monotonicity_spearman(panel, "rs_subind_20d", fwd_window=5)  # 預設 increasing
    dec = factor_monotonicity_spearman(panel, "rs_subind_20d", fwd_window=5, direction="decreasing")
    assert inc.filter(pl.col("stratum") == "全體").row(0, named=True)["significant"] is False
    assert dec.filter(pl.col("stratum") == "全體").row(0, named=True)["significant"] is True


def test_factor_monotonicity_missing_col_returns_empty():
    panel, episodes = _laggard_fixture()
    assert factor_monotonicity_table(panel, episodes, None).is_empty()
    assert factor_monotonicity_table(panel, episodes, "no_such_col").is_empty()
    assert factor_monotonicity_spearman(panel, None).is_empty()


def test_render_laggard_report_sections_and_verdict():
    panel, episodes = _laggard_fixture()
    tab = factor_monotonicity_table(panel, episodes, "rs_subind_20d", **_LAG_KW)
    sp = factor_monotonicity_spearman(panel, "rs_subind_20d", fwd_window=5, direction="decreasing")
    params = {"rs_window": "20", "n_buckets": 5, "fwd_window": 5, "position_low_pct": 15.0}
    md = render_laggard_monotonicity_report(tab, sp, "ambush", params, {})
    assert "落後" in md and "Spearman" in md and "裁決" in md
    assert "C-P2" in md or "否證" in md  # 必出一種裁決
    # 空輸入給誠實佔位、不爆
    empty_md = render_laggard_monotonicity_report(
        pl.DataFrame(), pl.DataFrame(), "ambush", params, {}
    )
    assert "無法分桶" in empty_md or "無法檢定" in empty_md


def test_laggard_lift_significance_extreme_buckets():
    # on-target 量尺：桶1(最落後) hit 高、桶5(最領先) hit 低、lift 單調遞減 → sig+monotone_dec
    buckets = pl.DataFrame(
        {
            "stratum": ["全體"] * 5,
            "bucket": [1, 2, 3, 4, 5],
            "hits": [100, 80, 50, 20, 5],
            "n_eval": [500, 500, 500, 500, 500],
            "lift": [2.74, 2.46, 1.74, 1.16, 0.24],
        }
    )
    out = _laggard_lift_significance(buckets, 1.96)
    assert out["全體"]["monotone_dec"] is True
    assert out["全體"]["sig"] is True  # 100/500 vs 5/500 → 兩比例 z 遠超界
    assert out["全體"]["z"] is not None and out["全體"]["z"] > 1.96
    # 反例：lift 非遞減（峰在中桶）→ monotone_dec False
    flat = buckets.with_columns(pl.Series("lift", [1.0, 1.4, 1.5, 1.2, 1.1]))
    assert _laggard_lift_significance(flat, 1.96)["全體"]["monotone_dec"] is False


# ── M-Part C / C-P2：冠軍 S+ 內落後濾鏡 precision 增量＋賺賠（docs/16 H3）──────────────


def _laggard_filter_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """冠軍 S+（foreign_flow_20d_z idx5 上穿 0.5、貼低）6 檔：3 檔落後(rs<0,有起漲、續漲)、
    3 檔領先(rs>0,無起漲、價平) → 落後濾鏡顯著提升 precision 且中位報酬不惡化。"""
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    rows, ep_rows = [], []
    for prefix, rs, has_ep in (("LAG", -5.0, True), ("LEAD", 5.0, False)):
        for k in range(3):
            sid = f"{prefix}{k}"
            for i, d in enumerate(days):
                close = 100.0 * (1 + 0.02 * max(0, i - 5)) if prefix == "LAG" else 100.0
                rows.append({"date": d, "stock_id": sid, "close": close,
                             "foreign_flow_20d_z": 2.0 if i >= 5 else 0.0,
                             "above_low_60d_pct": 4.0, "rs_subind_20d": rs})
            if has_ep:
                ep_rows.append({"stock_id": sid, "start_date": days[7]})
    panel = pl.DataFrame(rows)
    episodes = pl.DataFrame(ep_rows, schema={"stock_id": pl.Utf8, "start_date": pl.Date})
    return panel, episodes


_C2_KW = dict(s_z_threshold=0.5, s_low_pct=15.0, lag_threshold=0.0,
              lead_window=5, occupy_days=5, z_min_periods=2)


def test_laggard_filter_precision_gain():
    panel, episodes = _laggard_filter_fixture()
    tbl, z = laggard_filter_precision(panel, episodes, **_C2_KW)
    assert not tbl.is_empty()
    by = {r["group"]: r for r in tbl.iter_rows(named=True)}
    assert set(by) == {"S+全體", "S+且落後", "S+且領先"}
    assert by["S+且落後"]["lift"] > (by["S+且領先"]["lift"] or 0.0)  # 落後命中 > 領先
    assert by["S+且落後"]["lift"] > by["S+全體"]["lift"]  # 濾鏡優於聯集基線
    assert z is not None and z > 1.96  # 落後 vs 領先 兩比例 z 顯著

    # 缺欄回 (空表, None)
    empty, ez = laggard_filter_precision(panel.drop("rs_subind_20d"), episodes, **_C2_KW)
    assert empty.is_empty() and ez is None


def test_payoff_decay_extra_conditions_filters_and_suffixes():
    panel, _ = _laggard_filter_fixture()
    champ = "foreign_flow_20d_z (z>0.5) +low≤15"
    base = payoff_decay_table(panel, horizons=(5,), signals={champ}, z_thresholds=(0.5,))
    filt = payoff_decay_table(panel, horizons=(5,), signals={champ}, z_thresholds=(0.5,),
                              extra_conditions=[pl.col("rs_subind_20d") < 0.0],
                              name_suffix="＋落後")
    assert base.row(0, named=True)["signal"] == champ
    assert filt.row(0, named=True)["signal"] == champ + "＋落後"
    # 濾後只剩落後股（續漲）→ 中位報酬 ≥ 全體基線（領先股價平拉低基線）
    assert filt.row(0, named=True)["median_ret_pct"] >= base.row(0, named=True)["median_ret_pct"]
    assert filt.row(0, named=True)["n"] == 3  # 3 檔落後


def test_render_laggard_filter_report_verdict():
    panel, episodes = _laggard_filter_fixture()
    tbl, z = laggard_filter_precision(panel, episodes, **_C2_KW)
    champ = "foreign_flow_20d_z (z>0.5) +low≤15"
    base = payoff_decay_table(panel, horizons=(2, 5), signals={champ}, z_thresholds=(0.5,))
    filt = payoff_decay_table(panel, horizons=(2, 5), signals={champ}, z_thresholds=(0.5,),
                              extra_conditions=[pl.col("rs_subind_20d") < 0.0],
                              name_suffix="＋落後")
    params = {"s_flow_col": "foreign_flow_20d_z", "s_z_threshold": 0.5, "lag_threshold": 0.0,
              "horizons": [2, 5]}
    md = render_laggard_filter_report(tbl, z, base, filt, "ambush", params, {}, z_sig=1.96)
    assert "H3 precision" in md and "賺賠" in md and "裁決" in md
    assert "升級" in md  # precision 增量＋賺賠不惡化 → 升級
    empty_md = render_laggard_filter_report(pl.DataFrame(), None, base, filt, "ambush", params, {})
    assert "無法分組" in empty_md


# ── WS-H.4b regime 切片（signal_triggers / cp_regime_slice_section）──────────


def test_signal_triggers_rebuilds_named_signal():
    from tw_screener.backtest.stock_calib import signal_triggers

    panel, _ = _scan_fixture()
    trig = signal_triggers(
        panel, "trust_flow_20d_z (z>1.0)", z_thresholds=(1.0,), volume_thresholds=(1.0,)
    )
    assert trig.height == 1 and trig["stock_id"][0] == "A"  # idx5 單一上穿
    # 查無此名 → 空表不炸
    none = signal_triggers(panel, "沒有這個訊號", z_thresholds=(1.0,))
    assert none.is_empty()


def test_cp_regime_slice_missing_labels_unlabeled_bucket():
    from tw_screener.backtest.stock_calib import cp_regime_slice_section

    panel, episodes = _scan_fixture()
    scan = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,),
        position_low_pct=15.0, lead_window=5, occupy_days=5, z_min_periods=2,
    )
    empty_labels = pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8})
    section = cp_regime_slice_section(
        panel, scan, episodes, empty_labels,
        min_triggers=1, min_lift=1.0,
        z_thresholds=(1.0,), volume_thresholds=(1.0,),
        lead_window=5, occupy_days=5, z_min_periods=2,
    )
    text = "\n".join(section)
    assert "## regime 切片（WS-H.4b・最佳因子" in text
    assert "未標" in text  # regime 檔缺 → 全落未標桶照列
    assert "樣本期間" in text  # inference_footer 三行


def test_cp_regime_slice_no_qualified_factor_skips_honestly():
    from tw_screener.backtest.stock_calib import cp_regime_slice_section

    panel, episodes = _scan_fixture()
    scan = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,),
        position_low_pct=15.0, lead_window=5, occupy_days=5, z_min_periods=2,
    )
    empty_labels = pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8})
    section = cp_regime_slice_section(
        panel, scan, episodes, empty_labels,
        min_triggers=1, min_lift=99.0,  # 無因子過門檻
        z_thresholds=(1.0,), volume_thresholds=(1.0,),
        lead_window=5, occupy_days=5, z_min_periods=2,
    )
    assert any("無主 lift 可切片" in ln for ln in section)


def test_cp_report_with_regime_section_keeps_old_sections():
    """回歸：CP 報告＋切片＝純增段——舊段落全數仍在（同 cp_calib_runner 的接法）。"""
    from tw_screener.backtest.stock_calib import (
        cp_regime_slice_section,
        render_cp_calibration_report,
    )

    panel, episodes = _scan_fixture()
    scan = scan_stock_signals(
        panel, episodes, z_thresholds=(1.0,), volume_thresholds=(1.0,),
        position_low_pct=15.0, lead_window=5, occupy_days=5, z_min_periods=2,
    )
    params = {"fwd_n_days": 20, "fwd_x_pct": 15.0, "cooldown_days": 5, "lead_window": 5}
    report = render_cp_calibration_report(
        scan, episodes, "L1 埋伏", "測試獵物", params, {"universe": "listed"},
        min_triggers=1, min_lift=1.0,
    )
    empty_labels = pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8})
    full = report + "\n".join(
        cp_regime_slice_section(
            panel, scan, episodes, empty_labels,
            min_triggers=1, min_lift=1.0,
            z_thresholds=(1.0,), volume_thresholds=(1.0,),
            lead_window=5, occupy_days=5, z_min_periods=2,
        )
    ) + "\n"
    # 舊段落一個不少
    assert "# 個股起漲事件回測校準 — L1 埋伏" in full
    assert "## 訊號掃描" in full
    assert "## 建議因子名單" in full
    assert "統計力但書" in full
    # 新段落接尾
    assert "## regime 切片（WS-H.4b・最佳因子" in full
