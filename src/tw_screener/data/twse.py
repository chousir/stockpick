"""證交所 OpenAPI 資料抓取（sync，httpx）。"""

import re
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


# ─── 產業別對照表 ─────────────────────────────────────────────────────────────

_TWSE_INDUSTRY_NAMES: dict[str, str] = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光事業",
    "17": "金融保險",
    "18": "貿易百貨",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及周邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "35": "電力供應業",
    "36": "軟體工業",
    "37": "運動休閒業",
    "38": "其他製造業",
    "91": "存託憑證",
}


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


def _parse_institutional(payload: dict[str, Any]) -> pl.DataFrame:
    """
    解析 legacy T86 三大法人買賣超回應 → DataFrame。

    Input: {"stat": "OK", "date": "20260515", "fields": [...], "data": [[...], ...]}
    fields 為中文欄名清單，data 為 row list。
    """
    schema = {
        "date": pl.Date,
        "stock_id": pl.Utf8,
        "stock_name": pl.Utf8,
        "foreign_net": pl.Int64,
        "trust_net": pl.Int64,
        "dealer_net": pl.Int64,
        "total_net": pl.Int64,
    }
    if not payload or payload.get("stat") != "OK":
        return pl.DataFrame(schema=schema)
    fields: list[str] = payload.get("fields", [])
    data_rows: list[list[Any]] = payload.get("data", [])
    date_str: str = payload.get("date", "")  # YYYYMMDD
    if not date_str or len(date_str) != 8:
        logger.warning("T86 回應缺 date 欄位，略過")
        return pl.DataFrame(schema=schema)
    trade_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))

    idx = {name: i for i, name in enumerate(fields)}

    def col(row: list[Any], *candidates: str) -> str:
        for name in candidates:
            if name in idx:
                v = row[idx[name]]
                return v.strip() if isinstance(v, str) else str(v)
        return ""

    rows = []
    for r in data_rows:
        try:
            # foreign = 外陸資買賣超(不含外資自營商) + 外資自營商買賣超
            foreign_main = col(r, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
            foreign_dealer = col(r, "外資自營商買賣超股數")
            foreign_net = _clean_int(foreign_main) + (
                _clean_int(foreign_dealer) if foreign_dealer else 0
            )
            rows.append(
                {
                    "date": trade_date,
                    "stock_id": col(r, "證券代號").strip(),
                    "stock_name": col(r, "證券名稱").strip(),
                    "foreign_net": foreign_net,
                    "trust_net": _clean_int(col(r, "投信買賣超股數")),
                    "dealer_net": _clean_int(col(r, "自營商買賣超股數")),
                    "total_net": _clean_int(col(r, "三大法人買賣超股數")),
                }
            )
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"略過無效法人資料：{r[:2] if r else '?'} — {e}")
    return pl.DataFrame(rows, schema=schema)


_TPEX_BASE = "https://www.tpex.org.tw"
_TPEX_STOCK_DAY_PATH = "/www/zh-tw/afterTrading/tradingStock"

# 共用 schema：TWSE STOCK_DAY 與 TPEX tradingStock 對齊（11 欄、單位皆為股 / 元）
_STOCK_DAY_SCHEMA: dict[str, type[pl.DataType]] = {
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


def _parse_tpex_stock_day(payload: dict[str, Any], stock_id: str) -> pl.DataFrame:
    """
    解析 TPEX tradingStock 回應 → 與 _parse_stock_day 相同 schema。

    與 TWSE STOCK_DAY 的差異：
    - fields/data 在 payload["tables"][0]，不在頂層
    - 單位「成交張數」/「成交仟元」是仟（千），需 ×1000 轉成股 / 元
    - 「日 期」欄名中間有空格（標準化後比對）
    - 「成交筆數」欄名為「筆數」
    """
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
    if not payload or payload.get("stat") != "ok":
        return pl.DataFrame(schema=schema)
    tables = payload.get("tables") or []
    if not tables or not isinstance(tables, list):
        return pl.DataFrame(schema=schema)
    table = tables[0]
    fields: list[str] = [str(f).replace(" ", "") for f in table.get("fields", [])]
    data_rows: list[list[Any]] = table.get("data", [])
    idx = {name: i for i, name in enumerate(fields)}

    def col(row: list[Any], *candidates: str) -> str:
        for name in candidates:
            normalized = name.replace(" ", "")
            if normalized in idx:
                v = row[idx[normalized]]
                return v.strip() if isinstance(v, str) else str(v)
        return ""

    rows = []
    for r in data_rows:
        try:
            volume_lots = _clean_int(col(r, "成交張數"))  # 仟股
            value_lots = _clean_int(col(r, "成交仟元"))  # 仟元
            rows.append(
                {
                    "date": _roc_to_date(col(r, "日期", "日 期")),
                    "stock_id": stock_id,
                    "trade_volume": volume_lots * 1000,
                    "trade_value": value_lots * 1000,
                    "open": _clean_float(col(r, "開盤")),
                    "high": _clean_float(col(r, "最高")),
                    "low": _clean_float(col(r, "最低")),
                    "close": _clean_float(col(r, "收盤")),
                    "change": _clean_float(col(r, "漲跌")),
                    "transaction": _clean_int(col(r, "筆數", "成交筆數")),
                }
            )
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"略過無效 TPEX OHLCV 資料：{r[:1] if r else '?'} — {e}")
    return pl.DataFrame(rows, schema=schema)


def _parse_stock_day(payload: dict[str, Any], stock_id: str) -> pl.DataFrame:
    """
    解析 legacy STOCK_DAY 單檔月 OHLCV 回應 → DataFrame。

    Input: {"stat": "OK", "date": "20260501", "fields": [...], "data": [[...], ...]}
    fields 為中文欄名（如「日期」、「成交股數」），data 為 row list。
    """
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
    if not payload or payload.get("stat") != "OK":
        return pl.DataFrame(schema=schema)
    fields: list[str] = payload.get("fields", [])
    data_rows: list[list[Any]] = payload.get("data", [])
    idx = {name: i for i, name in enumerate(fields)}

    def col(row: list[Any], name: str) -> str:
        if name not in idx:
            return ""
        v = row[idx[name]]
        return v.strip() if isinstance(v, str) else str(v)

    rows = []
    for r in data_rows:
        try:
            rows.append(
                {
                    "date": _roc_to_date(col(r, "日期")),
                    "stock_id": stock_id,
                    "trade_volume": _clean_int(col(r, "成交股數")),
                    "trade_value": _clean_int(col(r, "成交金額")),
                    "open": _clean_float(col(r, "開盤價")),
                    "high": _clean_float(col(r, "最高價")),
                    "low": _clean_float(col(r, "最低價")),
                    "close": _clean_float(col(r, "收盤價")),
                    "change": _clean_float(col(r, "漲跌價差")),
                    "transaction": _clean_int(col(r, "成交筆數")),
                }
            )
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"略過無效 OHLCV 資料：{r[:1] if r else '?'} — {e}")
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


def _parse_listed_industry(data: list[dict[str, Any]]) -> pl.DataFrame:
    """解析 t187ap03_L（上市公司基本資料）→ stock_id / industry_code / industry_name。"""
    schema = {
        "stock_id": pl.Utf8,
        "stock_name": pl.Utf8,
        "industry_code": pl.Utf8,
        "industry_name": pl.Utf8,
    }
    rows = []
    for r in data:
        try:
            code = r.get("產業別", "").strip()
            rows.append(
                {
                    "stock_id": r["公司代號"].strip(),
                    "stock_name": r["公司名稱"].strip(),
                    "industry_code": code,
                    "industry_name": _TWSE_INDUSTRY_NAMES.get(code, "其他"),
                }
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"略過無效產業資料：{r.get('公司代號', '?')} — {e}")
    return pl.DataFrame(rows, schema=schema)


# OTC industry names from ISIN → (code, canonical_name)
# Same code as listed where the industry matches; new codes (42-46) for OTC-only categories
_OTC_INDUSTRY_MAP: dict[str, tuple[str, str]] = {
    "半導體業":       ("24", "半導體業"),
    "電子零組件業":   ("28", "電子零組件業"),
    "生技醫療業":     ("22", "生技醫療業"),
    "電機機械":       ("05", "電機機械"),
    "其他電子業":     ("31", "其他電子業"),
    "光電業":         ("26", "光電業"),
    "電腦及週邊設備業": ("25", "電腦及周邊設備業"),
    "通信網路業":     ("27", "通信網路業"),
    "電子通路業":     ("29", "電子通路業"),
    "資訊服務業":     ("30", "資訊服務業"),
    "其他業":         ("20", "其他"),
    "化學工業":       ("21", "化學工業"),
    "油電燃氣業":     ("23", "油電燃氣業"),
    "建材營造業":     ("14", "建材營造"),
    "航運業":         ("15", "航運業"),
    "觀光餐旅":       ("16", "觀光餐旅業"),
    "金融保險業":     ("17", "金融保險"),
    "食品工業":       ("02", "食品工業"),
    "塑膠工業":       ("03", "塑膠工業"),
    "紡織纖維":       ("04", "紡織纖維"),
    "電器電纜":       ("06", "電器電纜"),
    "鋼鐵工業":       ("10", "鋼鐵工業"),
    "運動休閒":       ("37", "運動休閒業"),
    "居家生活":       ("42", "居家生活"),
    "數位雲端":       ("43", "數位雲端"),
    "文化創意業":     ("44", "文化創意業"),
    "綠能環保":       ("45", "綠能環保"),
    "農業科技業":     ("46", "農業科技業"),
}

_ISIN_OTC_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"


def _parse_otc_industry(html: str) -> pl.DataFrame:
    """解析 ISIN 上櫃一覽表 HTML → stock_id / industry_code / industry_name。"""
    schema = {
        "stock_id": pl.Utf8,
        "stock_name": pl.Utf8,
        "industry_code": pl.Utf8,
        "industry_name": pl.Utf8,
    }
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) != 7:
            continue
        code_name = re.sub(r"<[^>]+>", "", cells[0]).strip()
        market = re.sub(r"<[^>]+>", "", cells[3]).strip()
        ind_raw = re.sub(r"<[^>]+>", "", cells[4]).strip()
        parts = code_name.split("　")  # full-width space
        if len(parts) < 2 or market != "上櫃" or not ind_raw:
            continue
        stock_id = parts[0].strip()
        if not stock_id.isdigit():
            continue
        code, name = _OTC_INDUSTRY_MAP.get(ind_raw, ("otc", ind_raw))
        rows.append(
            {
                "stock_id": stock_id,
                "stock_name": parts[1].strip(),
                "industry_code": code,
                "industry_name": name,
            }
        )
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


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
        # 上櫃股 stock_id 集合 lazy load（讀 otc_industry_*.parquet）
        self._otc_ids: set[str] | None = None

    def _load_otc_ids(self) -> set[str]:
        """讀 otc_industry_*.parquet 取得 OTC stock_id 集合；instance-level cache。

        若 cache 還沒建立，會自動呼叫 fetch_otc_industry() 抓 ISIN（首次 ~2 秒）。
        失敗 / 空資料時回空集合，下游 fetch_stock_history 會 fallback 走 TWSE。
        """
        if self._otc_ids is None:
            try:
                otc_df = self.fetch_otc_industry()
            except Exception as exc:
                logger.warning("_load_otc_ids 抓 OTC 清單失敗：{}", exc)
                otc_df = pl.DataFrame()
            if otc_df.is_empty() or "stock_id" not in otc_df.columns:
                self._otc_ids = set()
            else:
                self._otc_ids = set(otc_df["stock_id"].cast(pl.Utf8).to_list())
        return self._otc_ids

    def _throttle(self) -> None:
        """限速：確保連續請求間隔 ≥ interval_sec。"""
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)

    def _get(self, endpoint: str) -> list[dict[str, Any]]:
        """限速後發 GET 到 base_url + endpoint，回傳 JSON list。"""
        self._throttle()
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

    def _get_legacy(self, url: str) -> dict[str, Any]:
        """
        限速後發 GET 到完整 URL（legacy TWSE 端點），回傳整個 JSON dict
        （含 stat / fields / data / date 等鍵）。
        """
        self._throttle()
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
        if "json" not in content_type.lower():
            logger.warning(f"{url} 回傳非 JSON（{content_type[:40]}），略過")
            return {}
        return resp.json()

    def fetch_daily_all(self) -> pl.DataFrame:
        """
        抓全市場日線，快取到 daily_{trading_date}.parquet。

        檔名以「資料內 max(date)」為錨點（TWSE 自帶交易日判斷，會回最近一個
        交易日的全市場資料），而非執行當下的 today。週末跑會落到上週五的檔名，
        週一收盤前跑也仍是上週五，避免疊出多個檔名不同但內容相同的 cache。
        """
        # 先用 mtime 找最新的 daily_*.parquet；TTL 內就用，不打網
        latest_cache = self._latest_cache_file("daily_*.parquet")
        if latest_cache is not None and is_fresh(latest_cache, self.ttl_hours):
            logger.info(f"命中快取 {latest_cache}")
            return load_parquet(latest_cache)

        data = self._get("/exchangeReport/STOCK_DAY_ALL")
        df = _parse_daily_all(data)
        if df.is_empty():
            logger.warning("STOCK_DAY_ALL 回傳空資料")
            return df

        max_date = df["date"].max()
        date_str = max_date.strftime("%Y%m%d")
        cache_file = self.cache_dir / f"daily_{date_str}.parquet"
        save_parquet(df, cache_file)
        return df

    def fetch_institutional(self) -> pl.DataFrame:
        """
        抓三大法人（legacy T86）。query date 與檔名都以 latest_trading_date() 為錨點。

        非交易日跑會自動對齊上一個交易日，避免空查詢與檔名混亂。
        """
        trading_date = self.latest_trading_date()
        if trading_date is None:
            logger.warning("fetch_institutional: 無法取得 trading_date，跳過 T86 抓取")
            return pl.DataFrame(
                schema={
                    "date": pl.Date,
                    "stock_id": pl.Utf8,
                    "stock_name": pl.Utf8,
                    "foreign_net": pl.Int64,
                    "trust_net": pl.Int64,
                    "dealer_net": pl.Int64,
                    "total_net": pl.Int64,
                }
            )

        date_str = trading_date.strftime("%Y%m%d")
        cache_file = self.cache_dir / f"institutional_{date_str}.parquet"
        if is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        url = (
            f"https://www.twse.com.tw/fund/T86?response=json"
            f"&date={date_str}&selectType=ALLBUT0999"
        )
        payload = self._get_legacy(url)
        df = _parse_institutional(payload)
        if not df.is_empty():
            save_parquet(df, cache_file)
        else:
            logger.warning(
                "T86 {} 回傳空資料（可能 TWSE 該日無資料，或 T86 尚未發布）",
                date_str,
            )
        return df

    def latest_trading_date(self) -> date | None:
        """
        回傳最近一個交易日（從 fetch_daily_all 的 max(date) 推）。

        供其他模組做為 trading_date 唯一錨點：檔名 / week_tag / screened_at 都來自此。
        若無法取得（沒網又沒 cache，或 TWSE 回空）回 None，呼叫端決定 fallback。
        """
        df = self.fetch_daily_all()
        if df.is_empty() or "date" not in df.columns:
            return None
        return df["date"].max()

    def _latest_cache_file(self, pattern: str) -> Path | None:
        """回傳 cache_dir 內符合 pattern 的最新 mtime 檔案；無則 None。"""
        files = sorted(
            self.cache_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

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

    def fetch_listed_industry(self) -> pl.DataFrame:
        """抓上市公司基本資料（產業別），快取到 industry_YYYYMM.parquet（月更新）。"""
        ym = date.today().strftime("%Y%m")
        cache_file = self.cache_dir / f"industry_{ym}.parquet"
        if is_fresh(cache_file, self.ttl_hours * 30):  # 30 倍 TTL ≈ 每月更新
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)
        data = self._get("/opendata/t187ap03_L")
        df = _parse_listed_industry(data)
        if not df.is_empty():
            save_parquet(df, cache_file)
        else:
            logger.warning("t187ap03_L 回傳空資料，產業別分類無法取得")
        return df

    def fetch_otc_industry(self) -> pl.DataFrame:
        """抓上櫃公司產業別（ISIN 一覽表），快取到 otc_industry_YYYYMM.parquet（月更新）。"""
        ym = date.today().strftime("%Y%m")
        cache_file = self.cache_dir / f"otc_industry_{ym}.parquet"
        if is_fresh(cache_file, self.ttl_hours * 30):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)
        try:
            elapsed = time.monotonic() - self._last_req
            if elapsed < self.interval_sec:
                time.sleep(self.interval_sec - elapsed)
            r = httpx.get(_ISIN_OTC_URL, headers={"User-Agent": self.user_agent}, timeout=20)
            self._last_req = time.monotonic()
            html = r.content.decode("ms950", errors="replace")
        except Exception as e:
            logger.warning(f"fetch_otc_industry 網路錯誤：{e}")
            _empty: dict[str, type[pl.DataType]] = {
                "stock_id": pl.Utf8, "stock_name": pl.Utf8,
                "industry_code": pl.Utf8, "industry_name": pl.Utf8,
            }
            return pl.DataFrame(schema=_empty)
        df = _parse_otc_industry(html)
        if not df.is_empty():
            save_parquet(df, cache_file)
            logger.info(f"上櫃產業別：{len(df)} 檔")
        else:
            logger.warning("ISIN 上櫃一覽表解析結果為空")
        return df

    def fetch_stock_history(self, stock_id: str, months: int = 3) -> pl.DataFrame:
        """
        抓單檔近 N 個月的 OHLCV 歷史，自動分派 TWSE / TPEX。

        - OTC 股（in `_load_otc_ids()`）走 TPEX `tradingStock` 端點
        - 其餘走 TWSE `STOCK_DAY` 端點
        - 兩者共用 cache：stock_day_{stock_id}_{YYYYMM}.parquet（下游無感）
        - 兩者共用 schema：11 欄、單位皆為股 / 元（TPEX 抓回後 ×1000 對齊）
        """
        if stock_id in self._load_otc_ids():
            return self.fetch_stock_history_tpex(stock_id, months)
        return self._fetch_stock_history_twse(stock_id, months)

    def _fetch_stock_history_twse(self, stock_id: str, months: int = 3) -> pl.DataFrame:
        """
        TWSE 上市股 OHLCV 歷史（legacy STOCK_DAY 端點）。
        快取到 stock_day_{stock_id}_{YYYYMM}.parquet：
          - 過去月份永久快取（資料不再變動）
          - 當月用 ttl_hours 過期（資料每日新增）
        """
        _empty_schema = _STOCK_DAY_SCHEMA
        today = date.today()
        current_ym = today.strftime("%Y%m")

        # Fast path：所有月份的 cache 都齊全 → 一次讀完，靜默回傳
        expected_files: list[Path] = []
        all_cached = True
        for n in range(months):
            target = _months_back(today, n)
            ym = target.strftime("%Y%m")
            cf = self.cache_dir / f"stock_day_{stock_id}_{ym}.parquet"
            if cf.exists() and (ym != current_ym or is_fresh(cf, self.ttl_hours)):
                expected_files.append(cf)
            else:
                all_cached = False
                break

        if all_cached and expected_files:
            logger.debug(
                "stock_day {} cache hit ({} months)", stock_id, len(expected_files)
            )
            frames = [pl.read_parquet(f) for f in expected_files]
            return pl.concat(frames).unique(subset=["date"]).sort("date")

        # Slow path：逐月檢查 + 必要時打網
        frames: list[pl.DataFrame] = []
        consecutive_empty = 0

        for n in range(months):
            target = _months_back(today, n)
            ym = target.strftime("%Y%m")
            cache_file = self.cache_dir / f"stock_day_{stock_id}_{ym}.parquet"

            # 過去月份永久快取；當月用 ttl 控制
            if cache_file.exists() and (ym != current_ym or is_fresh(cache_file, self.ttl_hours)):
                logger.info(f"命中快取 {cache_file}")
                frames.append(load_parquet(cache_file))
                consecutive_empty = 0
                continue

            url = (
                f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json"
                f"&date={ym}01&stockNo={stock_id}"
            )
            payload = self._get_legacy(url)
            df = _parse_stock_day(payload, stock_id)
            if not df.is_empty():
                save_parquet(df, cache_file)
                frames.append(df)
                consecutive_empty = 0
            else:
                logger.warning(f"STOCK_DAY {stock_id} {ym} 空資料（可能未上市或休市）")
                consecutive_empty += 1
                # 連續 2 個月空 → 該股很可能是上櫃股或下市，TWSE STOCK_DAY 不收
                # 提早終止，避免每檔浪費 4-5 次無效 API 呼叫
                if consecutive_empty >= 2:
                    logger.info(
                        f"STOCK_DAY {stock_id} 連續 {consecutive_empty} 月空資料，跳過剩餘月份"
                    )
                    break

        if not frames:
            return pl.DataFrame(schema=_empty_schema)
        return pl.concat(frames).unique(subset=["date"]).sort("date")

    def fetch_stock_history_tpex(self, stock_id: str, months: int = 3) -> pl.DataFrame:
        """
        TPEX 上櫃股 OHLCV 歷史（tradingStock 端點）。

        - URL: https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={sid}&date=YYYY/MM/01
        - Cache 共用 stock_day_{stock_id}_{YYYYMM}.parquet（與 TWSE 同 schema）
        - 同樣有「連續 2 月空就 break」邏輯
        """
        _empty_schema = _STOCK_DAY_SCHEMA
        today = date.today()
        current_ym = today.strftime("%Y%m")

        # Fast path：所有月份 cache 齊全 → 一次讀完
        expected_files: list[Path] = []
        all_cached = True
        for n in range(months):
            target = _months_back(today, n)
            ym = target.strftime("%Y%m")
            cf = self.cache_dir / f"stock_day_{stock_id}_{ym}.parquet"
            if cf.exists() and (ym != current_ym or is_fresh(cf, self.ttl_hours)):
                expected_files.append(cf)
            else:
                all_cached = False
                break

        if all_cached and expected_files:
            logger.debug(
                "stock_day {} cache hit ({} months, TPEX)", stock_id, len(expected_files)
            )
            frames = [pl.read_parquet(f) for f in expected_files]
            return pl.concat(frames).unique(subset=["date"]).sort("date")

        # Slow path
        frames: list[pl.DataFrame] = []
        consecutive_empty = 0

        for n in range(months):
            target = _months_back(today, n)
            ym = target.strftime("%Y%m")
            cache_file = self.cache_dir / f"stock_day_{stock_id}_{ym}.parquet"

            if cache_file.exists() and (ym != current_ym or is_fresh(cache_file, self.ttl_hours)):
                logger.info(f"命中快取 {cache_file}")
                frames.append(load_parquet(cache_file))
                consecutive_empty = 0
                continue

            date_param = target.strftime("%Y/%m/01")
            url = f"{_TPEX_BASE}{_TPEX_STOCK_DAY_PATH}?code={stock_id}&date={date_param}"
            payload = self._get_legacy(url)
            df = _parse_tpex_stock_day(payload, stock_id)
            if not df.is_empty():
                save_parquet(df, cache_file)
                frames.append(df)
                consecutive_empty = 0
            else:
                logger.warning(f"TPEX tradingStock {stock_id} {ym} 空資料")
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    logger.info(
                        f"TPEX {stock_id} 連續 {consecutive_empty} 月空資料，跳過剩餘月份"
                    )
                    break

        if not frames:
            return pl.DataFrame(schema=_empty_schema)
        return pl.concat(frames).unique(subset=["date"]).sort("date")

    def fetch_stock_ohlcv(self, stock_id: str, n_days: int = 60) -> pl.DataFrame:
        """
        取單檔近 n_days 個交易日的 OHLCV。
        優先讀 stock_day_{stock_id}_*.parquet（由 fetch_stock_history 累積），
        缺資料時 fallback 到 daily_*.parquet（全市場快取，由 fetch_daily_all 累積）。
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
        # 優先：單檔月快取
        stock_day_files = sorted(self.cache_dir.glob(f"stock_day_{stock_id}_*.parquet"))
        stock_day_frames: list[pl.DataFrame] = []
        for f in stock_day_files:
            try:
                stock_day_frames.append(pl.read_parquet(f))
            except Exception as e:
                logger.warning(f"讀取 {f} 失敗：{e}")

        # 補充：全市場日快取（包含 name 等欄位）
        daily_files = sorted(self.cache_dir.glob("daily_*.parquet"))
        daily_frames: list[pl.DataFrame] = []
        for f in daily_files:
            try:
                df = pl.read_parquet(f)
                if "stock_id" in df.columns and "date" in df.columns:
                    filtered = df.filter(pl.col("stock_id") == stock_id)
                    if not filtered.is_empty():
                        # daily_*.parquet 多了 name 欄位，drop 後合併
                        if "name" in filtered.columns:
                            filtered = filtered.drop("name")
                        daily_frames.append(filtered)
            except Exception as e:
                logger.warning(f"讀取 {f} 失敗：{e}")

        all_frames = stock_day_frames + daily_frames
        if not all_frames:
            return pl.DataFrame(schema=_empty_schema)
        return pl.concat(all_frames).unique(subset=["date"]).sort("date").tail(n_days)

    def load_candidate_history(
        self, stock_ids: list[str], n_days: int = 60
    ) -> pl.DataFrame:
        """
        合併多檔候選股的歷史 OHLCV，給族群動能計算用。

        資料來源優先序：
          1. stock_day_{stock_id}_*.parquet（個股月線，需先呼叫 fetch_stock_history 補抓）
          2. daily_*.parquet（全市場日線累積；含所有股票，可能只有少數幾天）

        回傳：含 stock_id / date / close 三欄，按 (stock_id, date) 排序、去重。
        """
        _empty_schema = {"stock_id": pl.Utf8, "date": pl.Date, "close": pl.Float64}
        if not stock_ids:
            return pl.DataFrame(schema=_empty_schema)

        stock_id_set = set(stock_ids)
        frames: list[pl.DataFrame] = []

        # 1. stock_day caches (one file per stock per month)
        for sid in stock_ids:
            for f in sorted(self.cache_dir.glob(f"stock_day_{sid}_*.parquet")):
                try:
                    df = pl.read_parquet(f)
                    if {"stock_id", "date", "close"}.issubset(df.columns):
                        frames.append(df.select(["stock_id", "date", "close"]))
                except Exception as e:
                    logger.warning(f"讀取 {f} 失敗：{e}")

        # 2. daily caches (all stocks; filter to candidates)
        for f in sorted(self.cache_dir.glob("daily_*.parquet")):
            try:
                df = pl.read_parquet(f)
                if not {"stock_id", "date", "close"}.issubset(df.columns):
                    continue
                df = df.filter(pl.col("stock_id").is_in(list(stock_id_set))).select(
                    ["stock_id", "date", "close"]
                )
                if not df.is_empty():
                    frames.append(df)
            except Exception as e:
                logger.warning(f"讀取 {f} 失敗：{e}")

        if not frames:
            return pl.DataFrame(schema=_empty_schema)

        merged = (
            pl.concat(frames)
            .unique(subset=["stock_id", "date"])
            .sort(["stock_id", "date"])
        )
        # 限制每檔最多 n_days 日（用 tail 在每檔內取最近 n_days）
        merged = merged.group_by("stock_id", maintain_order=True).tail(n_days)
        return merged

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
