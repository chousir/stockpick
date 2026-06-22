"""週次清單與 meta。"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from tw_screener.webapp import data_access as da
from tw_screener.webapp.schemas import WeeksResponse

router = APIRouter(prefix="/api/weeks", tags=["weeks"])

# meta 要報「是否存在」的已知檔案（screen_result_* 另以 glob 偵測）
_KNOWN_FILES = {
    "candidates": "candidates_enriched.csv",
    "holdings": "holdings_enriched.csv",
    "watchlist": "watchlist_enriched.csv",
    "sector_rotation": "sector_rotation.csv",
    "theme_strength": "theme_strength.csv",
    "pick": "pick.md",
    "group_analysis": "group_analysis.md",
    "sector_rotation_md": "sector_rotation.md",
    "screen_log": "screen_log.md",
}

# pick.md 標頭：「資料基準：candidates 收盤 2026-06-16…」
_DATA_DATE_RE = re.compile(r"資料基準[：:].*?(\d{4}-\d{2}-\d{2})")
# group_analysis.md 標頭：「生成時間：2026-06-22」
_GEN_DATE_RE = re.compile(r"生成時間[：:]\s*(\d{4}-\d{2}-\d{2})")


@router.get("", response_model=WeeksResponse)
def get_weeks() -> WeeksResponse:
    """動態掃 reports/ 目錄（不快取），回實際週次清單與最新週。"""
    return WeeksResponse(weeks=da.list_weeks(), latest=da.latest_week())


@router.get("/{week}/meta")
def get_meta(week: str) -> dict:
    """各檔是否存在 ＋ 資料基準日（best-effort：pick.md → group_analysis.md → 缺則 null）。"""
    if not da.week_exists(week):
        raise HTTPException(status_code=404, detail=f"週次 {week} 不存在")

    files = {key: da.file_path(week, fname).is_file() for key, fname in _KNOWN_FILES.items()}
    files["screens"] = bool(list((da.file_path(week, "")).glob("screen_result_*.csv")))

    data_date: str | None = None
    pick = da.read_text(week, "pick.md")
    if pick and (m := _DATA_DATE_RE.search(pick)):
        data_date = m.group(1)

    generated_at: str | None = None
    ga = da.read_text(week, "group_analysis.md")
    if ga and (m := _GEN_DATE_RE.search(ga)):
        generated_at = m.group(1)

    return {
        "week": week,
        "data_date": data_date,
        "generated_at": generated_at,
        "files": files,
    }
