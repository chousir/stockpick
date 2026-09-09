"""FinMind 開源 API 抓取＋快取（docs/31 §20.14 估值歷史深度整合）。

比照 fred.py 形狀：純函式解析（不含 IO，方便測試）＋FinMindClient（節流／重試／快取）＋
create_client 工廠。每次請求回傳某 dataset＋stock_id 的**全歷史**（非增量），故快取粒度
是 per-(dataset,stock_id) 整檔覆蓋、24h TTL，沿用既有 cache.is_fresh 慣例。

目前只用 `TaiwanStockPER`（2005-10 起逐日 PE/PBR/殖利率）補自身估值歷史腿的深度——
TWSE `BWIBBU_d`／TPEX `peratio` 皆「只回最新一交易日、不可回補」，`valuation_ratios_*`
快取從 2026-06-12 才起累。`load_merged_valuation_history()` 把 FinMind 深度歷史與 TWSE
逐日快照合併，重疊日 TWSE 勝（§20.14 權威規則：當前橫斷面一律 TWSE，FinMind 只補歷史）。

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
        cache_file = self.cache_dir / f"per_{stock_id}.parquet"
        if not force and is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        payload = self._request(_DATASET_TAIWAN_STOCK_PER, stock_id, start_date)
        if payload is None:
            if cache_file.exists():
                logger.warning(
                    f"FinMind PER {stock_id} 抓取失敗，回退舊快取（可能已過 TTL）"
                )
                return load_parquet(cache_file)
            logger.warning(f"FinMind PER {stock_id} 抓取失敗，且無舊快取")
            return pl.DataFrame(schema=_PER_SCHEMA)

        df = _parse_taiwan_stock_per(payload)
        if df.is_empty():
            logger.warning(f"FinMind PER {stock_id} 解析後為空")
            return df
        save_parquet(df.sort("date"), cache_file)
        return df

    def fetch_all_per(
        self,
        stock_ids: list[str],
        start_date: str = "2005-01-01",
        force: bool = False,
    ) -> dict[str, pl.DataFrame]:
        """依序抓多檔 PER（concurrency=1，鐵律 1 精神），連續失敗／空結果達 3 次即停止後續。

        已成功的仍回傳；停止後尚未輪到的改讀舊快取（不因中途停而整批放棄），無舊快取回空表。
        「連續失敗」同時涵蓋 HTTP 失敗與解析後為空。force=True 見 fetch_taiwan_stock_per。
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
            if df.is_empty():
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    logger.warning(
                        "FinMind 連續 3 次抓取失敗／空結果，停止後續抓取（鐵律 1 精神）"
                    )
                    stopped = True
            else:
                consecutive_errors = 0
        return result


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
