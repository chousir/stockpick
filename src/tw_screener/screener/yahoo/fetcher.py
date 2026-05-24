"""fetcher.py — Yahoo 股市頁面取得器（rate limit + TTL cache + retry）。

比照 GoodinfoFetcher，但純 HTTP：不需 JS-init/CLIENT_KEY、無封鎖頁邏輯。
快取新鮮度用簡單 TTL（概念股成分鮮少變，同日重跑讀快取）。
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import random
import time
from pathlib import Path

import httpx
from loguru import logger
from tenacity import Retrying, stop_after_attempt, wait_exponential


class YahooFetcher:
    def __init__(
        self,
        cache_dir: Path,
        interval_sec: float,
        jitter_sec: float,
        ttl_hours: float,
        max_retries: int,
        backoff_base: float,
        user_agent: str,
        base_url: str,
    ) -> None:
        self._cache_dir = cache_dir / "yahoo"
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._ttl = datetime.timedelta(hours=ttl_hours)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/")
        self._last_request: float = 0.0

    # ── Cache ────────────────────────────────────────────────────────────────

    def _cache_path(self, url: str) -> Path:
        key = hashlib.md5(url.encode()).hexdigest()
        return self._cache_dir / f"{key}.html.gz"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return datetime.datetime.now() - mtime < self._ttl

    def _read_cache(self, path: Path) -> str:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()

    def _write_cache(self, path: Path, html: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(html)

    # ── Rate limit ───────────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        jitter = random.uniform(-self._jitter, self._jitter)
        target = max(0.0, self._interval + jitter)
        elapsed = time.monotonic() - self._last_request
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last_request = time.monotonic()

    # ── Network ──────────────────────────────────────────────────────────────

    def _http_get(self, url: str) -> str:
        self._rate_limit()
        headers = {"User-Agent": self._user_agent}
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, path_or_url: str, *, force: bool = False) -> str:
        """取 URL 的 HTML（相對路徑自動補 base_url），優先讀快取。

        force=True 略過快取強制打網；失敗指數退避重試，超過 max_retries 次 reraise。
        """
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self._base_url}{path_or_url}"
        )
        cache_path = self._cache_path(url)
        if not force and self._is_fresh(cache_path):
            logger.debug("Cache hit: {}", url)
            return self._read_cache(cache_path)

        logger.info("Fetching from network: {}", url)
        html = ""
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=self._backoff_base,
                min=self._backoff_base,
                max=self._backoff_base**3,
            ),
            reraise=True,
        ):
            with attempt:
                html = self._http_get(url)

        self._write_cache(cache_path, html)
        return html


def create_yahoo_fetcher(settings: dict, cache_dir: Path) -> YahooFetcher:
    """從 settings.yaml 內容建立 YahooFetcher。"""
    y = settings["yahoo"]
    return YahooFetcher(
        cache_dir=cache_dir,
        interval_sec=float(y["request_interval_sec"]),
        jitter_sec=float(y["request_interval_jitter_sec"]),
        ttl_hours=float(y["cache_ttl_hours"]),
        max_retries=int(y["max_retries"]),
        backoff_base=float(y["backoff_base"]),
        user_agent=y["user_agent"],
        base_url=y["base_url"],
    )
