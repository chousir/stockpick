"""docs/31 §20.13 Phase 1：機械式目標價校準回測（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.target_price_panel import forward_return_percentiles
from tw_screener.backtest.target_price_read import (
    EMBARGO_TD,
    MIN_BLOCKS,
    _assert_no_leak,
    calibration_by_regime,
    conditional_calibration,
    confidence_tier,
    coverage_table,
    descriptive_metrics,
    pooled_null_ci,
    project_from_lookup,
    split_fit_test,
)

_CELLS = [f"c{i}" for i in range(9)]  # 9 格，base 報酬隨 index 遞增（真的有貴賤序）


def _panel(n_days: int, stocks: int = 27, seed: int = 0) -> pl.DataFrame:
    """合成面板：date/stock_id/close/r20/r60/r120/regime/cell（9 格，base 報酬單調）。"""
    import random

    rng = random.Random(seed)
    d0 = date(2022, 1, 3)
    rows = []
    for di in range(n_days):
        d = d0 + timedelta(days=di)
        for si in range(stocks):
            ci = si % 9
            cell = _CELLS[ci]
            base = (ci - 4) * 1.5  # -6 .. +6
            rows.append(
                {
                    "date": d,
                    "stock_id": f"{1000 + si}",
                    "close": 100.0,
                    "r20": base + rng.gauss(0, 5),
                    "r60": base * 2 + rng.gauss(0, 8),
                    "r120": base * 3 + rng.gauss(0, 12),
                    "regime": ["進攻", "中性", "防禦"][di % 3],
                    "cell": cell,
                }
            )
    return pl.DataFrame(rows)


def test_split_fit_test_hard_cut_and_weekly_test() -> None:
    panel = _panel(1400)  # 2022-01 起約 3.8 年
    fit, test = split_fit_test(panel)
    assert fit["date"].max() <= date(2024, 12, 31)
    assert test["date"].min() >= date(2025, 1, 1)
    # embargo：fit 尾端剪掉 EMBARGO_TD 個交易日 → fit.max 明顯早於 fit_end
    no_embargo, _ = split_fit_test(panel, embargo_td=0)
    assert no_embargo["date"].max() > fit["date"].max()
    assert no_embargo["date"].n_unique() - fit["date"].n_unique() == EMBARGO_TD
    # test 為週頻 anchor：distinct 日期數應遠少於 test 窗日曆天數
    test_days = test["date"].n_unique()
    cal_days = (test["date"].max() - test["date"].min()).days
    assert test_days < cal_days / 5
    _assert_no_leak(fit, test)  # 不應 raise


def test_assert_no_leak_raises_on_overlap() -> None:
    d = pl.DataFrame({"date": [date(2024, 6, 1)], "stock_id": ["1000"]})
    with pytest.raises(ValueError, match="洩漏"):
        _assert_no_leak(d, d)


def test_project_from_lookup_computes_errors_and_pooled() -> None:
    panel = _panel(1400)
    fit, test = split_fit_test(panel)
    lookup = forward_return_percentiles(fit, horizons=(20,))
    proj = project_from_lookup(test, lookup, horizons=(20,))
    assert not proj.is_empty()
    assert {"err_cell", "err_pooled", "proj_cell_p50", "proj_pooled_p50"}.issubset(
        proj.columns
    )
    # pooled 投射對每列相同（同一 horizon）
    assert proj["proj_pooled_p50"].n_unique() == 1
    # err = |realized - proj_p50|
    row = proj.row(0, named=True)
    assert abs(row["err_cell"] - abs(row["realized"] - row["proj_cell_p50"])) < 1e-9


def test_conditional_calibration_detects_monotone_ordering() -> None:
    # 合成 9 格 base 報酬單調遞增 → fit P60 序與 test 實際序同向 → rho ≈ +1
    panel = _panel(1600, seed=1)
    fit, test = split_fit_test(panel)
    lookup = forward_return_percentiles(fit, horizons=(20,))
    proj = project_from_lookup(test, lookup, horizons=(20,))
    cond = conditional_calibration(proj, lookup, horizons=(20,))
    r = cond.row(0, named=True)
    assert r["n_cells"] == 9
    assert r["spearman_rho"] > 0.8
    assert "排序單調" in r["verdict_1a"]


def test_pooled_null_ci_blocks_guard() -> None:
    panel = _panel(1500, seed=2)
    fit, test = split_fit_test(panel)
    lookup = forward_return_percentiles(fit, horizons=(20, 60, 120))
    proj = project_from_lookup(test, lookup, horizons=(20, 60, 120))
    pooled = pooled_null_ci(proj, horizons=(20, 60, 120), n_boot=200)
    by_h = {r["horizon"]: r for r in pooled.iter_rows(named=True)}
    # r120 block_len = ceil(121/5) = 25 → n_blocks 少 → 無裁決標記
    assert by_h[120]["block_len"] == 25
    if by_h[120]["n_blocks"] < MIN_BLOCKS:
        assert "無裁決" in by_h[120]["verdict_1b"]


def test_coverage_table_eligibility_uses_blocks() -> None:
    panel = _panel(1500, seed=3)
    _, test = split_fit_test(panel)
    cov = coverage_table(test, horizons=(20, 60, 120))
    by_h = {r["horizon"]: r for r in cov.iter_rows(named=True)}
    assert by_h[20]["block_len"] == 5
    assert by_h[120]["block_len"] == 25
    # 一致性：eligible ⇒ n_blocks ≥ MIN_BLOCKS
    for r in cov.iter_rows(named=True):
        if r["verdict_eligible"]:
            assert r["n_blocks"] >= MIN_BLOCKS


def test_descriptive_and_regime_slices_run() -> None:
    panel = _panel(1400, seed=4)
    fit, test = split_fit_test(panel)
    lookup = forward_return_percentiles(fit, horizons=(20,))
    proj = project_from_lookup(test, lookup, horizons=(20,))
    desc = descriptive_metrics(proj, horizons=(20,))
    assert desc.row(0, named=True)["n"] > 0
    assert 0.0 <= desc.row(0, named=True)["cover_p50"] <= 1.0
    by_reg = calibration_by_regime(proj, horizons=(20,))
    assert set(by_reg["regime"].to_list()) == {"進攻", "中性", "防禦"}


def test_confidence_tier_formula() -> None:
    assert confidence_tier(1000, 10.0, regime_thin=False, horizon=20) == "高"
    assert confidence_tier(1000, 10.0, regime_thin=True, horizon=20) == "中"
    assert confidence_tier(1000, 10.0, regime_thin=False, horizon=120) == "中"
    assert confidence_tier(300, 40.0, regime_thin=False, horizon=20) == "中"
    assert confidence_tier(10, None, regime_thin=False, horizon=20) == "低"
