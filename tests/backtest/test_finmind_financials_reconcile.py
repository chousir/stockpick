"""tests/backtest/test_finmind_financials_reconcile.py — FinMind 財報對帳純函式（docs/31 §20.15）。

鎖兩個 2026-09-20 初跑暴露的比對錯位：本地 fundamentals 為累計 YTD（須還原單季）、
覆蓋率分母須限回補宇宙。
"""

from __future__ import annotations

import polars as pl

from tw_screener.backtest.finmind_financials_reconcile import (
    build_reconciliation,
    evaluate_criteria,
)


def _fm(rows: list[tuple]) -> pl.DataFrame:
    """rows: (sid, year, quarter, revenue_twd, eps) — 單季。"""
    return pl.DataFrame(
        [{"stock_id": s, "year": y, "quarter": q, "revenue": r, "eps": e}
         for s, y, q, r, e in rows],
        schema={"stock_id": pl.Utf8, "year": pl.Int64, "quarter": pl.Int64,
                "revenue": pl.Float64, "eps": pl.Float64},
    )


def _local(rows: list[tuple]) -> pl.DataFrame:
    """rows: (sid, year, quarter, revenue_m, eps) — 累計 YTD。"""
    return pl.DataFrame(
        [{"stock_id": s, "year": y, "quarter": q, "revenue_m": r, "eps": e}
         for s, y, q, r, e in rows],
        schema={"stock_id": pl.Utf8, "year": pl.Int32, "quarter": pl.Int32,
                "revenue_m": pl.Float64, "eps": pl.Float64},
    )


def test_ytd_local_decumulated_matches_single_quarter_finmind() -> None:
    # FinMind 單季 Q1=100M / Q2=120M；本地累計 Q1=100 / Q2=220
    fm = _fm([("2330", 2026, 1, 100e6, 1.0), ("2330", 2026, 2, 120e6, 2.0)])
    loc = _local([("2330", 2026, 1, 100.0, 1.0), ("2330", 2026, 2, 220.0, 3.0)])
    stats = build_reconciliation(fm, loc, set(), {"2330"})
    assert stats["revenue"]["n"] == 2
    assert stats["revenue"]["median"] == 1.0
    assert stats["eps_sign_consistency"] == 1.0
    passed, _ = evaluate_criteria(stats)
    assert passed


def test_local_quarter_without_prior_quarter_is_excluded_not_compared() -> None:
    # 本地只有累計 Q2、缺 Q1 → 無法還原單季，不得拿累計值去比
    fm = _fm([("2330", 2026, 2, 120e6, 2.0)])
    loc = _local([("2330", 2026, 2, 220.0, 3.0)])
    stats = build_reconciliation(fm, loc, set(), {"2330"})
    assert stats["revenue"]["n"] == 0
    assert stats["n_coverage_denom"] == 0


def test_coverage_denominator_limited_to_backfill_universe() -> None:
    # 2330 在宇宙且有 FinMind；9999 不在宇宙、無 FinMind → 不拉低判準覆蓋率
    fm = _fm([("2330", 2026, 1, 100e6, 1.0)])
    loc = _local([("2330", 2026, 1, 100.0, 1.0), ("9999", 2026, 1, 50.0, 0.5)])
    stats = build_reconciliation(fm, loc, set(), {"2330"})
    assert stats["coverage"] == 1.0
    assert stats["n_coverage_denom"] == 1
    assert stats["coverage_all_local"] == 0.5
    assert stats["n_coverage_denom_all_local"] == 2


def test_universe_member_missing_from_finmind_lowers_coverage() -> None:
    fm = _fm([("2330", 2026, 1, 100e6, 1.0)])
    loc = _local([("2330", 2026, 1, 100.0, 1.0), ("2317", 2026, 1, 80.0, 0.8)])
    stats = build_reconciliation(fm, loc, set(), {"2330", "2317"})
    assert stats["coverage"] == 0.5
    passed, checks = evaluate_criteria(stats)
    assert not passed
    assert any(not ok for _, ok, _ in checks)


def test_financial_stocks_excluded_from_coverage_denominator() -> None:
    fm = _fm([("2330", 2026, 1, 100e6, 1.0)])
    loc = _local([("2330", 2026, 1, 100.0, 1.0), ("2881", 2026, 1, 70.0, 0.7)])
    stats = build_reconciliation(fm, loc, {"2881"}, {"2330", "2881"})
    assert stats["coverage"] == 1.0
    assert stats["n_coverage_denom"] == 1
