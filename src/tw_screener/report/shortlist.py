"""report/shortlist.py — M-Pick1 機器排序 Top N（純函式；docs/11）。

設計意圖：
  pick.md 三層 16 檔太雜，且 4 週窗分層無鑑別力（核心 −0.4%／機會 −0.9%／補充池 +0.2%）。
  本模組只用 repo 已驗證的訊號做 **deterministic** 排序，產 reports/<週>/shortlist.csv；
  Opus 之後只負責依序寫說明（不得重排）：

  - 族群趨勢分 trend_score（F3 價格趨勢分，r+20 IC +0.11、三個 regime CI 皆 >0；
    docs/22、docs/23）＝主鍵：次產業依分數排名後切桶，只有前段桶可入選。
  - 距季線位階 ma60_dist_pct（候選池內 IC −0.217、5–10% 桶最好；弱證據、單 regime）
    ＝F2 上下限 gate＋同桶內次序鍵（偏好帶距離）。
  - 有效剔除旗標（強漲法人賣／低流動；位階延伸/過熱已被 F2 上限涵蓋、土洋對作無效不列）
    ＝gate。

  個股層法人流（水位／近端佔比／轉折）全數否證（docs/19、docs/20、docs/22 §4）→ 不入排序、
  不當否決理由，也**不寫進任何說明欄**（evidence／bear_hints／unvalidated_notes）。
  所有門檻讀 settings `picks.shortlist`（＋沿用 picks.core_ext_ma60_max_pct、
  propicks_flags.low_liquidity_amount、portfolio.factor_clusters）。

  IO（讀 enriched／輪動表／宇宙成員、寫 CSV、印摘要）由 shortlist_runner 負責。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import polars as pl
from loguru import logger

from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND
from tw_screener.analysis.portfolio import compute_factor_cluster_exposure

SHORTLIST_FILENAME = "shortlist.csv"

# 產物欄位（名稱與順序固定，docs/11 與 picks sync 皆依此讀）
SHORTLIST_COLUMNS: tuple[str, ...] = (
    "week", "data_date", "rotation_date", "stock_id", "name", "source", "sub_industry",
    "trend_score", "trend_rank", "trend_n", "trend_bucket", "close", "ma20_price",
    "ma60_price", "ma60_dist_pct", "low_20d", "low_60d", "amount_million", "pe_ratio",
    "flags", "tier", "rank", "gate_reason", "entry_low", "entry_high", "entry_text",
    "stop_price", "stop_basis", "stop_text", "evidence", "bear_hints", "unvalidated_notes",
)

SOURCE_CANDIDATE = "candidate"
SOURCE_WATCHLIST = "watchlist"
SOURCE_HOLDING = "holding"

TIER_TOP = "top"
TIER_ALT = "alt"
TIER_CAPPED = "capped"
TIER_GATED = "gated"
TIER_HELD = "held"
_TIER_ORDER = {TIER_TOP: 0, TIER_ALT: 1, TIER_CAPPED: 2, TIER_GATED: 3, TIER_HELD: 4}

GATE_HELD = "held"
CAP_SUB_INDUSTRY = "cap_sub_industry"
CAP_CLUSTER = "cap_cluster"
BEYOND_N = "beyond_n"
LOW_LIQUIDITY_FLAG = "低流動"
PRICE_DISC_FLAG = "價格不連續"   # flags 內的標記形如「價格不連續(2026-09-24 +19.2%)」

STOP_BASIS_MA60 = "MA60"
STOP_BASIS_LOW60 = "low_60d（均線糾結）"
STOP_BASIS_BELOW = "low_60d（已跌破 MA60）"   # 僅持股/gated 列可能（top/alt 已被 ext_min_pct 擋）
BEAR_HINT_HEAD = "族群層訊號、個股層未驗證"
VETO_REASON = "機器排序否決"   # picks sync：excluded 的否決紀錄 reason（docs/11 Opus 否決規則）
_BEAR_FUND_HEALTH = ("減速", "轉差")   # fundamental_health 的空方類別（M-BR1 §2.1 分類詞彙）
_EMPTY_STRATEGY = {"", "_", "-", "—"}

_TEXT_COLS = ("name", "asset_type", "industry", "theme", "strategy", "flags", "fundamental_health")
_NUM_COLS = (
    "close", "ma20_price", "ma60_price", "ma60_dist_pct", "low_20d", "low_60d",
    "amount_million", "pe_ratio", "val_gap_pct_composite",
)
_BOOL_COLS = ("price_discontinuity", "deep_value_growth", "contrarian_ready")


@dataclass(frozen=True)
class ShortlistConfig:
    """settings `picks.shortlist` ＋ 沿用的外部門檻。預設值＝settings.yaml 出貨值（缺鍵時兜底）。"""

    enabled: bool = True
    top_n: int = 5
    alt_n: int = 5
    trend_buckets: int = 5
    max_bucket_for_top: int = 2
    ext_min_pct: float = 0.0
    ext_max_pct: float = 15.0                     # picks.core_ext_ma60_max_pct（F2 上限）
    ext_band_pct: tuple[float, float] = (5.0, 10.0)
    exclude_flags: tuple[str, ...] = ("強漲法人賣", LOW_LIQUIDITY_FLAG)
    low_liquidity_amount: float = 100.0           # propicks_flags.low_liquidity_amount（百萬）
    max_per_sub_industry: int = 1
    max_per_cluster: int = 2
    include_watchlist: bool = True
    include_holdings: bool = False
    tangle_pct: float = 2.0
    bear_pe_high: float = 30.0
    bear_stop_dist_pct: float = 10.0
    max_vetoes: int = 2
    factor_clusters: tuple[Mapping[str, Any], ...] = ()   # portfolio.factor_clusters

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> ShortlistConfig:
        """從整份 settings dict 組出設定（picks.shortlist 為主，外部門檻沿用各自出處）。"""
        d = cls()
        picks = cfg.get("picks") or {}
        sl = picks.get("shortlist") or {}
        band = sl.get("ext_band_pct") or d.ext_band_pct
        return cls(
            enabled=bool(sl.get("enabled", d.enabled)),
            top_n=int(sl.get("top_n", d.top_n)),
            alt_n=int(sl.get("alt_n", d.alt_n)),
            trend_buckets=int(sl.get("trend_buckets", d.trend_buckets)),
            max_bucket_for_top=int(sl.get("max_bucket_for_top", d.max_bucket_for_top)),
            ext_min_pct=float(sl.get("ext_min_pct", d.ext_min_pct)),
            ext_max_pct=float(picks.get("core_ext_ma60_max_pct", d.ext_max_pct)),
            ext_band_pct=(float(band[0]), float(band[1])),
            exclude_flags=tuple(str(f) for f in sl.get("exclude_flags", d.exclude_flags)),
            low_liquidity_amount=float(
                (cfg.get("propicks_flags") or {}).get(
                    "low_liquidity_amount", d.low_liquidity_amount
                )
            ),
            max_per_sub_industry=int(sl.get("max_per_sub_industry", d.max_per_sub_industry)),
            max_per_cluster=int(sl.get("max_per_cluster", d.max_per_cluster)),
            include_watchlist=bool(sl.get("include_watchlist", d.include_watchlist)),
            include_holdings=bool(sl.get("include_holdings", d.include_holdings)),
            tangle_pct=float(sl.get("tangle_pct", d.tangle_pct)),
            bear_pe_high=float(sl.get("bear_pe_high", d.bear_pe_high)),
            bear_stop_dist_pct=float(sl.get("bear_stop_dist_pct", d.bear_stop_dist_pct)),
            max_vetoes=int(sl.get("max_vetoes", d.max_vetoes)),
            factor_clusters=tuple((cfg.get("portfolio") or {}).get("factor_clusters") or ()),
        )


# ─── 候選池 ───────────────────────────────────────────────────────────────────


def _truthy(col: str) -> pl.Expr:
    """bool／"true"／"True"／"1" → True；null／其他 → False。

    CSV 推斷型別不定：同一欄在某週是 Boolean、在全 null 的週會被讀成字串。
    """
    return (
        pl.col(col)
        .cast(pl.Utf8, strict=False)
        .str.strip_chars()
        .str.to_lowercase()
        .is_in(["true", "1", "t", "yes"])
        .fill_null(False)
    )


def normalize_enriched(df: pl.DataFrame, source: str) -> pl.DataFrame:
    """enriched CSV（candidates/watchlist/holdings 同口徑）→ 排序器所需欄＋source；缺欄補 null。"""
    exprs: list[pl.Expr] = [
        pl.col("stock_id").cast(pl.Utf8).str.strip_chars().alias("stock_id"),
        pl.lit(source, dtype=pl.Utf8).alias("source"),
    ]
    for c in _TEXT_COLS:
        exprs.append(
            pl.col(c).cast(pl.Utf8, strict=False).alias(c)
            if c in df.columns
            else pl.lit(None, dtype=pl.Utf8).alias(c)
        )
    for c in _NUM_COLS:
        exprs.append(
            pl.col(c).cast(pl.Float64, strict=False).alias(c)
            if c in df.columns
            else pl.lit(None, dtype=pl.Float64).alias(c)
        )
    for c in _BOOL_COLS:
        exprs.append(_truthy(c).alias(c) if c in df.columns else pl.lit(False).alias(c))
    return df.select(exprs)


def build_pool(
    candidates: pl.DataFrame,
    watchlist: pl.DataFrame | None,
    holdings: pl.DataFrame | None,
    cfg: ShortlistConfig,
) -> pl.DataFrame:
    """合併三個來源成候選池（每 stock_id 一列）；重複時 candidate ＞ watchlist ＞ holding。

    watchlist 只在 include_watchlist 時併入；holdings 一律併入（include_holdings=false 時
    由 apply_gates 標 held、不排名，停損價照算供持股動作表用）。
    """
    frames = [normalize_enriched(candidates, SOURCE_CANDIDATE)]
    if cfg.include_watchlist and watchlist is not None and not watchlist.is_empty():
        frames.append(normalize_enriched(watchlist, SOURCE_WATCHLIST))
    if holdings is not None and not holdings.is_empty():
        frames.append(normalize_enriched(holdings, SOURCE_HOLDING))
    pool = pl.concat(frames, how="vertical")
    return pool.filter(pl.col("stock_id").is_not_null() & (pl.col("stock_id") != "")).unique(
        subset=["stock_id"], keep="first", maintain_order=True
    )


# ─── 族群趨勢 ─────────────────────────────────────────────────────────────────


def order_members_by_concepts(members: pl.DataFrame, themes: pl.DataFrame) -> pl.DataFrame:
    """把 (sub_industry, stock_id) 依 concepts.yaml 標籤列序重排，使「多標籤取第一個」＝手標主身分。

    universe.csv／list_subindustries() 已按 sub_industry 字母序排過，直接取第一筆會變成
    「字母序最前」，而非 group_report._annotate_sector_flag_coverage 的慣例（themes_long
    原序＝concepts.yaml 手標順序，第一個次產業＝主身分）。themes＝load_themes() 輸出；
    查無列序的成員排最後、維持原序。
    """
    if members.is_empty() or themes.is_empty() or "kind" not in themes.columns:
        return members
    order = (
        themes.filter(pl.col("kind") == SUB_INDUSTRY_KIND)
        .select(
            pl.col("stock_id").cast(pl.Utf8),
            pl.col("theme").cast(pl.Utf8).alias("sub_industry"),
        )
        .unique(subset=["stock_id", "sub_industry"], keep="first", maintain_order=True)
        .with_row_index("_ord")
    )
    return (
        members.with_row_index("_orig")
        .join(order, on=["stock_id", "sub_industry"], how="left")
        .sort(["_ord", "_orig"], nulls_last=True)
        .drop(["_ord", "_orig"])
    )


def sector_trend_table(rotation: pl.DataFrame, trend_buckets: int) -> pl.DataFrame:
    """輪動表 → (sub_industry, trend_score, trend_rank, trend_n, trend_bucket)。

    trend_rank＝在「有 trend_score 的次產業」中依分數降冪排名，同分同名次（method=min，
    避免同分因列序落不同桶）；trend_n＝有分數的次產業數；trend_bucket＝ceil(rank·k/n)。
    不沿用檔內 radar_rank：它是 rank_by 鍵的列序名次（同分依列序、無分者也占名次）。
    """
    schema = {
        "sub_industry": pl.Utf8, "trend_score": pl.Float64, "trend_rank": pl.Int64,
        "trend_n": pl.Int64, "trend_bucket": pl.Int64,
    }
    if rotation.is_empty() or not {"sub_industry", "trend_score"}.issubset(rotation.columns):
        return pl.DataFrame(schema=schema)
    scored = (
        rotation.select(
            pl.col("sub_industry").cast(pl.Utf8),
            pl.col("trend_score").cast(pl.Float64, strict=False),
        )
        .drop_nulls()
        .unique(subset=["sub_industry"], keep="first", maintain_order=True)
    )
    n = scored.height
    if n == 0:
        return pl.DataFrame(schema=schema)
    k = max(1, int(trend_buckets))
    return (
        scored.with_columns(
            pl.col("trend_score").rank(method="min", descending=True).cast(pl.Int64)
            .alias("trend_rank"),
            pl.lit(n, dtype=pl.Int64).alias("trend_n"),
        )
        # ceil(rank·k/n) 用整數算，避開浮點邊界
        .with_columns(((pl.col("trend_rank") * k + n - 1) // n).alias("trend_bucket"))
        .select(list(schema))
    )


def attach_sector_trend(
    pool: pl.DataFrame,
    members: pl.DataFrame,
    rotation: pl.DataFrame,
    trend_buckets: int,
) -> pl.DataFrame:
    """每檔掛上主次產業（members 中該股的第一筆）與其趨勢分／名次／桶。

    members＝(sub_industry, stock_id) long table，列序即優先序（多標籤取第一個；
    runner 先以 order_members_by_concepts 對齊 concepts.yaml 手標順序）。
    無次產業標籤、或其次產業不在輪動表 → 趨勢欄全 null（apply_gates 標 no_trend_score）。
    """
    primary = (
        members.select(
            pl.col("stock_id").cast(pl.Utf8),
            pl.col("sub_industry").cast(pl.Utf8),
        )
        .drop_nulls()
        .unique(subset=["stock_id"], keep="first", maintain_order=True)
    )
    trend = sector_trend_table(rotation, trend_buckets)
    return pool.join(primary, on="stock_id", how="left").join(
        trend, on="sub_industry", how="left"
    )


# ─── gate ─────────────────────────────────────────────────────────────────────


def _flag_bases() -> pl.Expr:
    """flags（「;」分隔）→ 旗標基名清單（去掉「(…)」明細，如 價格不連續(2026-09-24 +19.2%)）。"""
    return (
        pl.col("flags")
        .fill_null("")
        .str.split(";")
        .list.eval(pl.element().str.replace(r"[(（].*$", "").str.strip_chars())
    )


def apply_gates(df: pl.DataFrame, cfg: ShortlistConfig) -> pl.DataFrame:
    """逐列標 gate_reason（null＝過 gate、可排名）；依下列固定順序，第一個命中者勝：

    1. held：source=holding 且 include_holdings=false（持股不排名、不占名額）
    2. etf：asset_type 為 ETF（不分大小寫）
    3. price_discontinuity：price_discontinuity 欄為真（bool 或 "true" 字串），
       或 flags 含「價格不連續(…)」標記
    4. flag:<旗標>：flags 含 exclude_flags 任一（依設定順序取第一個命中）
    5. flag:低流動：amount_million < propicks_flags.low_liquidity_amount
       （flags 未標時的直判；僅在 exclude_flags 含「低流動」時啟用）
    6. ext_unknown（ma60_dist_pct 缺）→ ext_below（< ext_min_pct）
       → ext_above（> picks.core_ext_ma60_max_pct）
    7. no_trend_score：無次產業標籤，或其次產業在輪動表無 trend_score
    8. bucket：trend_bucket > max_bucket_for_top
    """
    ext = pl.col("ma60_dist_pct")
    flag_bases = pl.col("_flag_bases")
    rules: list[tuple[pl.Expr, str]] = [
        (
            (pl.col("source") == SOURCE_HOLDING) & pl.lit(not cfg.include_holdings),
            GATE_HELD,
        ),
        (pl.col("asset_type").str.strip_chars().str.to_lowercase() == "etf", "etf"),
        (
            pl.col("price_discontinuity") | flag_bases.list.contains(PRICE_DISC_FLAG),
            "price_discontinuity",
        ),
    ]
    rules += [(flag_bases.list.contains(f), f"flag:{f}") for f in cfg.exclude_flags]
    if LOW_LIQUIDITY_FLAG in cfg.exclude_flags:
        rules.append(
            (pl.col("amount_million") < cfg.low_liquidity_amount, f"flag:{LOW_LIQUIDITY_FLAG}")
        )
    rules += [
        (ext.is_null(), "ext_unknown"),
        (ext < cfg.ext_min_pct, "ext_below"),
        (ext > cfg.ext_max_pct, "ext_above"),
        (pl.col("trend_score").is_null(), "no_trend_score"),
        (pl.col("trend_bucket") > cfg.max_bucket_for_top, "bucket"),
    ]
    reason = pl.coalesce(
        [pl.when(cond.fill_null(False)).then(pl.lit(label)) for cond, label in rules]
    )
    return (
        df.with_columns(_flag_bases().alias("_flag_bases"))
        .with_columns(reason.cast(pl.Utf8).alias("gate_reason"))
        .drop("_flag_bases")
    )


# ─── 排序與上限 ───────────────────────────────────────────────────────────────


def _band_dist(cfg: ShortlistConfig) -> pl.Expr:
    """與偏好帶 ext_band_pct 的距離（帶內＝0；ma60_dist 缺 → null）。"""
    lo, hi = cfg.ext_band_pct
    ext = pl.col("ma60_dist_pct")
    return (
        pl.when(ext.is_null())
        .then(None)
        .when(ext < lo)
        .then(lo - ext)
        .when(ext > hi)
        .then(ext - hi)
        .otherwise(0.0)
        .cast(pl.Float64)
    )


# 排序鍵（只在過 gate 者之間；amount_million 明示非訊號、僅決勝）
_RANK_KEYS = ["trend_bucket", "_band_dist", "trend_score", "amount_million", "stock_id"]
_RANK_DESC = [False, False, True, True, False]


def cluster_membership(
    df: pl.DataFrame, clusters_cfg: Sequence[Mapping[str, Any]]
) -> dict[str, list[str]]:
    """stock_id → 所屬因子簇名清單；重用 portfolio.compute_factor_cluster_exposure 的歸簇口徑
    （industry＋theme 多標籤任一命中 labels 即歸屬）。"""
    if df.is_empty() or not clusters_cfg:
        return {}
    exposure = compute_factor_cluster_exposure(df, [dict(c) for c in clusters_cfg])
    out: dict[str, list[str]] = {}
    for spec in exposure:
        for sid in cast("list[str]", spec["stock_ids"]):
            out.setdefault(sid, []).append(str(spec["name"]))
    return out


def rank_shortlist(df: pl.DataFrame, cfg: ShortlistConfig) -> pl.DataFrame:
    """過 gate 者排序、由上往下套上限，填 tier／rank，gate_reason 補 cap_*／beyond_n。

    排序鍵（全 deterministic）：trend_bucket 升冪 → 與 ext_band_pct 的距離升冪（帶內＝0）
    → trend_score 降冪 → amount_million 降冪（非訊號、僅決勝）→ stock_id 升冪。
    由上往下逐檔：同次產業已收滿 max_per_sub_industry → cap_sub_industry；任一所屬因子簇
    已收滿 max_per_cluster → cap_cluster；已收滿 top_n+alt_n → beyond_n；其餘依序給
    rank 1..（≤top_n 為 top、其後為 alt）。上限值 ≤0 ＝不設限。合格數不足時就少列。
    """
    df = df.with_columns(_band_dist(cfg).alias("_band_dist"))
    eligible = df.filter(pl.col("gate_reason").is_null()).sort(
        _RANK_KEYS, descending=_RANK_DESC, nulls_last=True
    )
    clusters = cluster_membership(
        eligible.select([c for c in ("stock_id", "industry", "theme") if c in eligible.columns]),
        cfg.factor_clusters,
    )
    capacity = max(0, cfg.top_n) + max(0, cfg.alt_n)
    sub_count: Counter[str] = Counter()
    cluster_count: Counter[str] = Counter()
    accepted = 0
    decided: list[dict[str, Any]] = []
    for row in eligible.select("stock_id", "sub_industry").iter_rows(named=True):
        sid, sub = row["stock_id"], row["sub_industry"]
        own_clusters = clusters.get(sid, [])
        cap: str | None = None
        if cfg.max_per_sub_industry > 0 and sub_count[sub] >= cfg.max_per_sub_industry:
            cap = CAP_SUB_INDUSTRY
        elif cfg.max_per_cluster > 0 and any(
            cluster_count[c] >= cfg.max_per_cluster for c in own_clusters
        ):
            cap = CAP_CLUSTER
        elif accepted >= capacity:
            cap = BEYOND_N
        if cap is not None:
            decided.append({"stock_id": sid, "_tier": TIER_CAPPED, "rank": None, "_cap": cap})
            continue
        accepted += 1
        sub_count[sub] += 1
        cluster_count.update(own_clusters)
        tier = TIER_TOP if accepted <= cfg.top_n else TIER_ALT
        decided.append({"stock_id": sid, "_tier": tier, "rank": accepted, "_cap": None})

    decided_df = pl.DataFrame(
        decided,
        schema={"stock_id": pl.Utf8, "_tier": pl.Utf8, "rank": pl.Int64, "_cap": pl.Utf8},
    )
    return (
        df.join(decided_df, on="stock_id", how="left")
        .with_columns(
            pl.when(pl.col("gate_reason") == GATE_HELD)
            .then(pl.lit(TIER_HELD))
            .when(pl.col("gate_reason").is_not_null())
            .then(pl.lit(TIER_GATED))
            .otherwise(pl.col("_tier"))
            .alias("tier"),
            pl.coalesce(pl.col("gate_reason"), pl.col("_cap")).alias("gate_reason"),
        )
        .drop(["_tier", "_cap"])
    )


# ─── 承接區／停損 ─────────────────────────────────────────────────────────────


def _entry_fields(
    close: float | None, ma20: float | None, ma60: float | None
) -> tuple[float | None, float | None, str]:
    """承接區：low＝min(MA20, MA60)、high＝min(max(MA20, MA60), 收盤)，附標籤文字。"""
    if close is None or ma20 is None or ma60 is None:
        return None, None, "承接區未取得（收盤／MA20／MA60 缺）"
    low_label, low, high_label, high = (
        ("MA60", ma60, "MA20", ma20) if ma60 <= ma20 else ("MA20", ma20, "MA60", ma60)
    )
    if close < high:
        high_label, high = "收盤", close
    if high < low:  # 收盤已低於兩條均線（僅持股/gated 列可能）→ 承接區不成立
        return None, None, f"收盤 {close:.2f} 低於 MA20/MA60，承接區不適用"
    low, high = round(low, 2), round(high, 2)
    return low, high, f"{low:.2f}–{high:.2f}（{low_label}–{high_label}）"


def _stop_fields(
    close: float | None,
    ma20: float | None,
    ma60: float | None,
    ma60_dist_pct: float | None,
    low_60d: float | None,
    tangle_pct: float,
) -> tuple[float | None, str | None, str]:
    """停損：預設 MA60；均線糾結（|MA20−MA60|/MA60 ≤ tangle_pct）或現價貼 MA60
    （0 ≤ 距季線 ≤ tangle_pct）→ low_60d（均線糾結）；現價已跌破 MA60 → low_60d（已跌破 MA60）。
    stop_text 固定格式供 parse_stop_price 抽回。"""
    if ma60 is None or ma60 <= 0:
        return None, None, "停損價未取得（MA60 缺）"
    dist = ma60_dist_pct
    if dist is None and close is not None:
        dist = (close / ma60 - 1.0) * 100.0
    tangled = (ma20 is not None and abs(ma20 - ma60) / ma60 * 100.0 <= tangle_pct) or (
        dist is not None and dist <= tangle_pct
    )
    below = dist is not None and dist < 0
    if not tangled:
        price, basis = ma60, STOP_BASIS_MA60
    elif low_60d is not None and low_60d > 0:
        price, basis = low_60d, STOP_BASIS_BELOW if below else STOP_BASIS_LOW60
    else:
        why = "已跌破 MA60" if below else "均線糾結"
        return None, None, f"停損價未取得（{why}、low_60d 缺）"
    price = round(price, 2)
    return price, basis, f"收盤跌破 {price:.2f}（{basis}）、隔日未收復出場"


def entry_stop(df: pl.DataFrame, tangle_pct: float) -> pl.DataFrame:
    """補 entry_low／entry_high／entry_text 與 stop_price／stop_basis／stop_text（每列都算）。

    承接區：entry_low＝min(MA20, MA60)，entry_high＝min(max(MA20, MA60), close)，
    entry_text 如「88.50–91.20（MA60–MA20）」。
    停損：預設 MA60（stop_basis=MA60）；均線糾結或現價貼 MA60（tangle_pct）→ low_60d
    （stop_basis=low_60d（均線糾結））；現價已跌破 MA60 → low_60d
    （stop_basis=low_60d（已跌破 MA60））。
    stop_text＝「收盤跌破 {價:.2f}（{basis}）、隔日未收復出場」。
    """
    entry_low: list[float | None] = []
    entry_high: list[float | None] = []
    entry_text: list[str] = []
    stop_price: list[float | None] = []
    stop_basis: list[str | None] = []
    stop_text: list[str] = []
    cols = ("close", "ma20_price", "ma60_price", "ma60_dist_pct", "low_60d")
    for r in df.select(cols).iter_rows(named=True):
        lo, hi, etxt = _entry_fields(r["close"], r["ma20_price"], r["ma60_price"])
        sp, sb, stxt = _stop_fields(
            r["close"], r["ma20_price"], r["ma60_price"], r["ma60_dist_pct"], r["low_60d"],
            tangle_pct,
        )
        entry_low.append(lo)
        entry_high.append(hi)
        entry_text.append(etxt)
        stop_price.append(sp)
        stop_basis.append(sb)
        stop_text.append(stxt)
    return df.with_columns(
        pl.Series("entry_low", entry_low, dtype=pl.Float64),
        pl.Series("entry_high", entry_high, dtype=pl.Float64),
        pl.Series("entry_text", entry_text, dtype=pl.Utf8),
        pl.Series("stop_price", stop_price, dtype=pl.Float64),
        pl.Series("stop_basis", stop_basis, dtype=pl.Utf8),
        pl.Series("stop_text", stop_text, dtype=pl.Utf8),
    )


# ─── 說明欄 ───────────────────────────────────────────────────────────────────


def _evidence(r: Mapping[str, Any], cfg: ShortlistConfig) -> str:
    """只放已驗證事實：族群趨勢分（名次/桶）＋距季線位階。"""
    if r["trend_score"] is not None:
        trend = (
            f"族群趨勢分 {r['trend_score']:.1f}"
            f"（#{r['trend_rank']}/{r['trend_n']}・第{r['trend_bucket']}桶）"
        )
    elif r["sub_industry"] is None:
        trend = "族群趨勢分 未取得（無次產業標籤）"
    else:
        trend = f"族群趨勢分 未取得（{r['sub_industry']} 不在輪動表）"
    ext = r["ma60_dist_pct"]
    if ext is None:
        return f"{trend}；距季線 未取得"
    lo, hi = cfg.ext_band_pct
    band = "（偏好帶內）" if lo <= ext <= hi else ""
    return f"{trend}；距季線 {ext:+.1f}%{band}"


def _bear_hints(r: Mapping[str, Any], cfg: ShortlistConfig) -> str:
    """空方提示（固定首項「族群層訊號、個股層未驗證」），以「；」串接。"""
    hints = [BEAR_HINT_HEAD]
    pe = r["pe_ratio"]
    if pe is not None and pe > cfg.bear_pe_high:
        hints.append(f"高PE {pe:.1f}（>{cfg.bear_pe_high:g}）")
    if r["fundamental_health"] in _BEAR_FUND_HEALTH:
        hints.append(f"月營收{r['fundamental_health']}（fundamental_health）")
    ext = r["ma60_dist_pct"]
    lo, hi = cfg.ext_band_pct
    if ext is not None and not lo <= ext <= hi:
        hints.append(f"距季線 {ext:+.1f}% 在偏好帶 {lo:g}–{hi:g}% 外")
    stop, close = r["stop_price"], r["close"]
    if stop is not None and close:
        dist = (close - stop) / close * 100.0
        if dist > cfg.bear_stop_dist_pct:
            hints.append(f"停損距現價 {dist:.1f}%（>{cfg.bear_stop_dist_pct:g}%）")
    return "；".join(hints)


def _unvalidated_notes(r: Mapping[str, Any]) -> str:
    """未驗證訊號（僅揭露、不影響排序）：策略命中／估值缺口／deep_value_growth／contrarian_ready。"""
    notes: list[str] = []
    strategy = (r["strategy"] or "").strip()
    if strategy not in _EMPTY_STRATEGY:
        notes.append(f"策略命中 {strategy}")
    if r["val_gap_pct_composite"] is not None:
        notes.append(f"估值缺口 {r['val_gap_pct_composite']:+.1f}%（綜合）")
    if r["deep_value_growth"]:
        notes.append("deep_value_growth")
    if r["contrarian_ready"]:
        notes.append("contrarian_ready（M-BR1 左側）")
    return "；".join(notes)


def evidence_notes(df: pl.DataFrame, cfg: ShortlistConfig) -> pl.DataFrame:
    """補 evidence（已驗證事實）／bear_hints（空方提示）／unvalidated_notes（未驗證揭露）。

    法人流（外資/投信/自營/三大法人）不論數字或字樣一律不寫進這三欄（docs/19、20、22 §4 否證）。
    需先跑過 attach_sector_trend 與 entry_stop（用到趨勢欄與 stop_price）。
    """
    evidence: list[str] = []
    bear: list[str] = []
    unvalidated: list[str] = []
    cols = (
        "sub_industry", "trend_score", "trend_rank", "trend_n", "trend_bucket",
        "ma60_dist_pct", "pe_ratio", "fundamental_health", "stop_price", "close",
        "strategy", "val_gap_pct_composite", "deep_value_growth", "contrarian_ready",
    )
    for r in df.select(cols).iter_rows(named=True):
        evidence.append(_evidence(r, cfg))
        bear.append(_bear_hints(r, cfg))
        unvalidated.append(_unvalidated_notes(r))
    return df.with_columns(
        pl.Series("evidence", evidence, dtype=pl.Utf8),
        pl.Series("bear_hints", bear, dtype=pl.Utf8),
        pl.Series("unvalidated_notes", unvalidated, dtype=pl.Utf8),
    )


# ─── 組裝／讀回 ───────────────────────────────────────────────────────────────


def _rotation_date(rotation: pl.DataFrame) -> date | None:
    """輪動表的資料日（date 欄最大值；缺欄/無法解析 → None）。"""
    if "date" not in rotation.columns or rotation.is_empty():
        return None
    parsed = rotation.select(
        pl.col("date").cast(pl.Utf8).str.to_date(strict=False).max()
    ).item()
    return parsed if isinstance(parsed, date) else None


def build_shortlist(
    pool: pl.DataFrame,
    members: pl.DataFrame,
    rotation: pl.DataFrame,
    cfg: ShortlistConfig,
    week: str,
    data_date: date | None,
) -> pl.DataFrame:
    """候選池 → shortlist 全表（每個被考慮的股票一列，欄位＝SHORTLIST_COLUMNS）。

    列序：top（rank 1..）→ alt → capped → gated → held，同 tier 內依排序鍵、stock_id。
    """
    df = attach_sector_trend(pool, members, rotation, cfg.trend_buckets)
    df = apply_gates(df, cfg)
    df = rank_shortlist(df, cfg)
    df = entry_stop(df, cfg.tangle_pct)
    df = evidence_notes(df, cfg)
    return (
        df.with_columns(
            pl.lit(week, dtype=pl.Utf8).alias("week"),
            pl.lit(data_date, dtype=pl.Date).alias("data_date"),
            pl.lit(_rotation_date(rotation), dtype=pl.Date).alias("rotation_date"),
            pl.col("tier").replace_strict(_TIER_ORDER, default=len(_TIER_ORDER)).alias("_tier_ord"),
        )
        .sort(
            ["_tier_ord", "rank", *_RANK_KEYS],
            descending=[False, False, *_RANK_DESC],
            nulls_last=True,
        )
        .select(SHORTLIST_COLUMNS)
    )


def load_shortlist(week_dir: Path) -> pl.DataFrame | None:
    """讀 reports/<週>/shortlist.csv（無檔 → None；讀壞 → warning＋None），供 picks sync 核對。"""
    path = week_dir / SHORTLIST_FILENAME
    if not path.is_file():
        return None
    try:
        df = pl.read_csv(
            path,
            infer_schema_length=None,
            schema_overrides={"stock_id": pl.Utf8, "rank": pl.Int64, "tier": pl.Utf8},
        )
    except (OSError, pl.exceptions.PolarsError) as e:
        logger.warning("讀取 {} 失敗：{}", path, e)
        return None
    missing = {"stock_id", "tier", "rank"} - set(df.columns)
    if missing:
        logger.warning("{} 缺欄 {}，視同無 shortlist", path, sorted(missing))
        return None
    return df
