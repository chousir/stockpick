"""enriched 表格端點。M-Dash 0：candidates（holdings/watchlist 等後續 milestone）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import ValidationError

from tw_screener.webapp import data_access as da
from tw_screener.webapp.schemas import EnrichedRow

router = APIRouter(prefix="/api/weeks", tags=["tables"])


def _rows_or_404(week: str, filename: str, what: str) -> list[dict[str, Any]]:
    if not da.week_exists(week):
        raise HTTPException(status_code=404, detail=f"週次 {week} 不存在")
    rows = da.read_table(week, filename)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"本週無{what}資料")
    return rows


def _validate(rows: list[dict[str, Any]]) -> list[EnrichedRow]:
    """逐列 Pydantic 驗證；單列壞掉退回 model_construct（不丟 500）。"""
    out: list[EnrichedRow] = []
    for r in rows:
        try:
            out.append(EnrichedRow.model_validate(r))
        except ValidationError as exc:
            logger.warning(f"列驗證失敗，原樣保留 stock_id={r.get('stock_id')}: {exc}")
            out.append(EnrichedRow.model_construct(**r))
    return out


@router.get("/{week}/candidates", response_model=list[EnrichedRow])
def get_candidates(week: str) -> list[EnrichedRow]:
    """候選股主表（candidates_enriched.csv）。"""
    return _validate(_rows_or_404(week, "candidates_enriched.csv", "候選股"))
