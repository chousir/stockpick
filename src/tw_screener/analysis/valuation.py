"""個股估值層：次產業橫斷面相對 PE（CP 值補漲研究 C1，docs/13-cp-value-research.md §4 Phase C）。

定位（守 docs/13 §C1「資料現實」與使用者 2026-06-14 範圍裁定）：
- **只做橫斷面相對 PE**——個股當期 PE vs 所屬次產業同儕中位數／百分位。這層才是 C2
  三重濾網真正要吃的「相對便宜」訊號。
- **不做自身 1~3 年歷史百分位**：快取只有 1 季 EPS（TWSE/TPEX OpenAPI 各端點僅回最新一季，
  歷史季無法回補），單季 EPS 下「PE 歷史」會退化成「股價歷史換標籤」＝編，故明標「未取得」。
- EPS 用**最新單季年化代理**（×annualize_factor），非真 TTM、未調季節性。重點：橫斷面**相對**
  排名對這個年化常數**不敏感**（全市場同乘 k，比值與百分位不變），只有顯示的 PE 絕對值是代理。
- 缺 EPS／EPS≤0 一律明標，不補零、不給負 PE（守 docs/13 §3.3「沒抓到不要編」）。

純函式計算，全離線、不打外部、不新增資料源（吃 fundamentals_*.parquet + daily_*）。
"""

from __future__ import annotations

import polars as pl

from tw_screener.analysis.sector_universe import PEER_FALLBACK_PREFIX

_PE_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "close": pl.Float64,
    "eps_q": pl.Float64,
    "eps_annualized": pl.Float64,
    "pe": pl.Float64,
    "pe_status": pl.Utf8,
}


def compute_pe(
    prices: pl.DataFrame, fundamentals: pl.DataFrame, annualize_factor: float = 4.0
) -> pl.DataFrame:
    """個股當期 PE（單一交易日橫斷面，單季 EPS 年化代理）。

    Args:
        prices: (stock_id, close) 同一交易日全市場最新收盤（呼叫端已框上市、取最新日）
        fundamentals: (stock_id, eps, ...) 最新單季基本面；缺則該股 pe_status=未取得
        annualize_factor: 單季 EPS → 年化的乘數（預設 ×4）。相對排名對此不敏感，只影響顯示 PE

    Returns:
        每檔一列：stock_id/close/eps_q/eps_annualized/pe/pe_status。pe_status：
        ok（EPS>0 有 PE）／虧損打平（EPS≤0，pe=null）／未取得（無 EPS，pe=null，不補零）。
    """
    if prices.is_empty():
        return pl.DataFrame(schema=_PE_SCHEMA)
    if fundamentals.is_empty():
        eps = pl.DataFrame(schema={"stock_id": pl.Utf8, "eps": pl.Float64})
    else:
        eps = fundamentals.select("stock_id", "eps").unique("stock_id", keep="first")
    df = (
        prices.select("stock_id", "close")
        .unique("stock_id", keep="first")
        .join(eps, on="stock_id", how="left")
        .with_columns((pl.col("eps") * annualize_factor).alias("eps_annualized"))
        .with_columns(
            pl.when(pl.col("eps").is_null() | (pl.col("eps") <= 0))
            .then(None)
            .otherwise(pl.col("close") / pl.col("eps_annualized"))
            .alias("pe"),
            pl.when(pl.col("eps").is_null())
            .then(pl.lit("未取得"))
            .when(pl.col("eps") <= 0)
            .then(pl.lit("虧損打平"))
            .otherwise(pl.lit("ok"))
            .alias("pe_status"),
        )
        .rename({"eps": "eps_q"})
    )
    return df.select(list(_PE_SCHEMA.keys()))


def compute_subind_relative(
    pe_df: pl.DataFrame, membership: pl.DataFrame, min_peers: int = 5
) -> pl.DataFrame:
    """每檔 PE vs 所屬次產業同儕中位數 + 百分位（多標籤取平均，守 stock_panel 慣例）。

    只用 pe_status=ok（有正 PE）者算同儕統計；虧損/未取得不進中位數，避免污染。
    同儕有效數 < min_peers 的次產業不計（樣本太小無代表性）。一檔多次產業 → 對各次產業
    各算相對值再平均（與 stock_panel._subindustry_returns 多標籤取 mean 同慣例）。

    Returns:
        (stock_id, subind_median_pe, pe_vs_subind_pct, pe_subind_pctile, peer_n, n_subind)。
        pe_vs_subind_pct：(pe/中位 −1)×100，負＝比同儕便宜。
        pe_subind_pctile：次產業內 PE 升冪百分位（0=最便宜、100=最貴）。
        無有效次產業同儕 → 各相對欄 null（標「同儕不足」由呼叫端處理）。
    """
    empty = pl.DataFrame(
        schema={
            "stock_id": pl.Utf8,
            "subind_median_pe": pl.Float64,
            "pe_vs_subind_pct": pl.Float64,
            "pe_subind_pctile": pl.Float64,
            "peer_n": pl.Int64,
            "n_subind": pl.Int64,
        }
    )
    ok = pe_df.filter(pl.col("pe_status") == "ok").select("stock_id", "pe")
    if ok.is_empty() or membership.is_empty():
        return empty
    pairs = membership.select("sub_industry", "stock_id").join(ok, on="stock_id", how="inner")
    if pairs.is_empty():
        return empty
    pairs = pairs.with_columns(
        pl.len().over("sub_industry").alias("_n"),
        pl.col("pe").median().over("sub_industry").alias("_median"),
        pl.col("pe").rank(method="average").over("sub_industry").alias("_rank"),
    ).filter(pl.col("_n") >= min_peers)
    if pairs.is_empty():
        return empty
    pairs = pairs.with_columns(
        ((pl.col("pe") / pl.col("_median") - 1) * 100).alias("_rel"),
        pl.when(pl.col("_n") > 1)
        .then((pl.col("_rank") - 1) / (pl.col("_n") - 1) * 100)
        .otherwise(None)
        .alias("_pctile"),
    )
    return pairs.group_by("stock_id").agg(
        pl.col("_median").mean().round(2).alias("subind_median_pe"),
        pl.col("_rel").mean().round(1).alias("pe_vs_subind_pct"),
        pl.col("_pctile").mean().round(1).alias("pe_subind_pctile"),
        pl.col("_n").max().alias("peer_n"),
        pl.col("sub_industry").n_unique().alias("n_subind"),
    )


def build_valuation(
    prices: pl.DataFrame,
    fundamentals: pl.DataFrame,
    membership: pl.DataFrame,
    annualize_factor: float = 4.0,
    min_peers: int = 5,
    cheap_pctile: float = 30.0,
) -> pl.DataFrame:
    """估值長表（docs/13 C1）：個股 PE + 次產業相對位階 + 相對便宜標。

    Returns:
        每檔一列：compute_pe 欄 + subind_median_pe/pe_vs_subind_pct/pe_subind_pctile/
        peer_n/n_subind + cheap_flag（次產業 PE 百分位 ≤ cheap_pctile 標「相對便宜」；
        無相對位階則空）。依 pe_subind_pctile 遞增（便宜在前），null 殿後。
    """
    pe_df = compute_pe(prices, fundamentals, annualize_factor)
    if pe_df.is_empty():
        return pe_df
    rel = compute_subind_relative(pe_df, membership, min_peers)
    pe_df = pe_df.with_columns(pl.col("pe").round(2))
    out = pe_df.join(rel, on="stock_id", how="left").with_columns(
        pl.when(pl.col("pe_subind_pctile").is_null())
        .then(pl.lit(""))
        .when(pl.col("pe_subind_pctile") <= cheap_pctile)
        .then(pl.lit("相對便宜"))
        .otherwise(pl.lit(""))
        .alias("cheap_flag")
    )
    # peer_source：同儕來自手標次產業（細）或 TWSE 產業別兜底（粗）——讓相對便宜可信度透明
    if not membership.is_empty():
        src = membership.group_by("stock_id").agg(
            (~pl.col("sub_industry").str.starts_with(PEER_FALLBACK_PREFIX)).any().alias("_fine")
        )
        out = out.join(src, on="stock_id", how="left").with_columns(
            pl.when(pl.col("pe_subind_pctile").is_null())
            .then(None)
            .when(pl.col("_fine"))
            .then(pl.lit("次產業"))
            .otherwise(pl.lit("產業別"))
            .alias("peer_source")
        ).drop("_fine")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias("peer_source"))
    return out.sort("pe_subind_pctile", descending=False, nulls_last=True)


def compute_valuation_meta(
    valuation: pl.DataFrame, data_date: str = "", universe: str = "listed"
) -> dict:
    """估值覆蓋率 meta + 誠實但書（docs/13 C1「缺 EPS 明標未取得」「非前瞻估值」）。"""
    notes = [
        "自身 1~3 年 PE 歷史百分位：未取得"
        "（快取僅 1 季 EPS，OpenAPI 各端點僅回最新季，無法回補歷史季）",
        "EPS＝最新單季年化代理（×4），非真 TTM、未調季節性；"
        "橫斷面相對排名對年化常數不敏感，僅顯示 PE 絕對值受影響",
        "僅「相對便宜（trailing 橫斷面）」，非前瞻估值（分析師前瞻 EPS 無資料源，不瞎掰）",
        "缺 EPS／EPS≤0 明標未取得／虧損打平，不補零、不給負 PE",
        "同儕分組：手標次產業（細）優先，未標上市股以 TWSE 產業別兜底"
        "（粗，peer_source=產業別）",
    ]
    if valuation.is_empty():
        return {"universe": universe, "data_date": data_date, "n_stocks": 0, "notes": notes}
    n = valuation.height
    n_pe = valuation.filter(pl.col("pe_status") == "ok").height
    n_loss = valuation.filter(pl.col("pe_status") == "虧損打平").height
    n_no_eps = valuation.filter(pl.col("pe_status") == "未取得").height
    n_rel = valuation.filter(pl.col("pe_subind_pctile").is_not_null()).height
    n_cheap = valuation.filter(pl.col("cheap_flag") == "相對便宜").height
    return {
        "universe": universe,
        "data_date": data_date,
        "n_stocks": n,
        "n_with_pe": n_pe,
        "n_loss": n_loss,
        "n_no_eps": n_no_eps,
        "n_with_relative": n_rel,
        "n_cheap": n_cheap,
        "pe_coverage_pct": round(100 * n_pe / n, 2) if n else 0.0,
        "relative_coverage_pct": round(100 * n_rel / n, 2) if n else 0.0,
        "notes": notes,
    }
