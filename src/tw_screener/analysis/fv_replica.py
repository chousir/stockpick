"""investing.com 式多模型估值仿製版（docs/31 §20.17，研究用）。

InvestingPro Fair Value 公開方法＝~17 個模型（DCF／EPV／同業倍數／DDM）**簡單平均**、
單一模型偏離現價 >+100% 或 <−60% 剔除、銀行不做 DCF。本模組在本地 FinMind 快取上照時點重建其中 10 個
（m1–m10，清單見 docs/31 §20.17），供 `backtest/fv_replica_panel.py` 逐月回放、
`fv_replica_read.py` 檢驗效度。

紅線：本模組輸出叫「仿製估值（研究用）」`fv_replica`，**不是**公允價／目標價／合理價，不進任何排序 /
picks / candidates / make week。「公允價值」一詞只用於指稱 investing.com 的產品。

純函式、全離線。DCF 本體與排除 gate 全部重用 `analysis/dcf.py`（m2 ≡ `dcf_intrinsic_est` 同法）。
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import polars as pl

from tw_screener.analysis.dcf import (
    _DEBT_COLS,
    conservative_growth_rate,
    dcf_intrinsic_value,
    dcf_with_guardrails,
    fcf_base,
    revenue_yoy_swing_pp,
)

MODEL_KEYS: tuple[str, ...] = (
    "m1_dcf5",
    "m2_dcf10",
    "m3_dcf_rev",
    "m4_epv",
    "m5_ddm_stable",
    "m6_ddm_2stage",
    "m7_peer_pe",
    "m8_peer_pb",
    "m9_peer_ps",
    "m10_peer_evs",
)

#: 法定申報截止（月, 日）；Q4＝次年 3/31（年報）。FinMind 快取無公告日 → 用截止日當可得日。
_DEADLINE_MD: dict[int, tuple[int, int]] = {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)}


def quarter_deadline(year: int, quarter: int) -> date:
    """(year, quarter) 財報的法定申報截止日（Q4 → 次年 3/31）。"""
    m, d = _DEADLINE_MD[quarter]
    return date(year + 1 if quarter == 4 else year, m, d)


def filter_available(df: pl.DataFrame, anchor: date) -> pl.DataFrame:
    """只留截止日 < anchor 的季別列（申報延遲 gate；df 需含 year/quarter）。"""
    if df.is_empty():
        return df
    q4 = pl.date(pl.col("year") + 1, 3, 31)
    q1 = pl.date(pl.col("year"), 5, 15)
    q2 = pl.date(pl.col("year"), 8, 14)
    q3 = pl.date(pl.col("year"), 11, 14)
    deadline = (
        pl.when(pl.col("quarter") == 1)
        .then(q1)
        .when(pl.col("quarter") == 2)
        .then(q2)
        .when(pl.col("quarter") == 3)
        .then(q3)
        .otherwise(q4)
    )
    return df.filter(deadline < anchor)


def fundamentals_asof(
    financials: pl.DataFrame,
    cashflow: pl.DataFrame,
    balancesheet: pl.DataFrame,
    anchor: date,
) -> dict[str, dict[str, Any]]:
    """錨點 t 時可得的財報 → 每檔一個 dict（TTM、完整年序列、最近一季資產負債）。

    - 完整年＝4 季都可得的年（營收／營業利益 4 季加總）；FCF 取現金流量表 Q4 累計列。
    - TTM＝最近 4 個可得季（須連續 4 季；不足 → TTM 欄 None）。
    """
    fin = filter_available(financials, anchor).sort(["stock_id", "year", "quarter"])
    cf = filter_available(cashflow, anchor).sort(["stock_id", "year", "quarter"])
    bs = filter_available(balancesheet, anchor).sort(["stock_id", "year", "quarter"])
    out: dict[str, dict[str, Any]] = {}

    for sid, g in fin.group_by("stock_id", maintain_order=True):
        rows = g.to_dicts()
        s: dict[str, Any] = {}
        years: dict[int, list[dict]] = {}
        for r in rows:
            years.setdefault(int(r["year"]), []).append(r)
        full = [y for y in sorted(years) if len(years[y]) >= 4]
        s["annual_revenue_by_year"] = [
            (y, sum(float(r["revenue"]) for r in years[y]))
            for y in full
            if all(r["revenue"] is not None for r in years[y])
        ]
        s["annual_revenue"] = [v for _, v in s["annual_revenue_by_year"]]
        s["annual_op_income"] = [
            sum(float(r["operating_income"]) for r in years[y])
            for y in full
            if all(r["operating_income"] is not None for r in years[y])
        ]
        last4 = rows[-4:]
        consecutive = len(last4) == 4 and all(
            (last4[i]["year"] * 4 + last4[i]["quarter"])
            - (last4[i - 1]["year"] * 4 + last4[i - 1]["quarter"])
            == 1
            for i in range(1, 4)
        )

        def _ttm(col: str, quarters: list[dict] = last4, ok: bool = consecutive) -> float | None:
            if not ok or any(r[col] is None for r in quarters):
                return None
            return sum(float(r[col]) for r in quarters)

        s["rev_ttm"] = _ttm("revenue")
        s["ni_ttm"] = _ttm("income_after_tax")
        s["eps_ttm"] = _ttm("eps")
        s["any_quarterly_loss"] = (
            any(r["income_after_tax"] is not None and r["income_after_tax"] < 0 for r in last4)
            if consecutive
            else None
        )
        s["last_q"] = (int(rows[-1]["year"]), int(rows[-1]["quarter"])) if rows else None
        out[str(sid[0])] = s

    for sid, g in cf.group_by("stock_id", maintain_order=True):
        fcf = []
        for r in g.filter(pl.col("quarter") == 4).iter_rows(named=True):
            ocf = r["ocf"] if r["ocf"] is not None else r["ocf_alt"]
            if ocf is not None and r["capex"] is not None:
                fcf.append((int(r["year"]), float(ocf) - abs(float(r["capex"]))))
        out.setdefault(str(sid[0]), {})["annual_fcf_by_year"] = fcf

    for sid, g in bs.group_by("stock_id", maintain_order=True):
        r = g.row(-1, named=True)
        debt = sum(float(r[c]) for c in _DEBT_COLS if r[c] is not None)
        cash = float(r["cash"]) if r["cash"] is not None else 0.0
        d = out.setdefault(str(sid[0]), {})
        d["net_debt"] = debt - cash
        d["equity"] = float(r["equity"]) if r["equity"] is not None else None
    return out


def derive_shares(
    ni_ttm: float | None,
    eps_ttm: float | None,
    equity: float | None,
    price: float | None,
    pbr: float | None,
    min_abs_eps: float = 0.1,
) -> float | None:
    """歷史股數：TTM 淨利／TTM EPS（|EPS|≥min_abs_eps）；否則 權益／(P/PBR)。皆不可得 → None。"""
    if ni_ttm is not None and eps_ttm is not None and abs(eps_ttm) >= min_abs_eps:
        sh = ni_ttm / eps_ttm
        if sh > 0:
            return sh
    if equity is not None and equity > 0 and price and pbr and pbr > 0:
        return equity / (price / pbr)
    return None


def peer_median(
    values: Mapping[str, float | None],
    sid: str,
    peers: Sequence[str],
    fallback_peers: Sequence[str],
    min_peers: int,
) -> float | None:
    """同儕中位（排除自己、只取正值）；同儕 <min_peers → 退 fallback；仍不足 → None。"""
    for group in (peers, fallback_peers):
        vals: list[float] = []
        for p in group:
            v = values.get(p)
            if p != sid and v is not None and v > 0:
                vals.append(v)
        if len(vals) >= min_peers:
            return float(statistics.median(vals))
    return None


def _dcf_cfg_with(dcf_cfg: Mapping[str, Any], stage1_years: int) -> dict[str, Any]:
    cfg = dict(dcf_cfg)
    cfg["stage1_years"] = stage1_years
    return cfg


def replica_models(
    snap: Mapping[str, Any],
    *,
    price: float,
    shares: float | None,
    bvps: float | None,
    dps_ttm: float | None,
    industry_name: str | None,
    is_financial: bool,
    peer_meds: Mapping[str, float | None],
    dcf_cfg: Mapping[str, Any],
    fv_cfg: Mapping[str, Any],
) -> dict[str, float | None]:
    """單檔單錨點 → m1–m10 per-share 值（不可得＝None）。公式見 docs/31 §20.17 表。"""
    out: dict[str, float | None] = dict.fromkeys(MODEL_KEYS)
    r = float(dcf_cfg.get("discount_rate_floor_pct", 8.0))
    gt = float(dcf_cfg.get("terminal_growth_pct", 2.0))
    tax = float(fv_cfg.get("epv_tax_rate", 0.2))
    ddm_years = int(fv_cfg.get("ddm_stage1_years", 5))

    annual_rev = snap.get("annual_revenue") or []
    fcf_pairs = snap.get("annual_fcf_by_year") or []
    annual_fcf = [v for _, v in fcf_pairs]
    growth = conservative_growth_rate(
        annual_rev,
        years=int(dcf_cfg.get("mechanical_growth_years", 5)),
        haircut=float(dcf_cfg.get("mechanical_growth_haircut", 0.7)),
        cap_pct=float(dcf_cfg.get("mechanical_growth_cap_pct", 15.0)),
    )
    swing = revenue_yoy_swing_pp(annual_rev)
    net_debt = snap.get("net_debt")
    fcf0, _ = fcf_base(annual_fcf)

    # m1/m2：DCF-FCFE（gate 全同 §20.15）
    for key, years in (("m1_dcf5", 5), ("m2_dcf10", 10)):
        res = dcf_with_guardrails(
            fcf0=fcf0,
            growth_pct=growth,
            computed_discount_rate_pct=0.0,
            net_debt=net_debt,
            shares=shares,
            dcf_cfg=_dcf_cfg_with(dcf_cfg, years),
            industry_name=industry_name,
            any_quarterly_loss=snap.get("any_quarterly_loss"),
            rev_yoy_swing_pp=swing,
        )
        out[key] = res["dcf_intrinsic_est"]

    # m3：營收驅動 DCF（同 gate；fcf0＝TTM 營收 × 近 3 年平均 FCF margin）
    rev_ttm = snap.get("rev_ttm")
    rev_by_year = dict(snap.get("annual_revenue_by_year") or [])
    margins = [f / rev_by_year[y] for y, f in fcf_pairs[-3:] if rev_by_year.get(y)]
    if out["m2_dcf10"] is not None and rev_ttm and len(margins) >= 3:
        m = sum(margins) / len(margins)
        if m > 0:
            out["m3_dcf_rev"] = dcf_intrinsic_value(
                rev_ttm * m, growth, r, gt, 10, net_debt or 0.0, shares
            )

    # m4：EPV
    op = snap.get("annual_op_income") or []
    if not is_financial and len(op) >= 3 and shares and net_debt is not None:
        ebit = sum(op[-3:]) / 3.0
        if ebit > 0:
            out["m4_epv"] = (ebit * (1 - tax) / (r / 100.0) - net_debt) / shares

    # m5/m6：DDM
    if dps_ttm and dps_ttm > 0:
        out["m5_ddm_stable"] = dps_ttm * (1 + gt / 100) / ((r - gt) / 100)
        if growth is not None:
            g1 = growth / 100.0
            pv = sum(dps_ttm * (1 + g1) ** t / (1 + r / 100) ** t for t in range(1, ddm_years + 1))
            d_n = dps_ttm * (1 + g1) ** ddm_years
            tv = d_n * (1 + gt / 100) / ((r - gt) / 100)
            out["m6_ddm_2stage"] = pv + tv / (1 + r / 100) ** ddm_years

    # m7–m10：同儕倍數
    eps_ttm = snap.get("eps_ttm")
    if eps_ttm and eps_ttm > 0 and peer_meds.get("pe"):
        out["m7_peer_pe"] = peer_meds["pe"] * eps_ttm  # type: ignore[operator]
    if bvps and bvps > 0 and peer_meds.get("pb"):
        out["m8_peer_pb"] = peer_meds["pb"] * bvps  # type: ignore[operator]
    if not is_financial and rev_ttm and rev_ttm > 0 and shares:
        if peer_meds.get("ps"):
            out["m9_peer_ps"] = peer_meds["ps"] * rev_ttm / shares  # type: ignore[operator]
        if peer_meds.get("evs") and net_debt is not None:
            out["m10_peer_evs"] = (peer_meds["evs"] * rev_ttm - net_debt) / shares  # type: ignore[operator]
    return out


def composite_main(
    models: Mapping[str, float | None], price: float, lo_mult: float, hi_mult: float
) -> tuple[float | None, int]:
    """主版（照 investing.com）：剔除 ≤0 與 ∉[lo_mult·P, hi_mult·P] 的模型 → 簡單平均。"""
    kept = [
        v
        for v in models.values()
        if v is not None and v > 0 and lo_mult * price <= v <= hi_mult * price
    ]
    if not kept:
        return None, 0
    return sum(kept) / len(kept), len(kept)


def composite_raw(models: Mapping[str, float | None]) -> tuple[float | None, int]:
    """副版：不做價格剔除、正值模型中位數（檢查主版剔除規則的循環性）。"""
    kept = [v for v in models.values() if v is not None and v > 0]
    if not kept:
        return None, 0
    return float(statistics.median(kept)), len(kept)
