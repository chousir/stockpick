"""parser.py — 解析 Goodinfo 自訂篩選 AJAX 回傳的 HTML fragment。"""

import re
from typing import Any

import polars as pl
from bs4 import BeautifulSoup, Tag
from loguru import logger

# Verified against live Goodinfo 交易狀況 AJAX response (2026-05-16)
_COL_MAP: dict[str, str] = {
    "代號": "stock_id",
    "名稱": "name",
    "市場": "market",
    "成交": "close",
    "漲跌幅": "change_pct",
    "成交張數": "volume_lots",
    "成交額(百萬)": "amount_million",
    "PER": "pe_ratio",
    "PBR": "pb_ratio",
}

_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "market": pl.Utf8,
    "close": pl.Float64,
    "change_pct": pl.Float64,
    "volume_lots": pl.Int64,
    "amount_million": pl.Float64,
    "pe_ratio": pl.Float64,
    "pb_ratio": pl.Float64,
}

_EMPTY_DF = pl.DataFrame(schema=_SCHEMA)
_TOO_MANY_RE = re.compile(r"共計(\d+)筆")


class GoodinfoParseError(Exception):
    """HTML 結構解析失敗（網站改版或非預期回應）。"""


class GoodinfoTooManyResultsError(Exception):
    """篩選結果超過 Goodinfo 匿名上限（300 筆）。"""

    def __init__(self, count: int) -> None:
        super().__init__(f"篩選結果 {count} 筆超過匿名上限 300 筆，請縮小篩選範圍")
        self.count = count


def parse_screener_result(html: str) -> pl.DataFrame:
    """解析 Goodinfo STEP=DATA AJAX 回傳的 HTML fragment，回傳 Polars DataFrame。

    Goodinfo 回傳 HTML fragment（非完整 HTML 頁面）：
    - 結果 > 300 筆 → raise GoodinfoTooManyResultsError
    - 找不到結果表 → raise GoodinfoParseError
    - 0 筆結果 → 回傳有正確 schema 的空 DataFrame

    驗證日期：2026-05-16，sheet=交易狀況
    """
    if "篩選條件範圍過大" in html:
        m = _TOO_MANY_RE.search(html)
        count = int(m.group(1)) if m else -1
        raise GoodinfoTooManyResultsError(count)

    soup = BeautifulSoup(html, "lxml")
    tbl: Tag | None = soup.find("table", id="tblStockList")  # type: ignore[assignment]
    if tbl is None:
        raise GoodinfoParseError("找不到 tblStockList，可能網站改版或回應格式異常")

    all_rows = tbl.find_all("tr")
    if not all_rows:
        return _EMPTY_DF

    # Read column positions from first header row
    header_cells = all_rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    col_idx: dict[str, int] = {h: i for i, h in enumerate(headers)}

    records: list[dict[str, Any]] = []
    for tr in all_rows[1:]:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        # Skip repeated sub-header rows (Goodinfo repeats headers every ~20 rows)
        if cells[0].get_text(strip=True) == "代號":
            continue

        stock_id = _extract_stock_id(cells, col_idx)
        if not stock_id:
            continue

        def get(col_name: str) -> str:
            idx = col_idx.get(col_name)
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True)

        records.append(
            {
                "stock_id": stock_id,
                "name": get("名稱") or None,
                "market": get("市場") or None,
                "close": _to_float(get("成交")),
                "change_pct": _to_float(get("漲跌幅")),
                "volume_lots": _to_int(get("成交張數")),
                "amount_million": _to_float(get("成交額(百萬)")),
                "pe_ratio": _to_float(get("PER")),
                "pb_ratio": _to_float(get("PBR")),
            }
        )

    if not records:
        return _EMPTY_DF

    logger.info("Parsed {} stocks from Goodinfo tblStockList", len(records))
    return pl.DataFrame(records, schema=_SCHEMA)


def _extract_stock_id(cells: list[Tag], col_idx: dict[str, int]) -> str:
    idx = col_idx.get("代號", 0)
    if idx >= len(cells):
        return ""
    cell = cells[idx]
    a = cell.find("a")
    if a:
        m = re.search(r"STOCK_ID=([A-Za-z0-9]+)", a.get("href", ""))
        if m:
            return m.group(1)
    return cell.get_text(strip=True)


def _to_float(s: str) -> float | None:
    s = s.replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return None


def _to_int(s: str) -> int | None:
    s = s.replace(",", "").strip()
    try:
        return int(s)
    except (ValueError, AttributeError):
        return None
