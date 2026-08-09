"""個股估值層：次產業橫斷面相對 PE/PB（CP 值補漲研究 C1，docs/13-cp-value-research.md §4 Phase C）。

定位（守 docs/13 §C1「資料現實」與使用者 2026-06-14 範圍裁定）：
- **只做橫斷面相對位階**——個股當期估值比 vs 所屬次產業同儕中位數／百分位。這層才是 C2
  三重濾網真正要吃的「相對便宜」訊號。
- PE/PBR/殖利率用**官方日資料**（TWSE BWIBBU_d 上市 + TPEX peratio 上櫃，trailing），
  非估算、非前瞻。取代舊版 EPS×4 單季年化代理。
- **PE 主、PB 補虧損股**：有正 trailing PE 者以 PE 算相對位階（val_metric=PE）；虧損／無正
  盈餘者無 PE，改以官方 PBR 算相對位階（val_metric=PB）——讓虧損股不再估值缺。PE-便宜
  與 PB-便宜意義不同（PB 便宜可能資產偏重／折價），故 val_metric 標明用哪個鏡頭。
- **不做自身 1~3 年歷史百分位**：官方日 ratios 才開始逐日累積，需數月才有歷史窗；本期僅
  當日橫斷面。明標「未取得」（守 docs/13 §3.3「沒抓到不要編」）。
- 缺值（虧損／無資料）一律明標，不補零、不給負值。

純函式計算，全離線、不打外部（吃 valuation_ratios_*.parquet）。
"""

from __future__ import annotations

import polars as pl

from tw_screener.analysis.sector_universe import PEER_FALLBACK_PREFIX

_VALUATION_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "market": pl.Utf8,
    "pe": pl.Float64,
    "pe_status": pl.Utf8,
    "pbr": pl.Float64,
    "dividend_yield": pl.Float64,
    "val_metric": pl.Utf8,           # PE / PB / null（用哪個鏡頭算相對位階）
    "val_median": pl.Float64,        # 所屬次產業同儕（該鏡頭）中位數
    "val_vs_subind_pct": pl.Float64,  # (值/中位 −1)×100，負＝比同儕便宜
    "val_pctile": pl.Float64,        # 次產業內升冪百分位（0=最便宜、100=最貴）
    "val_peer_n": pl.Int64,
    "cheap_flag": pl.Utf8,
    "peer_source": pl.Utf8,
}

_REL_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "subind_median": pl.Float64,
    "vs_subind_pct": pl.Float64,
    "subind_pctile": pl.Float64,
    "peer_n": pl.Int64,
}


def compute_subind_relative(
    df: pl.DataFrame, membership: pl.DataFrame, value_col: str = "pe", min_peers: int = 5
) -> pl.DataFrame:
    """每檔 value_col vs 所屬次產業同儕中位數 + 百分位（多標籤取平均，守 stock_panel 慣例）。

    只用 value_col 有效（非 null、>0）者算同儕統計，避免污染。同儕有效數 < min_peers 的
    次產業不計（樣本太小無代表性）。一檔多次產業 → 對各次產業各算相對值再平均（與
    stock_panel._subindustry_returns 多標籤取 mean 同慣例）。

    Returns:
        (stock_id, subind_median, vs_subind_pct, subind_pctile, peer_n)。
        vs_subind_pct：(值/中位 −1)×100。subind_pctile：次產業內升冪百分位。
        無有效次產業同儕 → 空表（呼叫端據此降級）。
    """
    empty = pl.DataFrame(schema=_REL_SCHEMA)
    if df.is_empty() or membership.is_empty():
        return empty
    vals = (
        df.select("stock_id", pl.col(value_col).alias("_v"))
        .filter(pl.col("_v").is_not_null() & (pl.col("_v") > 0))
    )
    if vals.is_empty():
        return empty
    pairs = membership.select("sub_industry", "stock_id").join(vals, on="stock_id", how="inner")
    if pairs.is_empty():
        return empty
    pairs = pairs.with_columns(
        pl.len().over("sub_industry").alias("_n"),
        pl.col("_v").median().over("sub_industry").alias("_median"),
        pl.col("_v").rank(method="average").over("sub_industry").alias("_rank"),
    ).filter(pl.col("_n") >= min_peers)
    if pairs.is_empty():
        return empty
    pairs = pairs.with_columns(
        ((pl.col("_v") / pl.col("_median") - 1) * 100).alias("_rel"),
        pl.when(pl.col("_n") > 1)
        .then((pl.col("_rank") - 1) / (pl.col("_n") - 1) * 100)
        .otherwise(None)
        .alias("_pctile"),
    )
    return pairs.group_by("stock_id").agg(
        pl.col("_median").mean().round(2).alias("subind_median"),
        pl.col("_rel").mean().round(1).alias("vs_subind_pct"),
        pl.col("_pctile").mean().round(1).alias("subind_pctile"),
        pl.col("_n").max().alias("peer_n"),
    )


def build_valuation(
    ratios: pl.DataFrame,
    membership: pl.DataFrame,
    min_peers: int = 5,
    cheap_pctile: float = 30.0,
) -> pl.DataFrame:
    """估值長表（docs/13 C1）：官方 PE/PBR + 次產業相對位階（PE 主、PB 補虧損股）+ 相對便宜標。

    Args:
        ratios: (stock_id, market, pe, pbr, dividend_yield) 官方日估值比（單一交易日橫斷面）。
        membership: (sub_industry, stock_id) 同儕分組（手標次產業細 / TWSE 產業別兜底）。

    Returns:
        每檔一列（_VALUATION_SCHEMA）。pe_status：ok（有正 PE）／無本益比（虧損或無正盈餘，
        改用 PB）。val_metric：PE（用 PE 算相對）／PB（虧損股退用 PB）／null（同儕不足）。
        cheap_flag：val_pctile ≤ cheap_pctile → 相對便宜（PE）或 相對便宜(PB)；否則空。
        依 val_pctile 遞增（便宜在前），null 殿後。
    """
    if ratios.is_empty():
        return pl.DataFrame(schema=_VALUATION_SCHEMA)
    base = (
        ratios.select("stock_id", "market", "pe", "pbr", "dividend_yield")
        .unique("stock_id", keep="first")
        .with_columns(
            pl.col("pe").round(2),
            pl.col("pbr").round(2),
            pl.col("dividend_yield").round(2),
            pl.when(pl.col("pe").is_not_null() & (pl.col("pe") > 0))
            .then(pl.lit("ok"))
            .otherwise(pl.lit("無本益比"))
            .alias("pe_status"),
        )
    )

    pe_rel = compute_subind_relative(
        base.filter(pl.col("pe_status") == "ok").select("stock_id", "pe"),
        membership, value_col="pe", min_peers=min_peers,
    ).rename({c: f"pe_{c}" for c in _REL_SCHEMA if c != "stock_id"})
    pb_rel = compute_subind_relative(
        base.select("stock_id", "pbr"), membership, value_col="pbr", min_peers=min_peers
    ).rename({c: f"pb_{c}" for c in _REL_SCHEMA if c != "stock_id"})

    out = base.join(pe_rel, on="stock_id", how="left").join(pb_rel, on="stock_id", how="left")
    has_pe = pl.col("pe_subind_pctile").is_not_null()
    out = out.with_columns(
        # PE 主、PB 補：有 PE 相對位階用 PE，否則退用 PB（虧損股或 PE 同儕不足）
        pl.when(has_pe).then(pl.lit("PE"))
        .when(pl.col("pb_subind_pctile").is_not_null()).then(pl.lit("PB"))
        .otherwise(None).alias("val_metric"),
        pl.when(has_pe).then(pl.col("pe_subind_median"))
        .otherwise(pl.col("pb_subind_median")).alias("val_median"),
        pl.when(has_pe).then(pl.col("pe_vs_subind_pct"))
        .otherwise(pl.col("pb_vs_subind_pct")).alias("val_vs_subind_pct"),
        pl.coalesce("pe_subind_pctile", "pb_subind_pctile").alias("val_pctile"),
        pl.when(has_pe).then(pl.col("pe_peer_n"))
        .otherwise(pl.col("pb_peer_n")).alias("val_peer_n"),
    ).with_columns(
        pl.when(pl.col("val_pctile").is_null())
        .then(pl.lit(""))
        .when(pl.col("val_pctile") <= cheap_pctile)
        .then(
            pl.when(pl.col("val_metric") == "PB")
            .then(pl.lit("相對便宜(PB)"))
            .otherwise(pl.lit("相對便宜"))
        )
        .otherwise(pl.lit(""))
        .alias("cheap_flag")
    )

    # peer_source：同儕來自手標次產業（細）或 TWSE 產業別兜底（粗）——讓相對便宜可信度透明
    if not membership.is_empty():
        src = membership.group_by("stock_id").agg(
            (~pl.col("sub_industry").str.starts_with(PEER_FALLBACK_PREFIX)).any().alias("_fine")
        )
        out = out.join(src, on="stock_id", how="left").with_columns(
            pl.when(pl.col("val_pctile").is_null())
            .then(None)
            .when(pl.col("_fine"))
            .then(pl.lit("次產業"))
            .otherwise(pl.lit("產業別"))
            .alias("peer_source")
        ).drop("_fine")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias("peer_source"))
    return out.select(list(_VALUATION_SCHEMA.keys())).sort(
        "val_pctile", descending=False, nulls_last=True
    )


def compute_valuation_meta(
    valuation: pl.DataFrame, data_date: str = "", universe: str = "上市+上櫃"
) -> dict:
    """估值覆蓋率 meta + 誠實但書（docs/13 C1）。"""
    notes = [
        "PE/PBR/殖利率＝官方 trailing（TWSE BWIBBU_d 上市 + TPEX peratio 上櫃），"
        "非估算、非前瞻",
        "PE 主、PB 補虧損股：有正 trailing PE 以 PE 算相對位階；虧損／無正盈餘改用官方 PBR"
        "（val_metric=PB）。PB-便宜與 PE-便宜意義不同（PB 便宜可能資產偏重／折價），須配合基本面",
        "自身 1~3 年估值歷史百分位：未取得（官方日 ratios 才開始逐日累積，需數月才有歷史窗，"
        "本期僅當日橫斷面）",
        "僅「相對便宜（橫斷面）」，非前瞻估值（分析師前瞻 EPS 無資料源，不瞎掰）",
        "同儕分組：手標次產業（細）優先，未標股以 TWSE 產業別兜底（粗，peer_source=產業別）",
    ]
    if valuation.is_empty():
        return {"universe": universe, "data_date": data_date, "n_stocks": 0, "notes": notes}
    n = valuation.height
    n_pe = valuation.filter(pl.col("pe_status") == "ok").height
    n_no_pe = valuation.filter(pl.col("pe_status") == "無本益比").height
    n_rel = valuation.filter(pl.col("val_pctile").is_not_null()).height
    n_via_pb = valuation.filter(pl.col("val_metric") == "PB").height
    n_cheap = valuation.filter(pl.col("cheap_flag") != "").height
    return {
        "universe": universe,
        "data_date": data_date,
        "n_stocks": n,
        "n_with_pe": n_pe,
        "n_no_pe": n_no_pe,
        "n_with_relative": n_rel,
        "n_via_pb": n_via_pb,
        "n_cheap": n_cheap,
        "pe_coverage_pct": round(100 * n_pe / n, 2) if n else 0.0,
        "relative_coverage_pct": round(100 * n_rel / n, 2) if n else 0.0,
        "notes": notes,
    }


def deep_value_growth(
    val_pctile: float | None,
    rev_yoy_pct: float | None,
    gross_margin_pct: float | None,
    ma60_dist_pct: float | None,
    base_zone: str | None,
    max_pctile: float = 20.0,
    min_yoy_pct: float = 30.0,
    min_gross_margin_pct: float = 25.0,
) -> bool:
    """M5 深值成長 tag＝**便宜且在成長、且還沒漲上去**（委託書 M5）。

    四條件同時成立：
      1. `val_pctile ≤ max_pctile`——次產業內最便宜的一段（0＝同業最便宜）
      2. `rev_yoy_pct ≥ min_yoy_pct`——確實在成長（不是衰退股的低估值陷阱）
      3. `gross_margin_pct ≥ min_gross_margin_pct`——有定價權（薄毛利的便宜只是薄毛利）
      4. `ma60_dist_pct < 0` **或** `base_zone=貼底`——位階還沒延伸

    **要修的病**（委託書 §問題盤點）：現制把估值/成長只當排雷不當進攻——「PE 5–8 倍 ＋
    YoY 三位數 ＋ 貼 60 日低」的組合在現制下累積最多**排除**旗標（`高PE` 反向、`價格已跌`、
    `位階延伸` 等），等於系統性地把最便宜的成長股丟掉。本 tag 把這組合**正面**標出來。

    ⚠️ **純描述 tag、非 gate**：不改排序、不改剔除、不自動進 picks。命中者 surfacing 到
    機會層評估段**逐檔過**——可判不進，不許不看。條件 4 的 `base_zone=貼底`＝距季線
    MA60 ≤+10%（未延伸），非距低點（語意見 `report/inflection_ambush.py` docstring）；
    此處與 `ma60_dist_pct < 0` 並聯，兩條都是 MA60 口徑，故無 M4.2 的混用問題。

    本 tag **未經前瞻檢驗**（沿 docs/22 §2 flow_turn、docs/24 §3.1 的教訓：直覺 ≠ 證據）。

    Returns:
        True＝命中深值成長。任一條缺值即 False（缺資料不放行，同 M1／M4）。
    """
    if val_pctile is None or float(val_pctile) > max_pctile:
        return False
    if rev_yoy_pct is None or float(rev_yoy_pct) < min_yoy_pct:
        return False
    if gross_margin_pct is None or float(gross_margin_pct) < min_gross_margin_pct:
        return False
    at_base = (ma60_dist_pct is not None and float(ma60_dist_pct) < 0) or (
        base_zone == "貼底"
    )
    return at_base
