"""report/pick_store.py — 每週 pick 持久化（規劃書 05 F1-PO1）。

設計意圖（規劃書 05 §F1）：
  pick.md 是給人讀的決策文件，但無固定 schema、命名曾漂移（W24 picks.md）、
  甚至整週斷供（W26）——閉環（PO2-PO4）需要機器可讀的底帳。本模組把每週決策
  固化成兩個 CSV：

  - reports/<week>/picks.csv     入選各層（core／opportunity／pool）
  - reports/<week>/excluded.csv  當週被旗標剔除／流放的股與原因（反事實底帳，
    供 PO4 算偽陰性率——「過熱」「土洋對作」各自擋掉多少報酬，旗標第一次有裁判）

  純檔案 IO＋schema 驗證，不打網。寫入以 (week, stock_id) upsert（重跑冪等）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
from loguru import logger

LAYERS = ("core", "opportunity", "pool")

PICKS_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,           # 週次 tag（= reports/ 子目錄名，如 2026-W27）
    "data_date": pl.Date,      # pick 依據的資料日（= screen_result 的 screened_at）
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "layer": pl.Utf8,          # core | opportunity | pool
    "sub_industry": pl.Utf8,   # 所屬次產業（vs 族群超額的基準籃；可空）
    "entry_zone": pl.Utf8,     # 進場條件（自由文字，如「回測23不破分批」）
    "stop": pl.Utf8,           # 停損條件（自由文字，如「收盤破季線19.16」）
    "ext_ma60_pct": pl.Float64,  # 入選時距季線乖離%（主虧損剖面欄，§1.3）
    "thesis_tag": pl.Utf8,     # 入選論點短標（如「F 主升續勢」）
}

EXCLUDED_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "reason": pl.Utf8,         # 剔除旗標類別（過熱／土洋對作／投信主導／低流動／轉弱…）
    "detail": pl.Utf8,         # 補充說明（可空）
}

PICKS_FILENAME = "picks.csv"
EXCLUDED_FILENAME = "excluded.csv"


def _read_csv(path: Path, schema: dict[str, type[pl.DataType]]) -> pl.DataFrame:
    """讀單一 CSV 並套 schema（缺欄補 null、多欄丟棄；日期字串轉 Date）。"""
    if not path.exists():
        return pl.DataFrame(schema=schema)
    try:
        df = pl.read_csv(path, schema_overrides={"stock_id": pl.Utf8})
    except Exception as e:  # noqa: BLE001 — 單檔壞掉不擋整批
        logger.warning("讀取 {} 失敗：{}", path, e)
        return pl.DataFrame(schema=schema)
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    if "data_date" in df.columns and df.schema["data_date"] == pl.Utf8:
        df = df.with_columns(pl.col("data_date").str.to_date(strict=False))
    return df.select(
        [
            pl.col(c).cast(dt) if c in df.columns else pl.lit(None, dtype=dt).alias(c)
            for c, dt in schema.items()
        ]
    )


def load_week_picks(week_dir: Path) -> pl.DataFrame:
    """讀單週 picks.csv（無檔 → 空表，schema 一致）。"""
    return _read_csv(week_dir / PICKS_FILENAME, PICKS_SCHEMA)


def load_week_excluded(week_dir: Path) -> pl.DataFrame:
    """讀單週 excluded.csv（無檔 → 空表，schema 一致）。"""
    return _read_csv(week_dir / EXCLUDED_FILENAME, EXCLUDED_SCHEMA)


def week_dirs(reports_dir: Path) -> list[Path]:
    """reports/ 下的週次目錄（名稱含 -W 的目錄，如 2026-W21），依名稱排序。

    週次目錄的唯一判準，artifact_check（F4 產物完整性檢查）亦共用。
    """
    if not reports_dir.exists():
        return []
    return sorted(p for p in reports_dir.iterdir() if p.is_dir() and "-W" in p.name)


def load_all_picks(reports_dir: Path) -> pl.DataFrame:
    """合併所有週次的 picks.csv（無任何檔 → 空表）。"""
    frames = [df for d in week_dirs(reports_dir) if not (df := load_week_picks(d)).is_empty()]
    return pl.concat(frames) if frames else pl.DataFrame(schema=PICKS_SCHEMA)


def load_all_excluded(reports_dir: Path) -> pl.DataFrame:
    """合併所有週次的 excluded.csv（無任何檔 → 空表）。"""
    frames = [
        df for d in week_dirs(reports_dir) if not (df := load_week_excluded(d)).is_empty()
    ]
    return pl.concat(frames) if frames else pl.DataFrame(schema=EXCLUDED_SCHEMA)


def weeks_without_picks(reports_dir: Path) -> list[str]:
    """有篩選產物（screen_result_*.csv）但沒有 picks.csv 的週次——產物斷供如實標（W26 型）。"""
    missing: list[str] = []
    for d in week_dirs(reports_dir):
        has_screen = any(d.glob("screen_result_*.csv"))
        if has_screen and not (d / PICKS_FILENAME).exists():
            missing.append(d.name)
    return missing


def _validate(row: dict, schema: dict[str, type[pl.DataType]]) -> None:
    if not str(row.get("stock_id") or "").strip():
        raise ValueError("stock_id 不可為空")
    if not str(row.get("week") or "").strip():
        raise ValueError("week 不可為空")
    if not isinstance(row.get("data_date"), date):
        raise ValueError("data_date 必須是 date（pick 依據的資料日）")
    if schema is PICKS_SCHEMA and row.get("layer") not in LAYERS:
        raise ValueError(f"layer 必須是 {LAYERS}，收到 {row.get('layer')!r}")
    if schema is EXCLUDED_SCHEMA and not str(row.get("reason") or "").strip():
        raise ValueError("excluded 列必須有 reason（剔除旗標類別）")


def _upsert(
    week_dir: Path, row: dict, schema: dict[str, type[pl.DataType]], filename: str
) -> pl.DataFrame:
    _validate(row, schema)
    existing = _read_csv(week_dir / filename, schema)
    new = pl.DataFrame([{c: row.get(c) for c in schema}], schema=schema)
    replaced = existing.filter(pl.col("stock_id") == row["stock_id"]).height
    if replaced:
        logger.info("{} 已有 {}，覆寫該列", filename, row["stock_id"])
        existing = existing.filter(pl.col("stock_id") != row["stock_id"])
    sort_cols = ["layer", "stock_id"] if "layer" in schema else ["stock_id"]
    merged = pl.concat([existing, new]).sort(sort_cols)
    week_dir.mkdir(parents=True, exist_ok=True)
    merged.write_csv(week_dir / filename)
    return merged


def upsert_pick(week_dir: Path, row: dict) -> pl.DataFrame:
    """寫入／覆寫一列 pick（以 stock_id 去重，冪等），回傳該週最新全表。"""
    return _upsert(week_dir, row, PICKS_SCHEMA, PICKS_FILENAME)


def upsert_excluded(week_dir: Path, row: dict) -> pl.DataFrame:
    """寫入／覆寫一列剔除紀錄（以 stock_id 去重，冪等），回傳該週最新全表。"""
    return _upsert(week_dir, row, EXCLUDED_SCHEMA, EXCLUDED_FILENAME)
