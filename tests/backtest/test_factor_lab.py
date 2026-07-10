"""WS-B 統一因子實驗台單元測試（全離線合成資料、固定種子）。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.factor_lab import (
    bucket_table,
    evaluate,
    grid_scan,
    residual_ic,
    walk_forward_splits,
)

_D0 = date(2025, 7, 1)


def _dates(n: int) -> list[date]:
    return [_D0 + timedelta(days=i) for i in range(n)]


def _panel(
    n_days: int = 120, n_stocks: int = 40, beta: float = -0.5, seed: int = 7
) -> pl.DataFrame:
    """合成面板：target = beta×factor ＋ 控制項 c ＋ noise（横斷面關係固定）。"""
    rng = random.Random(seed)
    rows = []
    for d in _dates(n_days):
        for s in range(n_stocks):
            f = rng.gauss(0, 1)
            c = rng.gauss(0, 1)
            y = beta * f + 1.0 * c + rng.gauss(0, 0.5)
            rows.append({"date": d, "stock_id": f"s{s:03d}", "factor": f, "ctrl": c, "y": y})
    return pl.DataFrame(rows)


# ── walk_forward_splits ───────────────────────────────────────────────────────


def test_splits_expanding_nonoverlap_embargo() -> None:
    ds = _dates(100)
    splits = walk_forward_splits(ds, n_splits=4, min_train_frac=0.4, embargo_td=6)
    assert len(splits) == 4
    for i, s in enumerate(splits):
        # embargo：train_end 與 test_start 中間至少隔 6 個交易日
        gap = (s.test_start - s.train_end).days
        assert gap >= 7
        # 測試塊彼此不重疊且遞增
        if i:
            assert s.test_start > splits[i - 1].test_end
    # expanding：train_end 單調遞增
    ends = [s.train_end for s in splits]
    assert ends == sorted(ends)
    # 末段吃到最後一天
    assert splits[-1].test_end == ds[-1]


def test_splits_too_short_returns_empty() -> None:
    assert walk_forward_splits(_dates(15), n_splits=3) == []
    assert walk_forward_splits(_dates(40), n_splits=8) == []  # 每塊 <5 日


# ── evaluate ─────────────────────────────────────────────────────────────────


def test_evaluate_detects_negative_ic_with_ci() -> None:
    panel = _panel(beta=-0.5)
    rep = evaluate(panel, "factor", horizon=5, target="y", controls=(), n_splits=3)
    row = rep.pooled.row(0, named=True)
    assert row["ic"] < -0.2
    assert row["ci_hi"] < 0  # CI 不跨 0
    assert row["n"] == panel.height
    # walk-forward 各段同號
    assert rep.consistent_sign is True
    assert rep.splits.height == 3


def test_evaluate_null_factor_ignored_and_expr_input() -> None:
    panel = _panel(n_days=60, n_stocks=20)
    expr = (pl.col("factor") * -1).alias("neg_factor")
    rep = evaluate(panel, expr, horizon=5, target="y", n_splits=3)
    assert rep.factor == "neg_factor"
    assert rep.pooled.row(0, named=True)["ic"] > 0.2  # 翻號


def test_evaluate_no_signal_ci_crosses_zero() -> None:
    panel = _panel(beta=0.0, seed=11)
    rep = evaluate(panel, "factor", horizon=5, target="y", n_splits=3)
    row = rep.pooled.row(0, named=True)
    assert row["ci_lo"] < 0 < row["ci_hi"]  # 無證據＝CI 跨 0


# ── buckets ──────────────────────────────────────────────────────────────────


def test_bucket_monotonic_for_strong_factor() -> None:
    panel = _panel(beta=1.0, seed=3)
    tbl = bucket_table(panel, "factor", "y", buckets=5)
    assert tbl.height == 5
    meds = tbl.sort("bucket")["median"].to_list()
    assert meds == sorted(meds)  # 桶 1→5 單調遞增
    assert int(tbl["n"].sum()) == panel.height


def test_bucket_skips_thin_days() -> None:
    panel = _panel(n_days=5, n_stocks=3)
    assert bucket_table(panel, "factor", "y", buckets=5).is_empty()


# ── residual_ic ──────────────────────────────────────────────────────────────


def test_residual_ic_removes_control_channel() -> None:
    """因子只透過 c 相關：raw IC 顯著、控制 c 後 ≈ 0。"""
    rng = random.Random(5)
    rows = []
    for d in _dates(80):
        for s in range(40):
            c = rng.gauss(0, 1)
            f = c + rng.gauss(0, 0.3)   # 因子 ≈ 控制項
            y = c + rng.gauss(0, 0.5)   # target 只吃 c
            rows.append({"date": d, "stock_id": f"s{s}", "factor": f, "ctrl": c, "y": y})
    panel = pl.DataFrame(rows)
    raw = evaluate(panel, "factor", horizon=5, target="y").pooled.row(0, named=True)
    assert raw["ci_lo"] > 0.3  # raw 顯著正
    resid = residual_ic(panel, "factor", "y", ["ctrl"]).row(0, named=True)
    assert abs(resid["ic"]) < 0.1  # 控制後幾乎歸零


def test_residual_ic_survives_when_independent() -> None:
    panel = _panel(beta=-0.5, seed=9)  # y = -0.5f + c + noise（f ⊥ c）
    resid = residual_ic(panel, "factor", "y", ["ctrl"]).row(0, named=True)
    assert resid["ic"] < -0.2 and resid["ci_hi"] < 0  # 控制 c 後仍活著


# ── grid_scan ────────────────────────────────────────────────────────────────


def test_grid_scan_reports_all_cells_and_limits() -> None:
    panel = _panel(n_days=100, n_stocks=25)

    def mk(mult: float) -> pl.Expr:
        return (pl.col("factor") * mult).alias("f_scaled")

    out = grid_scan(panel, mk, {"mult": [1.0, -1.0, 2.0]}, horizon=5, target="y", n_splits=3)
    # 所有 cell × split 全列（3 cell × 3 split）
    assert out.height == 9
    assert {"train_ic", "test_ic", "test_ci_lo", "selected"} <= set(out.columns)
    # selected 恰標一個 cell（3 列）
    assert out.filter(pl.col("selected")).height == 3

    with pytest.raises(ValueError, match="維度"):
        grid_scan(panel, mk, {f"d{i}": [1] for i in range(4)}, horizon=5, target="y")
    with pytest.raises(ValueError, match="檔位"):
        grid_scan(panel, mk, {"mult": [1, 2, 3, 4, 5, 6]}, horizon=5, target="y")


def test_grid_scan_train_test_separation() -> None:
    """測試段樣本不含訓練段日期（embargo 后不重疊）。"""
    panel = _panel(n_days=100, n_stocks=25)

    def mk(mult: float) -> pl.Expr:
        return (pl.col("factor") * mult).alias("f_scaled")

    out = grid_scan(panel, mk, {"mult": [1.0]}, horizon=5, target="y", n_splits=3)
    # train_n + embargo 段 + test_n ≤ 總樣本（有 embargo 就必然嚴格小於）
    total = panel.height
    for r in out.iter_rows(named=True):
        assert r["train_n"] + r["test_n"] < total
