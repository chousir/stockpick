"""FRED（Federal Reserve Economic Data）官方 API 抓取＋快取（docs/25 v2 macro_regime 模組）。

比照 twse.py 形狀：純函式解析（不含 IO，方便測試）＋FREDClient（節流／重試／快取）＋
create_client 工廠。每次抓取回傳序列全歷史（FRED 一次請求即回全部觀測值，非增量），
per-series parquet 快取、24h TTL，沿用既有 cache.is_fresh 慣例。
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import yaml
from loguru import logger

from .cache import is_fresh, load_parquet, save_parquet

_SERIES_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "value": pl.Float64,
}


def _parse_observations(payload: dict[str, Any]) -> pl.DataFrame:
    """解析 FRED `series/observations` JSON → (date, value) DataFrame。

    缺值（FRED 用字串 "." 表示，如序列該日未發布）→ value=None，不當 0（誠實原則，
    守 CLAUDE.md「資料沒有就寫未取得」）；該列仍保留（date 存在但 value 為 null），
    供下游區分「有序列但這天沒值」與「這天完全沒資料」。無法解析的觀測列整列略過。
    """
    observations = payload.get("observations") or []
    rows = []
    for o in observations:
        try:
            d = date.fromisoformat(str(o["date"]))
        except (KeyError, ValueError):
            continue
        raw = o.get("value", ".")
        value: float | None = None
        if raw not in (".", "", None):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = None
        rows.append({"date": d, "value": value})
    return pl.DataFrame(rows, schema=_SERIES_SCHEMA)


def _load_fred_api_key(dotenv_path: Path = Path(".env")) -> str | None:
    """讀 FRED API key：優先環境變數，否則解析 .env（不進 git，內容不印出／不 log）。

    專案沒有 python-dotenv 依賴（鐵律 4 新依賴需問過，未加）→ 手刻極簡 KEY=VALUE 解析。
    支援 `FRED_API_KEY`（慣例大寫）與 `fred_api`（本專案 .env 實際使用的小寫鍵名）。
    """
    for env_name in ("FRED_API_KEY", "fred_api"):
        v = os.environ.get(env_name)
        if v:
            return v
    if not dotenv_path.exists():
        return None
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip().lower() in ("fred_api", "fred_api_key"):
            return val.strip().strip('"').strip("'") or None
    return None


class FREDClient:
    """FRED 官方 API 同步 client（含本地 parquet 快取，per-series）。"""

    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        ttl_hours: float,
        user_agent: str,
        interval_sec: float,
        api_key: str,
        max_retries: int = 2,
        timeout_sec: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.ttl_hours = ttl_hours
        self.user_agent = user_agent
        self.interval_sec = interval_sec
        self.api_key = api_key
        self.max_retries = max_retries
        self.timeout_sec = timeout_sec
        self._last_req: float = 0.0

    def _throttle(self) -> None:
        """限速：確保連續請求間隔 ≥ interval_sec（鐵律 1 精神外推到 FRED）。"""
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)

    def _request(self, series_id: str) -> dict[str, Any] | None:
        """限速＋指數退避地 GET 單一序列全歷史，回傳解析後 JSON dict；失敗回 None。"""
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json"}
        for attempt in range(self.max_retries + 1):
            self._throttle()
            if attempt == 0:
                logger.info(f"FRED GET {series_id}")
            else:
                logger.info(f"FRED GET {series_id}（retry {attempt}/{self.max_retries}）")
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
                logger.warning(f"FRED {series_id} HTTP error: {e}")
                self._last_req = time.monotonic()
                if attempt < self.max_retries:
                    time.sleep(5 * (2**attempt))
                    continue
                return None
            self._last_req = time.monotonic()
            try:
                data = resp.json()
            except ValueError:
                logger.warning(f"FRED {series_id} 回傳非 JSON")
                return None
            if not isinstance(data, dict):
                return None
            return data
        return None

    def fetch_series(self, series_id: str, force: bool = False) -> pl.DataFrame:
        """抓單一 FRED 序列全歷史，快取到 {series_id}.parquet（24h TTL）。

        抓取失敗且有舊快取 → 回退舊快取並警告（寧可用稍舊資料，也不整條斷掉）；
        抓取失敗且無舊快取，或解析後為空 → 回空表，呼叫端自行判斷降級。
        force=True（CLI `--refresh`）→ 略過快取新鮮度檢查，強制重打 API。
        """
        cache_file = self.cache_dir / f"{series_id}.parquet"
        if not force and is_fresh(cache_file, self.ttl_hours):
            logger.info(f"命中快取 {cache_file}")
            return load_parquet(cache_file)

        payload = self._request(series_id)
        if payload is None:
            if cache_file.exists():
                logger.warning(f"FRED {series_id} 抓取失敗，回退舊快取（可能已過 TTL）")
                return load_parquet(cache_file)
            logger.warning(f"FRED {series_id} 抓取失敗，且無舊快取")
            return pl.DataFrame(schema=_SERIES_SCHEMA)

        df = _parse_observations(payload)
        if df.is_empty():
            logger.warning(f"FRED {series_id} 解析後為空")
            return df
        save_parquet(df, cache_file)
        return df

    def fetch_all(self, series_ids: list[str], force: bool = False) -> dict[str, pl.DataFrame]:
        """依序抓多條序列（concurrency=1，鐵律 1 精神），連續失敗／空結果達 3 次即停止後續抓取。

        已成功抓到的序列仍回傳；停止後尚未輪到的序列改嘗試讀舊快取（不因中途停而整批放棄），
        全新環境無舊快取者回空表。「連續失敗」計數同時涵蓋 HTTP 失敗與解析後為空，兩者
        對呼叫端而言都是「這條序列這次沒拿到可用資料」。force=True 見 fetch_series。
        """
        result: dict[str, pl.DataFrame] = {}
        consecutive_errors = 0
        stopped = False
        for sid in series_ids:
            if stopped:
                cache_file = self.cache_dir / f"{sid}.parquet"
                result[sid] = (
                    load_parquet(cache_file)
                    if cache_file.exists()
                    else pl.DataFrame(schema=_SERIES_SCHEMA)
                )
                continue
            df = self.fetch_series(sid, force=force)
            result[sid] = df
            if df.is_empty():
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    logger.warning("FRED 連續 3 次抓取失敗／空結果，停止後續抓取（鐵律 1 精神）")
                    stopped = True
            else:
                consecutive_errors = 0
        return result


def create_client(settings_path: Path = Path("config/settings.yaml")) -> FREDClient:
    """從 settings.yaml 建立 FREDClient；API key 讀環境變數或 .env（不進 git，不印出）。"""
    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    fetch_cfg = settings["macro_regime"]["fetch"]
    paths = settings["paths"]
    api_key = _load_fred_api_key()
    if not api_key:
        raise RuntimeError(
            "未設定 FRED API key——請在 .env 設定 fred_api=<key>"
            "（免費註冊：https://fred.stlouisfed.org/docs/api/api_key.html）"
        )
    return FREDClient(
        base_url=fetch_cfg["base_url"],
        cache_dir=Path(paths["cache_dir"]) / "fred",
        ttl_hours=float(fetch_cfg["cache_ttl_hours"]),
        user_agent=fetch_cfg["user_agent"],
        interval_sec=float(fetch_cfg["request_interval_sec"]),
        api_key=api_key,
        max_retries=int(fetch_cfg.get("max_retries", 2)),
        timeout_sec=float(fetch_cfg.get("timeout_sec", 30.0)),
    )
