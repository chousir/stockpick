"""原始 markdown 敘事端點（docs/17 §4）。只回原文，前端負責渲染。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tw_screener.webapp import data_access as da

router = APIRouter(prefix="/api/weeks", tags=["narrative"])

# name ∈ pick / group_analysis / sector_rotation / screen_log
_NAME_MAP = {
    "pick": "pick.md",
    "group_analysis": "group_analysis.md",
    "sector_rotation": "sector_rotation.md",
    "screen_log": "screen_log.md",
}


@router.get("/{week}/narrative/{name}")
def get_narrative(week: str, name: str) -> dict:
    if not da.week_exists(week):
        raise HTTPException(status_code=404, detail=f"週次 {week} 不存在")
    filename = _NAME_MAP.get(name)
    if filename is None:
        raise HTTPException(status_code=404, detail=f"未知的敘事類型：{name}")
    text = da.read_text(week, filename)
    if text is None:
        raise HTTPException(status_code=404, detail="本週無此敘事")
    return {"name": name, "markdown": text}
