"""parser.py — 解析 Goodinfo 自訂篩選結果頁。"""

import re
from typing import Any

import polars as pl
from bs4 import BeautifulSoup, Tag


class GoodinfoParseError(Exception):
    """HTML 結構解析失敗（網站改版或非預期回應）。"""


# 中文欄名 → DataFrame 欄位名映射（動態定位，不依賴 column index）
_COL_MAP: dict[str, str] = {
    "股號": "stock_id",
    "股名": "name",
    "產業": "industry",
    "概念股": "concept",
    "收盤": "close",
    "成交張數": "volume",
    "成交金額(億)": "amount",
}

_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "industry": pl.Utf8,
    "concept": pl.Utf8,
    "close": pl.Float64,
    "volume": pl.Int64,
    "amount": pl.Utf8,
}

_EMPTY_DF = pl.DataFrame(schema=_SCHEMA)


def _cell_text(cell: Tag) -> str:
    return cell.get_text(strip=True)


def _parse_float(s: str) -> float | None:
    cleaned = s.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(s: str) -> int | None:
    cleaned = s.replace(",", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_screener_result(html: str) -> pl.DataFrame:
    """解析 Goodinfo 篩選結果頁，回傳 Polars DataFrame。

    欄位用中文表頭定位，不依賴固定欄位 index（網站改版時只需更新 _COL_MAP）。
    找不到結果表 → raise GoodinfoParseError。
    0 筆結果 → 回傳有正確 schema 的空 DataFrame（正常情況）。
    """
    soup = BeautifulSoup(html, "lxml")

    # 優先用 id，回退到 class pattern
    table: Tag | None = soup.find("table", id="tblStockList")  # type: ignore[assignment]
    if table is None:
        table = soup.find("table", class_=re.compile(r"solid_\d+_padding"))  # type: ignore[assignment]
    if table is None:
        raise GoodinfoParseError("找不到結果表，可能網站改版")

    all_rows = table.find_all("tr")
    if not all_rows:
        return _EMPTY_DF

    # 從表頭建立欄位索引
    header_cells = all_rows[0].find_all("td")
    headers = [_cell_text(td) for td in header_cells]
    col_indices: dict[str, int] = {}
    for src, target in _COL_MAP.items():
        if src in headers:
            col_indices[target] = headers.index(src)

    rows: list[dict[str, Any]] = []
    for tr in all_rows[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue

        row: dict[str, Any] = {col: None for col in _SCHEMA}
        for target, idx in col_indices.items():
            if idx >= len(cells):
                continue
            text = _cell_text(cells[idx])
            if target == "close":
                row[target] = _parse_float(text)
            elif target == "volume":
                row[target] = _parse_int(text)
            else:
                row[target] = text or None

        rows.append(row)

    if not rows:
        return _EMPTY_DF

    return pl.DataFrame(rows, schema=_SCHEMA)
