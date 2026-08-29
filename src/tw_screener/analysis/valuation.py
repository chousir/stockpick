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


def _fill_from_broad(fine: pl.DataFrame, broad: pl.DataFrame) -> pl.DataFrame:
    """細分類（fine）算不出（樣本 <min_peers）時，用粗分類（broad）補值；細有值則不動。

    多帶一欄 `used_broad`（bool）＝「這檔的值是從粗分類補來的」，供 `peer_source` 標註
    透明（2026-08-29，docs/31 §20.8）：不可讓粗分類補來的值悄悄冒充「次產業」同儕。
    """
    rel_cols = [c for c in _REL_SCHEMA if c != "stock_id"]
    if fine.is_empty() and broad.is_empty():
        return pl.DataFrame(schema={**_REL_SCHEMA, "used_broad": pl.Boolean})
    if broad.is_empty():
        return fine.with_columns(pl.lit(False).alias("used_broad"))
    broad_r = broad.rename({c: f"_b_{c}" for c in rel_cols})
    base_fine = fine if not fine.is_empty() else pl.DataFrame(schema=_REL_SCHEMA)
    merged = base_fine.join(broad_r, on="stock_id", how="full", coalesce=True)
    merged = merged.with_columns(
        (pl.col("subind_pctile").is_null() & pl.col("_b_subind_pctile").is_not_null())
        .alias("used_broad")
    )
    for c in rel_cols:
        merged = merged.with_columns(pl.coalesce(pl.col(c), pl.col(f"_b_{c}")).alias(c))
    return merged.select("stock_id", *rel_cols, "used_broad")


def build_valuation(
    ratios: pl.DataFrame,
    membership: pl.DataFrame,
    min_peers: int = 5,
    cheap_pctile: float = 30.0,
    broad_membership: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """估值長表（docs/13 C1）：官方 PE/PBR + 次產業相對位階（PE 主、PB 補虧損股）+ 相對便宜標。

    Args:
        ratios: (stock_id, market, pe, pbr, dividend_yield) 官方日估值比（單一交易日橫斷面）。
        membership: (sub_industry, stock_id) 同儕分組（手標次產業細 / TWSE 產業別兜底）。
        broad_membership: 選填，`sector_universe.build_broad_industry_membership()` 輸出
            （全市場每檔一列的 TWSE 粗產業分類，不受手標與否影響）。當某檔的手標次產業
            樣本數 <`min_peers`（如晶圓代工全市場僅4檔，永遠算不出同儕）時，**單獨**對
            這幾檔補一次粗分類計算——手標樣本本來就夠的股票不受影響、數字不變
            （2026-08-29 使用者實測發現＋拍板修，docs/31 §20.8）。不傳＝維持舊行為
            （樣本不足永遠 null），向後相容。

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

    pe_valid = base.filter(pl.col("pe_status") == "ok").select("stock_id", "pe")
    pb_valid = base.select("stock_id", "pbr")
    pe_rel = compute_subind_relative(pe_valid, membership, value_col="pe", min_peers=min_peers)
    pb_rel = compute_subind_relative(pb_valid, membership, value_col="pbr", min_peers=min_peers)
    if broad_membership is not None and not broad_membership.is_empty():
        # 手標次產業樣本 <min_peers 時退用粗分類補值——只補「細算不出來」的股票，
        # 細已經算得出來的不動（2026-08-29，docs/31 §20.8）
        pe_rel = _fill_from_broad(
            pe_rel, compute_subind_relative(pe_valid, broad_membership, "pe", min_peers)
        )
        pb_rel = _fill_from_broad(
            pb_rel, compute_subind_relative(pb_valid, broad_membership, "pbr", min_peers)
        )
    else:
        pe_rel = pe_rel.with_columns(pl.lit(False).alias("used_broad"))
        pb_rel = pb_rel.with_columns(pl.lit(False).alias("used_broad"))
    pe_rel = pe_rel.rename(
        {c: f"pe_{c}" for c in (*_REL_SCHEMA, "used_broad") if c != "stock_id"}
    )
    pb_rel = pb_rel.rename(
        {c: f"pb_{c}" for c in (*_REL_SCHEMA, "used_broad") if c != "stock_id"}
    )

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
        # 這檔實際採用的鏡頭（PE或PB）是否吃了粗分類補值——peer_source 判讀要用，
        # 不可讓粗分類補來的值悄悄冒充「次產業」同儕
        pl.when(has_pe).then(pl.col("pe_used_broad"))
        .otherwise(pl.col("pb_used_broad")).fill_null(False).alias("_val_used_broad"),
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

    # peer_source：同儕來自手標次產業（細）、TWSE 產業別兜底（粗），或手標樣本不足
    # 退用粗分類（2026-08-29新增第三種，docs/31 §20.8）——讓相對便宜可信度透明
    if not membership.is_empty():
        src = membership.group_by("stock_id").agg(
            (~pl.col("sub_industry").str.starts_with(PEER_FALLBACK_PREFIX)).any().alias("_fine")
        )
        out = out.join(src, on="stock_id", how="left").with_columns(
            pl.when(pl.col("val_pctile").is_null())
            .then(None)
            .when(pl.col("_fine") & pl.col("_val_used_broad"))
            .then(pl.lit("產業別(次產業樣本不足)"))
            .when(pl.col("_fine"))
            .then(pl.lit("次產業"))
            .otherwise(pl.lit("產業別"))
            .alias("peer_source")
        ).drop("_fine")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias("peer_source"))
    return out.drop("_val_used_broad").select(list(_VALUATION_SCHEMA.keys())).sort(
        "val_pctile", descending=False, nulls_last=True
    )


_SELF_HISTORY_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8, "pe_self_pctile": pl.Float64, "pe_self_n": pl.Int64,
}


def compute_self_history_pctile(
    history: pl.DataFrame, min_snapshots: int = 8
) -> pl.DataFrame:
    """docs/31 §14：個股**自身**PE歷史分布位階（0=自己歷史上最便宜、100=自己歷史上最貴）——
    跟`build_valuation`的`val_pctile`（次產業同儕橫斷面）是不同維度，兩者互補：`val_pctile`
    答「比同業貴還便宜」，這個答「比自己過去貴還便宜」。

    **明確標示為粗版「相對便宜度」讀法，不是嚴謹公允價模型**——沒有利率調整（本益比可能
    隨利率變動，但本地無台灣無風險利率資料源，docs/31 §14.2已查核確認缺席，不可用美國
    公債利率頂替）；`valuation_ratios_*.parquet`目前約10週深度、23個快照（2026-08-23查核），
    稀疏但真實，不是靜態單日代理。

    Args:
        history: `client.load_valuation_ratios_history()` 輸出（date/stock_id/pe/...，
            逐日累積快照的長表，非單日橫斷面）。
        min_snapshots: 該股至少要有幾筆有效歷史PE（含最新一筆）才計算百分位——樣本太少
            （如只有2、3筆）不足以代表「歷史分布」，寧可留null（未取得），不假裝精確。

    Returns:
        (stock_id, pe_self_pctile, pe_self_n)；`pe_self_n`＝實際用來算百分位的有效PE筆數，
        供呼叫端判斷可信度（筆數越少、百分位越不穩）。歷史筆數不足或PE非正 → 該股不出現
        （呼叫端 left join 後自然為 null，不外插、不用0填）。
    """
    if history.is_empty():
        return pl.DataFrame(schema=_SELF_HISTORY_SCHEMA)
    valid = history.filter(pl.col("pe").is_not_null() & (pl.col("pe") > 0))
    if valid.is_empty():
        return pl.DataFrame(schema=_SELF_HISTORY_SCHEMA)
    counts = valid.group_by("stock_id").agg(pl.len().alias("_n")).filter(
        pl.col("_n") >= min_snapshots
    )
    if counts.is_empty():
        return pl.DataFrame(schema=_SELF_HISTORY_SCHEMA)
    eligible = valid.join(counts.select("stock_id"), on="stock_id", how="inner")
    latest = (
        eligible.sort(["stock_id", "date"])
        .group_by("stock_id", maintain_order=True)
        .agg(pl.col("date").last().alias("_latest_date"), pl.col("pe").last().alias("_latest_pe"))
    )
    ranked = eligible.with_columns(
        pl.col("pe").rank(method="average").over("stock_id").alias("_rank"),
        pl.len().over("stock_id").alias("_n"),
    )
    latest_rank = (
        ranked.join(latest, on="stock_id", how="inner")
        .filter(pl.col("date") == pl.col("_latest_date"))
        .unique(subset=["stock_id"], keep="last")
        .with_columns(
            pl.when(pl.col("_n") > 1)
            .then((pl.col("_rank") - 1) / (pl.col("_n") - 1) * 100)
            .otherwise(50.0)
            .round(1)
            .alias("pe_self_pctile")
        )
    )
    return latest_rank.select(
        "stock_id", "pe_self_pctile", pl.col("_n").cast(pl.Int64).alias("pe_self_n")
    ).sort("stock_id")


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


def market_cap_billion(shares_outstanding: int | None, close: float | None) -> float | None:
    """市值（億元）＝ 已發行股數 × 收盤價 / 1e8。任一輸入缺值即回 None（不猜）。

    近似 Goodinfo「市值 (億元)」（策略 D/E/F/G 門檻用的口徑），但非精確等值：
    - 已發行股數僅計普通股（`_parse_listed_shares`/`_parse_otc_shares` 語意），不含特別股。
    - 股數月頻更新（TWSE/TPEX 公司基本資料），收盤價日頻——股本異動（增資/庫藏股）到反映
      有月級延遲。
    - TDR（存託憑證）已在股數來源排除（見 `twse._TDR_INDUSTRY_CODE`），不會算出失真市值。
    """
    if shares_outstanding is None or close is None:
        return None
    return shares_outstanding * close / 1e8


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


_SELF_HISTORY_MEDIAN_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8, "pe_self_median": pl.Float64, "pe_self_n": pl.Int64,
}


def _self_history_median_generic(
    history: pl.DataFrame, ratio_col: str, min_snapshots: int
) -> pl.DataFrame:
    """`compute_self_history_median()`系列的共用核心——泛用欄名`_median`/`_n`，
    呼叫端各自 rename 成自己的輸出欄名（`pe_self_median`／`pb_self_median`／
    `yield_self_median`）。2026-08-29新增（docs/31 §20.9綜合版估值），把原本寫死
    `pe`欄的邏輯抽出來供PB/殖利率重用，PE呼叫端行為完全不變（純refactor、非新邏輯）。
    """
    schema = {"stock_id": pl.Utf8, "_median": pl.Float64, "_n": pl.Int64}
    if history.is_empty():
        return pl.DataFrame(schema=schema)
    valid = history.filter(pl.col(ratio_col).is_not_null() & (pl.col(ratio_col) > 0))
    if valid.is_empty():
        return pl.DataFrame(schema=schema)
    counts = valid.group_by("stock_id").agg(pl.len().alias("_n")).filter(
        pl.col("_n") >= min_snapshots
    )
    if counts.is_empty():
        return pl.DataFrame(schema=schema)
    eligible = valid.join(counts.select("stock_id"), on="stock_id", how="inner")
    return eligible.group_by("stock_id").agg(
        pl.col(ratio_col).median().round(2).alias("_median"),
        pl.len().cast(pl.Int64).alias("_n"),
    ).sort("stock_id")


def compute_self_history_median(
    history: pl.DataFrame, min_snapshots: int = 8
) -> pl.DataFrame:
    """docs/31 §20.7：個股自身PE歷史快照序列的中位數——供`implied_price_from_ratio_median()`
    的「自身回歸」腿使用。跟`compute_self_history_pctile()`同樣的輸入/篩選邏輯（同
    `min_snapshots`門檻、同樣要求pe>0），只是輸出中位數而非百分位排名——兩者互補不
    互相取代：百分位答「現在比自己過去貴還便宜」，中位數是估值回歸參考價要用的實際
    回歸錨點（若PE回到自身歷史中位數，價格會是多少）。

    Args:
        history: 同`compute_self_history_pctile()`——`client.load_valuation_ratios_history()`
            輸出（date/stock_id/pe/...，逐日累積快照長表）。
        min_snapshots: 同`compute_self_history_pctile()`——樣本太少不計算，寧可留null。

    Returns:
        (stock_id, pe_self_median, pe_self_n)。歷史筆數不足或PE非正 → 該股不出現。
    """
    return _self_history_median_generic(history, "pe", min_snapshots).rename(
        {"_median": "pe_self_median", "_n": "pe_self_n"}
    )


_SELF_HISTORY_MEDIAN_PB_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8, "pb_self_median": pl.Float64, "pb_self_n": pl.Int64,
}
_SELF_HISTORY_MEDIAN_YIELD_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8, "yield_self_median": pl.Float64, "yield_self_n": pl.Int64,
}


def compute_self_history_median_pb(
    history: pl.DataFrame, min_snapshots: int = 8
) -> pl.DataFrame:
    """docs/31 §20.9：個股自身PB歷史快照序列的中位數——估值回歸參考價（綜合版）的
    第三條線索。跟`compute_self_history_median()`（PE版）同一套篩選/門檻邏輯，只是
    換算PBR欄，供帳面價值角度的自身回歸腿（跟PE的獲利角度互補、不取代）。

    Args/Returns：同`compute_self_history_median()`，只是欄名換成`pb_self_median`／
    `pb_self_n`。
    """
    return _self_history_median_generic(history, "pbr", min_snapshots).rename(
        {"_median": "pb_self_median", "_n": "pb_self_n"}
    )


def compute_self_history_median_yield(
    history: pl.DataFrame, min_snapshots: int = 8
) -> pl.DataFrame:
    """docs/31 §20.9：個股自身殖利率歷史快照序列的中位數——估值回歸參考價（綜合版）
    的第五條線索。**注意方向與PE/PB相反**：殖利率越低＝價格相對越貴（股利固定時），
    須配`implied_price_from_yield_median()`（反向公式），不可誤用
    `implied_price_from_ratio_median()`（PE/PB正向公式）。

    Args/Returns：同`compute_self_history_median()`，只是欄名換成`yield_self_median`／
    `yield_self_n`，篩選門檻改用`dividend_yield`欄（>0 才算有效，0元股利不計入）。
    """
    return _self_history_median_generic(history, "dividend_yield", min_snapshots).rename(
        {"_median": "yield_self_median", "_n": "yield_self_n"}
    )


def implied_price_from_ratio_median(
    current_price: float | None,
    current_ratio: float | None,
    reference_median: float | None,
) -> float | None:
    """docs/31 §20.7「估值回歸參考價」：若`current_ratio`（PE或PB）回歸到
    `reference_median`，現價會落在哪裡——`current_price × (reference_median /
    current_ratio)`。同一公式供兩種回歸腿共用：傳`val_median`（`build_valuation()`
    輸出的同產業中位數）算「同儕回歸」；傳`pe_self_median`（`compute_self_history_
    median()`輸出）算「自身回歸」。

    **機械式回顧計算，非預測，不可稱「目標價」／「預估價」／「會漲到」**：只用已觀察
    到的現價與已算好的中位數重新定價，沒有任何前瞻/預測成分——跟MA60停損「結構性、
    非預測」同一類（docs/06/playbook/60已收錄這個框架，呼叫端輸出時須附標準免責句）。
    此公式**不是**利率調整/前瞻EPS的DCF公允價模型——那條路已兩次查核確認台灣無風險
    利率資料源缺席、不可行（docs/31 §14.2/§18），本函式刻意不碰。

    Args:
        current_price: 現價。
        current_ratio: 現在的PE或PB（依同一鏡頭跟`reference_median`對齊——呼叫端
            負責確保兩者同基準，本函式不驗證，例如`reference_median`傳PB中位數時
            `current_ratio`也要傳PBR，不可混用PE）。
        reference_median: 回歸錨點（同產業中位數或自身歷史中位數）。

    Returns:
        implied_price；`current_ratio<=0`或任一輸入缺值回None（不硬算、不猜）。
    """
    if current_price is None or current_ratio is None or reference_median is None:
        return None
    if current_ratio <= 0:
        return None
    return round(current_price * (reference_median / current_ratio), 2)


def implied_price_gap_pct(
    implied_price: float | None, current_price: float | None
) -> float | None:
    """`(implied_price / current_price − 1) × 100`——正＝估值回歸參考價高於現價
    （相對便宜，進場訊號）；負＝估值回歸參考價低於現價（基本面看好但價格已衝高，
    等回檔訊號）。正負號語意對應使用者原話（docs/31 §20.7）。
    """
    if implied_price is None or current_price is None or current_price <= 0:
        return None
    return round((implied_price / current_price - 1) * 100, 1)


def implied_price_from_yield_median(
    current_price: float | None,
    current_yield_pct: float | None,
    reference_yield_median_pct: float | None,
) -> float | None:
    """docs/31 §20.9「估值回歸參考價（綜合版）」殖利率腿——**方向與PE/PB相反**。

    股利固定時，殖利率(dividend_yield_pct)＝股利/價格×100，價格越高殖利率越低——
    跟PE/PB「比值越高、implied_price越高」相反。若目前殖利率回歸到參考中位數，
    現價會是 `current_price × (current_yield_pct / reference_yield_median_pct)`
    （注意分子分母順序與`implied_price_from_ratio_median()`相反，不可誤用該函式）。

    正負號語意仍與其他腿一致（`implied_price_gap_pct()`共用）：implied_price高於
    現價＝正＝相對便宜；低於現價＝負＝現價已跑贏、看好但衝高。**機械式回顧計算，
    非預測**，同`implied_price_from_ratio_median()`的紅線框架，不可稱「目標價」。

    Args:
        current_price: 現價。
        current_yield_pct: 目前殖利率（%，如3.5代表3.5%）。
        reference_yield_median_pct: 參考殖利率中位數（同儕橫斷面或自身歷史）。

    Returns:
        implied_price；任一輸入缺值或非正即 None（0元股利股無此腿，不猜）。
    """
    if current_price is None or current_yield_pct is None or reference_yield_median_pct is None:
        return None
    if current_price <= 0 or current_yield_pct <= 0 or reference_yield_median_pct <= 0:
        return None
    return round(current_price * (current_yield_pct / reference_yield_median_pct), 2)


_VALUATION_LEGS_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8, "pb_peer_median": pl.Float64, "yield_peer_median": pl.Float64,
}


def compute_valuation_legs(
    ratios: pl.DataFrame,
    membership: pl.DataFrame,
    min_peers: int = 5,
    broad_membership: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """docs/31 §20.9「估值回歸參考價（綜合版）」的**額外**同儕線索——跟`build_valuation()`
    平行、互不影響的計算，刻意不動`build_valuation()`本身（G5候選/`cp_valuation.md`
    依賴其既有val_metric/val_median行為，不可因為加綜合版順手改到）。

    `build_valuation()`只在PE不可用時才把PB當「主」鏡頭（val_metric=PB）；本函式
    **不論PE是否可用，都額外算一次PB同儕中位數**，供綜合版當獨立於獲利面的帳面
    價值線索；同理額外算殖利率同儕中位數。

    Args:
        ratios: 同`build_valuation()`——(stock_id, market, pe, pbr, dividend_yield)。
        membership: 同`build_valuation()`。
        min_peers: 同`build_valuation()`。
        broad_membership: 同`build_valuation()`——手標次產業樣本不足時的粗分類兜底
            （docs/31 §20.8），這裡同步套用，理由一致。

    Returns:
        (stock_id, pb_peer_median, yield_peer_median)。同儕不足（含粗分類兜底後仍
        不足）→ null，不猜。
    """
    if ratios.is_empty():
        return pl.DataFrame(schema=_VALUATION_LEGS_SCHEMA)
    base = ratios.select("stock_id", "pbr", "dividend_yield").unique(
        "stock_id", keep="first"
    )
    pb_rel = compute_subind_relative(
        base.select("stock_id", "pbr"), membership, value_col="pbr", min_peers=min_peers
    )
    yield_rel = compute_subind_relative(
        base.select("stock_id", "dividend_yield"), membership,
        value_col="dividend_yield", min_peers=min_peers,
    )
    if broad_membership is not None and not broad_membership.is_empty():
        pb_rel = _fill_from_broad(
            pb_rel,
            compute_subind_relative(
                base.select("stock_id", "pbr"), broad_membership, "pbr", min_peers
            ),
        )
        yield_rel = _fill_from_broad(
            yield_rel,
            compute_subind_relative(
                base.select("stock_id", "dividend_yield"), broad_membership,
                "dividend_yield", min_peers,
            ),
        )
    out = base.select("stock_id").join(
        pb_rel.select("stock_id", pl.col("subind_median").alias("pb_peer_median")),
        on="stock_id", how="left",
    ).join(
        yield_rel.select("stock_id", pl.col("subind_median").alias("yield_peer_median")),
        on="stock_id", how="left",
    )
    return out.select(list(_VALUATION_LEGS_SCHEMA.keys()))


def compute_composite_valuation_gap(
    legs: list[float | None],
) -> tuple[float | None, int]:
    """docs/31 §20.9：把多條估值缺口%線索（同儕PE/自身PE/同儕PB/自身PB/同儕殖利率/
    自身殖利率，各自可能null）合成一個「估值回歸參考價（綜合版）」缺口%。

    **取中位數、不取平均**——中位數對單一極端腿較不敏感（如某條線索因同儕樣本異常
    算出離譜大的gap%，平均會被拉走、中位數不會）。回傳的`n_legs`必須跟著結果一起
    印出（docs/11規格要求）：鏡頭數越少，這個綜合數字信心越低，1–2條不算穩健的
    「綜合」，只是剛好只有一條線索有資料，不可包裝成更權威的樣子。

    Args:
        legs: 各條線索的gap%（`implied_price_gap_pct()`/依殖利率反向公式算出的gap%），
            缺值傳None，函式自己過濾。

    Returns:
        (綜合gap%, 實際用了幾條非null線索)。全部為None → (None, 0)。
    """
    valid = [v for v in legs if v is not None]
    if not valid:
        return None, 0
    s = sorted(valid)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2
    return round(median, 1), n


def peg_like_ratio(pe: float | None, rev_yoy_pct: float | None) -> float | None:
    """docs/31 §18：PE 對月營收YoY成長率之比（PEG-like，非傳統EPS-based PEG）。

    傳統PEG＝PE / EPS成長率，但本地無法算EPS YoY（`fundamentals`快取僅2季，
    無去年同季可比），改用官方月營收YoY（`rev_yoy_pct`，TWSE自身算好的年增率，
    已在candidates_enriched.csv使用）當成長替代——這是妥協，不是等價物，呼叫端
    需標明「PE對營收成長比，非EPS-based PEG」。無利率調整（沒有台灣無風險利率
    資料源，docs/31 §14.2已查核確認缺席）。

    只在`pe>0`且`rev_yoy_pct>0`時有意義（虧損股無正PE；成長為負/零時比值方向
    會反轉、不可比照「數字小＝便宜」的PEG直覺去讀）——任一條件不滿足回None，
    不硬算、不給誤導性數字。
    """
    if pe is None or pe <= 0:
        return None
    if rev_yoy_pct is None or rev_yoy_pct <= 0:
        return None
    return round(pe / rev_yoy_pct, 2)
