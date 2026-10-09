"""tests/backtest/test_fv_replica_read.py — §20.17 效度檢驗的純函式（全離線、合成資料）。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest import fv_replica_read as rd
from tw_screener.backtest.fv_replica_panel import monthly_anchors


def test_months_spanned() -> None:
    assert [rd.months_spanned(h) for h in (60, 120, 250)] == [3, 6, 12]


def _ci(mean: float | None, lo: float | None, hi: float | None) -> dict:
    return {"mean": mean, "lo": lo, "hi": hi, "T": 50}


def test_verdict_branches() -> None:
    z = _ci(0.0, -0.01, 0.01)
    pos = _ci(0.05, 0.02, 0.08)
    neg = _ci(-0.05, -0.08, -0.02)
    assert "無預測力" in rd.verdict(z, z, True, True, z)
    assert "否證" in rd.verdict(neg, neg, True, True, z)
    assert "動能／規模代理" in rd.verdict(pos, z, True, True, z)
    assert "未達" in rd.verdict(pos, pos, False, True, z)
    assert "≈現行綜合缺口%" in rd.verdict(pos, pos, True, True, z)
    assert "勝過綜合缺口%" in rd.verdict(pos, pos, True, True, pos)


def _panel(n_dates: int = 40, n_stocks: int = 60, signal: float = 1.0) -> pl.DataFrame:
    rng = random.Random(0)
    rows = []
    d0 = date(2018, 1, 1)
    for i in range(n_dates):
        d = d0 + timedelta(days=30 * i)
        for s in range(n_stocks):
            up = rng.uniform(-50, 50)
            r = signal * up / 5 + rng.gauss(0, 5)
            rows.append(
                {
                    "date": d,
                    "stock_id": str(s),
                    "fv_upside_pct": up,
                    "price": 100.0,
                    "fv_replica": 100 * (1 + up / 100),
                    "r120": r,
                }
            )
    df = pl.DataFrame(rows)
    return df.with_columns(
        (pl.col("r120") - pl.col("r120").median().over("date")).alias("alpha120")
    )


def test_daily_partial_ic_detects_signal() -> None:
    ci = rd.series_ci(rd.daily_partial_ic_series(_panel(), "fv_upside_pct", "alpha120", []), 7)
    assert ci["T"] == 40 and ci["mean"] > 0.5 and rd.excludes_zero(ci)
    null = rd.series_ci(
        rd.daily_partial_ic_series(_panel(signal=0.0), "fv_upside_pct", "alpha120", []), 7
    )
    assert abs(null["mean"]) < 0.1


def test_convergence_beats_permutation_only_with_signal() -> None:
    cv = rd.convergence(_panel(), "fv_upside_pct", 120, abs_min=10.0, n_perm=20)
    assert cv["hit_a"] > cv["perm_a_hi"]
    cv0 = rd.convergence(_panel(signal=0.0), "fv_upside_pct", 120, abs_min=10.0, n_perm=20)
    assert cv0["perm_a_lo"] <= cv0["hit_a"] <= cv0["perm_a_hi"] + 0.05


def test_level_error_naive_comparison() -> None:
    df = pl.DataFrame({"price": [100.0, 100.0], "fv_replica": [120.0, 80.0], "r120": [20.0, -20.0]})
    le = rd.level_error(df, "fv_replica", 120)
    assert le["n"] == 2 and le["err_fv"] == pytest.approx(0.0) and le["share_fv_better"] == 1.0


def test_monthly_anchors_skip_thin_days() -> None:
    rows = []
    for d, n in ((date(2020, 1, 2), 3), (date(2020, 1, 3), 10), (date(2020, 2, 3), 10)):
        rows += [{"date": d, "stock_id": str(i), "close": 1.0} for i in range(n)]
    got = monthly_anchors(pl.DataFrame(rows), date(2020, 1, 1), date(2020, 12, 31), min_names=5)
    assert got == [date(2020, 1, 3), date(2020, 2, 3)]


def test_compare_investing_reports_agreement() -> None:
    filled = pl.DataFrame(
        {
            "stock_id": ["1", "2", "3", "4"],
            "price": [100.0, 100.0, 100.0, 100.0],
            "fv_replica": [120.0, 80.0, 150.0, 60.0],
            "m4_lo": [90.0, 70.0, 100.0, 40.0],
            "m4_hi": [130.0, 90.0, 160.0, 60.0],
            "investing_fv": ["120", "80", "150", "60"],
        }
    )
    md = rd.compare_investing(filled)
    assert "| fv_replica | 4 | +1.00 | 100% | 0.00 |" in md
    assert "m4_mid" in md
