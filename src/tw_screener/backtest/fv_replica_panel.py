"""backtest/fv_replica_panel.py — docs/31 §20.17：investing.com 式多模型估值仿製版的照時點月頻面板。

逐月第一個交易日（2016-04 ~ 2025-09）回放 FinMind 快取：只用法定申報截止日已過的財報
（`fv_replica.filter_available`）、股數由 TTM 淨利／TTM EPS 推、PE/PBR 取 ≤t 最後一列、DPS＝過去
365 日現金股利，算 m1–m10 → 主版 `fv_replica`（照 investing.com 剔除 + 平均）／副版 `fv_replica_raw`
（中位數、不剔除）；另算 Q3 基準 `up_peer_pe`／`up_composite6` 與 Q2 控制欄；接除權息還原
`total_return_targets` 的 r{h} → alpha{h}。

**這是研究面板、不是訊號**：輸出不進任何 production 路徑。母體＝FinMind 價格快取（今日次產業成員），
生存者偏差方向＝高估，報表必揭露。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import polars as pl
from loguru import logger

from tw_screener.analysis.fv_replica import (
    MODEL_KEYS,
    composite_main,
    composite_raw,
    derive_shares,
    fundamentals_asof,
    peer_median,
    replica_models,
)
from tw_screener.analysis.grouping import is_etf_or_warrant
from tw_screener.backtest.intra_pick_oos import adjustment_events, total_return_targets

#: 除權息事件中只有股票股利、不含現金的型別（DPS 不計）。
_STOCK_ONLY_EVENTS: frozenset[str] = frozenset({"權", "除權"})


def monthly_anchors(price: pl.DataFrame, start: date, end: date, min_names: int) -> list[date]:
    """每月第一個「當日有 ≥min_names 檔收盤」的交易日（部分抓取日不當錨點）。"""
    cal = (
        price.group_by("date")
        .agg(pl.len().alias("_n"))
        .filter((pl.col("_n") >= min_names) & pl.col("date").is_between(start, end))
        .sort("date")
    )
    if cal.is_empty():
        return []
    return (
        cal.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("_ym"))
        .group_by("_ym")
        .agg(pl.col("date").min())
        .sort("date")["date"]
        .to_list()
    )


def price_features(price: pl.DataFrame) -> pl.DataFrame:
    """(stock_id, date) → close、ma60_dist_pct、trail_r250（未還原收盤；控制欄用）。"""
    return (
        price.select("stock_id", "date", pl.col("close").cast(pl.Float64))
        .filter(pl.col("close") > 0)
        .sort("stock_id", "date")
        .with_columns(
            ((pl.col("close") / pl.col("close").rolling_mean(60).over("stock_id") - 1) * 100).alias(
                "ma60_dist_pct"
            ),
            ((pl.col("close") / pl.col("close").shift(250).over("stock_id") - 1) * 100).alias(
                "trail_r250"
            ),
        )
    )


def market_state(price: pl.DataFrame, ma_days: int = 200) -> pl.DataFrame:
    """母體等權指數（日報酬截 ±10% 防壞列）vs 其 ma_days 均線 → date, mkt_state∈{多,空}。"""
    rets = (
        price.select("stock_id", "date", "close")
        .filter(pl.col("close") > 0)
        .sort("stock_id", "date")
        .with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1)
            .clip(-0.1, 0.1)
            .alias("_r")
        )
        .group_by("date")
        .agg(pl.col("_r").mean())
        .sort("date")
        .with_columns((1 + pl.col("_r").fill_null(0)).cum_prod().alias("_idx"))
        .with_columns(pl.col("_idx").rolling_mean(ma_days).alias("_ma"))
    )
    return rets.select(
        "date",
        pl.when(pl.col("_ma").is_null())
        .then(None)
        .when(pl.col("_idx") >= pl.col("_ma"))
        .then(pl.lit("多"))
        .otherwise(pl.lit("空"))
        .alias("mkt_state"),
    )


def per_with_self_medians(per: pl.DataFrame, window_days: int, min_obs: int) -> pl.DataFrame:
    """FinMind PER 逐日 → 加自身 window_days 日曆日滾動中位（PE/PBR/殖利率；
    觀測 <min_obs → null）。"""
    w = f"{window_days}d"
    base = per.sort("stock_id", "date")
    out = base.with_columns(
        pl.col("pe").rolling_median_by("date", window_size=w).over("stock_id").alias("pe_self_med"),
        pl.col("pbr")
        .rolling_median_by("date", window_size=w)
        .over("stock_id")
        .alias("pb_self_med"),
        pl.col("dividend_yield")
        .replace(0.0, None)
        .rolling_median_by("date", window_size=w)
        .over("stock_id")
        .alias("dy_self_med"),
        pl.col("pbr")
        .is_not_null()
        .cast(pl.Int32)
        .rolling_sum_by("date", window_size=w)
        .over("stock_id")
        .alias("_n_obs"),
    )
    return out.with_columns(
        pl.when(pl.col("_n_obs") >= min_obs).then(pl.col(c)).otherwise(None).alias(c)
        for c in ("pe_self_med", "pb_self_med", "dy_self_med")
    ).drop("_n_obs")


def asof_rows(frame: pl.DataFrame, anchor: date, max_stale_days: int) -> pl.DataFrame:
    """每檔取 date ≤ anchor 的最後一列（距錨點超過 max_stale_days → 丟）。"""
    lo = anchor - timedelta(days=max_stale_days)
    return (
        frame.filter(pl.col("date").is_between(lo, anchor))
        .sort("stock_id", "date")
        .group_by("stock_id", maintain_order=True)
        .tail(1)
    )


def peer_sets(
    universe: Sequence[str], subind: pl.DataFrame, industry: pl.DataFrame
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """每檔 → (次產業聯集同儕, 28 類產業同儕)。皆含自己（peer_median 會排除）。"""
    uni = set(universe)
    members: dict[str, set[str]] = {}
    for sub, g in subind.filter(pl.col("stock_id").is_in(list(uni))).group_by("sub_industry"):
        ids = set(g["stock_id"].to_list())
        for s in ids:
            members.setdefault(s, set()).update(ids)
    by_ind: dict[str, list[str]] = {}
    ind_of: dict[str, str] = {}
    for r in industry.iter_rows(named=True):
        sid = str(r["stock_id"])
        if sid in uni and r.get("industry_name"):
            by_ind.setdefault(str(r["industry_name"]), []).append(sid)
            ind_of[sid] = str(r["industry_name"])
    sub_peers = {s: sorted(members.get(s, set())) for s in uni}
    ind_peers = {s: by_ind.get(ind_of.get(s, ""), []) for s in uni}
    return sub_peers, ind_peers


def _gap(implied: float | None, price: float) -> float | None:
    return None if implied is None or implied <= 0 else (implied / price - 1) * 100


def build_anchor_rows(
    anchor: date,
    *,
    px_rows: pl.DataFrame,
    per_rows: pl.DataFrame,
    fund: Mapping[str, Mapping[str, Any]],
    dps: Mapping[str, float],
    sub_peers: Mapping[str, Sequence[str]],
    ind_peers: Mapping[str, Sequence[str]],
    industry_of: Mapping[str, str],
    financial_industries: set[str],
    dcf_cfg: Mapping[str, Any],
    fv_cfg: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """單一錨點的全母體列（每檔：10 模型、主副版、基準、控制欄）。"""
    min_peers = int(fv_cfg.get("min_peers", 5))
    lo_mult, hi_mult = (
        float(fv_cfg.get("keep_lo_mult", 0.4)),
        float(fv_cfg.get("keep_hi_mult", 2.0)),
    )
    per_map = {str(r["stock_id"]): r for r in per_rows.iter_rows(named=True)}
    base: dict[str, dict[str, Any]] = {}
    for r in px_rows.iter_rows(named=True):
        sid = str(r["stock_id"])
        p = float(r["close"])
        pr = per_map.get(sid, {})
        f = fund.get(sid, {})
        pbr = pr.get("pbr")
        shares = derive_shares(
            f.get("ni_ttm"),
            f.get("eps_ttm"),
            f.get("equity"),
            p,
            pbr,
            min_abs_eps=float(fv_cfg.get("shares_min_abs_eps", 0.1)),
        )
        rev = f.get("rev_ttm")
        nd = f.get("net_debt")
        mcap = p * shares if shares else None
        base[sid] = {
            "price": p,
            "shares": shares,
            "pbr": pbr,
            "pe": pr.get("pe"),
            "dy": pr.get("dividend_yield") or None,
            "bvps": (p / pbr) if pbr else None,
            "ps": (mcap / rev) if mcap and rev and rev > 0 else None,
            "evs": ((mcap + nd) / rev) if mcap and nd is not None and rev and rev > 0 else None,
            "mcap": mcap,
            "row": r,
            "per": pr,
        }
    metric = {k: {s: b[k] for s, b in base.items()} for k in ("pe", "pbr", "ps", "evs", "dy")}

    rows: list[dict[str, Any]] = []
    for sid, b in base.items():
        sp, ip = sub_peers.get(sid, []), ind_peers.get(sid, [])
        meds = {
            "pe": peer_median(metric["pe"], sid, sp, ip, min_peers),
            "pb": peer_median(metric["pbr"], sid, sp, ip, min_peers),
            "ps": peer_median(metric["ps"], sid, sp, ip, min_peers),
            "evs": peer_median(metric["evs"], sid, sp, ip, min_peers),
            "dy": peer_median(metric["dy"], sid, sp, ip, min_peers),
        }
        ind = industry_of.get(sid)
        is_fin = ind in financial_industries if ind else False
        p = b["price"]
        models = replica_models(
            fund.get(sid, {}),
            price=p,
            shares=b["shares"],
            bvps=b["bvps"],
            dps_ttm=dps.get(sid),
            industry_name=ind,
            is_financial=is_fin,
            peer_meds=meds,
            dcf_cfg=dcf_cfg,
            fv_cfg=fv_cfg,
        )
        fv, n_kept = composite_main(models, p, lo_mult, hi_mult)
        fv_raw, n_raw = composite_raw(models)
        # Q3 基準：§20.9 六腿綜合版之歷史近似（同儕 PE/PB/殖利率＋自身 5 年中位，取中位）
        pr = b["per"]
        legs = []
        if b["pe"] and meds["pe"]:
            legs.append(_gap(p * meds["pe"] / b["pe"], p))
        if b["pbr"] and meds["pb"]:
            legs.append(_gap(p * meds["pb"] / b["pbr"], p))
        if b["dy"] and meds["dy"]:
            legs.append(_gap(p * b["dy"] / meds["dy"], p))
        if b["pe"] and pr.get("pe_self_med"):
            legs.append(_gap(p * pr["pe_self_med"] / b["pe"], p))
        if b["pbr"] and pr.get("pb_self_med"):
            legs.append(_gap(p * pr["pb_self_med"] / b["pbr"], p))
        if b["dy"] and pr.get("dy_self_med"):
            legs.append(_gap(p * b["dy"] / pr["dy_self_med"], p))
        legs_clean = [v for v in legs if v is not None]
        legs_clean.sort()
        comp6 = (
            None
            if not legs_clean
            else (
                legs_clean[len(legs_clean) // 2]
                if len(legs_clean) % 2
                else (legs_clean[len(legs_clean) // 2 - 1] + legs_clean[len(legs_clean) // 2]) / 2
            )
        )
        rr = b["row"]
        rows.append(
            {
                "date": anchor,
                "stock_id": sid,
                "price": p,
                "industry_name": ind,
                "is_financial": is_fin,
                **models,
                "fv_replica": fv,
                "fv_n_models": n_kept,
                "fv_upside_pct": _gap(fv, p),
                "fv_replica_raw": fv_raw,
                "fv_raw_n_models": n_raw,
                "fv_raw_upside_pct": _gap(fv_raw, p),
                "up_peer_pe": _gap(models["m7_peer_pe"], p),
                "up_composite6": comp6,
                "composite6_n_legs": len(legs_clean),
                "ma60_dist_pct": rr.get("ma60_dist_pct"),
                "trail_r250": rr.get("trail_r250"),
                "log_mktcap": math.log(b["mcap"]) if b["mcap"] and b["mcap"] > 0 else None,
            }
        )
    return rows


def dps_ttm_asof(div: pl.DataFrame, anchor: date, window_days: int = 365) -> dict[str, float]:
    """過去 365 日（不含錨點當日之後）現金股利合計（排除純股票股利事件）。"""
    sub = div.filter(
        pl.col("ex_date").is_between(anchor - timedelta(days=window_days), anchor, closed="right")
        & ~pl.col("event_type").is_in(list(_STOCK_ONLY_EVENTS))
        & (pl.col("dividend_value") > 0)
    )
    agg = sub.group_by("stock_id").agg(pl.col("dividend_value").sum())
    return {str(r["stock_id"]): float(r["dividend_value"]) for r in agg.iter_rows(named=True)}


def build_fv_replica_panel(
    *,
    price: pl.DataFrame,
    dividends: pl.DataFrame,
    per: pl.DataFrame,
    financials: pl.DataFrame,
    cashflow: pl.DataFrame,
    balancesheet: pl.DataFrame,
    subind: pl.DataFrame,
    industry: pl.DataFrame,
    dcf_cfg: Mapping[str, Any],
    fv_cfg: Mapping[str, Any],
    horizons: Sequence[int],
    anchors: Sequence[date] | None = None,
) -> pl.DataFrame:
    """全期月頻面板（每列＝錨點×個股）＋ r{h}/alpha{h} ＋ mkt_state。

    anchors 給定時改用指定錨點（A 輔線的「現值快照」用最新交易日，前瞻欄自然為 null）。
    """
    start = date.fromisoformat(str(fv_cfg.get("start", "2016-04-01")))
    end = date.fromisoformat(str(fv_cfg.get("end", "2025-09-30")))
    price = price.filter(
        ~pl.col("stock_id").map_elements(is_etf_or_warrant, return_dtype=pl.Boolean)
    )
    if anchors is None:
        anchors = monthly_anchors(price, start, end, int(fv_cfg.get("min_names_per_day", 500)))
    anchors = list(anchors)
    logger.info(
        "fv_replica 錨點 {} 個（{} ~ {}）",
        len(anchors),
        anchors[0] if anchors else None,
        anchors[-1] if anchors else None,
    )
    universe = sorted(price["stock_id"].unique().to_list())
    sub_peers, ind_peers = peer_sets(universe, subind, industry)
    industry_of = {
        str(r["stock_id"]): str(r["industry_name"])
        for r in industry.iter_rows(named=True)
        if r.get("industry_name")
    }
    financial_industries = set(dcf_cfg.get("exclude_industries", []))
    feats = price_features(price)
    per2 = per_with_self_medians(
        per, int(fv_cfg.get("self_window_days", 1825)), int(fv_cfg.get("self_min_obs", 250))
    )
    stale = int(fv_cfg.get("max_stale_days", 10))

    all_rows: list[dict[str, Any]] = []
    for a in anchors:
        px_rows = asof_rows(feats, a, 0)  # 只取錨點當日有收盤者
        per_rows = asof_rows(per2, a, stale)
        fund = fundamentals_asof(financials, cashflow, balancesheet, a)
        all_rows.extend(
            build_anchor_rows(
                a,
                px_rows=px_rows,
                per_rows=per_rows,
                fund=fund,
                dps=dps_ttm_asof(dividends, a, int(fv_cfg.get("dps_window_days", 365))),
                sub_peers=sub_peers,
                ind_peers=ind_peers,
                industry_of=industry_of,
                financial_industries=financial_industries,
                dcf_cfg=dcf_cfg,
                fv_cfg=fv_cfg,
            )
        )
    panel = pl.DataFrame(all_rows, infer_schema_length=None)

    covered = set(dividends["stock_id"].unique().to_list())
    tr = total_return_targets(price, adjustment_events(dividends), horizons, covered_ids=covered)
    tr = tr.filter(pl.col("date").is_in(anchors)).select(
        "date", "stock_id", *[f"r{h}" for h in horizons]
    )
    panel = panel.join(tr, on=["date", "stock_id"], how="left")
    panel = panel.with_columns(
        (pl.col(f"r{h}") - pl.col(f"r{h}").median().over("date")).alias(f"alpha{h}")
        for h in horizons
    )
    return panel.join(
        market_state(price, int(fv_cfg.get("market_ma_days", 200))), on="date", how="left"
    )


__all__ = ["MODEL_KEYS", "build_fv_replica_panel", "monthly_anchors"]
