"""總經／市場級事件行事曆：讀 config/macro_calendar.yaml → DataFrame，過濾未來窗。

比照 twse.filter_dividend_calendar 的形狀，但事件為**市場級**（FOMC/CPI/結算/法說…），
不依個股過濾。供 group_analysis.md Section 0.6 與 ProPicks 事件曝險判斷。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "name": pl.Utf8,
    "category": pl.Utf8,
    "severity": pl.Utf8,
    "verified": pl.Boolean,
    "note": pl.Utf8,
}

_DEFAULT_PATH = Path("config/macro_calendar.yaml")


def _coerce_date(value: object) -> date | None:
    """YAML 的 date 欄可能是原生 date / datetime / 字串；統一成 date，無法解析回 None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def load_macro_calendar(path: Path = _DEFAULT_PATH) -> pl.DataFrame:
    """讀 macro_calendar.yaml → 事件 DataFrame，依日期排序。

    欄位：date / name / category / severity / verified / note。
    檔不存在／空／格式錯 → 回空 DataFrame（不報錯，Section 0.6 自然不渲染）。
    """
    empty = pl.DataFrame(schema=_SCHEMA)
    if not path.exists():
        return empty
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — 設定檔壞掉不該整個流程炸掉
        logger.warning("load_macro_calendar 讀取失敗（{}）：{}", path, exc)
        return empty

    rows: list[dict] = []
    for e in data.get("events") or []:
        d = _coerce_date(e.get("date"))
        if d is None:
            logger.warning("macro_calendar 日期缺漏／格式錯（跳過）：{}", e.get("date"))
            continue
        rows.append(
            {
                "date": d,
                "name": str(e.get("name") or ""),
                "category": str(e.get("category") or ""),
                "severity": str(e.get("severity") or ""),
                "verified": bool(e.get("verified", False)),
                "note": str(e.get("note") or ""),
            }
        )
    if not rows:
        return empty
    return pl.DataFrame(rows, schema=_SCHEMA).sort("date")


def filter_macro_calendar(
    df: pl.DataFrame,
    today: date,
    lookahead_days: int,
) -> pl.DataFrame:
    """篩出 date 落在 [today, today+lookahead_days] 的市場級事件，依日期排序。"""
    if df.is_empty() or "date" not in df.columns:
        return df
    end = today + timedelta(days=lookahead_days)
    return df.filter(pl.col("date").is_between(today, end)).sort("date")
