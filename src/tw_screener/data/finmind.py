"""FinMind 開源 API 抓取＋快取（docs/31 §20.14 估值歷史深度整合）。

比照 fred.py 形狀：純函式解析（不含 IO，方便測試）＋FinMindClient（節流／重試／快取）＋
create_client 工廠。每次請求回傳某 dataset＋stock_id 的**全歷史**（非增量），故快取粒度
是 per-(dataset,stock_id) 整檔覆蓋、24h TTL，沿用既有 cache.is_fresh 慣例。

Phase 1（§20.14）：`TaiwanStockPER`（2005-10 起逐日 PE/PBR/殖利率）補自身估值歷史腿的深度——
TWSE `BWIBBU_d`／TPEX `peratio` 皆「只回最新一交易日、不可回補」，`valuation_ratios_*`
快取從 2026-06-12 才起累。`load_merged_valuation_history()` 把 FinMind 深度歷史與 TWSE
逐日快照合併，重疊日 TWSE 勝（§20.14 權威規則：當前橫斷面一律 TWSE，FinMind 只補歷史）。

Phase 2（§20.15，M-Val-FinMind2）：3 個長格式財報 dataset（CashFlowsStatement／
FinancialStatements／BalanceSheet）餵 `analysis/dcf.py` 的機械式 DCF。`_parse_finmind_long`
共用核心＋`fetch_cashflows/financials/balancesheet` 具名 wrapper＋`load_*_history`。parser
保持笨（不 de-cumulate／不 TTM），語意判斷全在 dcf.py。

M-Pick2（docs/32）：`TaiwanStockMonthRevenue` 月營收全歷史（寬表，形狀同 PER）餵族群內個股
因子研究；`fetch_month_revenue`＋`load_month_revenue_history`，不接 make week。

合規（鐵律 1 精神外推；官方開放資料、門檻比 Goodinfo 鬆）：concurrency=1、請求間隔
≥ settings.finmind.request_interval_sec、同 (dataset,stock_id) 24h 快取、連錯 3 次停。
token 選填（未註冊 300 req/hr、註冊 600）；用 httpx 直打、不裝 finmind pip 套件。
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import polars as pl
import yaml
from loguru import logger

from .cache import is_fresh, load_parquet, save_parquet

if TYPE_CHECKING:  # 只為型別標註，避免匯入循環（twse 也 import 本模組時）
    from .twse import TWSEClient

# dataset 名字串屬 API 契約 → 留在模組常數（比照 twse.py 端點路徑、settings.yaml 註解例外）
_DATASET_TAIWAN_STOCK_PER = "TaiwanStockPER"

# 欄名對齊 twse._VALUATION_RATIOS_SCHEMA 的 pe/pbr/dividend_yield（省 market 欄——
# FinMind 無此欄，load_merged_valuation_history 併表時補 None，自身腿不需要 market）
_PER_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "stock_id": pl.Utf8,
    "pe": pl.Float64,
    "pbr": pl.Float64,
    "dividend_yield": pl.Float64,
}

# ── M-Pick2（docs/32）：月營收（寬表，同 PER 形狀）──────────────────────────────
# 實測（2026-09-28，2330）：`{date, stock_id, country, revenue, revenue_month, revenue_year,
# create_time}`；`date`＝營收月的**次月 1 日**（2026-08 營收 → 2026-09-01）；`create_time`
# 近期列＝實際公告日、舊列為空字串；revenue 單位＝元（當月、非累計）。
_DATASET_MONTH_REVENUE = "TaiwanStockMonthRevenue"
_MONTH_REVENUE_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "year": pl.Int64,
    "month": pl.Int64,
    "revenue": pl.Float64,
    "create_date": pl.Date,
}


# ── Phase 2（M-Val-FinMind2）：財報 / 現金流 / 資產負債 3 個長格式 dataset ──────────
# 這 3 個 dataset 回傳 `{date, stock_id, type, value, origin_name}` 長格式（PER 是寬表，
# 例外）。date 是**日曆季末** `YYYY-MM-DD`（非 ROC，不 +1911）→ quarter=(月-1)//3+1。
# parser 保持笨：照抄所有季別，不 de-cumulate／不 TTM／不挑 FY——那些邏輯在 analysis/dcf.py。
#   - CashFlows：值為**累計 YTD**（Q4 列＝全年），2012Q1 起
#   - Financials：值為**單季**（FinMind 已 de-cumulate），4 季加總＝全年
#   - BalanceSheet：季末快照；另發 `<type>_per`（占總資產 %）列，field_map 未收 → 自然濾掉
_DATASET_CASHFLOWS = "TaiwanStockCashFlowsStatement"
_DATASET_FINANCIALS = "TaiwanStockFinancialStatements"
_DATASET_BALANCESHEET = "TaiwanStockBalanceSheet"

# type code → 短欄名。OCF 兩個等價 code（實測 2330/2317 逐筆相同）分別收成 ocf / ocf_alt，
# 由 dcf.py coalesce（parser 不做業務判斷）。capex `PropertyAndPlantAndEquipment` 為**負值**
# （「取得不動產、廠房及設備」現金流出）。
_CASHFLOWS_FIELD_MAP: dict[str, str] = {
    "CashFlowsFromOperatingActivities": "ocf",
    "NetCashInflowFromOperatingActivities": "ocf_alt",
    "PropertyAndPlantAndEquipment": "capex",
}
_FINANCIALS_FIELD_MAP: dict[str, str] = {
    "Revenue": "revenue",
    "OperatingIncome": "operating_income",
    "IncomeAfterTaxes": "income_after_tax",
    "EPS": "eps",
}
_BALANCESHEET_FIELD_MAP: dict[str, str] = {
    "CashAndCashEquivalents": "cash",
    "ShorttermBorrowings": "st_debt",
    "BondsPayable": "bonds_payable",
    "LongtermBorrowings": "lt_debt",
    "Equity": "equity",
    "Liabilities": "liabilities",
    "TotalAssets": "total_assets",
}


def _long_schema(cols: list[str]) -> dict[str, type[pl.DataType]]:
    """(stock_id, year, quarter) 主鍵 ＋ 各數值欄 Float64 的寬表 schema。"""
    s: dict[str, type[pl.DataType]] = {
        "stock_id": pl.Utf8,
        "year": pl.Int64,
        "quarter": pl.Int64,
    }
    for c in cols:
        s[c] = pl.Float64
    return s


_CASHFLOWS_WIDE_SCHEMA = _long_schema(["ocf", "ocf_alt", "capex"])
_FINANCIALS_WIDE_SCHEMA = _long_schema(
    ["revenue", "operating_income", "income_after_tax", "eps"]
)
_BALANCESHEET_WIDE_SCHEMA = _long_schema(
    ["cash", "st_debt", "bonds_payable", "lt_debt", "equity", "liabilities", "total_assets"]
)


def _to_float_or_none(raw: Any, *, drop_non_positive: bool) -> float | None:
    """轉 float；無法轉或（drop_non_positive 時）值 ≤ 0 → None（誠實原則，不當 0）。

    FinMind 對虧損股寫 `PER = 0.0`（非 null、非負），對缺殖利率的股票也可能寫 0 →
    一律映射成 None，語意對齊 twse `_parse_valuation_ratios` 的「缺值→null 不當 0」，
    也讓 `_self_history_median_generic` 的 `> 0` 過濾不會被 0 稀釋中位數。
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if drop_non_positive and v <= 0:
        return None
    return v


def _parse_taiwan_stock_per(payload: dict[str, Any]) -> pl.DataFrame:
    """解析 FinMind `TaiwanStockPER` JSON → (date, stock_id, pe, pbr, dividend_yield)。

    回傳是**寬表**：`{"date","stock_id","dividend_yield","PER","PBR"}`（實測確認，
    非 type/value 長格式——長格式只出現在財報/現金流 dataset）。
    虧損股 `PER=0.0` → pe=None；PBR/殖利率 ≤ 0 → None。無法解析的列整列略過（warn 不 raise）。
    """
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for r in data:
        try:
            d = date.fromisoformat(str(r["date"]))
            sid = str(r["stock_id"]).strip()
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"略過無效 FinMind PER 列：{r!r} — {e}")
            continue
        if not sid:
            logger.warning(f"略過無 stock_id 的 FinMind PER 列：{r!r}")
            continue
        rows.append({
            "date": d,
            "stock_id": sid,
            "pe": _to_float_or_none(r.get("PER"), drop_non_positive=True),
            "pbr": _to_float_or_none(r.get("PBR"), drop_non_positive=True),
            "dividend_yield": _to_float_or_none(
                r.get("dividend_yield"), drop_non_positive=True
            ),
        })
    return pl.DataFrame(rows, schema=_PER_SCHEMA)


def _parse_month_revenue(payload: dict[str, Any]) -> pl.DataFrame:
    """解析 FinMind `TaiwanStockMonthRevenue` JSON → (stock_id, year, month, revenue, create_date)。

    year/month＝營收所屬月（`revenue_year`/`revenue_month`；`date` 是次月 1 日，不用）。
    revenue 照抄（0／負值不改，分母判斷交給消費端）；create_time 空字串或無法解析 → null
    （舊列 FinMind 不給公告日——point-in-time 由消費端依法定期限推，不從此欄臆造）。
    同 (stock_id, year, month) 重複 → 保留最後一筆；無法解析的列略過（warn 不 raise）。
    """
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for r in data:
        try:
            sid = str(r["stock_id"]).strip()
            year = int(r["revenue_year"])
            month = int(r["revenue_month"])
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"略過無效 FinMind 月營收列：{r!r} — {e}")
            continue
        if not sid or not 1 <= month <= 12:
            logger.warning(f"略過無 stock_id 或月份越界的 FinMind 月營收列：{r!r}")
            continue
        created: date | None = None
        raw_ct = str(r.get("create_time") or "").strip()
        if raw_ct:
            try:
                created = date.fromisoformat(raw_ct[:10])
            except ValueError:
                created = None
        rows.append({
            "stock_id": sid,
            "year": year,
            "month": month,
            "revenue": _to_float_or_none(r.get("revenue"), drop_non_positive=False),
            "create_date": created,
        })
    if not rows:
        return pl.DataFrame(schema=_MONTH_REVENUE_SCHEMA)
    return (
        pl.DataFrame(rows, schema=_MONTH_REVENUE_SCHEMA)
        .unique(subset=["stock_id", "year", "month"], keep="last", maintain_order=True)
        .sort(["stock_id", "year", "month"])
    )


def _parse_finmind_long(
    payload: dict[str, Any],
    field_map: dict[str, str],
    schema: dict[str, type[pl.DataType]],
) -> pl.DataFrame:
    """解析 FinMind 長格式 `{date,stock_id,type,value}` → (stock_id, year, quarter, 寬欄…)。

    `field_map` 沒有的 `type` 整列略過（`<type>_per` 占比列因此自然被濾掉）。
    `date` 解析失敗或無 `stock_id` 的列 warn 後略過（不 raise）。同一 (stock_id,year,quarter)
    的多個 type 併進同一列；沒出現的欄留 None（誠實，不當 0）。空 → `pl.DataFrame(schema=schema)`。
    """
    data = payload.get("data") or []
    rec: dict[tuple[str, int, int], dict[str, float | None]] = {}
    for r in data:
        col = field_map.get(str(r.get("type")))
        if col is None:
            continue
        try:
            d = date.fromisoformat(str(r["date"]))
            sid = str(r["stock_id"]).strip()
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"略過無效 FinMind 長格式列：{r!r} — {e}")
            continue
        if not sid:
            logger.warning(f"略過無 stock_id 的 FinMind 長格式列：{r!r}")
            continue
        key = (sid, d.year, (d.month - 1) // 3 + 1)
        rec.setdefault(key, {})[col] = _to_float_or_none(
            r.get("value"), drop_non_positive=False
        )
    if not rec:
        return pl.DataFrame(schema=schema)
    base = {c: None for c in schema}
    rows = [
        {**base, "stock_id": k[0], "year": k[1], "quarter": k[2], **v}
        for k, v in rec.items()
    ]
    return pl.DataFrame(rows, schema=schema).sort(["stock_id", "year", "quarter"])


def _load_finmind_token(dotenv_path: Path = Path(".env")) -> str | None:
    """讀 FinMind token：優先環境變數 `FINMIND_TOKEN`，否則解析 .env（不進 git，不印出／不 log）。

    專案沒有 python-dotenv 依賴（鐵律 4）→ 手刻極簡 KEY=VALUE 解析，比照 _load_fred_api_key。
    **與 FRED 不同**：FinMind 未註冊也能用（300 req/hr），token 缺席不是錯誤 → 回 None、
    create_client 不 raise。
    """
    v = os.environ.get("FINMIND_TOKEN")
    if v:
        return v
    if not dotenv_path.exists():
        return None
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip().lower() == "finmind_token":
            return val.strip().strip('"').strip("'") or None
    return None


class FinMindClient:
    """FinMind 開源 API 同步 client（含本地 parquet 快取，per-(dataset,stock_id)）。"""

    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        ttl_hours: float,
        user_agent: str,
        interval_sec: float,
        token: str | None,
        max_retries: int = 2,
        timeout_sec: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.ttl_hours = ttl_hours
        self.user_agent = user_agent
        self.interval_sec = interval_sec
        self.token = token
        self.max_retries = max_retries
        self.timeout_sec = timeout_sec
        self._last_req: float = 0.0
        # 上一次 fetch_* 是否為「請求失敗」（HTTP error／額度用盡／status 非 200）。
        # 用來讓斷路器只對真正的失敗計數——FinMind 對「這檔沒 PER 資料」是正常回
        # `{"data":[]}`（HTTP 200），那不是異常、不該觸發停止（如興櫃新股整群）。
        self.last_request_failed: bool = False

    def _throttle(self) -> None:
        """限速：確保連續請求間隔 ≥ interval_sec（鐵律 1 精神外推到 FinMind）。"""
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)

    def _request(
        self, dataset: str, data_id: str, start_date: str
    ) -> dict[str, Any] | None:
        """限速＋指數退避地 GET 單一 (dataset, data_id) 全歷史，回傳解析後 JSON dict；失敗回 None。

        失敗涵蓋：HTTP error（含 402 額度用盡）、回傳非 JSON、body `status` 非 200
        （FinMind 額度用盡時 HTTP 402 + body status 402、或參數錯 HTTP 400）——三者
        對呼叫端而言都是「這次沒拿到可用資料」，交給 fetch_all_per 的斷路器計數。
        """
        params: dict[str, str] = {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start_date,
        }
        if self.token:
            params["token"] = self.token
        for attempt in range(self.max_retries + 1):
            self._throttle()
            if attempt == 0:
                logger.info(f"FinMind GET {dataset} {data_id}")
            else:
                logger.info(
                    f"FinMind GET {dataset} {data_id}（retry {attempt}/{self.max_retries}）"
                )
            try:
                resp = httpx.get(
                    self.base_url,
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout_sec,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.warning(f"FinMind {dataset} {data_id} HTTP error: {e}")
                self._last_req = time.monotonic()
                if attempt < self.max_retries:
                    time.sleep(5 * (2**attempt))
                    continue
                return None
            self._last_req = time.monotonic()
            try:
                payload = resp.json()
            except ValueError:
                logger.warning(f"FinMind {dataset} {data_id} 回傳非 JSON")
                return None
            if not isinstance(payload, dict):
                return None
            status = payload.get("status")
            if status not in (200, None):
                logger.warning(
                    f"FinMind {dataset} {data_id} status={status}：{payload.get('msg')}"
                )
                return None
            return payload
        return None

    def fetch_taiwan_stock_per(
        self, stock_id: str, start_date: str = "2005-01-01", force: bool = False
    ) -> pl.DataFrame:
        """抓單檔 TaiwanStockPER 全歷史，快取到 per_{stock_id}.parquet（24h TTL）。

        抓取失敗且有舊快取 → 回退舊快取並警告（寧可用稍舊資料）；抓取失敗且無舊快取，
        或解析後為空 → 回空表（schema=_PER_SCHEMA），呼叫端自行判斷。
        force=True（CLI `--force`）→ 略過 TTL、強制重打。每次回全歷史 → 命中即整檔覆寫。
        """
        self.last_request_failed = False
        cache_file = self.cache_dir / f"per_{stock_id}.parquet"
        if not force and is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        payload = self._request(_DATASET_TAIWAN_STOCK_PER, stock_id, start_date)
        if payload is None:
            self.last_request_failed = True
            if cache_file.exists():
                logger.warning(
                    f"FinMind PER {stock_id} 抓取失敗，回退舊快取（可能已過 TTL）"
                )
                return load_parquet(cache_file)
            logger.warning(f"FinMind PER {stock_id} 抓取失敗，且無舊快取")
            return pl.DataFrame(schema=_PER_SCHEMA)

        df = _parse_taiwan_stock_per(payload)
        if df.is_empty():
            # `{"data":[]}`＝FinMind 沒有這檔的 PER（興櫃新股等），HTTP 200、非異常
            logger.info(f"FinMind PER {stock_id} 無資料（data=[]）")
            return df
        save_parquet(df.sort("date"), cache_file)
        return df

    def fetch_all_per(
        self,
        stock_ids: list[str],
        start_date: str = "2005-01-01",
        force: bool = False,
    ) -> dict[str, pl.DataFrame]:
        """依序抓多檔 PER（concurrency=1，鐵律 1 精神），連續**請求失敗** 3 次即停止後續。

        已成功的仍回傳；停止後尚未輪到的改讀舊快取（不因中途停而整批放棄），無舊快取回空表。
        「連續失敗」＝`last_request_failed`（HTTP error／額度用盡／status 非 200）——**不**
        包含 `{"data":[]}`（FinMind 沒這檔的 PER，正常）。force=True 見 fetch_taiwan_stock_per。
        """
        result: dict[str, pl.DataFrame] = {}
        consecutive_errors = 0
        stopped = False
        for sid in stock_ids:
            if stopped:
                cache_file = self.cache_dir / f"per_{sid}.parquet"
                result[sid] = (
                    load_parquet(cache_file)
                    if cache_file.exists()
                    else pl.DataFrame(schema=_PER_SCHEMA)
                )
                continue
            df = self.fetch_taiwan_stock_per(sid, start_date=start_date, force=force)
            result[sid] = df
            if self.last_request_failed:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    logger.warning(
                        "FinMind 連續 3 次請求失敗，停止後續抓取（鐵律 1 精神）"
                    )
                    stopped = True
            else:
                consecutive_errors = 0
        return result

    # ── M-Pick2（docs/32）：月營收 ───────────────────────────────────────────────
    def fetch_month_revenue(
        self, stock_id: str, start_date: str = "2019-01-01", force: bool = False
    ) -> pl.DataFrame:
        """抓單檔 TaiwanStockMonthRevenue 全歷史 → month_revenue_{stock_id}.parquet（同 PER 抓法）。

        失敗且有舊快取 → 回退舊快取；失敗且無舊快取，或 `{"data":[]}` → 回空表
        （schema=_MONTH_REVENUE_SCHEMA）。`last_request_failed` 語意同 fetch_taiwan_stock_per。
        """
        self.last_request_failed = False
        cache_file = self.cache_dir / f"month_revenue_{stock_id}.parquet"
        if not force and is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        payload = self._request(_DATASET_MONTH_REVENUE, stock_id, start_date)
        if payload is None:
            self.last_request_failed = True
            if cache_file.exists():
                logger.warning(f"FinMind 月營收 {stock_id} 抓取失敗，回退舊快取")
                return load_parquet(cache_file)
            logger.warning(f"FinMind 月營收 {stock_id} 抓取失敗，且無舊快取")
            return pl.DataFrame(schema=_MONTH_REVENUE_SCHEMA)

        df = _parse_month_revenue(payload)
        if df.is_empty():
            logger.info(f"FinMind 月營收 {stock_id} 無資料（data=[]）")
            return df
        save_parquet(df, cache_file)
        return df

    # ── Phase 2：財報 / 現金流 / 資產負債（M-Val-FinMind2）─────────────────────
    def _fetch_dataset(
        self,
        dataset: str,
        prefix: str,
        schema: dict[str, type[pl.DataType]],
        field_map: dict[str, str],
        stock_id: str,
        start_date: str,
        force: bool,
    ) -> pl.DataFrame:
        """抓單檔長格式 dataset 全歷史 → `{prefix}_{stock_id}.parquet`（比照 PER 抓法）。

        抓取失敗且有舊快取 → 回退舊快取；失敗且無舊快取，或解析後為空 → 回空表。
        `last_request_failed` 只由「請求失敗」（HTTP error／額度／status 非 200）設 True，
        `{"data":[]}`（FinMind 沒這檔的該 dataset）不算。
        """
        self.last_request_failed = False
        cache_file = self.cache_dir / f"{prefix}_{stock_id}.parquet"
        if not force and is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        payload = self._request(dataset, stock_id, start_date)
        if payload is None:
            self.last_request_failed = True
            if cache_file.exists():
                logger.warning(f"FinMind {dataset} {stock_id} 抓取失敗，回退舊快取")
                return load_parquet(cache_file)
            logger.warning(f"FinMind {dataset} {stock_id} 抓取失敗，且無舊快取")
            return pl.DataFrame(schema=schema)

        df = _parse_finmind_long(payload, field_map, schema)
        if df.is_empty():
            logger.info(f"FinMind {dataset} {stock_id} 無資料（data=[]）")
            return df
        save_parquet(df, cache_file)
        return df

    def fetch_cashflows(
        self, stock_id: str, start_date: str = "2013-01-01", force: bool = False
    ) -> pl.DataFrame:
        """單檔現金流量表 → (stock_id, year, quarter, ocf, ocf_alt, capex)；值為累計 YTD。"""
        return self._fetch_dataset(
            _DATASET_CASHFLOWS, "cashflow", _CASHFLOWS_WIDE_SCHEMA,
            _CASHFLOWS_FIELD_MAP, stock_id, start_date, force,
        )

    def fetch_financials(
        self, stock_id: str, start_date: str = "2013-01-01", force: bool = False
    ) -> pl.DataFrame:
        """單檔財報 → (…, revenue, operating_income, income_after_tax, eps)；值為單季。"""
        return self._fetch_dataset(
            _DATASET_FINANCIALS, "financials", _FINANCIALS_WIDE_SCHEMA,
            _FINANCIALS_FIELD_MAP, stock_id, start_date, force,
        )

    def fetch_balancesheet(
        self, stock_id: str, start_date: str = "2013-01-01", force: bool = False
    ) -> pl.DataFrame:
        """單檔資產負債表 → cash / st_debt / bonds_payable / lt_debt / equity / total_assets …。"""
        return self._fetch_dataset(
            _DATASET_BALANCESHEET, "balancesheet", _BALANCESHEET_WIDE_SCHEMA,
            _BALANCESHEET_FIELD_MAP, stock_id, start_date, force,
        )


def _load_finmind_wide_history(
    cache_dir: Path, prefix: str, schema: dict[str, type[pl.DataType]]
) -> pl.DataFrame:
    """純讀**全部**已回補的 `{prefix}_*.parquet`（不打網）；無快取回空表（schema=schema）。"""
    files = sorted(Path(cache_dir).glob(f"{prefix}_*.parquet"))
    if not files:
        return pl.DataFrame(schema=schema)
    frames = [load_parquet(f) for f in files]
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(subset=["stock_id", "year", "quarter"], keep="last")
        .sort(["stock_id", "year", "quarter"])
    )


def load_cashflow_history(cache_dir: Path) -> pl.DataFrame:
    """全部 cashflow_*.parquet（M-Val-FinMind2）。"""
    return _load_finmind_wide_history(cache_dir, "cashflow", _CASHFLOWS_WIDE_SCHEMA)


def load_financials_history(cache_dir: Path) -> pl.DataFrame:
    """全部 financials_*.parquet（M-Val-FinMind2）。"""
    return _load_finmind_wide_history(cache_dir, "financials", _FINANCIALS_WIDE_SCHEMA)


def load_balancesheet_history(cache_dir: Path) -> pl.DataFrame:
    """全部 balancesheet_*.parquet（M-Val-FinMind2）。"""
    return _load_finmind_wide_history(
        cache_dir, "balancesheet", _BALANCESHEET_WIDE_SCHEMA
    )


def load_month_revenue_history(cache_dir: Path) -> pl.DataFrame:
    """純讀**全部**已回補的 month_revenue_*.parquet（不打網；M-Pick2）；無快取回空表。"""
    files = sorted(Path(cache_dir).glob("month_revenue_*.parquet"))
    if not files:
        return pl.DataFrame(schema=_MONTH_REVENUE_SCHEMA)
    frames = [load_parquet(f) for f in files]
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(subset=["stock_id", "year", "month"], keep="last")
        .sort(["stock_id", "year", "month"])
    )


def load_finmind_per_history(cache_dir: Path) -> pl.DataFrame:
    """純讀**全部**已回補的 per_*.parquet（不打網）；無快取回空表（schema=_PER_SCHEMA）。"""
    files = sorted(Path(cache_dir).glob("per_*.parquet"))
    if not files:
        return pl.DataFrame(schema=_PER_SCHEMA)
    frames = [load_parquet(f) for f in files]
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(subset=["stock_id", "date"], keep="last")
        .sort(["stock_id", "date"])
    )


def load_merged_valuation_history(
    twse_client: TWSEClient, finmind_cache_dir: Path
) -> pl.DataFrame:
    """TWSE 逐日快照（權威、近端）＋ FinMind PER 歷史（深度）合併成單一長表，供
    `compute_self_history_*` / `compute_self_history_pctile`。

    dedup key `(stock_id, date)`；重疊日 **TWSE 勝**（§20.14 權威規則：當前橫斷面一律
    TWSE，FinMind 只補歷史深度）。`finmind_cache_dir` 不存在／無檔 → 退回純 TWSE 行為
    （safe fallback，1a 未回補時或 FinMind 全掛時主流程不受影響）。
    FinMind 無 `market` 欄 → 併表時補 None（自身腿不使用 market）。

    **FinMind 資料封頂在 TWSE 最新快照日**：FinMind 更新頻率可能快過本地 TWSE 快取，
    未封頂會讓 merged 出現 TWSE 沒有的「未來」交易日 → 汙染任何用 `date.max()` 當
    「最新橫斷面日」的下游（如 `iso_week_snapshot_dates` 的面板重播錨點）。權威規則
    也要求「當前」一律走 TWSE，故 FinMind 只補 TWSE 已涵蓋範圍內的歷史深度。
    """
    twse_hist = twse_client.load_valuation_ratios_history()
    fm_hist = load_finmind_per_history(finmind_cache_dir)
    if fm_hist.is_empty():
        return twse_hist
    if not twse_hist.is_empty():
        twse_max = twse_hist["date"].max()
        fm_hist = fm_hist.filter(pl.col("date") <= twse_max)
    # TWSE 放後面 → unique(keep="last") 讓重疊日 TWSE 勝
    return (
        pl.concat([fm_hist, twse_hist], how="diagonal_relaxed")
        .unique(subset=["stock_id", "date"], keep="last")
        .sort(["stock_id", "date"])
    )


def create_client(settings_path: Path = Path("config/settings.yaml")) -> FinMindClient:
    """從 settings.yaml 建立 FinMindClient；token 讀環境變數或 .env（選填，缺席不 raise）。"""
    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    fm_cfg = settings["finmind"]
    paths = settings["paths"]
    token = _load_finmind_token()
    if not token:
        logger.info("未設 FinMind token，使用未註冊額度（300 req/hr）")
    return FinMindClient(
        base_url=fm_cfg["base_url"],
        cache_dir=Path(paths["cache_dir"]) / "finmind",
        ttl_hours=float(fm_cfg["cache_ttl_hours"]),
        user_agent=fm_cfg["user_agent"],
        interval_sec=float(fm_cfg["request_interval_sec"]),
        token=token,
        max_retries=int(fm_cfg.get("max_retries", 2)),
        timeout_sec=float(fm_cfg.get("timeout_sec", 30.0)),
    )
