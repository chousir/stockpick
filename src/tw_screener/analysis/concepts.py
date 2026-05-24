"""主題對照表載入：手標電子次產業（config/concepts.yaml）＋ Yahoo 概念股（config/themes.yaml）。

合併成統一多標籤 long table `(stock_id, theme, kind)`，一檔可屬多主題。
`kind`＝「次產業」（手標電子，完整）或「概念股」（Yahoo 爬，每主題前約 30 檔）。
主題強度排名見 grouping.rank_themes；報表逐股主題欄見 report/group_report。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml
from loguru import logger

# kind 取值
SUB_INDUSTRY_KIND = "次產業"
CONCEPT_KIND = "概念股"

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
_THEME_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "theme": pl.Utf8,
    "kind": pl.Utf8,
}


def load_concepts(path: Path = Path("config/concepts.yaml")) -> pl.DataFrame:
    """讀 concepts.yaml → DataFrame(stock_id, sub_industry)。多標籤以「、」串接。

    薄殼，保留供回溯/測試；主流程多標籤請用 load_themes()。檔案不存在時回空表。
    """
    if not path.exists():
        logger.warning("concepts.yaml 不存在（{}），次產業欄將留空", path)
        return pl.DataFrame(schema=_SCHEMA)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    mapping = data.get("concepts", {}) or {}
    rows = []
    for k, v in mapping.items():
        label = "、".join(str(x) for x in v) if isinstance(v, list) else str(v)
        rows.append({"stock_id": str(k), "sub_industry": label})
    if not rows:
        return pl.DataFrame(schema=_SCHEMA)
    return pl.DataFrame(rows, schema=_SCHEMA)


def load_themes(
    concepts_path: Path = Path("config/concepts.yaml"),
    themes_path: Path = Path("config/themes.yaml"),
) -> pl.DataFrame:
    """讀兩來源 → 統一 long table DataFrame(stock_id, theme, kind)，一檔多列。

    - concepts.yaml（手標電子次產業）：`concepts: {id: 主題 | [主題...]}` → kind=次產業。
    - themes.yaml（Yahoo 概念股）：`themes: {名稱: {kind, url, members:[id...]}}` → kind=概念股。
    任一檔不存在則該來源略過。兩檔都無 → 回空表。
    """
    rows: list[dict[str, str]] = []

    if concepts_path.exists():
        with open(concepts_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for sid, val in (data.get("concepts", {}) or {}).items():
            labels = val if isinstance(val, list) else [val]
            for lab in labels:
                rows.append(
                    {"stock_id": str(sid), "theme": str(lab), "kind": SUB_INDUSTRY_KIND}
                )
    else:
        logger.warning("concepts.yaml 不存在（{}），次產業主題將留空", concepts_path)

    if themes_path.exists():
        with open(themes_path, encoding="utf-8") as fh:
            tdata = yaml.safe_load(fh) or {}
        for name, info in (tdata.get("themes", {}) or {}).items():
            info = info or {}
            kind = str(info.get("kind", CONCEPT_KIND))
            for sid in info.get("members", []) or []:
                rows.append({"stock_id": str(sid), "theme": str(name), "kind": kind})
    else:
        logger.info(
            "themes.yaml 不存在（{}），概念股主題將留空（可跑 make build-themes）", themes_path
        )

    if not rows:
        return pl.DataFrame(schema=_THEME_SCHEMA)
    return pl.DataFrame(rows, schema=_THEME_SCHEMA).unique(maintain_order=True)


def unmapped_electronics(members: pl.DataFrame, themes_long: pl.DataFrame) -> list[str]:
    """回傳「屬電子大分類、但無『次產業』主題標記」的 stock_id（電子股仍手標，供增量維護提醒）。"""
    if members.is_empty() or "industry_name" not in members.columns:
        return []
    elec = members.filter(pl.col("industry_name").is_in(list(ELECTRONICS_INDUSTRIES)))
    if elec.is_empty():
        return []
    if themes_long.is_empty() or "kind" not in themes_long.columns:
        tagged: set[str] = set()
    else:
        tagged = set(
            themes_long.filter(pl.col("kind") == SUB_INDUSTRY_KIND)["stock_id"].to_list()
        )
    return [sid for sid in elec["stock_id"].to_list() if sid not in tagged]
