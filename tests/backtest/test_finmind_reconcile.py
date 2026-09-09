"""tests/backtest/test_finmind_reconcile.py — FinMind 對帳純函式（docs/31 §20.14）。"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.backtest.finmind_reconcile import (
    build_reconciliation,
    evaluate_criteria,
)

_FM_SCHEMA = {
    "date": pl.Date, "stock_id": pl.Utf8,
    "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64,
}
_TW_SCHEMA = {**_FM_SCHEMA, "market": pl.Utf8}


def _fm(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"date": d, "stock_id": s, "pe": pe, "pbr": pb, "dividend_yield": 2.0}
         for d, s, pe, pb in rows],
        schema=_FM_SCHEMA,
    )


def _tw(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"date": d, "stock_id": s, "market": "上市",
          "pe": pe, "pbr": pb, "dividend_yield": 2.0}
         for d, s, pe, pb in rows],
        schema=_TW_SCHEMA,
    )


def test_identical_sources_pass_all_criteria() -> None:
    days = [date(2026, 6, d) for d in (12, 13, 16, 17, 18)]
    fm = _fm([(d, s, 15.0, 3.0) for d in days for s in ("2330", "2317", "1101")])
    tw = _tw([(d, s, 15.0, 3.0) for d in days for s in ("2330", "2317", "1101")])
    stats = build_reconciliation(fm, tw)
    assert stats["pe"]["median"] == 1.0
    assert stats["pe_coverage"] == 1.0
    passed, checks = evaluate_criteria(stats)
    assert passed
    assert all(ok for _, ok, _ in checks)


def test_systematic_offset_fails_median_criterion() -> None:
    days = [date(2026, 6, d) for d in (12, 13, 16, 17, 18)]
    # FinMind PE 一律高 20% → 中位比值 1.2，超出 [0.97, 1.03]
    fm = _fm([(d, s, 18.0, 3.0) for d in days for s in ("2330", "2317")])
    tw = _tw([(d, s, 15.0, 3.0) for d in days for s in ("2330", "2317")])
    stats = build_reconciliation(fm, tw)
    assert stats["pe"]["median"] == 1.2
    passed, checks = evaluate_criteria(stats)
    assert not passed
    assert checks[0][1] is False  # 判準 1（中位比值）不過


def test_lossmaker_disagreement_counted() -> None:
    d = date(2026, 6, 12)
    fm = _fm([(d, "2498", None, 1.0)])   # FinMind 判虧損
    tw = _tw([(d, "2498", 30.0, 1.0)])   # TWSE 有值
    stats = build_reconciliation(fm, tw)
    assert stats["lossmaker_fm_none_tw_val"] == 1
    assert stats["pe"].get("n", 0) == 0  # 無可配對 PE


def test_coverage_below_threshold_fails() -> None:
    days = [date(2026, 6, d) for d in (12, 13, 16, 17, 18)]
    # 兩來源都涵蓋全 5 天（靠 2317）；但 FinMind 的 2330 只到第 3 天 → 2330 在 day4/5 未覆蓋
    # FinMind universe = {2330, 2317}；分母＝這兩檔 TWSE-有-PE 的 (stock,date)＝10 筆
    # FinMind 也有 PE 的＝2317×5 + 2330×3 = 8 → 覆蓋率 8/10 = 80% < 95%
    fm = _fm(
        [(d, "2317", 15.0, 3.0) for d in days]
        + [(d, "2330", 15.0, 3.0) for d in days[:3]]
    )
    tw = _tw([(d, s, 15.0, 3.0) for d in days for s in ("2317", "2330")])
    stats = build_reconciliation(fm, tw)
    assert stats["pe_coverage"] == 0.8
    passed, checks = evaluate_criteria(stats)
    assert not passed
    assert checks[2][1] is False  # 判準 3（覆蓋率）不過


def test_empty_pe_overlap_returns_not_passed() -> None:
    stats = build_reconciliation(
        _fm([(date(2026, 6, 12), "2330", None, 3.0)]),
        _tw([(date(2026, 6, 13), "2330", 15.0, 3.0)]),  # 日期不重疊
    )
    passed, _ = evaluate_criteria(stats)
    assert not passed
