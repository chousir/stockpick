"""fetcher.py — Goodinfo HTML 取得器（rate limit + cache + retry）。"""

import gzip
import hashlib
import random
import time
from pathlib import Path

import httpx
from loguru import logger
from tenacity import Retrying, stop_after_attempt, wait_exponential


class GoodinfoBlockedError(Exception):
    """Goodinfo 回傳了流量異常封鎖頁面。"""


class GoodinfoFetcher:
    _BLOCKED_MARKER = "您的瀏覽量異常"

    def __init__(
        self,
        cache_dir: Path,
        interval_sec: float,
        jitter_sec: float,
        ttl_hours: float,
        max_retries: int,
        user_agent: str,
        base_url: str,
    ) -> None:
        self._cache_dir = cache_dir / "goodinfo" / "screener"
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._ttl_hours = ttl_hours
        self._max_retries = max_retries
        self._user_agent = user_agent
        self._base_url = base_url
        self._last_request: float = 0.0

    # ── Cache ────────────────────────────────────────────────────────────────

    def _cache_path(self, url: str) -> Path:
        key = hashlib.md5(url.encode()).hexdigest()
        return self._cache_dir / f"{key}.html.gz"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        return age_hours < self._ttl_hours

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

    # ── Blocked detection ────────────────────────────────────────────────────

    def _check_blocked(self, html: str, url: str) -> None:
        if self._BLOCKED_MARKER in html:
            logger.error("Goodinfo access blocked: {}", url)
            raise GoodinfoBlockedError(url)

    # ── Network ──────────────────────────────────────────────────────────────

    def _http_get(self, url: str) -> str:
        self._rate_limit()
        headers = {
            "User-Agent": self._user_agent,
            "Referer": f"{self._base_url}/index.asp",
        }
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, url: str, *, force: bool = False) -> str:
        """取得 URL 的 HTML，優先讀快取；被擋時 raise GoodinfoBlockedError。

        force=True 略過快取強制打網。
        失敗時指數退避重試（5s → 25s → 125s），超過 max_retries 次 reraise。
        """
        cache_path = self._cache_path(url)

        if not force and self._is_fresh(cache_path):
            logger.debug("Cache hit: {}", url)
            html = self._read_cache(cache_path)
            self._check_blocked(html, url)
            return html

        logger.info("Fetching from network: {}", url)
        html = ""
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=5, min=5, max=125),
            reraise=True,
        ):
            with attempt:
                html = self._http_get(url)
                self._check_blocked(html, url)

        self._write_cache(cache_path, html)
        return html


def create_fetcher(settings: dict, cache_dir: Path) -> GoodinfoFetcher:
    """從 settings.yaml 內容建立 GoodinfoFetcher。"""
    gi = settings["goodinfo"]
    return GoodinfoFetcher(
        cache_dir=cache_dir,
        interval_sec=float(gi["request_interval_sec"]),
        jitter_sec=float(gi["request_interval_jitter_sec"]),
        ttl_hours=float(gi["cache_ttl_hours"]),
        max_retries=int(gi["max_retries"]),
        user_agent=gi["user_agent"],
        base_url=gi["base_url"],
    )
