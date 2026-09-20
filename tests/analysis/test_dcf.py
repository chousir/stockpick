"""tests/analysis/test_dcf.py — 機械式 DCF（M-Val-FinMind2，docs/31 §20.15）。全離線純函式。"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis import dcf
from tw_screener.data.finmind import (
    _BALANCESHEET_FIELD_MAP,
    _BALANCESHEET_WIDE_SCHEMA,
    _CASHFLOWS_FIELD_MAP,
    _CASHFLOWS_WIDE_SCHEMA,
    _FINANCIALS_FIELD_MAP,
    _FINANCIALS_WIDE_SCHEMA,
    _parse_finmind_long,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "finmind"

# 8 個護欄常數的測試複本——與 config/settings.yaml `cp_value.valuation.dcf` 對齊
# （docs/31 §20.13，這些值凍結不得改）
_DCF_CFG = {
    "equity_risk_premium_pct": 5.5,
    "terminal_growth_pct": 2.0,
    "stage1_years": 10,
    "sensitivity_step_pct": 1.0,
    "discount_rate_floor_pct": 8.0,
    "min_wacc_terminal_spread_pct": 3.0,
    "exclude_industries": ["銀行", "保險", "證券", "金融保險", "金控"],
    "exclude_if_quarterly_loss": True,
    "exclude_if_rev_yoy_swing_pp": 40.0,
}


def _fx(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _cf() -> pl.DataFrame:
    return _parse_finmind_long(
        _fx("cashflows_2330.json"), _CASHFLOWS_FIELD_MAP, _CASHFLOWS_WIDE_SCHEMA
    )


def _fin() -> pl.DataFrame:
    return _parse_finmind_long(
        _fx("financials_2330.json"), _FINANCIALS_FIELD_MAP, _FINANCIALS_WIDE_SCHEMA
    )


def _bs() -> pl.DataFrame:
    return _parse_finmind_long(
        _fx("balancesheet_2330.json"), _BALANCESHEET_FIELD_MAP, _BALANCESHEET_WIDE_SCHEMA
    )


# ── 年度序列 ────────────────────────────────────────────────────────────────
def test_annual_fcf_from_q4() -> None:
    got = dcf.annual_fcf_history(_cf())
    fy23 = got.filter(pl.col("year") == 2023)
    # FY2023：OCF 1,241,967,347,000 − |capex| 949,816,825,000
    assert fy23["fcf"].item() == pytest.approx(292_150_522_000.0)
    assert 2026 not in got["year"].to_list()  # 2026 無 Q4 → 不出現


def test_annual_revenue_incomplete_year_nulled() -> None:
    got = dcf.annual_revenue_history(_fin())
    assert got.filter(pl.col("year") == 2023)["revenue"].item() == pytest.approx(
        2_161_735_841_000.0, rel=1e-6
    )
    assert got.filter(pl.col("year") == 2026)["revenue"].item() is None  # n_q=2


def test_annual_fcf_empty() -> None:
    got = dcf.annual_fcf_history(pl.DataFrame(schema=_CASHFLOWS_WIDE_SCHEMA))
    assert got.is_empty()


# ── 純量 helper ─────────────────────────────────────────────────────────────
def test_conservative_growth_cagr_haircut_cap() -> None:
    # 100 → 200 over 4 年：CAGR = 2^(1/4)−1 ≈ 18.92% → ×0.7 ≈ 13.24%
    assert dcf.conservative_growth_rate([100, 120, 150, 175, 200]) == pytest.approx(
        13.243, abs=0.01
    )
    # cap 15%：暴衝序列被夾
    assert dcf.conservative_growth_rate([100, 400, 900, 1600]) == 15.0
    # <3 年 → None
    assert dcf.conservative_growth_rate([100, 150]) is None
    # 首年 ≤ 0 → None
    assert dcf.conservative_growth_rate([0, 100, 200]) is None


def test_revenue_yoy_swing() -> None:
    # YoY: +50%, −20%, +25% → swing = 50 − (−20) = 70pp
    assert dcf.revenue_yoy_swing_pp([100, 150, 120, 150]) == pytest.approx(70.0)
    assert dcf.revenue_yoy_swing_pp([100, 150]) is None


def test_fcf_base_conservative_min() -> None:
    # min(近3年均, 最近年)
    base, note = dcf.fcf_base([300.0, 200.0, 100.0])
    assert base == pytest.approx(100.0)
    assert "min(" in note
    base2, _ = dcf.fcf_base([50.0, 100.0])
    assert base2 == pytest.approx(100.0)  # <3 年 → 用最近年
    assert dcf.fcf_base([]) == (None, dcf.fcf_base([])[1])


def test_cost_of_equity() -> None:
    assert dcf.cost_of_equity(1.6, 1.0, 5.5) == pytest.approx(7.1)


# ── 核心公式（對手算值鎖死）─────────────────────────────────────────────────
def test_dcf_intrinsic_value_hand_computed() -> None:
    # fcf0=100, g=5%, r=8%, g_term=2%, N=10, net_debt=0, shares=10
    got = dcf.dcf_intrinsic_value(100.0, 5.0, 8.0, 2.0, 10, 0.0, 10.0)
    assert got == pytest.approx(214.1911909, abs=1e-4)


def test_dcf_intrinsic_value_guards() -> None:
    assert dcf.dcf_intrinsic_value(None, 5.0, 8.0, 2.0, 10, 0.0, 10.0) is None
    assert dcf.dcf_intrinsic_value(100.0, 5.0, 8.0, 2.0, 10, 0.0, 0.0) is None  # shares≤0
    assert dcf.dcf_intrinsic_value(100.0, 5.0, 2.0, 2.0, 10, 0.0, 10.0) is None  # r≤g_term


def test_net_cash_lifts_intrinsic() -> None:
    """淨現金（net_debt 負）→ per-share 內在值高於 net_debt=0 的情形。"""
    with_cash = dcf.dcf_intrinsic_value(100.0, 5.0, 8.0, 2.0, 10, -500.0, 10.0)
    no_cash = dcf.dcf_intrinsic_value(100.0, 5.0, 8.0, 2.0, 10, 0.0, 10.0)
    assert with_cash == pytest.approx(no_cash + 50.0)


# ── 護欄 ────────────────────────────────────────────────────────────────────
def _base_kwargs(**over: object) -> dict:
    kw = dict(
        fcf0=1.0e11,
        growth_pct=8.0,
        computed_discount_rate_pct=7.1,  # rf1.6 + 1.0*5.5
        net_debt=-2.0e11,
        shares=1.0e10,
        dcf_cfg=_DCF_CFG,
        industry_name="半導體業",
        any_quarterly_loss=False,
        rev_yoy_swing_pp=10.0,
    )
    kw.update(over)
    return kw


def test_guardrail_floor_always_binds() -> None:
    res = dcf.dcf_with_guardrails(**_base_kwargs())
    assert res["dcf_applicable"] is True
    assert res["dcf_discount_rate"] == pytest.approx(8.0)
    assert res["dcf_rate_binding"] == "floor"


def test_guardrail_sensitivity_grid_present() -> None:
    res = dcf.dcf_with_guardrails(**_base_kwargs())
    s = res["sensitivity"]
    for k in ("wacc_dn_g_dn", "wacc_dn_g_up", "wacc_up_g_dn", "wacc_up_g_up",
              "g1_050", "g1_075", "g1_100"):
        assert s[k] is not None
    # 折現率越低、永續成長越高 → 內在值越高
    assert s["wacc_dn_g_up"] > res["dcf_intrinsic_est"] > s["wacc_up_g_dn"]
    # Stage-1 成長越高 → 內在值越高
    assert s["g1_100"] > s["g1_075"] > s["g1_050"]


def test_exclude_financial_before_loss_gate() -> None:
    """金融業 gate 必須先於「近 4 季虧損」gate——即使 any_quarterly_loss=None（財報 vocab 不同）。"""
    res = dcf.dcf_with_guardrails(
        **_base_kwargs(industry_name="金融保險", any_quarterly_loss=None)
    )
    assert res["dcf_applicable"] is False
    assert "金融保險" in res["dcf_exclude_reason"]


def test_exclude_unknown_industry() -> None:
    res = dcf.dcf_with_guardrails(**_base_kwargs(industry_name=None))
    assert res["dcf_applicable"] is False
    assert "產業別未知" in res["dcf_exclude_reason"]


def test_exclude_quarterly_loss() -> None:
    res = dcf.dcf_with_guardrails(**_base_kwargs(any_quarterly_loss=True))
    assert res["dcf_applicable"] is False
    assert "虧損" in res["dcf_exclude_reason"]


def test_exclude_rev_swing() -> None:
    res = dcf.dcf_with_guardrails(**_base_kwargs(rev_yoy_swing_pp=55.0))
    assert res["dcf_applicable"] is False
    assert "波動" in res["dcf_exclude_reason"]


def test_exclude_missing_fcf_and_negative_fcf() -> None:
    assert dcf.dcf_with_guardrails(**_base_kwargs(fcf0=None))["dcf_applicable"] is False
    neg = dcf.dcf_with_guardrails(**_base_kwargs(fcf0=-1.0e9))
    assert neg["dcf_applicable"] is False
    assert "現金流為負" in neg["dcf_exclude_reason"]


# ── 全市場組裝 ─────────────────────────────────────────────────────────────
def test_build_dcf_inputs_2330() -> None:
    industry_df = pl.DataFrame(
        {"stock_id": ["2330"], "stock_name": ["台積電"],
         "industry_code": ["24"], "industry_name": ["半導體業"]}
    )
    got = dcf.build_dcf_inputs(
        _cf(), _fin(), _bs(),
        shares_map={"2330": 25_932_370_067},
        industry_df=industry_df,
        dcf_cfg=_DCF_CFG,
        risk_free_rate_pct=1.6,
        erp_pct=5.5,
    )
    row = got.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row["dcf_applicable"] is True
    assert row["dcf_discount_rate"] == pytest.approx(8.0)
    assert row["dcf_rate_binding"] == "floor"
    assert row["net_debt"] < 0  # 淨現金
    assert row["dcf_intrinsic_est"] is not None
    # 手代兩階段公式核對機械值（誤差 <1%）
    hand = dcf.dcf_intrinsic_value(
        row["fcf_base"], row["growth_pct"], 8.0, 2.0, 10, row["net_debt"], float(row["shares"])
    )
    assert row["dcf_intrinsic_est"] == pytest.approx(hand, rel=1e-9)


def test_build_dcf_inputs_empty() -> None:
    got = dcf.build_dcf_inputs(
        pl.DataFrame(schema=_CASHFLOWS_WIDE_SCHEMA),
        pl.DataFrame(schema=_FINANCIALS_WIDE_SCHEMA),
        pl.DataFrame(schema=_BALANCESHEET_WIDE_SCHEMA),
        shares_map={}, industry_df=pl.DataFrame(),
        dcf_cfg=_DCF_CFG, risk_free_rate_pct=1.6, erp_pct=5.5,
    )
    assert got.is_empty()
    assert set(got.columns) == set(dcf._DCF_INPUTS_SCHEMA)
