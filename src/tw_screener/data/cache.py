"""本地 parquet 快取工具。"""

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
from loguru import logger


def is_fresh(path: Path, ttl_hours: float) -> bool:
    """回傳 True 若 path 存在且修改時間在 ttl_hours 內。"""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=ttl_hours)


def save_parquet(df: pl.DataFrame, path: Path) -> None:
    """儲存 DataFrame 到 parquet，自動建立父目錄。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    logger.info(f"快取寫入 {path} ({len(df)} 筆)")


def load_parquet(path: Path) -> pl.DataFrame:
    """從 parquet 讀取 DataFrame。"""
    logger.info(f"讀快取 {path}")
    return pl.read_parquet(path)


def find_latest(directory: Path, pattern: str) -> Path | None:
    """在 directory 找符合 pattern 的最新檔案（依修改時間降冪）。"""
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None
