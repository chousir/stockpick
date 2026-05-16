"""證交所 OpenAPI 資料抓取（sync，httpx）。"""

import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import yaml
from loguru import logger

from .cache import is_fresh, load_parquet, save_parquet

# ─── 字串轉換工具 ─────────────────────────────────────────────────────────────


def _clean_int(s: str) -> int:
    """移除千位符號後轉 int，無效字串 raise ValueError。"""
    return int(s.strip().replace(",", ""))


def _clean_float(s: str) -> float | None:
    """移除千位符號與特殊字元後轉 float；無效值回傳 None。"""
    cleaned = s.strip().replace(",", "").replace("△", "").replace("▽", "")
    if not cleaned or cleaned in ("-", "--", "X", "N/A"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _roc_to_date(roc_str: str) -> date:
    """民國年斜線格式（如 '115/05/15'）轉 date。"""
    parts = roc_str.strip().split("/")
    return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))


def _roc_compact_to_date(roc_compact: str) -> date:
    """
    民國年緊湊格式（如 '1150514'，格式 YYYMMDD）轉 date。
    TWSE STOCK_DAY_ALL 的 Date 欄位使用此格式。
    """
    roc_year = int(roc_compact[:3])
    month = int(roc_compact[3:5])
    day = int(roc_compact[5:7])
    return date(roc_year + 1911, month, day)


def _roc_pubdate_to_ym(pubdate: str) -> str:
    """
    出表日期（如 '1140416'，格式 YYYMMDD）轉 YYYYMM。
    僅取年份與月份（代表「何時出表」）。
    """
    roc_year = int(pubdate[:3])
    month = pubdate[3:5]
    return f"{roc_year + 1911}{month}"


def _roc_ym_to_ym(roc_ym: str) -> str:
    """
    資料年月（如 '11504'，格式 YYYММ）轉 YYYYMM。
    TWSE t187ap05_L 的「資料年月」欄位使用此格式。
    """
    roc_year = int(roc_ym[:3])
    month = roc_ym[3:].zfill(2)
    return f"{roc_year + 1911}{month}"


# ─── Parse 函數（純 function，不含 I/O，方便測試）──────────────────────────────


def _parse_daily_all(data: list[dict[str, Any]]) -> pl.DataFrame:
    """
    解析 STOCK_DAY_ALL 回應 → DataFrame。
    每筆記錄含 Date（ROC 緊湊格式，如 '1150514'）。
    """
    schema = {
        "date": pl.Date,
        "stock_id": pl.Utf8,
        "name": pl.Utf8,
        "trade_volume": pl.Int64,
        "trade_value": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "change": pl.Float64,
        "transaction": pl.Int64,
    }
    rows = []
    for r in data:
        try:
            rows.append(
                {
                    "date": _roc_compact_to_date(r["Date"]),
                    "stock_id": r["Code"].strip(),
                    "name": r["Name"].strip(),
                    "trade_volume": _clean_int(r["TradeVolume"]),
                    "trade_value": _clean_int(r["TradeValue"]),
                    "open": _clean_float(r["OpeningPrice"]),
                    "high": _clean_float(r["HighestPrice"]),
                    "low": _clean_float(r["LowestPrice"]),
                    "close": _clean_float(r["ClosingPrice"]),
                    "change": _clean_float(r["Change"]),
                    "transaction": _clean_int(r["Transaction"]),
                }
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"略過無效行情資料：{r.get('Code', '?')} — {e}")
    return pl.DataFrame(rows, schema=schema)


def _parse_institutional(data: list[dict[str, Any]]) -> pl.DataFrame:
    """解析 T86 三大法人買賣超 → DataFrame。"""
    schema = {
        "date": pl.Date,
        "stock_id": pl.Utf8,
        "stock_name": pl.Utf8,
        "foreign_net": pl.Int64,
        "trust_net": pl.Int64,
        "dealer_net": pl.Int64,
        "total_net": pl.Int64,
    }
    rows = []
    for r in data:
        try:
            rows.append(
                {
                    "date": _roc_to_date(r["Date"]),
                    "stock_id": r["StockID"].strip(),
                    "stock_name": r["StockName"].strip(),
                    "foreign_net": _clean_int(r.get("ForeignInvestmentNetBuyOrSell", "0")),
                    "trust_net": _clean_int(r.get("ForeignInvestmentTrustNetBuyOrSell", "0")),
                    "dealer_net": _clean_int(r.get("DealersNetBuyOrSell", "0")),
                    "total_net": _clean_int(r.get("TotalInstitutionalInvestors", "0")),
                }
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"略過無效法人資料：{r.get('StockID', '?')} — {e}")
    return pl.DataFrame(rows, schema=schema)


def _parse_stock_day(data: list[dict[str, Any]], stock_id: str) -> pl.DataFrame:
    """解析單檔月 OHLCV（STOCK_DAY）→ DataFrame。"""
    schema = {
        "date": pl.Date,
        "stock_id": pl.Utf8,
        "trade_volume": pl.Int64,
        "trade_value": pl.Int64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "change": pl.Float64,
        "transaction": pl.Int64,
    }
    rows = []
    for r in data:
        try:
            rows.append(
                {
                    "date": _roc_to_date(r["Date"]),
                    "stock_id": stock_id,
                    "trade_volume": _clean_int(r["TradeVolume"]),
                    "trade_value": _clean_int(r["TradeValue"]),
                    "open": _clean_float(r["OpeningPrice"]),
                    "high": _clean_float(r["HighestPrice"]),
                    "low": _clean_float(r["LowestPrice"]),
                    "close": _clean_float(r["ClosingPrice"]),
                    "change": _clean_float(r["Change"]),
                    "transaction": _clean_int(r["Transaction"]),
                }
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"略過無效 OHLCV 資料：{r.get('Date', '?')} — {e}")
    return pl.DataFrame(rows, schema=schema)


def _parse_revenue(data: list[dict[str, Any]]) -> pl.DataFrame:
    """
    解析月營收（t187ap05_L）→ DataFrame。
    實際欄位名稱：「資料年月」、「營業收入-當月營收」等。
    """
    schema = {
        "stock_id": pl.Utf8,
        "company_name": pl.Utf8,
        "year_month": pl.Utf8,
        "revenue": pl.Int64,
        "prev_year_revenue": pl.Int64,
        "yoy_pct": pl.Float64,
    }
    rows = []
    for r in data:
        try:
            prev_raw = r.get("營業收入-去年當月營收", "")
            rows.append(
                {
                    "stock_id": r["公司代號"].strip(),
                    "company_name": r["公司名稱"].strip(),
                    "year_month": _roc_ym_to_ym(r["資料年月"]),
                    "revenue": _clean_int(r["營業收入-當月營收"]),
                    "prev_year_revenue": _clean_int(prev_raw) if prev_raw.strip() else None,
                    "yoy_pct": _clean_float(r.get("營業收入-去年同月增減(%)", "")),
                }
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"略過無效營收資料：{r.get('公司代號', '?')} — {e}")
    return pl.DataFrame(rows, schema=schema)


# ─── 月份計算工具 ─────────────────────────────────────────────────────────────


def _months_back(base: date, n: int) -> date:
    """回傳 base 往前 n 個月的第一天（不依賴 dateutil）。"""
    month = base.month - n
    year = base.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


# ─── Client ───────────────────────────────────────────────────────────────────


class TWSEClient:
    """證交所 OpenAPI 同步 client（含本地 parquet 快取）。"""

    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        ttl_hours: float,
        user_agent: str,
        interval_sec: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.ttl_hours = ttl_hours
        self.user_agent = user_agent
        self.interval_sec = interval_sec
        self._last_req: float = 0.0

    def _get(self, endpoint: str) -> list[dict[str, Any]]:
        """限速後發 GET，回傳 JSON list；若回傳 HTML 則視為端點不可用。"""
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.info(f"HTTP GET {url}")
        resp = httpx.get(
            url,
            headers={"User-Agent": self.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        self._last_req = time.monotonic()
        content_type = resp.headers.get("content-type", "")
        if "application/json" not in content_type:
            logger.warning(f"{endpoint} 回傳非 JSON（{content_type[:40]}），略過")
            return []
        data = resp.json()
        return data if isinstance(data, list) else []

    def fetch_daily_all(self) -> pl.DataFrame:
        """抓全市場日線，快取到 daily_YYYYMMDD.parquet。"""
        today = date.today().strftime("%Y%m%d")
        cache_file = self.cache_dir / f"daily_{today}.parquet"
        if is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)
        data = self._get("/exchangeReport/STOCK_DAY_ALL")
        df = _parse_daily_all(data)
        if not df.is_empty():
            save_parquet(df, cache_file)
        else:
            logger.warning("STOCK_DAY_ALL 回傳空資料（今日可能非交易日）")
        return df

    def fetch_institutional(self) -> pl.DataFrame:
        """抓三大法人，快取到 institutional_YYYYMMDD.parquet。"""
        today = date.today().strftime("%Y%m%d")
        cache_file = self.cache_dir / f"institutional_{today}.parquet"
        if is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)
        data = self._get("/fund/T86")
        df = _parse_institutional(data)
        if not df.is_empty():
            save_parquet(df, cache_file)
        else:
            logger.warning("T86 回傳空資料或端點不可用，法人資料略過")
        return df

    def fetch_revenue(self) -> pl.DataFrame:
        """抓月營收（全市場），快取到 revenue_YYYYMM.parquet。"""
        ym = date.today().strftime("%Y%m")
        cache_file = self.cache_dir / f"revenue_{ym}.parquet"
        if is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)
        data = self._get("/opendata/t187ap05_L")
        df = _parse_revenue(data)
        if not df.is_empty():
            save_parquet(df, cache_file)
        return df

    def fetch_stock_ohlcv(self, stock_id: str, n_days: int = 60) -> pl.DataFrame:
        """
        從累積的全市場日線快取（daily_*.parquet）取特定股票近 n_days 個交易日。
        需先執行 fetch_daily_all 累積快取；每多一天就多一筆歷史。
        """
        _empty_schema = {
            "date": pl.Date,
            "stock_id": pl.Utf8,
            "trade_volume": pl.Int64,
            "trade_value": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "change": pl.Float64,
            "transaction": pl.Int64,
        }
        files = sorted(self.cache_dir.glob("daily_*.parquet"))
        if not files:
            logger.warning("無日線快取，請先執行 fetch-twse")
            return pl.DataFrame(schema=_empty_schema)
        frames: list[pl.DataFrame] = []
        for f in files:
            try:
                df = pl.read_parquet(f)
                if "stock_id" not in df.columns or "date" not in df.columns:
                    continue
                filtered = df.filter(pl.col("stock_id") == stock_id)
                if not filtered.is_empty():
                    frames.append(filtered)
            except Exception as e:
                logger.warning(f"讀取 {f} 失敗：{e}")
        if not frames:
            return pl.DataFrame(schema=_empty_schema)
        return pl.concat(frames).sort("date").tail(n_days)

    def fetch_stock_institutional(self, stock_id: str, n_days: int = 20) -> pl.DataFrame:
        """從累積的法人快取讀取特定股票近 n_days 筆。"""
        _empty_schema = {
            "date": pl.Date,
            "stock_id": pl.Utf8,
            "stock_name": pl.Utf8,
            "foreign_net": pl.Int64,
            "trust_net": pl.Int64,
            "dealer_net": pl.Int64,
            "total_net": pl.Int64,
        }
        files = list(self.cache_dir.glob("institutional_*.parquet"))
        if not files:
            return pl.DataFrame(schema=_empty_schema)
        frames = [pl.read_parquet(f) for f in files]
        return pl.concat(frames).filter(pl.col("stock_id") == stock_id).sort("date").tail(n_days)

    def fetch_stock_revenue(self, stock_id: str, n_months: int = 12) -> pl.DataFrame:
        """從累積的月營收快取讀取特定股票近 n_months 筆。"""
        _empty_schema = {
            "stock_id": pl.Utf8,
            "company_name": pl.Utf8,
            "year_month": pl.Utf8,
            "revenue": pl.Int64,
            "prev_year_revenue": pl.Int64,
            "yoy_pct": pl.Float64,
        }
        files = list(self.cache_dir.glob("revenue_*.parquet"))
        if not files:
            return pl.DataFrame(schema=_empty_schema)
        frames = [pl.read_parquet(f) for f in files]
        return (
            pl.concat(frames)
            .filter(pl.col("stock_id") == stock_id)
            .sort("year_month")
            .tail(n_months)
        )


# ─── Factory ──────────────────────────────────────────────────────────────────


def create_client(settings_path: Path = Path("config/settings.yaml")) -> TWSEClient:
    """從 settings.yaml 建立 TWSEClient。"""
    with open(settings_path) as f:
        settings = yaml.safe_load(f)
    twse = settings["twse"]
    paths = settings["paths"]
    return TWSEClient(
        base_url=twse["base_url"],
        cache_dir=Path(paths["cache_dir"]) / "twse",
        ttl_hours=float(twse["cache_ttl_hours"]),
        user_agent=twse["user_agent"],
        interval_sec=float(twse["request_interval_sec"]),
    )
