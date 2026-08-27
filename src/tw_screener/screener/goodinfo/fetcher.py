"""fetcher.py — Goodinfo HTML 取得器（rate limit + cache + retry）。"""

import datetime
import gzip
import hashlib
import random
import re
import time
from pathlib import Path

import httpx
from loguru import logger
from tenacity import Retrying, stop_after_attempt, wait_exponential

# Goodinfo 首次訪問時回傳 JS session init 頁面，需要：
#   1. 計算 CLIENT_KEY cookie
#   2. 追蹤 window.location.replace() 的重導向 URL
_JS_REDIRECT_RE = re.compile(r"window\.location\.replace\('([^']+)'\)")
_ARR_CONST_RE = re.compile(r"arr\[(\d+)\]\s*=\s*'([^']+)'")


class GoodinfoBlockedError(Exception):
    """Goodinfo 回傳了流量異常封鎖頁面。"""


class GoodinfoFetcher:
    _BLOCKED_MARKER = "您的瀏覽量異常"

    def __init__(
        self,
        cache_dir: Path,
        interval_sec: float,
        jitter_sec: float,
        refresh_hour: int,
        max_retries: int,
        backoff_base: float,
        user_agent: str,
        base_url: str,
        referer: str,
    ) -> None:
        self._cache_dir = cache_dir / "goodinfo" / "screener"
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._refresh_hour = refresh_hour
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._user_agent = user_agent
        self._base_url = base_url
        self._referer = referer
        self._last_request: float = 0.0

    # ── Cache ────────────────────────────────────────────────────────────────

    def _cache_path(self, url: str) -> Path:
        key = hashlib.md5(url.encode()).hexdigest()
        return self._cache_dir / f"{key}.html.gz"

    def _last_publish_boundary(self, now: datetime.datetime) -> datetime.datetime:
        """最近一個『交易日盤後資料就緒』時點。

        以 refresh_hour（盤後資料穩定，預設 15:00）為界：當天未到則退前一天，
        再把界線退到最近的工作日（週六/日無新交易資料，沿用前一交易日）。
        註：未含國定假日表，連假期間最多每日多抓一次（量極小、可接受）。
        """
        boundary = now.replace(hour=self._refresh_hour, minute=0, second=0, microsecond=0)
        if now < boundary:
            boundary -= datetime.timedelta(days=1)
        while boundary.weekday() >= 5:  # 5=週六, 6=週日
            boundary -= datetime.timedelta(days=1)
        return boundary

    def _is_fresh(self, path: Path) -> bool:
        """快取是否在最近一個交易日盤後就緒時點之後抓取（同一交易日內重複跑讀快取）。"""
        if not path.exists():
            return False
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return mtime >= self._last_publish_boundary(datetime.datetime.now())

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

    # ── Blocked / JS-init detection ──────────────────────────────────────────

    def _check_blocked(self, html: str, url: str) -> None:
        if self._BLOCKED_MARKER in html:
            logger.error("Goodinfo access blocked: {}", url)
            raise GoodinfoBlockedError(url)

    @staticmethod
    def _is_js_init_page(html: str) -> bool:
        """Goodinfo 的 JavaScript session 初始化頁（需要追蹤 JS 重導向）。"""
        return "window.location.replace" in html and "CLIENT_KEY" in html

    # ── Network ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_client_key(html: str) -> str:
        """從 JS init 頁面還原 Goodinfo 的 CLIENT_KEY cookie 值。

        Goodinfo JS:
            arr[0] = '4.8'
            arr[1] = <constant1>
            arr[2] = <constant2>
            arr[3] = GetTimezoneOffset()          # JS: minutes west of UTC
            arr[4] = Date.now()/86400000 - tz/1440 + 25569  # Excel 日期
            arr[5..7] = 0
        """
        constants = {int(k): v for k, v in _ARR_CONST_RE.findall(html)}
        arr1 = constants.get(1, "38077.6754492846")
        arr2 = constants.get(2, "46966.5643381734")

        # JS getTimezoneOffset() = minutes WEST of UTC (negative for UTC+N)
        now = datetime.datetime.now(datetime.UTC).astimezone()
        js_tz = int(-now.utcoffset().total_seconds() / 60)

        # arr[4]: Excel date-time of now in local timezone
        excel_date = time.time() / 86400 - js_tz / 1440 + 25569

        return f"4.8|{arr1}|{arr2}|{js_tz}|{excel_date}|0|0|0"

    def _raise_if_blocked_status(self, resp: httpx.Response, url: str) -> None:
        """4xx/5xx 狀態碼視為封鎖（比`_check_blocked()`更早期的封鎖形態——連 200 帶
        Cloudflare 驗證頁都沒有，直接被拒絕連線），統一轉成`GoodinfoBlockedError`，
        沿用 runner.py 既有「整批中斷」語意與乾淨錯誤訊息，不讓 raw
        `httpx.HTTPStatusError`以未捕捉例外炸穿整條 `make week`（docs/31 §19.3
        同一原則：封鎖時清楚回報，不是讓呼叫端崩潰）。
        """
        if resp.status_code >= 400:
            logger.error("Goodinfo HTTP {}（可能封鎖）：{}", resp.status_code, url)
            raise GoodinfoBlockedError(f"{url} (HTTP {resp.status_code})")

    def _http_get(self, url: str) -> str:
        self._rate_limit()
        headers = {
            "User-Agent": self._user_agent,
            "Referer": self._referer,
        }
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            resp = client.get(url, headers=headers)
            self._raise_if_blocked_status(resp, url)
            html = resp.text

            # Goodinfo 首次訪問先回傳 JS init 頁面再重導向
            if self._is_js_init_page(html):
                m = _JS_REDIRECT_RE.search(html)
                if m:
                    client_key = self._build_client_key(html)
                    client.cookies.set("CLIENT_KEY", client_key, domain="goodinfo.tw")
                    redirect_url = f"{self._base_url}/{m.group(1)}"
                    logger.debug("JS init → redirect: {}", redirect_url)
                    self._rate_limit()
                    resp = client.get(redirect_url, headers=headers)
                    self._raise_if_blocked_status(resp, url)
                    html = resp.text

            return html

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, url: str, *, force: bool = False) -> str:
        """取得 URL 的 HTML，優先讀快取；被擋時 raise GoodinfoBlockedError。

        force=True 略過快取強制打網。
        快取內容若為 JS init 頁面（上次抓到了重導向前的內容）則視為 cache miss。
        失敗時指數退避重試（5s → 25s → 125s），超過 max_retries 次 reraise。
        """
        cache_path = self._cache_path(url)

        if not force and self._is_fresh(cache_path):
            html = self._read_cache(cache_path)
            if not self._is_js_init_page(html):
                self._check_blocked(html, url)
                logger.debug("Cache hit: {}", url)
                return html
            logger.debug("Cached JS init page, refetching: {}", url)

        logger.info("Fetching from network: {}", url)
        html = ""
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=self._backoff_base,
                min=self._backoff_base,
                max=self._backoff_base ** 3,
            ),
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
        refresh_hour=int(gi["cache_refresh_hour"]),
        max_retries=int(gi["max_retries"]),
        backoff_base=float(gi["backoff_base"]),
        user_agent=gi["user_agent"],
        base_url=gi["base_url"],
        referer=gi["referer"],
    )
