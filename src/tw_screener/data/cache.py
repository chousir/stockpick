"""本地 parquet 快取工具。"""

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
from loguru import logger


def _last_day_of_month(year: int, month: int) -> date:
    """該年月最後一天（不依賴 calendar 模組）。"""
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return nxt - timedelta(days=1)


def _file_rep_date(path: Path) -> date | None:
    """解析快取檔名尾端的日期 token → 代表日期（該檔涵蓋範圍的「上界」）。

    - `*_YYYYMMDD.parquet`（單交易日檔，如 daily_/institutional_/valuation_ratios_）→ 該日
    - `*_YYYYMM.parquet`（單月檔，如 stock_day_<sid>_YYYYMM，內含整月多日）→ 該月最後一天
    無法解析 → None（呼叫端保守保留）。
    """
    token = path.stem.split("_")[-1]
    if not token.isdigit():
        return None
    try:
        if len(token) == 8:
            return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
        if len(token) == 6:
            return _last_day_of_month(int(token[:4]), int(token[4:6]))
    except ValueError:
        return None
    return None


def select_recent_cache_files(files: list[Path], n_days: int) -> list[Path]:
    """從快取檔清單篩出「近 n_days 交易日窗」可能涵蓋的檔（保留輸入順序）。

    定位：**安全超集過濾器**——讓「先按檔名日期縮檔、再讀內容」取代「全讀再 filter」，
    避免隨累積週數線性變慢（規劃書 01 P1）。呼叫端仍會對內容做精確的近 n_days 日 filter
    （`merged.head(n_days)` / `is_in(recent_dates)`），故本函式**寧可多收、不可漏收**：
    視窗刻意放寬（n_days×8//5 日曆日換算 + 31 日緩衝，吸收週末/連假與單月檔上界），
    確保任何「落在近 n_days 交易日內」的檔都被保留，超收的部分由呼叫端 filter 收斂。

    錨點取自檔名（非 wall-clock）→ 行為與「資料內最新日」一致、測試可重現。
    無可解析日期 token 時回傳原清單（保守降級，不誤刪）。
    """
    if not files:
        return []
    dated = [(d, f) for f in files if (d := _file_rep_date(f)) is not None]
    if not dated:
        return list(files)
    anchor = max(d for d, _ in dated)
    window_days = n_days * 8 // 5 + 31  # 交易日→日曆日寬鬆換算 + 單月檔/連假緩衝
    cutoff = anchor - timedelta(days=window_days)
    keep = {id(f) for d, f in dated if d >= cutoff}
    # 無法解析 token 的檔一律保留（保守）；可解析者依 cutoff 取捨；維持輸入順序
    return [f for f in files if _file_rep_date(f) is None or id(f) in keep]


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
