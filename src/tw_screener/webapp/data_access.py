"""讀 reports/ 的資料存取層。

- read-on-request；以 (週次, 檔名, mtime) 為 key 做記憶體快取，檔案變動才重讀。
- 週次資料夾動態偵測（不寫死週數）；`/weeks` 目錄掃描不快取，新跑的一週重整即見。
- 數值欄的空字串/`-`/`NaN` 統一轉 None；字串欄保留原值（docs/17 §3.2）。
- 缺檔回 None（路由層轉 404），缺欄回 None，絕不丟 500。
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from loguru import logger

# 週次資料夾命名：2026-W25
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
# 整格視為「缺值」的字串哨兵（只比對去空白後的整格，不誤殺含 - 的正常字串）
_NULL_SENTINELS = {"", "-", "nan", "NaN", "null", "None", "—"}

_SETTINGS_PATH = Path("config/settings.yaml")


@lru_cache(maxsize=1)
def _reports_dir() -> Path:
    """從 config/settings.yaml 取 reports_dir，預設 reports（相對 cwd）。"""
    try:
        with _SETTINGS_PATH.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return Path(cfg.get("paths", {}).get("reports_dir", "reports"))
    except OSError:
        return Path("reports")


def list_weeks() -> list[str]:
    """掃 reports/ 下符合週次命名的資料夾，升冪排序。目錄掃描不快取。"""
    root = _reports_dir()
    if not root.is_dir():
        return []
    weeks = [p.name for p in root.iterdir() if p.is_dir() and WEEK_RE.match(p.name)]
    return sorted(weeks)


def latest_week() -> str | None:
    weeks = list_weeks()
    return weeks[-1] if weeks else None


def week_exists(week: str) -> bool:
    return bool(WEEK_RE.match(week)) and (_reports_dir() / week).is_dir()


def file_path(week: str, filename: str) -> Path:
    return _reports_dir() / week / filename


def _clean_value(v: Any) -> Any:
    """單一格清洗：NaN→None；整格為缺值哨兵字串→None；其餘原樣。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str) and v.strip() in _NULL_SENTINELS:
        return None
    return v


# (week, filename, mtime_ns) → rows；mtime 變了才重讀
_table_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}


def read_table(week: str, filename: str) -> list[dict[str, Any]] | None:
    """讀單一 CSV → list[dict]，已清洗缺值。檔案不存在回 None。"""
    if not week_exists(week):
        return None
    path = file_path(week, filename)
    if not path.is_file():
        return None

    key = (week, filename, path.stat().st_mtime_ns)
    cached = _table_cache.get(key)
    if cached is not None:
        return cached

    try:
        # 識別欄強制讀成字串，避免純數字股號被推斷成 int（會丟失 ETF 如 0050 的前導零）
        header = pl.read_csv(path, n_rows=0).columns
        overrides = {c: pl.Utf8 for c in ("stock_id",) if c in header}
        df = pl.read_csv(path, infer_schema_length=2000, schema_overrides=overrides)
    except Exception as exc:  # noqa: BLE001 — 壞檔不該炸掉整個 dashboard
        logger.warning(f"讀取失敗 {path}: {exc}")
        return None

    rows = [{k: _clean_value(v) for k, v in row.items()} for row in df.to_dicts()]
    # 同 (week,filename) 舊 mtime 的快取清掉，避免無限增長
    for stale in [k for k in _table_cache if k[0] == week and k[1] == filename]:
        del _table_cache[stale]
    _table_cache[key] = rows
    return rows


# 個股彙整來源（優先序 holdings > candidates > watchlist，docs/17 §4）
_STOCK_SOURCES = (
    ("holdings", "holdings_enriched.csv"),
    ("candidates", "candidates_enriched.csv"),
    ("watchlist", "watchlist_enriched.csv"),
)
# 四策略原始篩選命中檔（screen_result_{id}_*.csv，後綴可變故用 glob）
_SCREEN_IDS = ("d", "e", "f", "g")


def find_stock(week: str, stock_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    """跨 candidates/holdings/watchlist 找單一股並合併（docs/17 §4）。

    回 (merged_row, sources)；三表皆查無回 (None, [])。
    合併優先序 holdings > candidates > watchlist：低→高依序覆寫，**只用非 None 值覆寫**
    （高優先表的缺欄不會抹掉低優先表已有的值），欄位取聯集。
    """
    found: dict[str, dict[str, Any]] = {}
    for name, fname in _STOCK_SOURCES:
        rows = read_table(week, fname)
        if rows is None:
            continue
        match = next((r for r in rows if str(r.get("stock_id")) == stock_id), None)
        if match is not None:
            found[name] = match
    if not found:
        return None, []

    merged: dict[str, Any] = {}
    for name in ("watchlist", "candidates", "holdings"):  # 低→高優先
        row = found.get(name)
        if row:
            merged.update({k: v for k, v in row.items() if v is not None})
    sources = [name for name, _ in _STOCK_SOURCES if name in found]  # holdings→candidates→watchlist
    return merged, sources


def screens_hit(week: str, stock_id: str) -> list[str]:
    """掃 screen_result_{d,e,f,g}_*.csv，回此股命中的策略代號（去重、固定 d/e/f/g 序）。"""
    if not week_exists(week):
        return []
    base = file_path(week, "")
    hit: list[str] = []
    for sid in _SCREEN_IDS:
        for path in sorted(base.glob(f"screen_result_{sid}_*.csv")):
            rows = read_table(week, path.name)
            if rows and any(str(r.get("stock_id")) == stock_id for r in rows):
                hit.append(sid)
                break
    return hit


def read_text(week: str, filename: str) -> str | None:
    """讀單一 markdown/文字檔。檔案不存在回 None。"""
    if not week_exists(week):
        return None
    path = file_path(week, filename)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"讀取失敗 {path}: {exc}")
        return None
