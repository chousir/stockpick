"""tests/data/test_twse.py — 證交所資料層單元測試（全離線）。"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tw_screener.data.cache import find_latest, is_fresh, load_parquet, save_parquet
from tw_screener.data.twse import (
    _TWSE_INDUSTRY_NAMES,
    TWSEClient,
    _clean_float,
    _clean_int,
    _months_back,
    _parse_daily_all,
    _parse_institutional,
    _parse_listed_industry,
    _parse_revenue,
    _parse_stock_day,
    _roc_compact_to_date,
    _roc_pubdate_to_ym,
    _roc_to_date,
    _roc_ym_to_ym,
    create_client,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "twse"


# ─── 字串轉換工具 ─────────────────────────────────────────────────────────────


def test_clean_int_comma():
    assert _clean_int("18,560") == 18560


def test_clean_int_no_comma():
    assert _clean_int("5000") == 5000


def test_clean_int_invalid():
    with pytest.raises(ValueError):
        _clean_int("--")


def test_clean_float_positive():
    assert _clean_float("5.00") == pytest.approx(5.0)


def test_clean_float_negative():
    assert _clean_float("-0.50") == pytest.approx(-0.5)


def test_clean_float_dash():
    assert _clean_float("--") is None


def test_clean_float_empty():
    assert _clean_float("") is None


def test_roc_to_date():
    assert _roc_to_date("115/05/15") == date(2026, 5, 15)


def test_roc_to_date_early():
    assert _roc_to_date("112/01/01") == date(2023, 1, 1)


def test_roc_compact_to_date():
    assert _roc_compact_to_date("1150514") == date(2026, 5, 14)


def test_roc_pubdate_to_ym():
    assert _roc_pubdate_to_ym("1140416") == "202504"


def test_roc_pubdate_to_ym_march():
    assert _roc_pubdate_to_ym("1140317") == "202503"


def test_roc_ym_to_ym():
    assert _roc_ym_to_ym("11504") == "202604"


def test_roc_ym_to_ym_single_digit_month():
    assert _roc_ym_to_ym("1151") == "202601"


# ─── Parse 函數（離線 fixture）────────────────────────────────────────────────


def test_parse_daily_all():
    with open(FIXTURE_DIR / "daily_sample.json") as f:
        data = json.load(f)
    df = _parse_daily_all(data)
    assert len(df) == 2
    assert df["stock_id"].to_list() == ["2330", "2317"]
    assert df["date"][0] == date(2026, 5, 14)
    assert df["close"][0] == pytest.approx(1075.0)
    assert df["change"][1] == pytest.approx(-0.5)


def test_parse_daily_all_empty():
    df = _parse_daily_all([])
    assert df.is_empty()
    assert "stock_id" in df.columns
    assert "close" in df.columns


def test_parse_institutional():
    with open(FIXTURE_DIR / "institutional_sample.json") as f:
        data = json.load(f)
    df = _parse_institutional(data)
    assert len(df) == 2
    assert df["stock_id"][0] == "2330"
    assert df["date"][0] == date(2026, 5, 15)
    assert df["foreign_net"][0] == 2000
    assert df["total_net"][0] == 2400


def test_parse_institutional_empty():
    df = _parse_institutional([])
    assert df.is_empty()
    assert "date" in df.columns


def test_parse_stock_day():
    with open(FIXTURE_DIR / "stock_day_sample.json") as f:
        data = json.load(f)
    df = _parse_stock_day(data, "2330")
    assert len(df) == 3
    assert df["stock_id"].unique().to_list() == ["2330"]
    assert df["date"][0] == date(2026, 5, 1)
    assert df["close"][0] == pytest.approx(1070.0)


def test_parse_revenue():
    with open(FIXTURE_DIR / "revenue_sample.json") as f:
        data = json.load(f)
    df = _parse_revenue(data)
    assert len(df) == 3
    # 2330 的兩筆（不同月份）
    df_2330 = df.filter(pl.col("stock_id") == "2330")
    assert len(df_2330) == 2
    # 資料年月 "11504" → "202604"
    assert "202604" in df_2330["year_month"].to_list()
    assert df_2330["revenue"][0] == 260040059
    assert df_2330["yoy_pct"][0] == pytest.approx(20.34)


def test_parse_revenue_empty():
    df = _parse_revenue([])
    assert df.is_empty()
    assert "stock_id" in df.columns


def test_parse_listed_industry():
    with open(FIXTURE_DIR / "industry_sample.json") as f:
        data = json.load(f)
    df = _parse_listed_industry(data)
    assert len(df) == 8
    row_2330 = df.filter(pl.col("stock_id") == "2330").to_dicts()[0]
    assert row_2330["industry_code"] == "24"
    assert row_2330["industry_name"] == "半導體業"


def test_parse_listed_industry_unknown_code():
    """未知產業碼應 fallback 為「其他」。"""
    data = [{"公司代號": "9999X", "公司名稱": "測試", "產業別": "99"}]
    df = _parse_listed_industry(data)
    assert df["industry_name"][0] == "其他"


def test_parse_listed_industry_empty():
    df = _parse_listed_industry([])
    assert df.is_empty()
    assert "industry_code" in df.columns


def test_twse_industry_names_has_semiconductor():
    assert _TWSE_INDUSTRY_NAMES["24"] == "半導體業"


# ─── Cache 工具 ──────────────────────────────────────────────────────────────


def test_cache_is_fresh_missing(tmp_path: Path):
    assert not is_fresh(tmp_path / "nonexistent.parquet", 24.0)


def test_cache_is_fresh_new_file(tmp_path: Path):
    p = tmp_path / "test.parquet"
    p.touch()
    assert is_fresh(p, 24.0)


def test_cache_is_stale(tmp_path: Path):
    p = tmp_path / "old.parquet"
    p.touch()
    old_ts = (datetime.now() - timedelta(hours=25)).timestamp()
    os.utime(p, (old_ts, old_ts))
    assert not is_fresh(p, 24.0)


def test_cache_save_load_roundtrip(tmp_path: Path):
    df = pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    p = tmp_path / "subdir" / "test.parquet"
    save_parquet(df, p)
    loaded = load_parquet(p)
    assert loaded.shape == (3, 2)
    assert loaded["a"].to_list() == [1, 2, 3]


def test_find_latest(tmp_path: Path):
    (tmp_path / "a.parquet").touch()
    (tmp_path / "b.parquet").touch()
    result = find_latest(tmp_path, "*.parquet")
    assert result is not None
    assert result.suffix == ".parquet"


def test_find_latest_missing_dir(tmp_path: Path):
    result = find_latest(tmp_path / "nonexistent", "*.parquet")
    assert result is None


# ─── TWSEClient ──────────────────────────────────────────────────────────────


def test_client_init(tmp_path: Path):
    client = TWSEClient(
        base_url="https://openapi.twse.com.tw/v1",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test-agent/1.0",
        interval_sec=1.0,
    )
    assert client.base_url == "https://openapi.twse.com.tw/v1"
    assert client.ttl_hours == 6.0


def test_fetch_daily_all_cache_hit(tmp_path: Path):
    """第二次呼叫命中快取，不呼叫 _get。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    # 預先放快取
    today = date.today().strftime("%Y%m%d")
    cache_file = tmp_path / f"daily_{today}.parquet"
    sample_df = pl.DataFrame(
        {
            "stock_id": ["2330"],
            "name": ["台積電"],
            "trade_volume": [1000],
            "trade_value": [1000000],
            "open": [1075.0],
            "high": [1080.0],
            "low": [1070.0],
            "close": [1075.0],
            "change": [5.0],
            "transaction": [500],
        }
    )
    save_parquet(sample_df, cache_file)

    get_calls: list[str] = []

    def mock_get(endpoint: str) -> list:
        get_calls.append(endpoint)
        return []

    client._get = mock_get  # type: ignore[method-assign]

    df = client.fetch_daily_all()
    assert len(get_calls) == 0, "快取命中時不應呼叫 _get"
    assert len(df) == 1


def test_fetch_institutional_cache_hit(tmp_path: Path):
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    today = date.today().strftime("%Y%m%d")
    cache_file = tmp_path / f"institutional_{today}.parquet"
    sample_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 15)],
            "stock_id": ["2330"],
            "stock_name": ["台積電"],
            "foreign_net": [2000],
            "trust_net": [500],
            "dealer_net": [-100],
            "total_net": [2400],
        }
    )
    save_parquet(sample_df, cache_file)

    get_calls: list[str] = []
    client._get = lambda ep: get_calls.append(ep) or []  # type: ignore[method-assign]

    df = client.fetch_institutional()
    assert len(get_calls) == 0
    assert len(df) == 1


def test_fetch_stock_institutional_empty(tmp_path: Path):
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    df = client.fetch_stock_institutional("2330")
    assert df.is_empty()


def test_fetch_stock_institutional_filters(tmp_path: Path):
    """應只回傳指定 stock_id 的資料。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    sample = pl.DataFrame(
        {
            "date": [date(2026, 5, 15), date(2026, 5, 15)],
            "stock_id": ["2330", "2317"],
            "stock_name": ["台積電", "鴻海"],
            "foreign_net": [2000, 200],
            "trust_net": [500, 0],
            "dealer_net": [-100, 0],
            "total_net": [2400, 200],
        }
    )
    save_parquet(sample, tmp_path / "institutional_20260515.parquet")

    df = client.fetch_stock_institutional("2330")
    assert len(df) == 1
    assert df["stock_id"][0] == "2330"


def test_fetch_stock_revenue_empty(tmp_path: Path):
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    df = client.fetch_stock_revenue("2330")
    assert df.is_empty()


def test_create_client():
    """從真實 settings.yaml 建立 client（不打網路）。"""
    client = create_client(Path("config/settings.yaml"))
    assert "openapi.twse.com.tw" in client.base_url
    assert client.interval_sec >= 1.0
    assert client.ttl_hours > 0


def test_fetch_revenue_cache_hit(tmp_path: Path):
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    ym = date.today().strftime("%Y%m")
    cache_file = tmp_path / f"revenue_{ym}.parquet"
    sample = pl.DataFrame(
        {
            "stock_id": ["2330"],
            "company_name": ["台積電"],
            "year_month": ["202504"],
            "revenue": [260040059],
            "prev_year_revenue": [216087780],
            "yoy_pct": [20.34],
        }
    )
    save_parquet(sample, cache_file)

    get_calls: list[str] = []
    client._get = lambda ep: get_calls.append(ep) or []  # type: ignore[method-assign]

    df = client.fetch_revenue()
    assert len(get_calls) == 0
    assert len(df) == 1


def test_fetch_daily_all_empty_response(tmp_path: Path):
    """_get 回空 list 時應回傳空 DataFrame，且不寫快取。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    client._get = lambda ep: []  # type: ignore[method-assign]

    df = client.fetch_daily_all()
    assert df.is_empty()
    today = date.today().strftime("%Y%m%d")
    assert not (tmp_path / f"daily_{today}.parquet").exists()


# ─── _months_back ─────────────────────────────────────────────────────────────


def test_months_back_same_year():
    d = _months_back(date(2026, 5, 15), 2)
    assert d == date(2026, 3, 1)


def test_months_back_wraparound():
    d = _months_back(date(2026, 1, 15), 2)
    assert d == date(2025, 11, 1)


def test_months_back_zero():
    d = _months_back(date(2026, 5, 15), 0)
    assert d == date(2026, 5, 1)


# ─── models.py ────────────────────────────────────────────────────────────────

from tw_screener.data.models import DailyPrice, InstitutionalTrade, MonthlyRevenue  # noqa: E402


def test_model_daily_price():
    m = DailyPrice(
        stock_id="2330",
        name="台積電",
        trade_volume=18560,
        trade_value=19924200000,
        open=1075.0,
        high=1080.0,
        low=1070.0,
        close=1075.0,
        change=5.0,
        transaction=12345,
    )
    assert m.stock_id == "2330"
    assert m.close == 1075.0


def test_model_daily_price_null_change():
    m = DailyPrice(
        stock_id="0050",
        name="元大台灣50",
        trade_volume=5000,
        trade_value=875000000,
        open=175.0,
        high=176.0,
        low=174.0,
        close=175.0,
        change=None,
        transaction=3000,
    )
    assert m.change is None


def test_model_institutional_trade():
    m = InstitutionalTrade(
        date=date(2026, 5, 15),
        stock_id="2330",
        stock_name="台積電",
        foreign_net=2000,
        trust_net=500,
        dealer_net=-100,
        total_net=2400,
    )
    assert m.total_net == 2400


def test_model_monthly_revenue():
    m = MonthlyRevenue(
        stock_id="2330",
        company_name="台積電",
        year_month="202504",
        revenue=260040059,
        prev_year_revenue=216087780,
        yoy_pct=20.34,
    )
    assert m.year_month == "202504"


def test_model_monthly_revenue_nulls():
    m = MonthlyRevenue(
        stock_id="9999",
        company_name="新創公司",
        year_month="202504",
        revenue=1000000,
        prev_year_revenue=None,
        yoy_pct=None,
    )
    assert m.prev_year_revenue is None


# ─── Legacy 解析器補充測試 ─────────────────────────────────────────────────────


def test_parse_stock_day_bad_stat():
    """stat 非 OK 時應回傳空 DataFrame（schema 完整）。"""
    df = _parse_stock_day({"stat": "很抱歉，沒有符合條件的資料!"}, "2330")
    assert df.is_empty()
    assert "close" in df.columns


def test_parse_stock_day_empty_payload():
    df = _parse_stock_day({}, "2330")
    assert df.is_empty()


def test_parse_institutional_combines_foreign_and_dealer():
    """foreign_net = 外陸資買賣超(不含外資自營商) + 外資自營商買賣超。"""
    payload = {
        "stat": "OK",
        "date": "20260515",
        "fields": [
            "證券代號",
            "證券名稱",
            "外陸資買賣超股數(不含外資自營商)",
            "外資自營商買賣超股數",
            "投信買賣超股數",
            "自營商買賣超股數",
            "三大法人買賣超股數",
        ],
        "data": [["2330", "台積電", "1,000", "500", "100", "-50", "1,550"]],
    }
    df = _parse_institutional(payload)
    assert len(df) == 1
    assert df["foreign_net"][0] == 1500  # 1000 + 500
    assert df["trust_net"][0] == 100
    assert df["total_net"][0] == 1550


def test_parse_institutional_missing_date():
    """無 date 欄位時應視為空（避免寫錯日期到快取）。"""
    payload = {"stat": "OK", "fields": [], "data": []}
    df = _parse_institutional(payload)
    assert df.is_empty()


def test_fetch_stock_ohlcv_prefers_stock_day_cache(tmp_path: Path):
    """有 stock_day_*.parquet 時優先讀，並去重日期。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    # stock_day 月快取（精準歷史）
    stock_day_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 1), date(2026, 5, 2)],
            "stock_id": ["2330", "2330"],
            "trade_volume": [1000, 1100],
            "trade_value": [1000000, 1100000],
            "open": [1070.0, 1080.0],
            "high": [1075.0, 1085.0],
            "low": [1065.0, 1075.0],
            "close": [1070.0, 1080.0],
            "change": [10.0, 10.0],
            "transaction": [500, 550],
        }
    )
    save_parquet(stock_day_df, tmp_path / "stock_day_2330_202605.parquet")

    # daily 全市場快取（同一天）
    daily_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 2)],
            "stock_id": ["2330"],
            "name": ["台積電"],
            "trade_volume": [9999],
            "trade_value": [9999],
            "open": [1080.0],
            "high": [1085.0],
            "low": [1075.0],
            "close": [1080.0],
            "change": [10.0],
            "transaction": [550],
        }
    )
    save_parquet(daily_df, tmp_path / "daily_20260502.parquet")

    df = client.fetch_stock_ohlcv("2330", n_days=60)
    # 兩個來源合併、去重後仍是 2 筆
    assert len(df) == 2
    assert df["close"].to_list() == [1070.0, 1080.0]
