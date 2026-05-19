"""tests/data/test_tpex.py — TPEX parser 與 fetch_stock_history 分派測試。"""

import json
from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.data.cache import save_parquet
from tw_screener.data.twse import (
    TWSEClient,
    _parse_stock_day,
    _parse_tpex_stock_day,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "tpex"


def _load_fixture() -> dict:
    with open(FIXTURE_DIR / "tradingStock_sample.json", encoding="utf-8") as f:
        return json.load(f)


# ─── _parse_tpex_stock_day ────────────────────────────────────────────────────


def test_parse_tpex_stock_day_schema_matches_twse():
    """TPEX 解析結果的 column schema 應該與 TWSE _parse_stock_day 完全一致。"""
    payload = _load_fixture()
    df_tpex = _parse_tpex_stock_day(payload, "3293")
    # 用一個簡單的 TWSE-style 空 payload 來取得 TWSE schema
    df_twse_empty = _parse_stock_day({"stat": "OK"}, "2330")
    assert set(df_tpex.columns) == set(df_twse_empty.columns)
    for col in df_tpex.columns:
        assert df_tpex.schema[col] == df_twse_empty.schema[col], (
            f"column {col} schema mismatch: {df_tpex.schema[col]} vs {df_twse_empty.schema[col]}"
        )


def test_parse_tpex_stock_day_basic_fields():
    """基本欄位（日期、開高低收）應正確對應。"""
    payload = _load_fixture()
    df = _parse_tpex_stock_day(payload, "3293")
    assert len(df) == 2
    row0 = df.row(0, named=True)
    assert row0["date"] == date(2025, 10, 1)
    assert row0["stock_id"] == "3293"
    assert row0["open"] == 800.0
    assert row0["high"] == 810.0
    assert row0["low"] == 795.0
    assert row0["close"] == 805.0
    assert row0["change"] == 5.0


def test_parse_tpex_stock_day_units_converted():
    """成交張數 / 成交仟元應 ×1000 轉換為 股 / 元。"""
    payload = _load_fixture()
    df = _parse_tpex_stock_day(payload, "3293")
    row0 = df.row(0, named=True)
    assert row0["trade_volume"] == 1234 * 1000   # 成交張數（仟股）→ 股
    assert row0["trade_value"] == 987654 * 1000  # 成交仟元 → 元
    assert row0["transaction"] == 1500


def test_parse_tpex_stock_day_empty_data():
    """tables[0].data 為空 list → 回空 DF（schema 仍正確）。"""
    payload = {
        "stat": "ok",
        "tables": [
            {
                "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                           "收盤", "漲跌", "筆數"],
                "data": [],
            }
        ],
    }
    df = _parse_tpex_stock_day(payload, "9999")
    assert df.is_empty()
    assert "date" in df.columns
    assert "trade_volume" in df.columns


def test_parse_tpex_stock_day_no_tables():
    """payload 無 tables → 回空 DF。"""
    df = _parse_tpex_stock_day({"stat": "ok"}, "9999")
    assert df.is_empty()


def test_parse_tpex_stock_day_stat_not_ok():
    """stat != 'ok' → 回空 DF。"""
    df = _parse_tpex_stock_day({"stat": "查無資料"}, "9999")
    assert df.is_empty()


def test_parse_tpex_stock_day_field_with_spaces():
    """「日 期」欄名中間有空格，標準化後仍可解析。"""
    payload = _load_fixture()
    df = _parse_tpex_stock_day(payload, "3293")
    assert df["date"][0] == date(2025, 10, 1)


# ─── fetch_stock_history dispatch ─────────────────────────────────────────────


def _make_client(tmp_path: Path) -> TWSEClient:
    return TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )


def test_load_otc_ids_returns_empty_when_no_cache(tmp_path: Path):
    """無 otc_industry cache 又抓不到時，_load_otc_ids 回空 set。"""
    client = _make_client(tmp_path)
    # mock fetch_otc_industry 回空
    client.fetch_otc_industry = lambda: pl.DataFrame()  # type: ignore[method-assign]
    assert client._load_otc_ids() == set()


def test_load_otc_ids_reads_cache(tmp_path: Path):
    """有 otc_industry cache 時應回 stock_id 集合。"""
    client = _make_client(tmp_path)
    otc_df = pl.DataFrame(
        {
            "stock_id": ["3293", "5258", "8299"],
            "stock_name": ["鈊象", "群電", "群聯"],
            "industry_code": ["31", "31", "31"],
            "industry_name": ["其他電子業", "其他電子業", "其他電子業"],
        }
    )
    client.fetch_otc_industry = lambda: otc_df  # type: ignore[method-assign]
    assert client._load_otc_ids() == {"3293", "5258", "8299"}


def test_fetch_stock_history_dispatches_to_tpex_for_otc(tmp_path: Path):
    """OTC stock_id 應走 TPEX path（檢查 URL 含 tradingStock）。"""
    client = _make_client(tmp_path)
    client._otc_ids = {"3293"}

    captured: list[str] = []

    def mock_get_legacy(url: str) -> dict:
        captured.append(url)
        return _load_fixture()  # 回 TPEX 樣式

    client._get_legacy = mock_get_legacy  # type: ignore[method-assign]

    df = client.fetch_stock_history("3293", months=1)
    assert len(captured) == 1
    assert "tradingStock" in captured[0]
    assert "code=3293" in captured[0]
    assert not df.is_empty()
    # schema 應與 TWSE 一致
    assert "trade_volume" in df.columns


def test_fetch_stock_history_dispatches_to_twse_for_listed(tmp_path: Path):
    """非 OTC stock_id 應走 TWSE STOCK_DAY path。"""
    client = _make_client(tmp_path)
    client._otc_ids = {"3293"}  # 不含 2330

    captured: list[str] = []

    def mock_get_legacy(url: str) -> dict:
        captured.append(url)
        return {"stat": "OK", "fields": [], "data": []}

    client._get_legacy = mock_get_legacy  # type: ignore[method-assign]

    client.fetch_stock_history("2330", months=1)
    assert len(captured) == 1
    assert "STOCK_DAY" in captured[0]
    assert "stockNo=2330" in captured[0]


def test_fetch_stock_history_tpex_writes_cache(tmp_path: Path):
    """TPEX 抓回資料後，cache 檔名應為 stock_day_{sid}_{YYYYMM}.parquet（與 TWSE 共用）。"""
    client = _make_client(tmp_path)
    client._get_legacy = lambda url: _load_fixture()  # type: ignore[method-assign]
    df = client.fetch_stock_history_tpex("3293", months=1)
    assert not df.is_empty()
    # 應該寫入當月份 cache
    current_ym = date.today().strftime("%Y%m")
    cache_file = tmp_path / f"stock_day_3293_{current_ym}.parquet"
    assert cache_file.exists(), f"預期 cache 檔 {cache_file} 存在"


def test_fetch_stock_history_tpex_skips_on_consecutive_empty(tmp_path: Path):
    """TPEX 連續 2 月空資料應提早終止（節省 API 呼叫）。"""
    client = _make_client(tmp_path)
    call_count = [0]

    def mock_get_legacy(url: str) -> dict:
        call_count[0] += 1
        return {"stat": "ok", "tables": [{"fields": [], "data": []}]}

    client._get_legacy = mock_get_legacy  # type: ignore[method-assign]

    client.fetch_stock_history_tpex("9999", months=6)
    # 應該打 2 次後 break，不是打 6 次
    assert call_count[0] == 2, f"預期 2 次 API call（連續 2 月空 break），實際 {call_count[0]} 次"


def test_fetch_stock_history_tpex_cache_hit_no_network(tmp_path: Path):
    """TPEX 有 cache 時不應再打網（fast path）。"""
    client = _make_client(tmp_path)
    # 預放 cache（用今天的 YYYYMM）
    current_ym = date.today().strftime("%Y%m")
    cache_file = tmp_path / f"stock_day_3293_{current_ym}.parquet"
    sample_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 15)],
            "stock_id": ["3293"],
            "trade_volume": [1234000],
            "trade_value": [987654000],
            "open": [800.0],
            "high": [810.0],
            "low": [795.0],
            "close": [805.0],
            "change": [5.0],
            "transaction": [1500],
        }
    )
    save_parquet(sample_df, cache_file)

    call_count = [0]
    client._get_legacy = lambda url: call_count.__setitem__(0, call_count[0] + 1) or {}  # type: ignore[method-assign]

    df = client.fetch_stock_history_tpex("3293", months=1)
    assert call_count[0] == 0, "cache hit 時不應打網"
    assert len(df) == 1
