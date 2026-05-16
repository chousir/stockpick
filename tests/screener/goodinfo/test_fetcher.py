"""tests/screener/goodinfo/test_fetcher.py — GoodinfoFetcher 單元測試（全離線）。"""

import os
import time
from pathlib import Path

import pytest

from tw_screener.screener.goodinfo.fetcher import GoodinfoBlockedError, GoodinfoFetcher

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "goodinfo"


def make_fetcher(tmp_path: Path, **kwargs: object) -> GoodinfoFetcher:
    defaults: dict = {
        "cache_dir": tmp_path,
        "interval_sec": 0.0,
        "jitter_sec": 0.0,
        "ttl_hours": 24.0,
        "max_retries": 1,
        "user_agent": "test/1.0",
        "base_url": "https://goodinfo.tw/tw",
    }
    defaults.update(kwargs)
    return GoodinfoFetcher(**defaults)


# ─── 初始化 ──────────────────────────────────────────────────────────────────


def test_fetcher_ttl(tmp_path: Path):
    f = make_fetcher(tmp_path, ttl_hours=6.0)
    assert f._ttl_hours == 6.0


def test_fetcher_max_retries(tmp_path: Path):
    f = make_fetcher(tmp_path, max_retries=3)
    assert f._max_retries == 3


# ─── Cache path ───────────────────────────────────────────────────────────────


def test_cache_path_consistent(tmp_path: Path):
    f = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?x=1"
    assert f._cache_path(url) == f._cache_path(url)


def test_cache_path_different_urls(tmp_path: Path):
    f = make_fetcher(tmp_path)
    url_a = "https://goodinfo.tw/tw/StockList.asp?x=1"
    url_b = "https://goodinfo.tw/tw/StockList.asp?x=2"
    assert f._cache_path(url_a) != f._cache_path(url_b)


def test_cache_path_under_cache_dir(tmp_path: Path):
    f = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?x=1"
    assert str(tmp_path) in str(f._cache_path(url))


# ─── is_fresh ─────────────────────────────────────────────────────────────────


def test_is_fresh_missing_file(tmp_path: Path):
    f = make_fetcher(tmp_path)
    assert not f._is_fresh(tmp_path / "nonexistent.html.gz")


def test_is_fresh_new_file(tmp_path: Path):
    f = make_fetcher(tmp_path)
    p = tmp_path / "test.html.gz"
    p.touch()
    assert f._is_fresh(p)


def test_is_stale(tmp_path: Path):
    f = make_fetcher(tmp_path, ttl_hours=0.001)  # ~3.6 seconds
    p = tmp_path / "old.html.gz"
    p.touch()
    old_ts = time.time() - 100  # 100 seconds ago
    os.utime(p, (old_ts, old_ts))
    assert not f._is_fresh(p)


# ─── Cache hit ────────────────────────────────────────────────────────────────


def test_cache_hit_skips_network(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?cache=hit"

    fetcher._write_cache(fetcher._cache_path(url), "<html><body>cached</body></html>")

    http_calls: list[str] = []

    def mock_http(u: str) -> str:  # pragma: no cover
        http_calls.append(u)
        return ""

    fetcher._http_get = mock_http  # type: ignore[method-assign]

    result = fetcher.get(url)
    assert len(http_calls) == 0
    assert "cached" in result


def test_cache_hit_returns_content(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?cache=content"
    expected = "<html><body>hello world</body></html>"
    fetcher._write_cache(fetcher._cache_path(url), expected)
    fetcher._http_get = lambda u: ""  # type: ignore[method-assign]
    assert fetcher.get(url) == expected


# ─── Cache miss → network ─────────────────────────────────────────────────────


def test_cache_miss_calls_network(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?miss=1"
    html = "<html><body>fresh</body></html>"
    fetcher._http_get = lambda u: html  # type: ignore[method-assign]

    result = fetcher.get(url)
    assert result == html


def test_cache_miss_writes_cache(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?miss=write"
    fetcher._http_get = lambda u: "<html>ok</html>"  # type: ignore[method-assign]

    fetcher.get(url)
    assert fetcher._cache_path(url).exists()


# ─── force=True ───────────────────────────────────────────────────────────────


def test_force_bypasses_cache(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?force=1"

    fetcher._write_cache(fetcher._cache_path(url), "<html>old</html>")

    http_calls: list[str] = []
    fresh_html = "<html>fresh</html>"

    def mock_http(u: str) -> str:
        http_calls.append(u)
        return fresh_html

    fetcher._http_get = mock_http  # type: ignore[method-assign]

    result = fetcher.get(url, force=True)
    assert len(http_calls) == 1
    assert result == fresh_html


# ─── Blocked detection ────────────────────────────────────────────────────────


def test_blocked_raises_on_network_response(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?blocked=network"
    blocked_html = (FIXTURE_DIR / "blocked.html").read_text(encoding="utf-8")
    fetcher._http_get = lambda u: blocked_html  # type: ignore[method-assign]

    with pytest.raises(GoodinfoBlockedError):
        fetcher.get(url)


def test_blocked_raises_from_cache(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?blocked=cache"
    blocked_html = (FIXTURE_DIR / "blocked.html").read_text(encoding="utf-8")
    fetcher._write_cache(fetcher._cache_path(url), blocked_html)

    with pytest.raises(GoodinfoBlockedError):
        fetcher.get(url)


def test_blocked_does_not_write_cache(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?blocked=nocache"
    blocked_html = (FIXTURE_DIR / "blocked.html").read_text(encoding="utf-8")
    fetcher._http_get = lambda u: blocked_html  # type: ignore[method-assign]

    with pytest.raises(GoodinfoBlockedError):
        fetcher.get(url)

    # Cache should not have been written (error raised before write)
    assert not fetcher._cache_path(url).exists()


# ─── Stale cache refetches ────────────────────────────────────────────────────


def test_stale_cache_triggers_network(tmp_path: Path):
    fetcher = make_fetcher(tmp_path, ttl_hours=0.001)
    url = "https://goodinfo.tw/tw/StockList.asp?stale=1"

    fetcher._write_cache(fetcher._cache_path(url), "<html>old</html>")
    old_ts = time.time() - 100
    os.utime(fetcher._cache_path(url), (old_ts, old_ts))

    http_calls: list[str] = []

    def mock_http(u: str) -> str:
        http_calls.append(u)
        return "<html>new</html>"

    fetcher._http_get = mock_http  # type: ignore[method-assign]

    fetcher.get(url)
    assert len(http_calls) == 1


# ─── JS init page detection ───────────────────────────────────────────────────

_JS_INIT_HTML = """<!DOCTYPE html><html><head></head><body></body>
<script language='javascript'>
var arr = []; arr[0]='4.8'; arr[1]='38077'; arr[2]='46966';
setCookie('CLIENT_KEY', '', -1, '/');
setTimeout(function(){
    window.location.replace('StockList.asp?MARKET_CAT=test&REINIT=46158.5');
}, 100);
</script></html>"""


def test_is_js_init_page_true(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    assert fetcher._is_js_init_page(_JS_INIT_HTML)


def test_is_js_init_page_false(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    assert not fetcher._is_js_init_page("<html><body>normal page</body></html>")


def test_build_client_key_format(tmp_path: Path):
    fetcher = make_fetcher(tmp_path)
    key = fetcher._build_client_key(_JS_INIT_HTML)
    parts = key.split("|")
    assert len(parts) == 8
    assert parts[0] == "4.8"
    assert float(parts[4]) > 0  # Excel date > 0


def test_js_init_cache_treated_as_miss(tmp_path: Path):
    """快取內容是 JS init 頁面時，應重新打網而非直接回傳。"""
    fetcher = make_fetcher(tmp_path)
    url = "https://goodinfo.tw/tw/StockList.asp?js=1"

    fetcher._write_cache(fetcher._cache_path(url), _JS_INIT_HTML)

    http_calls: list[str] = []
    fresh_html = "<html><body>real content</body></html>"

    def mock_http(u: str) -> str:
        http_calls.append(u)
        return fresh_html

    fetcher._http_get = mock_http  # type: ignore[method-assign]

    result = fetcher.get(url)
    assert len(http_calls) == 1
    assert result == fresh_html
