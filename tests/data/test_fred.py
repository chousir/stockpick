"""tests/data/test_fred.py — FRED 資料層單元測試（全離線，docs/25 v2 macro_regime）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from tw_screener.data.fred import (
    FREDClient,
    _load_fred_api_key,
    _parse_observations,
    create_client,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "fred"


def _load_json(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# ── _parse_observations ─────────────────────────────────────────────────────


def test_parse_observations_normal() -> None:
    payload = _load_json("observations_normal.json")
    df = _parse_observations(payload)
    # 8 筆原始觀測，1 筆日期無法解析（"not-a-date"）整列略過 → 7 筆
    assert df.height == 7
    assert set(df.columns) == {"date", "value"}
    assert df["date"].dtype == pl.Date


def test_parse_observations_missing_value_is_null_not_zero() -> None:
    """FRED 用 "." 表示缺值 → None，不是 0（誠實原則：沒資料不能編）。"""
    payload = _load_json("observations_normal.json")
    df = _parse_observations(payload)
    row = df.filter(pl.col("date") == date(2026, 7, 26))
    assert row.height == 1
    assert row["value"].item() is None


def test_parse_observations_empty() -> None:
    payload = _load_json("observations_empty.json")
    df = _parse_observations(payload)
    assert df.is_empty()
    assert set(df.columns) == {"date", "value"}


def test_parse_observations_no_observations_key() -> None:
    df = _parse_observations({})
    assert df.is_empty()


# ── _load_fred_api_key ──────────────────────────────────────────────────────


def test_load_fred_api_key_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRED_API_KEY", "env-key-123")
    assert _load_fred_api_key(tmp_path / "nonexistent.env") == "env-key-123"


def test_load_fred_api_key_from_dotenv_lowercase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("fred_api", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("fred_api=abc123\n", encoding="utf-8")
    assert _load_fred_api_key(dotenv) == "abc123"


def test_load_fred_api_key_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("fred_api", raising=False)
    assert _load_fred_api_key(tmp_path / "nonexistent.env") is None


# ── FREDClient 快取（離線：預先寫入 parquet 快取，不觸發 httpx）────────────────


def test_fetch_series_cache_hit_no_network(tmp_path: Path) -> None:
    cache_dir = tmp_path / "fred"
    cache_dir.mkdir()
    seeded = pl.DataFrame(
        {"date": [date(2026, 7, 30)], "value": [2.14]},
        schema={"date": pl.Date, "value": pl.Float64},
    )
    seeded.write_parquet(cache_dir / "BAA10Y.parquet")

    client = FREDClient(
        base_url="https://api.stlouisfed.org/fred/series/observations",
        cache_dir=cache_dir,
        ttl_hours=24.0,
        user_agent="test/0.1",
        interval_sec=0.0,
        api_key="dummy",
    )
    # 快取新鮮（剛寫入）→ 命中快取，不需要真的打網（httpx 不會被呼叫）
    df = client.fetch_series("BAA10Y")
    assert df.height == 1
    assert df["value"].item() == 2.14


def test_fetch_all_stops_after_three_consecutive_failures(tmp_path: Path) -> None:
    """連續 3 次抓取失敗／空結果 → 停止後續抓取（鐵律 1 精神）。"""
    cache_dir = tmp_path / "fred"
    cache_dir.mkdir()

    client = FREDClient(
        base_url="http://127.0.0.1:1",  # 保證連不上，觸發失敗路徑（不打真網）
        cache_dir=cache_dir,
        ttl_hours=24.0,
        user_agent="test/0.1",
        interval_sec=0.0,
        api_key="dummy",
        max_retries=0,
        timeout_sec=1.0,
    )
    result = client.fetch_all(["A", "B", "C", "D", "E"])
    assert len(result) == 5
    # 前三條真的嘗試抓（全失敗回空表），第 3 次觸發停止，後兩條改走「讀舊快取」路徑
    # （無快取 → 也是空表，但不再呼叫 _request）
    assert all(df.is_empty() for df in result.values())


def test_create_client_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("fred_api", raising=False)
    monkeypatch.chdir(tmp_path)  # 確保沒有專案 .env 被誤讀
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "paths:\n  cache_dir: data/cache\n"
        "macro_regime:\n  fetch:\n    base_url: x\n    user_agent: y\n"
        "    request_interval_sec: 1\n    cache_ttl_hours: 24\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="FRED API key"):
        create_client(settings_path)
