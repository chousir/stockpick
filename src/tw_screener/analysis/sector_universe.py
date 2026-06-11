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


def load_industry_mapping(cache_dir: Path) -> pl.DataFrame:
    """純讀最新月份 industry_YYYYMM + otc_industry_YYYYMM 快取 → 全市場 28 類對照。

    回傳 (stock_id, stock_name, industry_code, industry_name)，上市優先去重。
    無快取時回空表（誠實，不打網補抓——抓取由 make fetch-twse 流程負責）。
    """
    frames: list[pl.DataFrame] = []
    for pattern in ("industry_[0-9]*.parquet", "otc_industry_[0-9]*.parquet"):
        files = sorted(cache_dir.glob(pattern))
        if not files:
            logger.warning("load_industry_mapping：找不到 {}（{}）", pattern, cache_dir)
            continue
        try:
            frames.append(pl.read_parquet(files[-1]).select(list(_INDUSTRY_SCHEMA)))
        except Exception as e:
            logger.warning("讀取 {} 失敗：{}", files[-1], e)
    if not frames:
        return pl.DataFrame(schema=_INDUSTRY_SCHEMA)
    # 上市排前（pattern 順序），同 stock_id 重複時 keep first
    return (
        pl.concat(frames)
        .unique(subset=["stock_id"], keep="first")
        .sort("stock_id")
    )
