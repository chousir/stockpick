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
    _BALANCESHEET_FIELD_MAP,
    _BALANCESHEET_WIDE_SCHEMA,
    _CASHFLOWS_FIELD_MAP,
    _CASHFLOWS_WIDE_SCHEMA,
    _FINANCIALS_FIELD_MAP,
    _FINANCIALS_WIDE_SCHEMA,
    _MONTH_REVENUE_SCHEMA,
    FinMindClient,
    _load_finmind_token,
    _parse_finmind_long,
    _parse_month_revenue,
    _parse_taiwan_stock_per,
    create_client,
    load_balancesheet_history,
    load_cashflow_history,
    load_financials_history,
    load_finmind_per_history,
    load_merged_valuation_history,
    load_month_revenue_history,
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


# ── _parse_finmind_long（Phase 2：3 個長格式財報 dataset）───────────────────


def test_parse_cashflows_q4_is_fy() -> None:
    """CashFlows 累計 YTD：Q4 列的 ocf 就是全年（parser 不 de-cumulate，照抄）。"""
    df = _parse_finmind_long(
        _load_json("cashflows_2330.json"), _CASHFLOWS_FIELD_MAP, _CASHFLOWS_WIDE_SCHEMA
    )
    assert set(df.columns) == {"stock_id", "year", "quarter", "ocf", "ocf_alt", "capex"}
    fy23 = df.filter((pl.col("year") == 2023) & (pl.col("quarter") == 4))
    assert fy23["ocf"].item() == pytest.approx(1_241_967_347_000.0)
    # Q1 < Q4（確認是累計而非單季）
    q1 = df.filter((pl.col("year") == 2023) & (pl.col("quarter") == 1))["ocf"].item()
    assert q1 < fy23["ocf"].item()


def test_parse_cashflows_capex_sign_negative() -> None:
    """回歸鎖：capex（PropertyAndPlantAndEquipment）為**負值**。

    FinMind 慣例若某日翻成正值，`fcf = ocf − |capex|` 會被無聲加倍 → 這個斷言讓它大聲報錯。
    """
    df = _parse_finmind_long(
        _load_json("cashflows_2330.json"), _CASHFLOWS_FIELD_MAP, _CASHFLOWS_WIDE_SCHEMA
    )
    assert (df["capex"].drop_nulls() < 0).all()


def test_parse_cashflows_ocf_alt_matches_primary() -> None:
    """兩個 OCF code（primary `ocf` / alt `ocf_alt`）實測逐筆相同。"""
    df = _parse_finmind_long(
        _load_json("cashflows_2330.json"), _CASHFLOWS_FIELD_MAP, _CASHFLOWS_WIDE_SCHEMA
    )
    paired = df.filter(pl.col("ocf").is_not_null() & pl.col("ocf_alt").is_not_null())
    assert paired.height > 0
    assert (paired["ocf"] - paired["ocf_alt"]).abs().max() == pytest.approx(0.0)


def test_parse_financials_per_quarter() -> None:
    """Financials 單季：4 季 Revenue 加總 ≈ 全年（2023 ≈ 2,162bn）。"""
    df = _parse_finmind_long(
        _load_json("financials_2330.json"), _FINANCIALS_FIELD_MAP, _FINANCIALS_WIDE_SCHEMA
    )
    fy23 = df.filter(pl.col("year") == 2023)["revenue"].sum()
    assert fy23 == pytest.approx(2_161_735_841_000.0, rel=1e-6)
    # 單季 EPS 每季 < 全年加總（確認非累計）
    q = df.filter((pl.col("year") == 2024) & (pl.col("quarter") == 3))["eps"].item()
    assert 0 < q < 20


def test_parse_balancesheet_filters_per_rows() -> None:
    """`<type>_per` 占比列不在 field_map → 不落表；只留絕對金額欄。"""
    df = _parse_finmind_long(
        _load_json("balancesheet_2330.json"),
        _BALANCESHEET_FIELD_MAP,
        _BALANCESHEET_WIDE_SCHEMA,
    )
    assert "equity" in df.columns
    assert not any(c.endswith("_per") for c in df.columns)
    latest = df.sort(["year", "quarter"]).tail(1)
    assert latest["cash"].item() > 0
    assert latest["total_assets"].item() > latest["equity"].item()


def test_parse_long_skips_unmapped_and_bad_rows() -> None:
    payload = {
        "msg": "success",
        "status": 200,
        "data": [
            {"date": "2024-03-31", "stock_id": "9999", "type": "Revenue", "value": 100.0},
            {"date": "2024-03-31", "stock_id": "9999", "type": "SomeUnmapped", "value": 5.0},
            {"date": "bad-date", "stock_id": "9999", "type": "Revenue", "value": 1.0},
            {"stock_id": "9999", "type": "Revenue", "value": 1.0},
            {"date": "2024-06-30", "type": "Revenue", "value": 1.0},
        ],
    }
    df = _parse_finmind_long(payload, _FINANCIALS_FIELD_MAP, _FINANCIALS_WIDE_SCHEMA)
    assert df.height == 1
    assert df["revenue"].item() == pytest.approx(100.0)
    assert df["operating_income"].item() is None


def test_parse_long_empty() -> None:
    df = _parse_finmind_long({"data": []}, _FINANCIALS_FIELD_MAP, _FINANCIALS_WIDE_SCHEMA)
    assert df.is_empty()
    assert set(df.columns) == set(_FINANCIALS_WIDE_SCHEMA)


def test_load_wide_history_dedup(tmp_path: Path) -> None:
    """同 (stock_id, year, quarter) 重複時 keep last；無檔回空表。"""
    assert load_cashflow_history(tmp_path).is_empty()
    df_a = pl.DataFrame(
        {"stock_id": ["1"], "year": [2024], "quarter": [4], "revenue": [10.0],
         "operating_income": [1.0], "income_after_tax": [1.0], "eps": [0.1]},
        schema=_FINANCIALS_WIDE_SCHEMA,
    )
    df_b = df_a.with_columns(pl.lit(20.0).alias("revenue"))
    df_a.write_parquet(tmp_path / "financials_1.parquet")
    df_b.write_parquet(tmp_path / "financials_1b.parquet")
    got = load_financials_history(tmp_path)
    assert got.height == 1
    assert got["revenue"].item() == pytest.approx(20.0)
    assert load_balancesheet_history(tmp_path).is_empty()


# ── _parse_month_revenue（M-Pick2，docs/32）────────────────────────────────


def test_parse_month_revenue_uses_revenue_month_not_publish_date() -> None:
    """year/month＝營收所屬月（revenue_year/month），不是 date（次月 1 日）。"""
    df = _parse_month_revenue(_load_json("month_revenue_2330.json"))
    first = df.row(0, named=True)
    assert (first["stock_id"], first["year"], first["month"]) == ("2330", 2018, 12)
    assert first["revenue"] == pytest.approx(89830598000.0)
    assert first["create_date"] is None  # 舊列 create_time 空字串 → null，不臆造


def test_parse_month_revenue_dedup_bad_rows_and_create_date() -> None:
    df = _parse_month_revenue(_load_json("month_revenue_2330.json"))
    # 8 列：無 stock_id／月份 13／缺 revenue_month 三列略過；2026-08 重複留最後一筆
    assert df.height == 4
    aug = df.filter((pl.col("year") == 2026) & (pl.col("month") == 8)).row(0, named=True)
    assert aug["revenue"] == pytest.approx(514805337001.0)
    assert aug["create_date"] == date(2026, 9, 10)  # 帶時間字串只取日期
    jul = df.filter((pl.col("year") == 2026) & (pl.col("month") == 7)).row(0, named=True)
    assert jul["revenue"] == 0.0  # 0 照抄，分母判斷交給消費端
    assert jul["create_date"] is None  # 無法解析 → null


def test_parse_month_revenue_empty() -> None:
    df = _parse_month_revenue({"msg": "success", "status": 200, "data": []})
    assert df.is_empty()
    assert dict(df.schema) == _MONTH_REVENUE_SCHEMA


def test_load_month_revenue_history_dedup(tmp_path: Path) -> None:
    assert load_month_revenue_history(tmp_path).is_empty()
    a = pl.DataFrame(
        {"stock_id": ["1"], "year": [2025], "month": [3], "revenue": [10.0],
         "create_date": [None]},
        schema=_MONTH_REVENUE_SCHEMA,
    )
    a.write_parquet(tmp_path / "month_revenue_1.parquet")
    a.with_columns(pl.lit(20.0).alias("revenue")).write_parquet(
        tmp_path / "month_revenue_1b.parquet"
    )
    got = load_month_revenue_history(tmp_path)
    assert got.height == 1
    assert got["revenue"].item() == pytest.approx(20.0)


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


def test_fetch_all_per_circuit_breaker_on_request_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """連續 3 次「請求失敗」（last_request_failed）→ 停止後續（鐵律 1 精神）。"""
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    calls: list[str] = []
    client = _client(cache_dir)

    def _fake_fetch(sid: str, start_date: str = "", force: bool = False) -> pl.DataFrame:
        calls.append(sid)
        client.last_request_failed = True  # 模擬 HTTP/額度失敗
        return pl.DataFrame(schema=finmind._PER_SCHEMA)

    monkeypatch.setattr(client, "fetch_taiwan_stock_per", _fake_fetch)
    result = client.fetch_all_per(["A", "B", "C", "D", "E"])
    assert len(result) == 5
    assert calls == ["A", "B", "C"]  # 第 3 次觸發停止，D/E 不再打


def test_fetch_all_per_empty_data_does_not_trip_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{"data":[]}`（FinMind 沒這檔 PER，非異常）連續多檔也不該觸發斷路器。"""
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    calls: list[str] = []
    client = _client(cache_dir)

    def _fake_fetch(sid: str, start_date: str = "", force: bool = False) -> pl.DataFrame:
        calls.append(sid)
        client.last_request_failed = False  # 請求成功、只是沒資料
        return pl.DataFrame(schema=finmind._PER_SCHEMA)

    monkeypatch.setattr(client, "fetch_taiwan_stock_per", _fake_fetch)
    client.fetch_all_per(["A", "B", "C", "D", "E"])
    assert calls == ["A", "B", "C", "D", "E"]  # 全部都打，沒停


def test_fetch_taiwan_stock_per_empty_data_not_marked_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"msg": "success", "status": 200, "data": []}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    client = _client(cache_dir, max_retries=0)
    df = client.fetch_taiwan_stock_per("7853")
    assert df.is_empty()
    assert client.last_request_failed is False  # data=[] 不算失敗


def test_fetch_taiwan_stock_per_http_failure_marked_failed(tmp_path: Path) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    client = _client(
        cache_dir, base_url="http://127.0.0.1:1", max_retries=0, timeout_sec=1.0
    )
    client.fetch_taiwan_stock_per("9999")
    assert client.last_request_failed is True


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


def test_fetch_month_revenue_cache_hit_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    pl.DataFrame(
        {"stock_id": ["2330"], "year": [2026], "month": [8], "revenue": [5.0],
         "create_date": [date(2026, 9, 10)]},
        schema=_MONTH_REVENUE_SCHEMA,
    ).write_parquet(cache_dir / "month_revenue_2330.parquet")

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("httpx.get 不應被呼叫（快取新鮮）")

    monkeypatch.setattr(httpx, "get", _boom)
    df = _client(cache_dir).fetch_month_revenue("2330")
    assert df.height == 1


def test_fetch_month_revenue_writes_cache_and_flags_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "finmind"
    cache_dir.mkdir()
    payload = _load_json("month_revenue_2330.json")

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    client = _client(cache_dir, max_retries=0)
    df = client.fetch_month_revenue("2330")
    assert df.height == 4 and not client.last_request_failed
    assert (cache_dir / "month_revenue_2330.parquet").exists()

    def _down(*a: object, **k: object) -> object:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    down = _client(tmp_path / "empty", max_retries=0)
    assert down.fetch_month_revenue("9999").is_empty()
    assert down.last_request_failed  # 請求失敗 → 斷路器可計數
