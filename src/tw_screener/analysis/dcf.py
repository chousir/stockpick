"""機械式 DCF（附錄 G M3，M-Val-FinMind2，docs/31 §20.15）。

**完全機械、護欄兜住**的 per-share 內在價值 `dcf_intrinsic_est` ＋敏感度網格，餵 pick.md
附錄 G 的 Opus M3 當機械錨點（Opus 不重算 DCF 本體，只讀值／從敏感度網格內插）。

紅線（CLAUDE.md 鐵律 2）：`dcf_intrinsic_est` **不是**公允價／目標價／合理價，不進任何
排序 / picks / F2 位階 / 進場階梯 / 停損。只有附錄 G「綜合估值區間」（Opus M4 judgement）
能對外，且固定帶「無歷史驗證、不可回測」免責。

護欄常數一字不動地讀自 `cp_value.valuation.dcf`（8 key，docs/31 §20.13）——這是 DCF 能
被「不否決」的唯一理由，本模組不得自行調整。

**8% 折現率地板恆綁定**：rf 1.6% + β(=1.0)·ERP 5.5% = 7.1% < 8.0% → 每檔折現率都 = 8.0%
（要 rf>6.4% 或 β>1.16 才破地板）。`dcf_intrinsic_est` 因此是**單一風險參數模型**，跨股
變異全來自 FCF／成長／淨負債／股數，不含個股風險區分。

純函式、全離線；FinMind 財報 dataset 語意見 data/finmind.py：
- CashFlows 累計 YTD → quarter==4 列＝全年（避開 de-cumulation 風險）
- Financials 單季 → 4 季加總＝全年
- BalanceSheet 季末快照；capex（`PropertyAndPlantAndEquipment`）為**負值**
"""

from __future__ import annotations

from typing import Any

import polars as pl

_DCF_INPUTS_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "dcf_applicable": pl.Boolean,
    "dcf_exclude_reason": pl.Utf8,
    "dcf_intrinsic_est": pl.Float64,
    "dcf_discount_rate": pl.Float64,       # % ── 恆為 8.0（地板）除非 rf/β 破地板
    "dcf_rate_binding": pl.Utf8,           # computed / floor / spread
    "dcf_terminal_growth": pl.Float64,     # %
    "fcf_base": pl.Float64,                # 元（FinMind 原始量綱）
    "growth_pct": pl.Float64,              # Stage-1 年成長率（保守外推、已 haircut/clip）
    "net_debt": pl.Float64,                # 元；負＝淨現金
    "shares": pl.Int64,
    "rev_yoy_swing_pp": pl.Float64,
    "any_quarterly_loss": pl.Boolean,
    "dcf_debt_codes_found": pl.Utf8,       # 實際命中的短期債 code（漏抓看得見）
    "fcf_base_note": pl.Utf8,
    "growth_note": pl.Utf8,
    # 敏感度網格（進研究 CSV dcf_inputs.csv，不進 candidates_enriched）
    "sens_wacc_dn_g_dn": pl.Float64,
    "sens_wacc_dn_g_up": pl.Float64,
    "sens_wacc_up_g_dn": pl.Float64,
    "sens_wacc_up_g_up": pl.Float64,
    "sens_g1_050": pl.Float64,
    "sens_g1_075": pl.Float64,
    "sens_g1_100": pl.Float64,
}

_DEBT_COLS = ("st_debt", "bonds_payable", "lt_debt")


# ── 年度序列 ────────────────────────────────────────────────────────────────
def annual_fcf_history(cashflow_wide: pl.DataFrame) -> pl.DataFrame:
    """CashFlows（累計 YTD）→ 每檔每年 FCF（取 quarter==4 列＝全年）。

    `fcf = coalesce(ocf, ocf_alt) − |capex|`（capex 為負值，用 `.abs()` 對符號翻轉穩健）。
    缺 Q4 或 ocf/capex 任一缺 → 該年不出現（不外插）。

    Returns:
        (stock_id, year, fcf, ocf, capex)；空輸入 → 空表。
    """
    if cashflow_wide.is_empty():
        return pl.DataFrame(
            schema={"stock_id": pl.Utf8, "year": pl.Int64, "fcf": pl.Float64,
                    "ocf": pl.Float64, "capex": pl.Float64}
        )
    return (
        cashflow_wide.filter(pl.col("quarter") == 4)
        .with_columns(pl.coalesce("ocf", "ocf_alt").alias("_ocf"))
        .filter(pl.col("_ocf").is_not_null() & pl.col("capex").is_not_null())
        .select(
            "stock_id", "year",
            (pl.col("_ocf") - pl.col("capex").abs()).alias("fcf"),
            pl.col("_ocf").alias("ocf"),
            "capex",
        )
        .sort(["stock_id", "year"])
    )


def annual_revenue_history(financials_wide: pl.DataFrame) -> pl.DataFrame:
    """Financials（單季）→ 每檔每年營收（4 季加總）。不足 4 季 → revenue null。

    Returns:
        (stock_id, year, revenue, n_q)；空輸入 → 空表。
    """
    if financials_wide.is_empty():
        return pl.DataFrame(
            schema={"stock_id": pl.Utf8, "year": pl.Int64,
                    "revenue": pl.Float64, "n_q": pl.Int64}
        )
    return (
        financials_wide.filter(pl.col("revenue").is_not_null())
        .group_by(["stock_id", "year"])
        .agg(
            pl.col("revenue").sum().alias("_sum"),
            pl.len().alias("n_q"),
        )
        .with_columns(
            pl.when(pl.col("n_q") >= 4).then(pl.col("_sum")).otherwise(None).alias("revenue")
        )
        .select("stock_id", "year", "revenue", "n_q")
        .sort(["stock_id", "year"])
    )


def quarterly_profit_flags(
    financials_wide: pl.DataFrame, lookback_q: int = 4
) -> pl.DataFrame:
    """近 `lookback_q` 季逐季稅後淨利 → any_loss。不足 `lookback_q` 季 → any_loss null。

    Returns:
        (stock_id, any_quarterly_loss)；空輸入 → 空表。
    """
    schema = {"stock_id": pl.Utf8, "any_quarterly_loss": pl.Boolean}
    if financials_wide.is_empty():
        return pl.DataFrame(schema=schema)
    recent = (
        financials_wide.filter(pl.col("income_after_tax").is_not_null())
        .sort(["stock_id", "year", "quarter"])
        .group_by("stock_id", maintain_order=True)
        .tail(lookback_q)
    )
    return (
        recent.group_by("stock_id")
        .agg(
            pl.len().alias("_n"),
            (pl.col("income_after_tax") < 0).any().alias("_loss"),
        )
        .select(
            "stock_id",
            pl.when(pl.col("_n") >= lookback_q)
            .then(pl.col("_loss"))
            .otherwise(None)
            .alias("any_quarterly_loss"),
        )
        .sort("stock_id")
    )


# ── 純量 helper ─────────────────────────────────────────────────────────────
def conservative_growth_rate(
    annual_revenue: list[float],
    years: int = 5,
    haircut: float = 0.7,
    cap_pct: float = 15.0,
    floor_pct: float = 0.0,
) -> float | None:
    """近 `years` 年完整年營收 CAGR × haircut，clip 到 [floor_pct, cap_pct]（%）。

    `annual_revenue`：由舊到新的完整年營收（呼叫端已濾掉不足 4 季的年）。
    有效年數 < 3（＝至少 2 段年增）或首年營收 ≤ 0 → None。
    """
    vals = [v for v in annual_revenue if v is not None]
    if len(vals) < 3:
        return None
    window = vals[-years:]
    first, last = window[0], window[-1]
    if first is None or first <= 0 or last is None or last <= 0:
        return None
    n = len(window) - 1
    cagr = (last / first) ** (1.0 / n) - 1.0
    rate = cagr * 100.0 * haircut
    return max(floor_pct, min(cap_pct, rate))


def revenue_yoy_swing_pp(annual_revenue: list[float], recent_years: int = 3) -> float | None:
    """近 `recent_years` 段年增率（%）的 max − min（pp）。不足 `recent_years` 段 → None。"""
    vals = [v for v in annual_revenue if v is not None and v > 0]
    if len(vals) < recent_years + 1:
        return None
    yoy = [(vals[i] / vals[i - 1] - 1.0) * 100.0 for i in range(1, len(vals))]
    tail = yoy[-recent_years:]
    return max(tail) - min(tail)


def fcf_base(annual_fcf: list[float]) -> tuple[float | None, str]:
    """FCF 基準：min(近 3 年平均, 最近 1 年)（保守）。

    `annual_fcf`：由舊到新的年 FCF。全缺 → (None, 說明)。
    """
    vals = [v for v in annual_fcf if v is not None]
    if not vals:
        return None, "FCF 不可得（無 Q4 現金流量資料）"
    latest = vals[-1]
    if len(vals) >= 3:
        avg3 = sum(vals[-3:]) / 3.0
        base = min(avg3, latest)
        note = f"min(近3年均 {avg3:,.0f}, 最近年 {latest:,.0f})"
        return base, note
    return latest, f"最近年 {latest:,.0f}（歷史 <3 年，未取均）"


def cost_of_equity(
    risk_free_rate_pct: float, beta: float, erp_pct: float
) -> float:
    """股權資金成本（%）＝ rf + β·ERP。pipeline 一律用此值當折現率（無可靠 WACC 推法；
    且 8% 地板恆綁定 → WACC vs CoE 差異不影響輸出）。"""
    return risk_free_rate_pct + beta * erp_pct


def dcf_intrinsic_value(
    fcf0: float | None,
    growth_pct: float | None,
    discount_rate_pct: float | None,
    terminal_growth_pct: float,
    stage1_years: int,
    net_debt: float | None,
    shares: float | None,
) -> float | None:
    """兩階段 FCFE per-share 內在價值。任一輸入缺／shares≤0／r≤永續成長 → None。

    Σ_{t=1..N} FCF0(1+g)^t/(1+r)^t + [FCF_N(1+g_term)/(r−g_term)]/(1+r)^N，減 net_debt，除 shares。
    """
    if (
        fcf0 is None or growth_pct is None or discount_rate_pct is None
        or net_debt is None or shares is None or shares <= 0
    ):
        return None
    g = growth_pct / 100.0
    r = discount_rate_pct / 100.0
    gt = terminal_growth_pct / 100.0
    if r <= gt:
        return None
    pv_stage1 = 0.0
    for t in range(1, stage1_years + 1):
        pv_stage1 += fcf0 * (1.0 + g) ** t / (1.0 + r) ** t
    fcf_n = fcf0 * (1.0 + g) ** stage1_years
    tv = fcf_n * (1.0 + gt) / (r - gt)
    pv_tv = tv / (1.0 + r) ** stage1_years
    equity_value = pv_stage1 + pv_tv - net_debt
    return equity_value / shares


def _binding_discount_rate(
    computed_pct: float, floor_pct: float, terminal_growth_pct: float, spread_pct: float
) -> tuple[float, str]:
    """回 (折現率%, binding∈{computed,floor,spread})。逐步套地板→spread，記錄最終由誰決定。"""
    rate = max(computed_pct, floor_pct)
    binding = "floor" if floor_pct >= computed_pct else "computed"
    min_rate = terminal_growth_pct + spread_pct
    if rate < min_rate:
        return min_rate, "spread"
    return rate, binding


def dcf_with_guardrails(
    *,
    fcf0: float | None,
    growth_pct: float | None,
    computed_discount_rate_pct: float,
    net_debt: float | None,
    shares: float | None,
    dcf_cfg: dict[str, Any],
    industry_name: str | None,
    any_quarterly_loss: bool | None,
    rev_yoy_swing_pp: float | None,
) -> dict[str, Any]:
    """套排除 gate ＋ 護欄 → dcf_intrinsic_est ＋ 敏感度網格 ＋ 全假設。

    排除 gate 順序（先命中先出）：金融業 → 產業別未知 → 近 4 季虧損 → 營收波動過大 →
    FCF/成長/股數 不足 → FCF 為負。命中 → `dcf_applicable=False`、不算數字。
    護欄常數一字不動讀自 `dcf_cfg`（8 key）。
    """
    gt = float(dcf_cfg.get("terminal_growth_pct", 2.0))
    n_years = int(dcf_cfg.get("stage1_years", 10))
    step = float(dcf_cfg.get("sensitivity_step_pct", 1.0))
    floor = float(dcf_cfg.get("discount_rate_floor_pct", 8.0))
    spread = float(dcf_cfg.get("min_wacc_terminal_spread_pct", 3.0))
    exclude_industries = set(dcf_cfg.get("exclude_industries", []))
    exclude_loss = bool(dcf_cfg.get("exclude_if_quarterly_loss", True))
    swing_thr = float(dcf_cfg.get("exclude_if_rev_yoy_swing_pp", 40.0))

    out: dict[str, Any] = {
        "dcf_applicable": False,
        "dcf_exclude_reason": None,
        "dcf_intrinsic_est": None,
        "dcf_discount_rate": None,
        "dcf_rate_binding": None,
        "dcf_terminal_growth": gt,
        "sensitivity": {},
        "assumptions": {},
    }

    def _reject(reason: str) -> dict[str, Any]:
        out["dcf_exclude_reason"] = reason
        return out

    if not industry_name:
        return _reject("排除：產業別未知")
    if industry_name in exclude_industries:
        return _reject(f"排除：{industry_name}（FCF 對此業無意義）")
    if exclude_loss and any_quarterly_loss is True:
        return _reject("排除：近 4 季有單季虧損")
    if rev_yoy_swing_pp is not None and rev_yoy_swing_pp > swing_thr:
        return _reject(
            f"排除：近 3 年營收年增波動 {rev_yoy_swing_pp:.0f}pp > {swing_thr:.0f}pp（極端週期）"
        )
    if fcf0 is None or growth_pct is None or shares is None or shares <= 0:
        return _reject("排除：FCF／成長／股數 資料不足")
    if fcf0 <= 0:
        return _reject("排除：近年自由現金流為負／零")
    nd = net_debt if net_debt is not None else 0.0

    rate, binding = _binding_discount_rate(computed_discount_rate_pct, floor, gt, spread)
    central = dcf_intrinsic_value(fcf0, growth_pct, rate, gt, n_years, nd, shares)
    if central is None:
        return _reject("排除：DCF 公式無解（折現率 ≤ 永續成長）")

    def _corner(r_c: float, g_c: float) -> float | None:
        r_use = max(r_c, g_c + spread)
        return dcf_intrinsic_value(fcf0, growth_pct, r_use, g_c, n_years, nd, shares)

    sensitivity = {
        "wacc_dn_g_dn": _corner(rate - step, gt - step),
        "wacc_dn_g_up": _corner(rate - step, gt + step),
        "wacc_up_g_dn": _corner(rate + step, gt - step),
        "wacc_up_g_up": _corner(rate + step, gt + step),
        "g1_050": dcf_intrinsic_value(fcf0, growth_pct * 0.50, rate, gt, n_years, nd, shares),
        "g1_075": dcf_intrinsic_value(fcf0, growth_pct * 0.75, rate, gt, n_years, nd, shares),
        "g1_100": central,
    }

    out.update(
        dcf_applicable=True,
        dcf_intrinsic_est=central,
        dcf_discount_rate=rate,
        dcf_rate_binding=binding,
        sensitivity=sensitivity,
        assumptions={
            "fcf0": fcf0,
            "growth_pct": growth_pct,
            "discount_rate_pct": rate,
            "discount_rate_binding": binding,
            "terminal_growth_pct": gt,
            "stage1_years": n_years,
            "net_debt": nd,
            "shares": shares,
            "computed_cost_of_equity_pct": computed_discount_rate_pct,
        },
    )
    return out


# ── 全市場組裝 ─────────────────────────────────────────────────────────────
def build_dcf_inputs(
    cashflow_wide: pl.DataFrame,
    financials_wide: pl.DataFrame,
    balancesheet_wide: pl.DataFrame,
    shares_map: dict[str, Any],
    industry_df: pl.DataFrame,
    dcf_cfg: dict[str, Any],
    risk_free_rate_pct: float,
    erp_pct: float,
    default_beta: float = 1.0,
) -> pl.DataFrame:
    """全市場機械式 DCF 輸入＋結果表（純計算、無 IO）。供 group_runner 與研究 CSV 共用。

    `net_debt = Σ(st_debt, bonds_payable, lt_debt) − cash`，取最近一季 BalanceSheet；
    負值＝淨現金（正常）。`shares_map` 為 TWSE 已發行普通股數（權威，非 FinMind OrdinaryShare）。

    Returns:
        `_DCF_INPUTS_SCHEMA` 每檔一列；空輸入（cashflow ＋ financials 皆空）→ 空表。
    """
    if cashflow_wide.is_empty() and financials_wide.is_empty():
        return pl.DataFrame(schema=_DCF_INPUTS_SCHEMA)

    fcf_hist = annual_fcf_history(cashflow_wide)
    rev_hist = annual_revenue_history(financials_wide)
    loss_flags = quarterly_profit_flags(financials_wide)

    fcf_by_sid: dict[str, list[float]] = {}
    for r in fcf_hist.sort(["stock_id", "year"]).iter_rows(named=True):
        if r["fcf"] is not None:
            fcf_by_sid.setdefault(str(r["stock_id"]), []).append(float(r["fcf"]))
    rev_by_sid: dict[str, list[float]] = {}
    for r in rev_hist.sort(["stock_id", "year"]).iter_rows(named=True):
        if r["revenue"] is not None:
            rev_by_sid.setdefault(str(r["stock_id"]), []).append(float(r["revenue"]))
    loss_by_sid: dict[str, bool | None] = {
        str(r["stock_id"]): r["any_quarterly_loss"]
        for r in loss_flags.iter_rows(named=True)
    }

    # 最近一季 BalanceSheet
    net_debt_by_sid: dict[str, float] = {}
    debt_codes_by_sid: dict[str, str] = {}
    if not balancesheet_wide.is_empty():
        latest_bs = (
            balancesheet_wide.sort(["stock_id", "year", "quarter"])
            .group_by("stock_id", maintain_order=True)
            .tail(1)
        )
        for r in latest_bs.iter_rows(named=True):
            debt = sum(float(r[c]) for c in _DEBT_COLS if r[c] is not None)
            cash = float(r["cash"]) if r["cash"] is not None else 0.0
            net_debt_by_sid[str(r["stock_id"])] = debt - cash
            found = [c for c in _DEBT_COLS if r[c] is not None]
            debt_codes_by_sid[str(r["stock_id"])] = ",".join(found) if found else "(無)"

    industry_by_sid: dict[str, str] = (
        {
            str(r["stock_id"]): str(r["industry_name"])
            for r in industry_df.iter_rows(named=True)
            if r.get("industry_name")
        }
        if not industry_df.is_empty() and "industry_name" in industry_df.columns
        else {}
    )

    computed_coe = cost_of_equity(risk_free_rate_pct, default_beta, erp_pct)
    # 保守外推參數（M-Val-FinMind2 新增，非凍結的護欄 8 key；settings 可調，鐵律 5）
    growth_years = int(dcf_cfg.get("mechanical_growth_years", 5))
    growth_haircut = float(dcf_cfg.get("mechanical_growth_haircut", 0.7))
    growth_cap = float(dcf_cfg.get("mechanical_growth_cap_pct", 15.0))

    sids = sorted(set(fcf_by_sid) | set(rev_by_sid) | set(loss_by_sid))
    rows: list[dict[str, Any]] = []
    for sid in sids:
        annual_fcf = fcf_by_sid.get(sid, [])
        annual_rev = rev_by_sid.get(sid, [])
        base_val, base_note = fcf_base(annual_fcf)
        growth = conservative_growth_rate(
            annual_rev, years=growth_years, haircut=growth_haircut, cap_pct=growth_cap
        )
        growth_note = (
            f"近{min(growth_years, max(len(annual_rev) - 1, 0))}年營收CAGR×{growth_haircut}"
            f"，clip[0,{growth_cap:.0f}]" if growth is not None
            else "營收完整年 <3 → 無保守外推"
        )
        swing = revenue_yoy_swing_pp(annual_rev)
        shares_raw = shares_map.get(sid)
        shares = (
            float(shares_raw)  # type: ignore[arg-type]
            if shares_raw not in (None, "")
            else None
        )
        nd = net_debt_by_sid.get(sid)
        loss = loss_by_sid.get(sid)

        res = dcf_with_guardrails(
            fcf0=base_val,
            growth_pct=growth,
            computed_discount_rate_pct=computed_coe,
            net_debt=nd,
            shares=shares,
            dcf_cfg=dcf_cfg,
            industry_name=industry_by_sid.get(sid),
            any_quarterly_loss=loss,
            rev_yoy_swing_pp=swing,
        )
        s = res["sensitivity"]
        rows.append({
            "stock_id": sid,
            "dcf_applicable": res["dcf_applicable"],
            "dcf_exclude_reason": res["dcf_exclude_reason"],
            "dcf_intrinsic_est": res["dcf_intrinsic_est"],
            "dcf_discount_rate": res["dcf_discount_rate"],
            "dcf_rate_binding": res["dcf_rate_binding"],
            "dcf_terminal_growth": res["dcf_terminal_growth"],
            "fcf_base": base_val,
            "growth_pct": growth,
            "net_debt": nd,
            "shares": int(shares) if shares is not None else None,
            "rev_yoy_swing_pp": swing,
            "any_quarterly_loss": loss,
            "dcf_debt_codes_found": debt_codes_by_sid.get(sid),
            "fcf_base_note": base_note,
            "growth_note": growth_note,
            "sens_wacc_dn_g_dn": s.get("wacc_dn_g_dn"),
            "sens_wacc_dn_g_up": s.get("wacc_dn_g_up"),
            "sens_wacc_up_g_dn": s.get("wacc_up_g_dn"),
            "sens_wacc_up_g_up": s.get("wacc_up_g_up"),
            "sens_g1_050": s.get("g1_050"),
            "sens_g1_075": s.get("g1_075"),
            "sens_g1_100": s.get("g1_100"),
        })
    return pl.DataFrame(rows, schema=_DCF_INPUTS_SCHEMA).sort("stock_id")
