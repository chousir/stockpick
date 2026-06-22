"""enriched / 族群表格端點。M-Dash 0：candidates；M-Dash 2：sectors / themes。"""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, ValidationError

from tw_screener.webapp import data_access as da
from tw_screener.webapp.schemas import EnrichedRow, SectorRow, ThemeRow

router = APIRouter(prefix="/api/weeks", tags=["tables"])

M = TypeVar("M", bound=BaseModel)


def _rows_or_404(week: str, filename: str, what: str) -> list[dict[str, Any]]:
    if not da.week_exists(week):
        raise HTTPException(status_code=404, detail=f"週次 {week} 不存在")
    rows = da.read_table(week, filename)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"本週無{what}資料")
    return rows


def _validate(rows: list[dict[str, Any]], model: type[M]) -> list[M]:
    """逐列 Pydantic 驗證；單列壞掉退回 model_construct（不丟 500）。"""
    out: list[M] = []
    for r in rows:
        try:
            out.append(model.model_validate(r))
        except ValidationError as exc:
            logger.warning(f"列驗證失敗（{model.__name__}）: {exc}")
            out.append(model.model_construct(**r))
    return out


@router.get("/{week}/candidates", response_model=list[EnrichedRow])
def get_candidates(week: str) -> list[EnrichedRow]:
    """候選股主表（candidates_enriched.csv）。"""
    return _validate(_rows_or_404(week, "candidates_enriched.csv", "候選股"), EnrichedRow)


@router.get("/{week}/holdings", response_model=list[EnrichedRow])
def get_holdings(week: str) -> list[EnrichedRow]:
    """持股損益表（holdings_enriched.csv）。API 回明文，遮罩在前端（docs/17 §5.1）。"""
    return _validate(_rows_or_404(week, "holdings_enriched.csv", "持股"), EnrichedRow)


@router.get("/{week}/sectors", response_model=list[SectorRow])
def get_sectors(week: str) -> list[SectorRow]:
    """族群資金雷達（sector_rotation.csv）。"""
    return _validate(_rows_or_404(week, "sector_rotation.csv", "族群輪動"), SectorRow)


@router.get("/{week}/themes", response_model=list[ThemeRow])
def get_themes(week: str) -> list[ThemeRow]:
    """主題/次產業強度（theme_strength.csv）。"""
    return _validate(_rows_or_404(week, "theme_strength.csv", "主題強度"), ThemeRow)
