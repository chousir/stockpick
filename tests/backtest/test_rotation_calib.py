"""rotation_calib 測試（R2）：起漲點偵測 / z 標準化 / 觸發穿越 / 命中統計，全離線。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.rotation_calib import (
    compute_triggers,
    detect_breakouts,
    evaluate_triggers,
    render_calibration_report,
    scan_signals,
    standardize_signals,
)

D0 = date(2026, 1, 1)


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _basket(sub: str, index_series: list[float]) -> pl.DataFrame:
    ds = _dates(len(index_series))
    return pl.DataFrame(
        {
            "sub_industry": [sub] * len(ds),
            "date": ds,
            "basket_return_pct": [0.0] * len(ds),
            "basket_index": index_series,
            "members_priced": [5] * len(ds),
        }
    )


# ─── detect_breakouts ─────────────────────────────────────────────────────────


def test_detect_breakout_flat_base_then_rise():
    """60 日打底 100 → 連漲 15 日（約 +16%）→ 起漲點在打底末日，且只計一次。"""
    idx = [100.0] * 60
    for i in range(15):
        idx.append(idx[-1] * 1.01)
    idx += [idx[-1]] * 10  # 高檔走平
    eps = detect_breakouts(
        _basket("記憶體", idx), x_pct=10.0, n_days=15, m_days=60,
        low_base_tol_pct=3.0, cooldown_days=15,
    )
    assert eps.height == 1
    assert eps["start_date"][0] == _dates(len(idx))[59]  # 滿 M 日歷史的首個合格日
    assert eps["fwd_return_pct"][0] == pytest.approx(16.1, abs=0.2)


def test_detect_breakout_rejects_mid_trend():
    """一路上漲（無低基期）：即使前瞻漲幅夠也不算起漲。"""
    idx = [100.0]
    for _ in range(129):
        idx.append(idx[-1] * 1.01)  # +1%/日，130 日
    eps = detect_breakouts(
        _basket("孤鳥", idx), x_pct=10.0, n_days=15, m_days=60,
        low_base_tol_pct=3.0, cooldown_days=15,
    )
    assert eps.is_empty()


def test_detect_breakout_needs_m_days_history():
    idx = [100.0] * 20 + [100.0 * 1.01**i for i in range(1, 16)]
    eps = detect_breakouts(_basket("新籃", idx), m_days=60)
    assert eps.is_empty()  # 35 日 < M=60


# ─── standardize_signals ──────────────────────────────────────────────────────


def test_standardize_z_score():
    n = 10
    flows = pl.DataFrame(
        {
            "sub_industry": ["A"] * n,
            "date": _dates(n),
            "members": [5] * n,
            "net_flow_5d": [0.0] * (n - 1) + [10.0],
            "flow_breadth_5d": [0.5] * n,
        }
    )
    out = standardize_signals(flows, z_window=10, z_min_periods=5)
    assert "net_flow_5d_z" in out.columns
    assert "flow_breadth_5d_z" not in out.columns  # breadth 不轉
    # 最後一點：mean=1, std=sqrt(10*(0.9^2)+... )；只驗 z > 2（明顯離群）
    assert out["net_flow_5d_z"][-1] > 2.0
    # 前 4 筆 warmup → null
    assert out["net_flow_5d_z"].head(4).null_count() == 4


def test_standardize_zero_std_is_null():
    flows = pl.DataFrame(
        {
            "sub_industry": ["A"] * 8,
            "date": _dates(8),
            "members": [5] * 8,
            "net_flow_5d": [3.0] * 8,  # 常數 → std=0
        }
    )
    out = standardize_signals(flows, z_window=8, z_min_periods=4)
    assert out["net_flow_5d_z"].null_count() == 8


# ─── compute_triggers ─────────────────────────────────────────────────────────


def test_triggers_crossing_only_once():
    """連續高於門檻只在穿越日觸發一次；回落再穿越再觸發。"""
    vals = [0.0, 2.0, 2.5, 2.2, 0.0, 3.0]
    flows_z = pl.DataFrame(
        {
            "sub_industry": ["A"] * 6,
            "date": _dates(6),
            "sig_z": vals,
            "flow_momentum": [1.0] * 6,
        }
    )
    trig = compute_triggers(flows_z, "sig_z", 1.0)
    assert trig["date"].to_list() == [_dates(6)[1], _dates(6)[5]]


def test_triggers_momentum_filter():
    flows_z = pl.DataFrame(
        {
            "sub_industry": ["A"] * 3,
            "date": _dates(3),
            "sig_z": [0.0, 2.0, 0.0],
            "flow_momentum": [0.0, -5.0, 0.0],  # 穿越日動能為負
        }
    )
    assert compute_triggers(flows_z, "sig_z", 1.0, require_momentum=True).is_empty()


# ─── evaluate_triggers ────────────────────────────────────────────────────────


def _episodes(sub: str, ds: list[date]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "sub_industry": [sub] * len(ds),
            "start_date": ds,
            "base_index": [100.0] * len(ds),
            "fwd_return_pct": [12.0] * len(ds),
        }
    )


def test_evaluate_hit_with_lead():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    triggers = pl.DataFrame({"sub_industry": ["A"], "date": [cal[45]]})  # 領先 5 日
    stats = evaluate_triggers(triggers, episodes, cal, lead_window=15, occupy_days=15)
    assert stats["hits"] == 1 and stats["false_positives"] == 0
    assert stats["hit_rate"] == 1.0 and stats["recall"] == 1.0
    assert stats["median_lead_days"] == 5
    assert stats["lift"] is not None and stats["lift"] > 1.0


def test_evaluate_false_positive_and_recall():
    cal = _dates(100)
    episodes = _episodes("A", [cal[80]])
    triggers = pl.DataFrame({"sub_industry": ["A"], "date": [cal[10]]})  # 遠離起漲
    stats = evaluate_triggers(triggers, episodes, cal, lead_window=15, occupy_days=15)
    assert stats["hits"] == 0 and stats["false_positives"] == 1
    assert stats["recall"] == 0.0  # episode 無觸發覆蓋


def test_evaluate_excludes_in_episode_and_tail():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    triggers = pl.DataFrame(
        {"sub_industry": ["A", "A"], "date": [cal[55], cal[95]]}
        # 55 在 occupy (50, 65]；95 在尾端不足 lead_window
    )
    stats = evaluate_triggers(triggers, episodes, cal, lead_window=15, occupy_days=15)
    assert stats["n_triggers"] == 0


def test_evaluate_trigger_on_start_day_counts():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    triggers = pl.DataFrame({"sub_industry": ["A"], "date": [cal[50]]})
    stats = evaluate_triggers(triggers, episodes, cal, lead_window=15, occupy_days=15)
    assert stats["hits"] == 1 and stats["median_lead_days"] == 0


# ─── scan_signals + report ────────────────────────────────────────────────────


def _scan_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """90 日、單一次產業：good_flow 在起漲（pos 70）前 3 日跳高；噪音欄全程平。"""
    n = 90
    good = [0.0] * n
    good[67] = 500.0  # 起漲前 3 日資金跳入
    flows = pl.DataFrame(
        {
            "sub_industry": ["記憶體"] * n,
            "date": _dates(n),
            "members": [8] * n,
            "net_flow_5d": good,
            "flow_momentum": [1.0] * n,
            "flow_breadth_5d": [0.4] * n,
        }
    )
    episodes = _episodes("記憶體", [_dates(n)[70]])
    return flows, episodes


def test_scan_signals_finds_leading_signal():
    flows, episodes = _scan_fixture()
    scan = scan_signals(
        flows, episodes, z_window=60, z_min_periods=30,
        z_thresholds=(1.0,), breadth_thresholds=(0.5,),
        lead_window=15, occupy_days=15,
    )
    best = scan.row(0, named=True)
    assert best["signal"].startswith("net_flow_5d")
    assert best["hits"] == 1 and best["hit_rate"] == 1.0
    assert best["median_lead_days"] == 3
    # breadth 0.4 永遠不過 0.5 門檻 → 0 觸發
    breadth_row = scan.filter(pl.col("signal").str.contains("breadth")).row(0, named=True)
    assert breadth_row["n_triggers"] == 0


def test_render_report_contains_sections():
    flows, episodes = _scan_fixture()
    scan = scan_signals(
        flows, episodes, z_thresholds=(1.0,), breadth_thresholds=(0.5,),
        z_window=60, z_min_periods=30, lead_window=15, occupy_days=15,
    )
    params = {
        "x_pct": 10.0, "n_days": 15, "m_days": 60, "low_base_tol_pct": 3.0,
        "cooldown_days": 15, "lead_window": 15, "z_window": 60,
    }
    md = render_calibration_report(
        scan, episodes, params, (D0, D0 + timedelta(days=89)), min_triggers=1, min_lift=1.0
    )
    assert "# 次產業起漲點回測校準報告" in md
    assert "## 訊號掃描" in md
    assert "## 建議名單" in md
    assert "entry_signal" in md  # 建議 settings 區塊
    assert "net_flow_5d" in md
