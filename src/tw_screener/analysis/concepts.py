"""次產業對照表載入：config/concepts.yaml → stock_id / sub_industry。

並存於 TWSE 大分類、只供報表顯示（不影響族群強度排名）。見 docs/05-group-analysis.md。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml
from loguru import logger

# 需要次產業細分的電子大分類（非電子的 TWSE 分類已夠細，不強制標）
ELECTRONICS_INDUSTRIES: frozenset[str] = frozenset(
    {
        "半導體業",
        "電子零組件業",
        "光電業",
        "電腦及周邊設備業",
        "通信網路業",
        "電子通路業",
        "其他電子業",
        "資訊服務業",
    }
)

_SCHEMA: dict[str, type[pl.DataType]] = {"stock_id": pl.Utf8, "sub_industry": pl.Utf8}


def load_concepts(path: Path = Path("config/concepts.yaml")) -> pl.DataFrame:
    """讀 concepts.yaml → DataFrame(stock_id, sub_industry)。檔案不存在時回空表。"""
    if not path.exists():
        logger.warning("concepts.yaml 不存在（{}），次產業欄將留空", path)
        return pl.DataFrame(schema=_SCHEMA)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    mapping = data.get("concepts", {}) or {}
    rows = [{"stock_id": str(k), "sub_industry": str(v)} for k, v in mapping.items()]
    if not rows:
        return pl.DataFrame(schema=_SCHEMA)
    return pl.DataFrame(rows, schema=_SCHEMA)


def unmapped_electronics(members: pl.DataFrame) -> list[str]:
    """回傳「屬電子大分類、但 sub_industry 為空」的 stock_id（供增量維護提醒）。"""
    if members.is_empty() or "industry_name" not in members.columns:
        return []
    if "sub_industry" not in members.columns:
        cond = pl.col("industry_name").is_in(list(ELECTRONICS_INDUSTRIES))
    else:
        cond = pl.col("industry_name").is_in(list(ELECTRONICS_INDUSTRIES)) & (
            pl.col("sub_industry").is_null()
        )
    return members.filter(cond)["stock_id"].to_list()
