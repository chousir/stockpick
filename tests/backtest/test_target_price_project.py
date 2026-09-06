"""docs/31 §20.13 Phase 2：本週實驗性機械式目標價 runner（全離線合成資料）。

驗 project_week_targets 的投射算術、cell 查無剔除、tier 封頂、_pooled 並列、
_assert_edges_match 切點守衛。
"""

from __future__ import annotations

import polars as pl
import pytest

from tw_screener.backtest.target_price_panel import PCTILES
from tw_screener.backtest.target_price_project import (
    _assert_edges_match,
    project_week_targets,
)


def _fit_lookup(rows: list[dict], horizon: int = 20) -> pl.DataFrame:
    """rows: {cell, p25, p50, p75, n, iqr}。補齊 forward_return_percentiles schema。"""
    out = []
    for r in rows:
        d = {
            "cell": r["cell"],
            "horizon": horizon,
            "n": r.get("n", 50000),
            "n_dates": r.get("n_dates", 600),
            "iqr": r.get("iqr", 10.0),
        }
        for k in PCTILES:
            d[f"p{k}"] = r.get(f"p{k}", r.get("p50", 0.0))
        d["p25"] = r.get("p25", -5.0)
        d["p50"] = r.get("p50", 0.0)
        d["p75"] = r.get("p75", 5.0)
        out.append(d)
    return pl.DataFrame(out)


_CELL_A = "位階中段-8~8｜族群內領先>5"
_CELL_B = "位階貼低≤-8｜族群內落後≤-5"


def _latest(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_project_basic_arithmetic():
    """target = close×(1+cell_P50/100)；p25/p75 同理；_pooled 並列。"""
    lk = _fit_lookup(
        [
            {"cell": _CELL_A, "p25": -4.0, "p50": 2.0, "p75": 8.0, "n": 40000, "iqr": 12.0},
            {"cell": "_pooled", "p25": -5.0, "p50": 0.5, "p75": 6.0, "n": 500000, "iqr": 11.0},
        ]
    )
    latest = _latest(
        [{"stock_id": "1234", "close": 100.0, "cell": _CELL_A, "regime": "中性"}]
    )
    targets, warn = project_week_targets(latest, lk, horizon=20, tier_cap="低")
    assert warn == []
    r = targets.row(0, named=True)
    assert r["target_mechanical"] == pytest.approx(102.0)
    assert r["p25"] == pytest.approx(96.0)
    assert r["p75"] == pytest.approx(108.0)
    assert r["target_pooled"] == pytest.approx(100.5)
    assert r["horizon_td"] == 20
    assert r["iqr_pp"] == pytest.approx(12.0)
    assert r["n_cell"] == 40000


def test_tier_capped_to_low():
    """cell n 大、iqr 小 ⇒ confidence_tier 原本回「高」，但顯示 tier 封頂「低」；tier_raw 保留。"""
    lk = _fit_lookup(
        [
            {"cell": _CELL_A, "p50": 1.0, "n": 100000, "iqr": 9.0},  # → tier_raw 高
            {"cell": "_pooled", "p50": 0.0, "n": 500000, "iqr": 10.0},
        ]
    )
    latest = _latest([{"stock_id": "1", "close": 50.0, "cell": _CELL_A, "regime": "進攻"}])
    targets, _ = project_week_targets(latest, lk, horizon=20, tier_cap="低")
    r = targets.row(0, named=True)
    assert r["tier"] == "低"
    assert r["tier_raw"] == "高"


def test_cell_not_found_skipped_with_warning():
    """profile cell 不在 fit_lookup（或 profile null）→ 該股不出列、記 warning。"""
    lk = _fit_lookup(
        [
            {"cell": _CELL_A, "p50": 1.0},
            {"cell": "_pooled", "p50": 0.0},
        ]
    )
    latest = _latest(
        [
            {"stock_id": "1", "close": 50.0, "cell": _CELL_A, "regime": "中性"},
            {"stock_id": "2", "close": 50.0, "cell": _CELL_B, "regime": "中性"},  # 不在 lk
            {"stock_id": "3", "close": 50.0, "cell": None, "regime": "中性"},     # profile null
        ]
    )
    targets, warn = project_week_targets(latest, lk, horizon=20)
    assert targets["stock_id"].to_list() == ["1"]
    assert any("2" in w for w in warn) and any("3" in w for w in warn)


def test_zero_close_skipped():
    lk = _fit_lookup([{"cell": _CELL_A, "p50": 1.0}, {"cell": "_pooled", "p50": 0.0}])
    latest = _latest([{"stock_id": "1", "close": 0.0, "cell": _CELL_A, "regime": "中性"}])
    targets, warn = project_week_targets(latest, lk, horizon=20)
    assert targets.is_empty()
    assert any("close" in w for w in warn)


def test_assert_edges_match_ok():
    lk = _fit_lookup(
        [
            {"cell": "位階貼低≤-8｜族群內領先>5", "p50": 1.0},
            {"cell": "位階延伸>8｜族群內落後≤-5", "p50": -1.0},
            {"cell": "_pooled", "p50": 0.0},
        ]
    )
    _assert_edges_match(lk, (-8.0, 8.0), (-5.0, 5.0))  # 不 raise


def test_assert_edges_match_rejects_drifted_edges():
    """fit_lookup 內嵌 ≤-8 / >5，但 settings 改成 -10 / -3 → raise（跑完不得回調）。"""
    lk = _fit_lookup(
        [
            {"cell": "位階貼低≤-8｜族群內領先>5", "p50": 1.0},
            {"cell": "_pooled", "p50": 0.0},
        ]
    )
    with pytest.raises(ValueError, match="切點與 settings 不符"):
        _assert_edges_match(lk, (-10.0, 10.0), (-3.0, 3.0))


def test_empty_inputs():
    empty = pl.DataFrame()
    targets, warn = project_week_targets(empty, empty, horizon=20)
    assert targets.is_empty()
    assert warn
