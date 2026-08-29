"""次產業宇宙資料層（rotation 引擎 R0，見 docs/12-sector-rotation.md §2.1-2.2）。

提供兩個純讀 API（不打網）：
- list_subindustries()：concepts.yaml 手標次產業 → 全成員 long table（已濾 ETF/權證）。
  這是輪動引擎的「主粒度」——與 grouping.py 只看當週候選股不同，這裡回傳
  **全次產業成員**，供無偏的籃子報酬 / 資金流向計算（研究軌 + 生產軌共用）。
- load_industry_mapping()：最新 industry_YYYYMM + otc_industry_YYYYMM 快取 →
  全市場 TWSE 28 類對照（粗層交叉驗證用）。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from loguru import logger

from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND, load_themes
from tw_screener.analysis.grouping import is_etf_or_warrant
from tw_screener.data.cache import find_latest

_MEMBERSHIP_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8,
    "stock_id": pl.Utf8,
}
_INDUSTRY_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "stock_name": pl.Utf8,
    "industry_code": pl.Utf8,
    "industry_name": pl.Utf8,
}


def list_subindustries(
    concepts_path: Path = Path("config/concepts.yaml"),
) -> pl.DataFrame:
    """讀 concepts.yaml 手標次產業 → long table (sub_industry, stock_id)。

    只取 kind=次產業 的標籤（概念股主題排除，留給 R6）；一檔可屬多個次產業。
    ETF / 權證以 is_etf_or_warrant 過濾。檔案不存在時回空表。
    """
    themes = load_themes(concepts_path)
    if themes.is_empty():
        return pl.DataFrame(schema=_MEMBERSHIP_SCHEMA)
    subs = themes.filter(pl.col("kind") == SUB_INDUSTRY_KIND)
    etf_ids = [
        sid for sid in subs["stock_id"].unique().to_list() if is_etf_or_warrant(sid)
    ]
    if etf_ids:
        logger.debug("次產業成員濾掉 ETF/權證 {} 檔", len(etf_ids))
        subs = subs.filter(~pl.col("stock_id").is_in(etf_ids))
    return (
        subs.select(pl.col("theme").alias("sub_industry"), pl.col("stock_id"))
        .unique(maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )


def audit_priceless_members(
    members: pl.DataFrame,
    priced_ids: set[str] | list[str],
) -> pl.DataFrame:
    """回傳 members 中 stock_id 不在 priced_ids（近日有日線收盤）的列，按 sub_industry 排序。

    無價成員多為興櫃／下市／誤標——它們會讓次產業籃子悄悄縮水（價格軸與資金流被低估）。
    純報表用，**不改 concepts.yaml**。members 需含 sub_industry / stock_id；priced_ids 為
    近日日線快取出現過的全部 stock_id（如 load_market_history 的 stock_id 去重）。
    members 空 → 回原表。
    """
    if members.is_empty() or "stock_id" not in members.columns:
        return members
    priced = set(str(s) for s in priced_ids)
    return members.filter(~pl.col("stock_id").is_in(list(priced))).sort(
        ["sub_industry", "stock_id"]
    )


PEER_FALLBACK_PREFIX = "產業別:"


def build_peer_membership(
    hand_tagged: pl.DataFrame, industry: pl.DataFrame
) -> pl.DataFrame:
    """估值用 peer 分組：手標次產業優先，未標股以 TWSE 28 類產業別兜底（docs/13 §C1 覆蓋率）。

    僅供**估值相對位階**用——讓未手標的個股也有同儕中位數可比，把「估值缺」大幅減少；
    輪動型態標籤/籃子仍用純手標 list_subindustries（粒度較細、語意不同），不混用。

    Args:
        hand_tagged: list_subindustries 輸出 (sub_industry, stock_id)
        industry: load_industry_mapping 輸出 (stock_id, ..., industry_name)

    Returns:
        (sub_industry, stock_id)：手標股保留其次產業（可多列）；未手標股各得一列，
        sub_industry＝「{PEER_FALLBACK_PREFIX}{TWSE 產業別}」（前綴以利區分粗/細同儕，
        且避免與手標次產業同名碰撞）。industry_name 缺/空者不兜底（誠實留「估值缺」）。
    """
    frames: list[pl.DataFrame] = []
    tagged_ids: list[str] = []
    if not hand_tagged.is_empty():
        frames.append(hand_tagged.select("sub_industry", "stock_id"))
        tagged_ids = hand_tagged["stock_id"].unique().to_list()
    if not industry.is_empty():
        fb = (
            industry.filter(
                pl.col("industry_name").is_not_null()
                & (pl.col("industry_name") != "")
                & ~pl.col("stock_id").is_in(tagged_ids)
            )
            .select(
                (pl.lit(PEER_FALLBACK_PREFIX) + pl.col("industry_name")).alias("sub_industry"),
                "stock_id",
            )
        )
        frames.append(fb)
    if not frames:
        return pl.DataFrame(schema=_MEMBERSHIP_SCHEMA)
    return (
        pl.concat(frames)
        .unique(["sub_industry", "stock_id"], maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )


def build_broad_industry_membership(industry: pl.DataFrame) -> pl.DataFrame:
    """估值用**粗**同儕分組——TWSE/TPEX 產業別，不論手標與否，全市場每檔一列。

    與 `build_peer_membership` 的差異：後者的粗分類只給「完全未手標」的股票用，
    手標次產業本身太小（如晶圓代工全市場僅4檔，永遠 <`min_peers`）的股票兩頭落空、
    同儕估值永遠算不出來（2026-08-29 使用者實測發現，台積電/聯電/世界/力積電皆是
    此例，docs/31 §20.8）。本函式**不排除已手標股**，讓 `build_valuation()` 能在
    細分類樣本不足時，單獨對這些股票補一次粗分類同儕計算（見該函式 `broad_membership`
    參數）——細算得出來的股票不受影響，只補「細算不出來」的缺口。

    Args:
        industry: load_industry_mapping 輸出 (stock_id, ..., industry_name)

    Returns:
        (sub_industry, stock_id)：sub_industry＝「{PEER_FALLBACK_PREFIX}{TWSE 產業別}」。
        industry_name 缺/空者不兜底（誠實留「估值缺」，同 `build_peer_membership`）。
    """
    if industry.is_empty():
        return pl.DataFrame(schema=_MEMBERSHIP_SCHEMA)
    return (
        industry.filter(
            pl.col("industry_name").is_not_null() & (pl.col("industry_name") != "")
        )
        .select(
            (pl.lit(PEER_FALLBACK_PREFIX) + pl.col("industry_name")).alias("sub_industry"),
            "stock_id",
        )
        .unique(["sub_industry", "stock_id"], maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )


def load_industry_mapping(cache_dir: Path) -> pl.DataFrame:
    """純讀最新月份 industry_YYYYMM + otc_industry_YYYYMM 快取 → 全市場 28 類對照。

    回傳 (stock_id, stock_name, industry_code, industry_name)，上市優先去重。
    無快取時回空表（誠實，不打網補抓——抓取由 make fetch-twse 流程負責）。
    """
    frames: list[pl.DataFrame] = []
    for pattern in ("industry_[0-9]*.parquet", "otc_industry_[0-9]*.parquet"):
        latest = find_latest(cache_dir, pattern, by="name")
        if latest is None:
            logger.warning("load_industry_mapping：找不到 {}（{}）", pattern, cache_dir)
            continue
        try:
            frames.append(pl.read_parquet(latest).select(list(_INDUSTRY_SCHEMA)))
        except Exception as e:  # noqa: BLE001 — 單檔壞不擋整批
            logger.warning("讀取 {} 失敗：{}", latest, e)
    if not frames:
        return pl.DataFrame(schema=_INDUSTRY_SCHEMA)
    # 上市排前（pattern 順序），同 stock_id 重複時 keep first
    return (
        pl.concat(frames)
        .unique(subset=["stock_id"], keep="first")
        .sort("stock_id")
    )
