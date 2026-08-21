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


# 快取保留窗的「家族 → 檔名前綴」對照（規劃書 01 P2）。前綴含尾底線、以 str.startswith 比對。
# 巢狀關係刻意同窗故不細分：daily_ 也涵蓋 daily_all_、institutional_ 涵蓋 institutional_otc_、
# margin_ 涵蓋 margin_otc_；otc_daily_ 不被 daily_ 前綴涵蓋故另列。未列入者（dividend_calendar_/
# revenue_/industry_/fundamentals_…）一律不刪（保守，且非「隨週數無限增長」的痛點）。
_RETENTION_FAMILIES: dict[str, tuple[str, ...]] = {
    "daily_days": ("daily_", "otc_daily_"),
    "institutional_days": ("institutional_",),
    "margin_days": ("margin_",),
    "valuation_days": ("valuation_ratios_",),
}


def _prune_family(files: list[Path], days: object) -> list[Path]:
    """單一家族內：以該家族檔名最新日為錨，回傳早於「錨 − days 日曆日」的檔。

    錨取自檔名（非 wall-clock）→ 可重現、且家族停更時不會誤刪最新檔。
    days 非正整數 → 視為未設定、回空（不刪）。無可解析日期 token 的檔不納入（保守保留）。
    """
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return []
    dated = [(d, f) for f in files if (d := _file_rep_date(f)) is not None]
    if not dated:
        return []
    cutoff = max(d for d, _ in dated) - timedelta(days=days)
    return [f for d, f in dated if d < cutoff]


def select_prune_candidates(cache_dir: Path, retention: dict[str, object] | None) -> list[Path]:
    """依保留窗挑出「可刪的超窗快取檔」（規劃書 01 P2），**不實際刪除**。

    逐家族（daily/institutional/margin/valuation）依 `_prune_family` 取超窗檔。
    個股月檔 `stock_day_<sid>_*` 依 `stock_day_keep_all`（預設 True）全留；明確設 False
    時改用 `daily_days` 窗清理（個股月檔本質即日線價格）。回傳依檔名排序的建議刪除清單；
    呼叫端決定真刪或 `--dry` 只印。cache_dir 不存在或無設定 → 回空。
    """
    retention = retention or {}
    if not cache_dir.exists():
        return []
    files = [f for f in cache_dir.glob("*.parquet") if f.is_file()]
    prune: list[Path] = []
    for key, prefixes in _RETENTION_FAMILIES.items():
        fam = [f for f in files if f.name.startswith(prefixes)]
        prune.extend(_prune_family(fam, retention.get(key)))
    if retention.get("stock_day_keep_all", True) is False:
        stock_day = [f for f in files if f.name.startswith("stock_day_")]
        prune.extend(_prune_family(stock_day, retention.get("daily_days")))
    return sorted(prune)


def is_fresh(path: Path, ttl_hours: float) -> bool:
    """回傳 True 若 path 存在且修改時間在 ttl_hours 內。"""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=ttl_hours)


def is_fresh_with_columns(path: Path, ttl_hours: float, required_columns: list[str]) -> bool:
    """`is_fresh()` 之外，再檢查快取是否含 `required_columns`（只讀 schema、不讀整檔）。

    防「改 parser 加欄位後，TTL 內的舊 schema 快取被誤判新鮮、新欄位永遠不出現」——
    缺欄位一律視為 stale，強制重抓（見 playbook 90-lessons：市值/累計YoY 落地時的教訓）。
    """
    if not is_fresh(path, ttl_hours):
        return False
    try:
        schema_cols = pl.read_parquet_schema(path).keys()
    except Exception as e:  # noqa: BLE001 — schema 讀取失敗一律視為 stale，強制重抓
        logger.warning(f"讀快取 schema 失敗 {path}，視為 stale：{e}")
        return False
    return set(required_columns).issubset(schema_cols)


def save_parquet(df: pl.DataFrame, path: Path) -> None:
    """儲存 DataFrame 到 parquet，自動建立父目錄。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    logger.info(f"快取寫入 {path} ({len(df)} 筆)")


def load_parquet(path: Path) -> pl.DataFrame:
    """從 parquet 讀取 DataFrame。"""
    logger.info(f"讀快取 {path}")
    return pl.read_parquet(path)


def find_latest(directory: Path, pattern: str, by: str = "mtime") -> Path | None:
    """在 directory 找符合 pattern 的最新檔案；無則 None（全專案「取最新快取」單一入口）。

    by="mtime"：修改時間最新（同家族被重抓時以抓取時間為準）；
    by="name"：檔名排序最大（檔名含日期 token 的家族，日期即新舊，與重抓時間無關）。
    """
    if not directory.exists():
        return None
    files = list(directory.glob(pattern))
    if not files:
        return None
    if by == "name":
        return sorted(files)[-1]
    return max(files, key=lambda p: p.stat().st_mtime)
