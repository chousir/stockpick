"""tests/data/test_finmind.py — FinMind 資料層單元測試（全離線，docs/31 §20.14）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import pytest

from tw_screener.data import finmind
from tw_screener.data.finmind import (
    FinMindClient,
    _load_finmind_token,
    _parse_taiwan_stock_per,
    create_client,
    load_finmind_per_history,
    load_merged_valuation_history,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "finmind"


def _load_json(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# ── _parse_taiwan_stock_per ─────────────────────────────────────────────────


def test_parse_per_normal() -> None:
    df = _parse_taiwan_stock_per(_load_json("taiwan_stock_per_2330_normal.json"))
    assert df.height == 5
    assert set(df.columns) == {"date", "stock_id", "pe", "pbr", "dividend_yield"}
    assert df["date"].dtype == pl.Date
    assert df["stock_id"].dtype == pl.Utf8
    row = df.filter(pl.col("date") == date(2015, 1, 5))
    assert row["pe"].item() == pytest.approx(15.82)
    assert row["pbr"].item() == pytest.approx(3.78)
    assert row["dividend_yield"].item() == pytest.approx(2.14)


def test_parse_per_lossmaker_per_zero_to_none() -> None:
    """回歸鎖：虧損股 FinMind 寫 PER=0.0 → pe 欄全 None，pbr/殖利率仍有值。"""
    df = _parse_taiwan_stock_per(_load_json("taiwan_stock_per_2498_lossmaker.json"))
    assert df.height == 4
    assert df["pe"].null_count() == 4
    assert df["pbr"].null_count() == 0
    assert df["dividend_yield"].null_count() == 0


def test_parse_per_dividend_yield_units_are_percent() -> None:
    """dividend_yield 直接是百分比（2.14 = 2.14%），不做 /100。"""
    df = _parse_taiwan_stock_per(_load_json("taiwan_stock_per_2330_normal.json"))
    assert df["dividend_yield"].max() == pytest.approx(2.24)


def test_parse_per_empty() -> None:
    df = _parse_taiwan_stock_per(_load_json("taiwan_stock_per_empty.json"))
    assert df.is_empty()
    assert set(df.columns) == {"date", "stock_id", "pe", "pbr", "dividend_yield"}


def test_parse_per_no_data_key() -> None:
    assert _parse_taiwan_stock_per({}).is_empty()


def test_parse_per_bad_rows_skipped_and_non_positive_to_none() -> None:
    """壞列（無日期/無 stock_id/日期無法解析）整列略過不 raise；非數值與 ≤0 → None。"""
    df = _parse_taiwan_stock_per(_load_json("taiwan_stock_per_bad_rows.json"))
    # 6 列輸入：good / bad-date / no-date-key / no-stock_id / non-numeric+neg-pbr / all-zero
    # → 保留 3 列（2020-03-02、2020-03-04、2020-03-05）
    assert df.height == 3
    assert sorted(str(d) for d in df["date"].to_list()) == [
        "2020-03-02",
        "2020-03-04",
        "2020-03-05",
    ]
    r4 = df.filter(pl.col("date") == date(2020, 3, 4))
    assert r4["pe"].item() is None  # "-" 無法轉 float
    assert r4["pbr"].item() is None  # -2.0 ≤ 0
    assert r4["dividend_yield"].item() is None  # "N/A" 無法轉 float
    r5 = df.filter(pl.col("date") == date(2020, 3, 5))
    assert r5["pe"].item() is None  # 0.0 ≤ 0
    assert r5["pbr"].item() == pytest.approx(1.5)  # 正值保留


# ── _load_finmind_token ────────────────────────────────────────────────────


def test_load_finmind_token_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FINMIND_TOKEN", "env-token-abc")
    assert _load_finmind_token(tmp_path / "nonexistent.env") == "env-token-abc"


def test_load_finmind_token_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text('FINMIND_TOKEN="tok-from-dotenv"\n', encoding="utf-8")
    assert _load_finmind_token(dotenv) == "tok-from-dotenv"


def test_load_finmind_token_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """與 FRED 不同：token 缺席回 None（未註冊也能用），不 raise。"""
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert _load_finmind_token(tmp_path / "nonexistent.env") is None


def test_create_client_no_token_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "paths:\n  cache_dir: data/cache\n"
        "finmind:\n  base_url: https://x\n  user_agent: y\n"
        "  request_interval_sec: 6\n  cache_ttl_hours: 24\n  timeout_sec: 30\n",
        encoding="utf-8",
    )
    client = create_client(settings_path)  # 不 raise
    assert client.token is None
    assert client.cache_dir == Path("data/cache/finmind")


# ── FinMindClient（離線）────────────────────────────────────────────────────


def _client(cache_dir: Path, **kw: object) -> FinMindClient:
    defaults: dict[str, object] = {
        "base_url": "https://api.finmindtrade.com/api/v4/data",
        "cache_dir": cache_dir,
        "ttl_hours": 24.0,
        "user_agent": "test/0.1",
        "interval_sec": 0.0,
        "token": None,
    }
    defaults.update(kw)
    return FinMindClient(**defaults)  # type: ignore[arg-type]


def test_fetch_per_cache_hit_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    seeded = pl.DataFrame(
        {"date": [date(2026, 7, 30)], "stock_id": ["2330"],
         "pe": [15.0], "pbr": [3.0], "dividend_yield": [2.0]},
        schema=finmind._PER_SCHEMA,
    )
    seeded.write_parquet(cache_dir / "per_2330.parquet")

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("httpx.get 不應被呼叫（快取新鮮）")

    monkeypatch.setattr(httpx, "get", _boom)
    df = _client(cache_dir).fetch_taiwan_stock_per("2330")
    assert df.height == 1
    assert df["pe"].item() == 15.0


def test_fetch_per_http_failure_returns_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    stale = pl.DataFrame(
        {"date": [date(2020, 1, 2)], "stock_id": ["2330"],
         "pe": [11.0], "pbr": [2.0], "dividend_yield": [3.0]},
        schema=finmind._PER_SCHEMA,
    )
    stale.write_parquet(cache_dir / "per_2330.parquet")
    monkeypatch.setattr(finmind, "is_fresh", lambda *a, **k: False)  # 強制過期

    client = _client(
        cache_dir, base_url="http://127.0.0.1:1", max_retries=0, timeout_sec=1.0
    )
    df = client.fetch_taiwan_stock_per("2330")
    assert df.height == 1
    assert df["pe"].item() == 11.0  # 回退舊快取


def test_fetch_per_http_failure_no_cache_returns_empty(tmp_path: Path) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    client = _client(
        cache_dir, base_url="http://127.0.0.1:1", max_retries=0, timeout_sec=1.0
    )
    df = client.fetch_taiwan_stock_per("9999")
    assert df.is_empty()
    assert set(df.columns) == set(finmind._PER_SCHEMA)


def test_fetch_per_status_402_treated_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """額度用盡（body status=402）→ 當失敗回空表，讓 fetch_all_per 斷路器計數。"""
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    payload = _load_json("taiwan_stock_per_status_402.json")

    class _Resp:
        def raise_for_status(self) -> None:  # HTTP 200，body 才帶 402
            return None

        def json(self) -> dict:
            return payload

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    df = _client(cache_dir, max_retries=0).fetch_taiwan_stock_per("2330")
    assert df.is_empty()


def test_fetch_all_per_circuit_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """連續 3 次空結果 → 停止後續抓取（鐵律 1 精神）。"""
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    calls: list[str] = []

    def _fake_fetch(sid: str, start_date: str = "", force: bool = False) -> pl.DataFrame:
        calls.append(sid)
        return pl.DataFrame(schema=finmind._PER_SCHEMA)

    client = _client(cache_dir)
    monkeypatch.setattr(client, "fetch_taiwan_stock_per", _fake_fetch)
    result = client.fetch_all_per(["A", "B", "C", "D", "E"])
    assert len(result) == 5
    assert calls == ["A", "B", "C"]  # 第 3 次觸發停止，D/E 不再打
    assert all(df.is_empty() for df in result.values())


def test_fetch_all_per_resets_counter_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    good = pl.DataFrame(
        {"date": [date(2020, 1, 2)], "stock_id": ["X"],
         "pe": [10.0], "pbr": [1.0], "dividend_yield": [2.0]},
        schema=finmind._PER_SCHEMA,
    )
    seq = {"A": good, "B": pl.DataFrame(schema=finmind._PER_SCHEMA),
           "C": pl.DataFrame(schema=finmind._PER_SCHEMA), "D": good,
           "E": pl.DataFrame(schema=finmind._PER_SCHEMA)}
    client = _client(cache_dir)
    monkeypatch.setattr(
        client, "fetch_taiwan_stock_per",
        lambda sid, start_date="", force=False: seq[sid],
    )
    result = client.fetch_all_per(["A", "B", "C", "D", "E"])
    assert not result["D"].is_empty()  # D 成功前只有 2 連空、未觸發停止


# ── load_finmind_per_history / load_merged_valuation_history ─────────────────


def _twse_stub(history: pl.DataFrame) -> object:
    class _Stub:
        def load_valuation_ratios_history(self) -> pl.DataFrame:
            return history

    return _Stub()


def test_load_finmind_per_history_dedup(tmp_path: Path) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    for sid, pe in (("2330", 15.0), ("2317", 12.0)):
        pl.DataFrame(
            {"date": [date(2020, 1, 2)], "stock_id": [sid],
             "pe": [pe], "pbr": [2.0], "dividend_yield": [3.0]},
            schema=finmind._PER_SCHEMA,
        ).write_parquet(cache_dir / f"per_{sid}.parquet")
    hist = load_finmind_per_history(cache_dir)
    assert hist.height == 2
    assert set(hist["stock_id"].to_list()) == {"2330", "2317"}


def test_load_finmind_per_history_no_files(tmp_path: Path) -> None:
    hist = load_finmind_per_history(tmp_path / "nope")
    assert hist.is_empty()
    assert set(hist.columns) == set(finmind._PER_SCHEMA)


def test_load_merged_valuation_history_overlap_twse_wins(tmp_path: Path) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    # FinMind：2020-01-02（深度）+ 2026-06-15（與 TWSE 重疊日）
    pl.DataFrame(
        {"date": [date(2020, 1, 2), date(2026, 6, 15)], "stock_id": ["2330", "2330"],
         "pe": [15.0, 99.0], "pbr": [3.0, 3.0], "dividend_yield": [2.0, 2.0]},
        schema=finmind._PER_SCHEMA,
    ).write_parquet(cache_dir / "per_2330.parquet")
    twse_hist = pl.DataFrame(
        {"date": [date(2026, 6, 15)], "stock_id": ["2330"], "market": ["上市"],
         "pe": [22.0], "pbr": [3.1], "dividend_yield": [2.1]},
        schema={"date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
                "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64},
    )
    merged = load_merged_valuation_history(_twse_stub(twse_hist), cache_dir)
    assert merged.height == 2
    overlap = merged.filter(pl.col("date") == date(2026, 6, 15))
    assert overlap["pe"].item() == 22.0  # TWSE 勝，不是 FinMind 的 99.0
    deep = merged.filter(pl.col("date") == date(2020, 1, 2))
    assert deep["pe"].item() == 15.0  # FinMind 深度歷史保留


def test_load_merged_valuation_history_caps_finmind_at_twse_max(tmp_path: Path) -> None:
    """FinMind 更新快過本地 TWSE 快取時，超過 TWSE 最新日的 FinMind 列要被丟掉
    （否則 merged 的 date.max() 會汙染下游面板重播錨點）。"""
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    pl.DataFrame(
        {"date": [date(2020, 1, 2), date(2026, 6, 20), date(2026, 9, 9)],
         "stock_id": ["2330", "2330", "2330"],
         "pe": [15.0, 20.0, 25.0], "pbr": [3.0, 3.0, 3.0],
         "dividend_yield": [2.0, 2.0, 2.0]},
        schema=finmind._PER_SCHEMA,
    ).write_parquet(cache_dir / "per_2330.parquet")
    twse_hist = pl.DataFrame(
        {"date": [date(2026, 6, 15), date(2026, 6, 20)], "stock_id": ["2330", "2330"],
         "market": ["上市", "上市"], "pe": [22.0, 21.0], "pbr": [3.1, 3.1],
         "dividend_yield": [2.1, 2.1]},
        schema={"date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
                "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64},
    )
    merged = load_merged_valuation_history(_twse_stub(twse_hist), cache_dir)
    assert merged["date"].max() == date(2026, 6, 20)  # 2026-09-09 被封頂丟掉
    assert date(2026, 9, 9) not in merged["date"].to_list()
    assert merged.filter(pl.col("date") == date(2020, 1, 2)).height == 1  # 深度保留


def test_load_merged_valuation_history_no_finmind_falls_back(tmp_path: Path) -> None:
    twse_hist = pl.DataFrame(
        {"date": [date(2026, 6, 15)], "stock_id": ["2330"], "market": ["上市"],
         "pe": [22.0], "pbr": [3.1], "dividend_yield": [2.1]},
        schema={"date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
                "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64},
    )
    merged = load_merged_valuation_history(_twse_stub(twse_hist), tmp_path / "nope")
    assert merged.equals(twse_hist)  # FinMind 目錄不存在 → 原封不動回 TWSE
