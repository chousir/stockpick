"""個股彙整端點（docs/17 §4/§5.4）。M-Dash 3：跨表 join + 命中策略 + 族群位置。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from tw_screener.screener.goodinfo.url_builder import stock_detail_url
from tw_screener.webapp import data_access as da
from tw_screener.webapp.schemas import EnrichedRow, SectorRow, StockDetail

router = APIRouter(prefix="/api/weeks", tags=["stock"])


def _match_sectors(week: str, row: dict[str, Any]) -> list[SectorRow] | None:
    """以個股 theme 標籤（、分隔）對 sector_rotation 的 sub_industry 配對。

    本週無 sector_rotation 檔回 None；有檔但無對應次產業回 []（docs/17 §4）。
    """
    sectors = da.read_table(week, "sector_rotation.csv")
    if sectors is None:
        return None
    tags = {t.strip() for t in str(row.get("theme") or "").split("、") if t.strip()}
    if not tags:
        return []
    return [
        SectorRow.model_validate(s) for s in sectors if str(s.get("sub_industry") or "") in tags
    ]


@router.get("/{week}/stock/{stock_id}", response_model=StockDetail)
def get_stock(week: str, stock_id: str) -> StockDetail:
    """個股鑽取：合併 candidates/holdings/watchlist、命中策略、所屬族群位置。"""
    if not da.week_exists(week):
        raise HTTPException(status_code=404, detail=f"週次 {week} 不存在")
    merged, sources = da.find_stock(week, stock_id)
    if merged is None:
        raise HTTPException(status_code=404, detail=f"本週查無個股 {stock_id}")

    goodinfo = merged.get("goodinfo_url") or stock_detail_url(stock_id)
    return StockDetail(
        stock_id=stock_id,
        week=week,
        sources=sources,
        screens=da.screens_hit(week, stock_id),
        row=EnrichedRow.model_validate(merged),
        sectors=_match_sectors(week, merged),
        goodinfo_url=str(goodinfo),
    )
