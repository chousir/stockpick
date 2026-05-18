"""tests/test_week_tag.py — derive_week_tag 行為驗證（trading_date 錨點）。"""

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.data.cache import save_parquet
from tw_screener.screener.runner import derive_week_tag


def _make_settings(tmp_path: Path) -> tuple[Path, Path]:
    """寫一個最小可用的 settings.yaml 到 tmp_path 並回傳 (settings_path, twse_cache_dir)。

    twse_cache_dir 是 `<cache_dir>/twse`，跟 create_client 內部一致。
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    twse_cache = cache_dir / "twse"
    twse_cache.mkdir()
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"""paths:
  data_dir: {tmp_path}/data
  cache_dir: {cache_dir}
  raw_dir: {tmp_path}/raw
  reports_dir: {tmp_path}/reports
  logs_dir: {tmp_path}/logs
  watchlist_dir: {tmp_path}/watchlist
  strategies_dir: {tmp_path}/strategies

goodinfo:
  base_url: "https://goodinfo.tw/tw"
  user_agent: "test"
  request_interval_sec: 0
  request_interval_jitter_sec: 0
  cache_ttl_hours: 24
  max_retries: 1
  backoff_base: 5
  referer: "https://goodinfo.tw/tw/index.asp"
  concurrency: 1

twse:
  base_url: "https://test.invalid/v1"
  user_agent: "test"
  request_interval_sec: 0
  cache_ttl_hours: 6
""",
        encoding="utf-8",
    )
    return settings, twse_cache


def _stash_daily(cache_dir: Path, max_date: date) -> None:
    """放一份 daily cache 讓 latest_trading_date 拿到 max_date。"""
    save_parquet(
        pl.DataFrame(
            {
                "date": [max_date],
                "stock_id": ["2330"],
                "name": ["台積電"],
                "trade_volume": [1],
                "trade_value": [1],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "change": [0.0],
                "transaction": [1],
            }
        ),
        cache_dir / f"daily_{max_date.strftime('%Y%m%d')}.parquet",
    )


def test_derive_week_tag_friday(tmp_path: Path):
    """trading_date 為週五 5/15 → 2026-W20。"""
    settings, twse_cache = _make_settings(tmp_path)
    _stash_daily(twse_cache, date(2026, 5, 15))
    assert derive_week_tag(settings) == "2026-W20"


def test_derive_week_tag_monday(tmp_path: Path):
    """trading_date 為週一 5/18 → 2026-W21（新週開始）。"""
    settings, twse_cache = _make_settings(tmp_path)
    _stash_daily(twse_cache, date(2026, 5, 18))
    assert derive_week_tag(settings) == "2026-W21"


def test_derive_week_tag_fallback_to_today_when_no_data(tmp_path: Path):
    """無 daily cache 又拿不到 trading_date 時 → fallback 到 today。"""
    settings, _ = _make_settings(tmp_path)
    # 不放 daily cache、base_url 設成 test.invalid 故 HTTP 也會失敗
    # → derive_week_tag 走 fallback 到 today.strftime
    result = derive_week_tag(settings)
    assert result == date.today().strftime("%Y-W%V")
