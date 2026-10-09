"""tests/analysis/test_fv_replica.py — investing.com 式多模型估值仿製版（docs/31 §20.17）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from tw_screener.analysis import fv_replica as fr
from tw_screener.analysis.dcf import build_dcf_inputs

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
_FV_CFG = {"epv_tax_rate": 0.2, "ddm_stage1_years": 5}


def _synthetic(sid: str = "9999", years: range = range(2013, 2025)) -> tuple[pl.DataFrame, ...]:
    """穩定成長 5%/年的合成公司：單季營收、YTD 現金流、季末資產負債。"""
    fin, cf, bs = [], [], []
    for i, y in enumerate(years):
        rev_q = 100e6 * 1.05**i
        for q in (1, 2, 3, 4):
            fin.append(
                {
                    "stock_id": sid,
                    "year": y,
                    "quarter": q,
                    "revenue": rev_q,
                    "operating_income": rev_q * 0.2,
                    "income_after_tax": rev_q * 0.16,
                    "eps": rev_q * 0.16 / 10e6,
                }
            )
            cf.append(
                {
                    "stock_id": sid,
                    "year": y,
                    "quarter": q,
                    "ocf": rev_q * 0.25 * q,
                    "ocf_alt": None,
                    "capex": -rev_q * 0.05 * q,
                }
            )
            bs.append(
                {
                    "stock_id": sid,
                    "year": y,
                    "quarter": q,
                    "cash": 50e6,
                    "st_debt": 10e6,
                    "bonds_payable": None,
                    "lt_debt": 20e6,
                    "equity": 500e6,
                    "liabilities": 100e6,
                    "total_assets": 600e6,
                }
            )
    return pl.DataFrame(fin), pl.DataFrame(cf), pl.DataFrame(bs)


@pytest.mark.parametrize(
    ("y", "q", "expected"),
    [
        (2024, 1, date(2024, 5, 15)),
        (2024, 2, date(2024, 8, 14)),
        (2024, 3, date(2024, 11, 14)),
        (2023, 4, date(2024, 3, 31)),
    ],
)
def test_quarter_deadline(y: int, q: int, expected: date) -> None:
    assert fr.quarter_deadline(y, q) == expected


def test_filter_available_is_strictly_after_deadline() -> None:
    df = pl.DataFrame({"year": [2023, 2024, 2024], "quarter": [4, 1, 2]})
    # 截止日當天不可用（< anchor），隔天才可用；Q2 在 5/16 尚不可得（無前視）
    assert fr.filter_available(df, date(2024, 3, 31)).height == 0
    assert fr.filter_available(df, date(2024, 4, 1)).height == 1
    assert fr.filter_available(df, date(2024, 5, 15)).height == 1
    got = fr.filter_available(df, date(2024, 5, 16))
    assert got.select("year", "quarter").rows() == [(2023, 4), (2024, 1)]


def test_fundamentals_asof_respects_filing_lag() -> None:
    fin, cf, bs = _synthetic(years=range(2013, 2025))
    snap = fr.fundamentals_asof(fin, cf, bs, date(2024, 5, 16))["9999"]
    assert snap["last_q"] == (2024, 1)
    assert snap["annual_revenue_by_year"][-1][0] == 2023  # 2024 年未滿 4 季
    assert snap["annual_fcf_by_year"][-1][0] == 2023
    assert snap["rev_ttm"] == pytest.approx(
        sum(100e6 * 1.05**10 for _ in range(3)) + 100e6 * 1.05**11
    )


def test_fundamentals_asof_ttm_needs_consecutive_quarters() -> None:
    fin, cf, bs = _synthetic(years=range(2020, 2024))
    fin = fin.filter(~((pl.col("year") == 2023) & (pl.col("quarter") == 2)))
    snap = fr.fundamentals_asof(fin, cf, bs, date(2025, 1, 1))["9999"]
    assert snap["rev_ttm"] is None and snap["eps_ttm"] is None


def test_derive_shares_paths() -> None:
    assert fr.derive_shares(16e6, 1.6, None, None, None) == pytest.approx(10e6)
    # EPS 太小 → 退 權益/(P/PBR)
    assert fr.derive_shares(1e5, 0.01, 500e6, 50.0, 1.0) == pytest.approx(10e6)
    assert fr.derive_shares(None, None, None, 50.0, None) is None


def test_peer_median_excludes_self_and_falls_back() -> None:
    vals = {"a": 10.0, "b": 20.0, "c": 30.0, "d": None, "e": -5.0, "x": 99.0}
    assert fr.peer_median(vals, "x", ["a", "b", "c", "x"], [], min_peers=3) == 20.0
    # 次產業不足 → 退產業
    assert fr.peer_median(vals, "x", ["a", "x"], ["a", "b", "c"], min_peers=3) == 20.0
    assert fr.peer_median(vals, "x", ["a"], ["b"], min_peers=3) is None


def test_composite_main_drops_outside_band_and_averages() -> None:
    models = {"a": 50.0, "b": 150.0, "c": 300.0, "d": 30.0, "e": None, "f": -1.0}
    fv, n = fr.composite_main(models, price=100.0, lo_mult=0.4, hi_mult=2.0)
    assert n == 2 and fv == pytest.approx(100.0)  # 只留 50、150
    fv_raw, n_raw = fr.composite_raw(models)
    assert n_raw == 4 and fv_raw == pytest.approx(100.0)  # median(30,50,150,300)


def test_m2_matches_production_dcf_intrinsic_est() -> None:
    """正確性 gate：m2（10y FCFE）與 production `dcf_intrinsic_est` 同輸入同參數 → 同值。"""
    fin, cf, bs = _synthetic()
    snap = fr.fundamentals_asof(fin, cf, bs, date(2030, 1, 1))["9999"]
    shares = 10e6
    prod = build_dcf_inputs(
        cf,
        fin,
        bs,
        {"9999": shares},
        pl.DataFrame({"stock_id": ["9999"], "industry_name": ["電子零組件業"]}),
        _DCF_CFG,
        risk_free_rate_pct=1.6,
        erp_pct=5.5,
    )["dcf_intrinsic_est"][0]
    models = fr.replica_models(
        snap,
        price=100.0,
        shares=shares,
        bvps=50.0,
        dps_ttm=None,
        industry_name="電子零組件業",
        is_financial=False,
        peer_meds={},
        dcf_cfg=_DCF_CFG,
        fv_cfg=_FV_CFG,
    )
    assert prod is not None
    assert models["m2_dcf10"] == pytest.approx(prod)
    assert models["m1_dcf5"] is not None and models["m3_dcf_rev"] is not None


def test_financials_skip_dcf_epv_ps_but_keep_pb_and_ddm() -> None:
    fin, cf, bs = _synthetic()
    snap = fr.fundamentals_asof(fin, cf, bs, date(2030, 1, 1))["9999"]
    models = fr.replica_models(
        snap,
        price=100.0,
        shares=10e6,
        bvps=50.0,
        dps_ttm=3.0,
        industry_name="金融保險",
        is_financial=True,
        peer_meds={"pe": 12.0, "pb": 1.2, "ps": 3.0, "evs": 3.0},
        dcf_cfg=_DCF_CFG,
        fv_cfg=_FV_CFG,
    )
    for k in ("m1_dcf5", "m2_dcf10", "m3_dcf_rev", "m4_epv", "m9_peer_ps", "m10_peer_evs"):
        assert models[k] is None, k
    assert models["m8_peer_pb"] == pytest.approx(60.0)
    assert models["m5_ddm_stable"] == pytest.approx(3.0 * 1.02 / 0.06)
    assert models["m7_peer_pe"] is not None
