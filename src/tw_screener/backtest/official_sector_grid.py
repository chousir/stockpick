"""backtest/official_sector_grid.py — docs/31 §10 milestone：用 TWSE 官方產業分類＋
MI_INDEX 官方市值加權指數，重測 G3 milestone 的 leftover finding「族群前5」。

**跟 G3 的關係（§10.1 已寫，這裡重申避免誤讀）**：這是全新假設，不是重跑 G3 的資料——
分組單位換成官方產業分類（`official_membership_frame` 已有，WS-J.2 robustness 用途）、
族群強弱量尺換成官方發布的市值加權指數點數（不是等權籃子）。只測「族群前5」單一維度，
刻意不重建 G3 的 4 維格（G3 leftover finding 已明講其餘 3 維沒有加值，疊上去只會稀釋
訊號、也是另一種 p-hack）。門檻／停止條件見 docs/31 §10.3，執行前已寫死。

**industry_name → MI_INDEX 類指數映射**（§10.2 查核結果，非猜測）：32 種可直接映射
（去`業`/`工業`字尾對應官方類指數，`化學`/`生技醫療`/`水泥`/`塑膠`優先採拆分版，
2021-07-01 起即已存在，不需退回合併版當備援）；4 種較新分類指數存在但歷史較淺
（如實納入，資料缺席的日期自然 null，不外插）；1 組跨期改名（觀光／觀光餐旅，候選清單
「先試新名查無退回舊名」解決）；7 種查無對應官方指數，明確排除（見 `_UNMAPPED_INDUSTRIES`）。
"""

from __future__ import annotations

import math

import polars as pl

from tw_screener.backtest.factor_lab import (
    REGIME_LABELS,
    REGIME_MIN_N,
    moving_block_bootstrap_ci,
)

# industry_name → 候選 MI_INDEX index_name（依偏好順序；第一個在當日表格出現的即採用）。
# 2026-08-23 查核：32 組跨 2021-07-01/2022-06-01/2023-01-03/2026-08-21 四個日期穩定映射。
_INDUSTRY_TO_INDEX_ALIASES: dict[str, tuple[str, ...]] = {
    "半導體業": ("半導體類指數",),
    "光電業": ("光電類指數",),
    "電子零組件業": ("電子零組件類指數",),
    "其他電子業": ("其他電子類指數",),
    "電腦及周邊設備業": ("電腦及週邊設備類指數",),
    "通信網路業": ("通信網路類指數",),
    "電子通路業": ("電子通路類指數",),
    "資訊服務業": ("資訊服務類指數",),
    "化學工業": ("化學類指數",),
    "生技醫療業": ("生技醫療類指數",),
    "塑膠工業": ("塑膠類指數",),
    "水泥工業": ("水泥類指數",),
    "食品工業": ("食品類指數",),
    "紡織纖維": ("紡織纖維類指數",),
    "電機機械": ("電機機械類指數",),
    "電器電纜": ("電器電纜類指數",),
    "玻璃陶瓷": ("玻璃陶瓷類指數",),
    "造紙工業": ("造紙類指數",),
    "鋼鐵工業": ("鋼鐵類指數",),
    "橡膠工業": ("橡膠類指數",),
    "汽車工業": ("汽車類指數",),
    "建材營造": ("建材營造類指數",),
    "航運業": ("航運類指數",),
    # 跨期改名：2021-2022 用「觀光類指數」，2026 已改名「觀光餐旅類指數」——兩個公司
    # 分類名映射到同一個 canonical 群組，候選清單先試新名查無退回舊名。
    "觀光事業": ("觀光餐旅類指數", "觀光類指數"),
    "觀光餐旅業": ("觀光餐旅類指數", "觀光類指數"),
    "貿易百貨": ("貿易百貨類指數",),
    "油電燃氣業": ("油電燃氣類指數",),
    "金融保險": ("金融保險類指數",),
    # 較新分類：官方指數存在但歷史較淺（2021-07-01 尚無，2026-08-21 已有）——如實納入，
    # 資料缺席的日期該股 trend_score 自然為 null，不外插、不排除整組。
    "綠能環保": ("綠能環保類指數",),
    "數位雲端": ("數位雲端類指數",),
    "運動休閒業": ("運動休閒類指數",),
    "居家生活": ("居家生活類指數",),
}

# 查無對應官方指數、明確排除（§10.2）——不在 _INDUSTRY_TO_INDEX_ALIASES 裡的 industry_name
# 一律視為排除，此常數只用來讓排除清單在程式碼裡顯式可讀、供文件/測試對照，非額外判斷邏輯。
_UNMAPPED_INDUSTRIES: tuple[str, ...] = (
    "存託憑證", "其他", "其他製造業", "文化創意業", "農業科技業", "軟體工業", "電力供應業",
)

_MEMBERSHIP_SCHEMA: dict[str, type[pl.DataType]] = {"sub_industry": pl.Utf8, "stock_id": pl.Utf8}
_BASKET_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8, "date": pl.Date, "basket_index": pl.Float64,
}


def _smean(s: pl.Series) -> float | None:
    v = s.mean()
    return float(v) if isinstance(v, (int, float)) else None


def build_official_sector_membership(official_membership: pl.DataFrame) -> pl.DataFrame:
    """`official_membership_frame()` 輸出（sub_industry=原始industry_name）→ 過濾＋改名
    成本模組的 canonical 群組標籤（§10.2 映射）。

    Args:
        official_membership: `rotation_efficacy.official_membership_frame(industry)` 的
            輸出（sub_industry 欄目前放的是原始 industry_name，尚未映射）。

    Returns:
        (sub_industry, stock_id)；sub_industry 已換成 canonical 群組標籤（映射表的 key），
        查無對應官方指數的 industry_name 整批排除——呼叫端可用兩表 stock_id 數量差算
        排除檔數（同 `official_membership_frame` 的既有誠實帳慣例）。
    """
    if official_membership.is_empty():
        return pl.DataFrame(schema=_MEMBERSHIP_SCHEMA)
    return (
        official_membership.filter(pl.col("sub_industry").is_in(list(_INDUSTRY_TO_INDEX_ALIASES)))
        .select("sub_industry", "stock_id")
        .unique(maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )


def _pick_index_series(idx: pl.DataFrame, candidates: tuple[str, ...]) -> pl.DataFrame:
    """每個日期取候選清單裡優先序最高、當天有出現的那一個 index_name（date, close_index）。

    處理跨期改名（如觀光類指數→觀光餐旅類指數）；候選清單只有一個名字時等同精確 join。
    """
    sub = idx.filter(pl.col("index_name").is_in(list(candidates)))
    if sub.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "close_index": pl.Float64})
    pref = {name: i for i, name in enumerate(candidates)}
    return (
        sub.with_columns(pl.col("index_name").replace_strict(pref).alias("_pref"))
        .sort(["date", "_pref"])
        .unique(subset=["date"], keep="first")
        .select("date", "close_index")
    )


def build_official_sector_baskets(sector_index: pl.DataFrame) -> pl.DataFrame:
    """`client.load_sector_index_history()` 輸出（index_name, date, close_index）→
    以 canonical 群組標籤為 key 的 (sub_industry, date, basket_index) frame，
    供直接餵給 `rotation_efficacy.trend_score_series(price, membership, baskets)`
    （該函式只讀 baskets 的這三欄，見模組頂 docstring）。
    """
    if sector_index.is_empty():
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    idx = sector_index.select("date", "index_name", "close_index")
    rows: list[pl.DataFrame] = []
    for canonical, candidates in _INDUSTRY_TO_INDEX_ALIASES.items():
        picked = _pick_index_series(idx, candidates)
        if picked.is_empty():
            continue
        rows.append(
            picked.select(
                pl.lit(canonical).alias("sub_industry"),
                "date",
                pl.col("close_index").alias("basket_index"),
            )
        )
    if not rows:
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    return pl.concat(rows).sort(["sub_industry", "date"])


_PURITY_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8, "dominant_official": pl.Utf8,
    "n_total": pl.UInt32, "n_match": pl.UInt32, "purity": pl.Float64,
}


def compute_subindustry_purity(
    hand_membership: pl.DataFrame, official_industry: pl.DataFrame
) -> pl.DataFrame:
    """§10.6：手標次產業（`list_subindustries()`）成員 join 官方 `industry_name`，
    算每個手標群組的多數官方類別與純度（`n_match`/`n_total`）。

    純度分母含 industry_name 缺席（null/空字串）的成員——這些股票對「這組能不能對應到
    單一官方類別」也是雜訊來源，不能悄悄從分母排除、假裝乾淨。全員皆缺席
    → `dominant_official=None`、`purity=0.0`（不是 100%，避免「查無資料」誤讀成「完全
    純」）。
    """
    if hand_membership.is_empty() or official_industry.is_empty():
        return pl.DataFrame(schema=_PURITY_SCHEMA)
    joined = hand_membership.join(
        official_industry.select("stock_id", "industry_name"), on="stock_id", how="left"
    )
    totals = joined.group_by("sub_industry").agg(pl.len().cast(pl.UInt32).alias("n_total"))
    counts = (
        joined.filter(pl.col("industry_name").is_not_null() & (pl.col("industry_name") != ""))
        .group_by("sub_industry", "industry_name")
        .agg(pl.len().cast(pl.UInt32).alias("n_match"))
    )
    if counts.is_empty():
        return totals.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("dominant_official"),
            pl.lit(0, dtype=pl.UInt32).alias("n_match"),
            pl.lit(0.0).alias("purity"),
        ).select(list(_PURITY_SCHEMA))
    top = (
        counts.sort(["sub_industry", "n_match"], descending=[False, True])
        .unique(subset=["sub_industry"], keep="first")
        .rename({"industry_name": "dominant_official"})
    )
    return (
        totals.join(top, on="sub_industry", how="left")
        .with_columns(
            pl.col("n_match").fill_null(0),
            (pl.col("n_match").fill_null(0) / pl.col("n_total")).alias("purity"),
        )
        .select(list(_PURITY_SCHEMA))
        .sort("sub_industry")
    )


def _eligible_hand_groups(purity: pl.DataFrame, min_purity: float) -> pl.DataFrame:
    """§10.6 門檻：purity≥min_purity 且多數官方類別本身有 MI_INDEX 對應（§10.2）——
    兩者缺一不納入，不猜測、不用次高票替代。
    """
    return purity.filter(
        (pl.col("purity") >= min_purity)
        & pl.col("dominant_official").is_not_null()
        & pl.col("dominant_official").is_in(list(_INDUSTRY_TO_INDEX_ALIASES))
    )


def build_hand_sector_membership(
    hand_membership: pl.DataFrame, purity: pl.DataFrame, min_purity: float = 0.7
) -> pl.DataFrame:
    """§10.6：手標次產業原始標籤（不改名）→ 過濾至 purity 達標的群組。"""
    if hand_membership.is_empty() or purity.is_empty():
        return pl.DataFrame(schema=_MEMBERSHIP_SCHEMA)
    eligible = _eligible_hand_groups(purity, min_purity)["sub_industry"].to_list()
    return (
        hand_membership.filter(pl.col("sub_industry").is_in(eligible))
        .select("sub_industry", "stock_id")
        .unique(maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )


def build_hand_sector_baskets(
    sector_index: pl.DataFrame, purity: pl.DataFrame, min_purity: float = 0.7
) -> pl.DataFrame:
    """§10.6：手標次產業標籤 → 用其「多數官方類別」對應的 MI_INDEX 指數當 basket_index。

    多個手標群組若多數票落在同一個官方類別（如封測/晶圓代工/IC設計服務皆→半導體業），
    會共用同一條 basket_index 數列——這是已知、predetermined 的副作用（見 §10.6），
    不是 bug，呼叫端報告需如實揭露。
    """
    if sector_index.is_empty() or purity.is_empty():
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    idx = sector_index.select("date", "index_name", "close_index")
    eligible = _eligible_hand_groups(purity, min_purity)
    rows: list[pl.DataFrame] = []
    for r in eligible.iter_rows(named=True):
        candidates = _INDUSTRY_TO_INDEX_ALIASES[r["dominant_official"]]
        picked = _pick_index_series(idx, candidates)
        if picked.is_empty():
            continue
        rows.append(
            picked.select(
                pl.lit(r["sub_industry"]).alias("sub_industry"),
                "date",
                pl.col("close_index").alias("basket_index"),
            )
        )
    if not rows:
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    return pl.concat(rows).sort(["sub_industry", "date"])


def _block_len_snapshots(h: int, snapshot_gap_td: int) -> int:
    return max(1, math.ceil((h + 1) / max(1, snapshot_gap_td)))


def official_group_rank_grid(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    top_n_groups: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """單一維度「族群前5 vs 非前5」forward 報酬對照（docs/31 §10.3 預先登記）。

    刻意只有 2 個 cell（不像 g3_grid 的 16 格）——G3 leftover finding 只在乎族群強弱
    這一個維度，另外 3 維（貼落後/位階/資金）已知沒有加值，本節不重建、避免稀釋訊號
    或變相 p-hack。

    Args:
        stock_rows: 需含 date / sub_industry（canonical 官方群組）/ trend_score（官方
            指數版）/ alpha{h}。
    """
    schema = {
        "horizon": pl.Int64, "group_top5": pl.Boolean, "n": pl.UInt32, "n_dates": pl.UInt32,
        "mean": pl.Float64, "median": pl.Float64, "win_rate": pl.Float64,
        "delta_mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64,
        "mean_h1": pl.Float64, "mean_h2": pl.Float64,
    }
    need = {"date", "sub_industry", "trend_score"}
    if stock_rows.is_empty() or not need.issubset(stock_rows.columns):
        return pl.DataFrame(schema=schema)
    base = stock_rows.drop_nulls(list(need))
    if base.is_empty():
        return pl.DataFrame(schema=schema)

    group_rank = (
        base.select("date", "sub_industry", "trend_score")
        .unique(subset=["date", "sub_industry"])
        .with_columns(
            pl.col("trend_score").rank(method="min", descending=True).over("date").alias("_rank")
        )
        .select("date", "sub_industry", "_rank")
    )
    base = base.join(group_rank, on=["date", "sub_industry"], how="left").with_columns(
        (pl.col("_rank") <= top_n_groups).alias("group_top5")
    )
    mid_date = base["date"].median()

    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for is_top5, g in sub.group_by("group_top5", maintain_order=True):
            top5 = bool(is_top5[0] if isinstance(is_top5, tuple) else is_top5)
            vals = [float(v) for v in g[tgt].to_list()]
            h1 = g.filter(pl.col("date") <= mid_date)[tgt]
            h2 = g.filter(pl.col("date") > mid_date)[tgt]
            med = g[tgt].median()
            daily_delta = (
                g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
            )
            delta_vals = [float(v) for v in daily_delta["_m"].to_list() if v is not None]
            ci_lo, ci_hi = (
                moving_block_bootstrap_ci(delta_vals, block_len=block_len, n_boot=n_boot, seed=seed)
                if delta_vals else (None, None)
            )
            rows.append(
                {
                    "horizon": h, "group_top5": top5,
                    "n": len(vals), "n_dates": len(delta_vals),
                    "mean": sum(vals) / len(vals) if vals else None,
                    "median": float(med) if isinstance(med, (int, float)) else None,
                    "win_rate": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
                    "delta_mean": sum(delta_vals) / len(delta_vals) if delta_vals else None,
                    "ci_lo": ci_lo, "ci_hi": ci_hi,
                    "mean_h1": _smean(h1), "mean_h2": _smean(h2),
                }
            )
    return pl.DataFrame(rows, schema=schema).sort(["horizon", "group_top5"])


def official_group_rank_by_regime(
    stock_rows: pl.DataFrame,
    horizons: tuple[int, ...] = (10, 20),
    top_n_groups: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
    regime_col: str = "regime",
    snapshot_gap_td: int = 5,
) -> pl.DataFrame:
    """族群前5格×regime forward 報酬（對delta做CI，理由同 g3_grid 同名函式）。"""
    schema = {
        "horizon": pl.Int64, "regime": pl.Utf8, "n": pl.Int64, "n_dates": pl.Int64,
        "mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64, "thin": pl.Boolean,
    }
    need = {"date", "sub_industry", "trend_score"}
    if (
        stock_rows.is_empty()
        or not need.issubset(stock_rows.columns)
        or regime_col not in stock_rows.columns
        or stock_rows[regime_col].drop_nulls().is_empty()
    ):
        return pl.DataFrame(schema=schema)
    base = stock_rows.drop_nulls(list(need))
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    group_rank = (
        base.select("date", "sub_industry", "trend_score")
        .unique(subset=["date", "sub_industry"])
        .with_columns(
            pl.col("trend_score").rank(method="min", descending=True).over("date").alias("_rank")
        )
        .select("date", "sub_industry", "_rank")
    )
    base = base.join(group_rank, on=["date", "sub_industry"], how="left")

    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in base.columns:
            continue
        sub = base.drop_nulls([tgt])
        pop_by_date = sub.group_by("date").agg(pl.col(tgt).mean().alias("_pop_mean"))
        sub = sub.join(pop_by_date, on="date", how="left").with_columns(
            (pl.col(tgt) - pl.col("_pop_mean")).alias("_delta")
        )
        top5_only = sub.filter(pl.col("_rank") <= top_n_groups)
        block_len = _block_len_snapshots(h, snapshot_gap_td)
        for reg in REGIME_LABELS:
            g = top5_only.filter(pl.col(regime_col) == reg)
            daily = g.group_by("date").agg(pl.col("_delta").mean().alias("_m")).sort("date")
            vals = [float(v) for v in daily["_m"].to_list() if v is not None]
            lo_ci, hi_ci = (
                moving_block_bootstrap_ci(vals, block_len=block_len, n_boot=n_boot, seed=seed)
                if vals else (None, None)
            )
            rows.append(
                {
                    "horizon": h, "regime": reg, "n": g.height, "n_dates": len(vals),
                    "mean": (sum(vals) / len(vals)) if vals else None,
                    "ci_lo": lo_ci, "ci_hi": hi_ci, "thin": len(vals) < REGIME_MIN_N,
                }
            )
    return pl.DataFrame(rows, schema=schema)
