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
    _parse_dividend_calendar,
    _parse_institutional,
    _parse_listed_industry,
    _parse_revenue,
    _parse_stock_day,
    _parse_tpex_institutional,
    _parse_valuation_ratios,
    _roc_compact_to_date,
    _roc_pubdate_to_ym,
    _roc_to_date,
    _roc_ym_to_ym,
    create_client,
    filter_dividend_calendar,
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


def test_parse_valuation_ratios_merges_both_markets():
    # 上市 BWIBBU_d：Date 西元緊湊、PEratio 空字串=虧損（PB 仍在）
    bwibbu = [
        {"Date": "20260612", "Code": "2330", "PEratio": "26.2",
         "PBratio": "7.5", "DividendYield": "1.50"},
        {"Date": "20260612", "Code": "1101", "PEratio": "",  # 虧損/無正盈餘 → null
         "PBratio": "0.78", "DividendYield": "3.26"},
    ]
    # 上櫃 peratio：Date 民國緊湊、PriceEarningRatio 'N/A'=無
    tpex = [
        {"Date": "1150612", "SecuritiesCompanyCode": "5483", "PriceEarningRatio": "12.53",
         "PriceBookRatio": "1.66", "YieldRatio": "5.98"},
        {"Date": "1150612", "SecuritiesCompanyCode": "6488", "PriceEarningRatio": "N/A",
         "PriceBookRatio": "2.10", "YieldRatio": "0.00"},
    ]
    df = _parse_valuation_ratios(bwibbu, tpex)
    by = {r["stock_id"]: r for r in df.iter_rows(named=True)}

    assert len(df) == 4
    assert by["2330"]["market"] == "上市"
    assert by["2330"]["date"] == date(2026, 6, 12)
    assert by["2330"]["pe"] == pytest.approx(26.2)
    assert by["1101"]["pe"] is None and by["1101"]["pbr"] == pytest.approx(0.78)
    # 上櫃民國日期換算正確、N/A → null PE 但 PB 仍在
    assert by["5483"]["market"] == "上櫃"
    assert by["5483"]["date"] == date(2026, 6, 12)
    assert by["6488"]["pe"] is None and by["6488"]["pbr"] == pytest.approx(2.10)


def test_parse_valuation_ratios_skips_all_null_and_empty():
    # PE 與 PB 皆無 → 整列略過（停牌/無資料）
    bwibbu = [{"Date": "20260612", "Code": "9999", "PEratio": "", "PBratio": "",
               "DividendYield": ""}]
    df = _parse_valuation_ratios(bwibbu, [])
    assert df.is_empty()
    assert {"stock_id", "pe", "pbr", "dividend_yield", "market"} <= set(df.columns)


def test_parse_dividend_calendar():
    with open(FIXTURE_DIR / "twt48u_all_sample.json") as f:
        data = json.load(f)
    df = _parse_dividend_calendar(data)
    assert len(df) == 5
    assert df["ex_date"][0] == date(2026, 5, 15)
    assert df["stock_id"][0] == "2330"
    assert df["type"][0] == "息"
    assert df["cash_dividend"][0] == pytest.approx(5.0)
    # 權值股：無現金股利、有股票股利率
    row1234 = df.filter(pl.col("stock_id") == "1234")
    assert row1234["type"][0] == "權"
    assert row1234["cash_dividend"][0] is None
    assert row1234["stock_dividend_ratio"][0] == pytest.approx(100.0)


def test_parse_dividend_calendar_empty():
    df = _parse_dividend_calendar([])
    assert df.is_empty()
    assert "ex_date" in df.columns
    assert "cash_dividend" in df.columns


def test_filter_dividend_calendar():
    with open(FIXTURE_DIR / "twt48u_all_sample.json") as f:
        data = json.load(f)
    df = _parse_dividend_calendar(data)
    today = date(2026, 5, 21)
    out = filter_dividend_calendar(df, today, 14, ["2891", "6446", "2330"])
    # 窗 [05-21, 06-04] 且為候選股：2330(05-15 過去)、1234(非候選)、2881(06-10 窗外) 皆出局
    assert out["stock_id"].to_list() == ["2891", "6446"]
    assert out["ex_date"].to_list() == [date(2026, 5, 22), date(2026, 5, 27)]


def test_filter_dividend_calendar_empty_candidates():
    with open(FIXTURE_DIR / "twt48u_all_sample.json") as f:
        data = json.load(f)
    df = _parse_dividend_calendar(data)
    out = filter_dividend_calendar(df, date(2026, 5, 21), 14, [])
    assert out.is_empty()


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
    """命中快取時不呼叫 _get；檔名用內容 max(date)，不再用 today。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    # 預先放快取（用 trading_date 而非 today 當檔名，模擬週末跑後的狀態）
    cache_file = tmp_path / "daily_20260515.parquet"
    sample_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 15)],
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


def test_fetch_daily_all_saves_with_max_date(tmp_path: Path):
    """新抓時，檔名要用回傳資料的 max(date)，不是 today。"""
    client = TWSEClient(
        base_url="https://openapi.twse.com.tw/v1",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )

    def mock_get(endpoint: str) -> list:
        return [
            {
                "Date": "1150515",  # ROC 民國 115/05/15 → 西元 2026-05-15
                "Code": "2330",
                "Name": "台積電",
                "TradeVolume": "1000",
                "TradeValue": "1000000",
                "OpeningPrice": "1075.0",
                "HighestPrice": "1080.0",
                "LowestPrice": "1070.0",
                "ClosingPrice": "1075.0",
                "Change": "5.0",
                "Transaction": "500",
            }
        ]

    client._get = mock_get  # type: ignore[method-assign]

    df = client.fetch_daily_all()
    assert not df.is_empty()
    # 檔名應該是 daily_20260515.parquet（max date），不是 today
    expected_file = tmp_path / "daily_20260515.parquet"
    assert expected_file.exists(), f"預期建立 {expected_file}"


def test_latest_trading_date_from_cache(tmp_path: Path):
    """latest_trading_date 應從 cache 內容取 max(date)。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    cache_file = tmp_path / "daily_20260515.parquet"
    sample_df = pl.DataFrame(
        {
            "date": [date(2026, 5, 14), date(2026, 5, 15)],
            "stock_id": ["2330", "2330"],
            "name": ["台積電", "台積電"],
            "trade_volume": [1000, 1000],
            "trade_value": [1000000, 1000000],
            "open": [1070.0, 1075.0],
            "high": [1080.0, 1080.0],
            "low": [1065.0, 1070.0],
            "close": [1075.0, 1078.0],
            "change": [5.0, 3.0],
            "transaction": [500, 500],
        }
    )
    save_parquet(sample_df, cache_file)

    client._get = lambda ep: []  # type: ignore[method-assign]
    td = client.latest_trading_date()
    assert td == date(2026, 5, 15)


def test_latest_trading_date_returns_none_when_no_data(tmp_path: Path):
    """完全無 cache 又抓不到資料時，latest_trading_date 回 None。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    client._get = lambda ep: []  # type: ignore[method-assign]
    assert client.latest_trading_date() is None


def test_fetch_institutional_cache_hit(tmp_path: Path):
    """T86 cache 用 trading_date 為檔名，不是 today。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    # 先放 daily cache 才能讓 latest_trading_date 拿到 5/15
    daily_cache = tmp_path / "daily_20260515.parquet"
    save_parquet(
        pl.DataFrame(
            {
                "date": [date(2026, 5, 15)],
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
        daily_cache,
    )

    # 預放 institutional cache（用 trading_date 命名）
    inst_cache = tmp_path / "institutional_20260515.parquet"
    save_parquet(
        pl.DataFrame(
            {
                "date": [date(2026, 5, 15)],
                "stock_id": ["2330"],
                "stock_name": ["台積電"],
                "foreign_net": [2000],
                "trust_net": [500],
                "dealer_net": [-100],
                "total_net": [2400],
            }
        ),
        inst_cache,
    )

    legacy_calls: list[str] = []
    client._get_legacy = (  # type: ignore[method-assign]
        lambda url: legacy_calls.append(url) or {}
    )
    client._get = lambda ep: []  # type: ignore[method-assign]

    df = client.fetch_institutional()
    assert len(legacy_calls) == 0, "trading_date cache hit 時不應再打 T86 API"
    assert len(df) == 1


def test_fetch_institutional_uses_trading_date_in_query(tmp_path: Path):
    """T86 query date 與檔名用 trading_date（從 latest_trading_date 取），不是 today。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    # daily cache → trading_date = 5/14
    daily_cache = tmp_path / "daily_20260514.parquet"
    save_parquet(
        pl.DataFrame(
            {
                "date": [date(2026, 5, 14)],
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
        daily_cache,
    )

    legacy_calls: list[str] = []
    client._get_legacy = (  # type: ignore[method-assign]
        lambda url: legacy_calls.append(url)
        or {
            "stat": "OK",
            "date": "20260514",
            "fields": [
                "證券代號", "證券名稱",
                "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
                "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數",
            ],
            "data": [["2330", "台積電", "1000", "0", "200", "100", "1300"]],
        }
    )
    client._get = lambda ep: []  # type: ignore[method-assign]

    df = client.fetch_institutional()
    assert len(legacy_calls) == 1
    assert "date=20260514" in legacy_calls[0], "query date 應為 trading_date"
    # 檔名也應用 trading_date
    assert (tmp_path / "institutional_20260514.parquet").exists()
    assert len(df) == 1


def _seed_daily(client_dir: Path, d: date) -> None:
    save_parquet(
        pl.DataFrame(
            {
                "date": [d], "stock_id": ["2330"], "name": ["台積電"],
                "trade_volume": [1], "trade_value": [1], "open": [1.0],
                "high": [1.0], "low": [1.0], "close": [1.0],
                "change": [0.0], "transaction": [1],
            }
        ),
        client_dir / f"daily_{d.strftime('%Y%m%d')}.parquet",
    )


def test_fetch_institutional_history_skips_weekends(tmp_path: Path):
    """回補：週末直接跳過不打網、收滿 days 即停，逐日存快取。"""
    import re

    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    # latest_trading_date = 2026-05-18（週一）→ 往回會經過週末 5/17、5/16
    assert date(2026, 5, 18).weekday() == 0
    _seed_daily(tmp_path, date(2026, 5, 18))
    client._get = lambda ep: []  # type: ignore[method-assign]

    called: list[str] = []

    def fake_legacy(url: str) -> dict:
        called.append(url)
        d = re.search(r"date=(\d{8})", url).group(1)  # type: ignore[union-attr]
        return {
            "stat": "OK", "date": d,
            "fields": [
                "證券代號", "證券名稱",
                "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
                "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數",
            ],
            "data": [["2330", "台積電", "1000", "0", "200", "100", "1300"]],
        }

    client._get_legacy = fake_legacy  # type: ignore[method-assign]

    df = client.fetch_institutional_history(days=3)
    dates = sorted(str(d) for d in df["date"].unique().to_list())
    assert dates == ["2026-05-14", "2026-05-15", "2026-05-18"]
    # 週末（5/16 六、5/17 日）不該被打
    assert not any("20260516" in u or "20260517" in u for u in called)
    assert (tmp_path / "institutional_20260518.parquet").exists()


def test_fetch_institutional_history_reuses_cache(tmp_path: Path):
    """已有快取的日期直接讀檔、不重打網。"""
    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    _seed_daily(tmp_path, date(2026, 5, 18))
    # 預放 5/18 法人快取
    save_parquet(
        pl.DataFrame(
            {
                "date": [date(2026, 5, 18)], "stock_id": ["2330"],
                "stock_name": ["台積電"], "foreign_net": [1],
                "trust_net": [0], "dealer_net": [0], "total_net": [1],
            }
        ),
        tmp_path / "institutional_20260518.parquet",
    )
    client._get = lambda ep: []  # type: ignore[method-assign]
    called: list[str] = []
    client._get_legacy = lambda url: called.append(url) or {}  # type: ignore[method-assign]

    client.fetch_institutional_history(days=1)
    assert called == [], "已有快取的日期不應重打 T86"


def test_load_institutional_history_reads_recent(tmp_path: Path):
    """純讀快取，回最近 n_days 個交易日（不打網）。"""
    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    for d in [date(2026, 5, 15), date(2026, 5, 18), date(2026, 5, 19)]:
        save_parquet(
            pl.DataFrame(
                {
                    "date": [d], "stock_id": ["2330"], "stock_name": ["台積電"],
                    "foreign_net": [1], "trust_net": [0],
                    "dealer_net": [0], "total_net": [1],
                }
            ),
            tmp_path / f"institutional_{d.strftime('%Y%m%d')}.parquet",
        )
    df = client.load_institutional_history(n_days=2)
    got = sorted(str(d) for d in df["date"].unique().to_list())
    assert got == ["2026-05-18", "2026-05-19"]


def test_load_institutional_history_empty(tmp_path: Path):
    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    df = client.load_institutional_history()
    assert df.is_empty()
    assert "total_net" in df.columns


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
    # 空 response → 不應建任何 daily_*.parquet
    assert list(tmp_path.glob("daily_*.parquet")) == []


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


# ─── _parse_institutional 邊界檢查（W21 修補）─────────────────────────────────


def test_parse_institutional_skip_short_rows():
    """T86 中 row 欄位數少於 fields 一半時應跳過（TWSE 偶有 placeholder row）。"""
    payload = {
        "stat": "OK",
        "date": "20260518",
        "fields": [
            "證券代號", "證券名稱",
            "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
            "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數",
        ],
        "data": [
            # 正常 row（7 欄）
            ["2330", "台積電", "1000", "0", "200", "100", "1300"],
            # 異常 row（只有 2 欄，應該被略過而不是 IndexError）
            ["2615", "萬海"],
            # 異常 row（3 欄）
            ["3708", "上緯投控", "500"],
            # 正常 row
            ["2454", "聯發科", "500", "0", "100", "50", "650"],
        ],
    }
    df = _parse_institutional(payload)
    # 應該只保留 2 個正常 row
    assert len(df) == 2
    stocks = df["stock_id"].to_list()
    assert "2330" in stocks
    assert "2454" in stocks
    assert "2615" not in stocks
    assert "3708" not in stocks


# ─── _get_legacy retry 邏輯（W21 修補）────────────────────────────────────────


def test_get_legacy_retries_on_non_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TWSE 回 HTML（非 JSON）時，_get_legacy 應退避重試。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )

    call_count = [0]

    class FakeResp:
        def __init__(self, content_type: str, payload: dict):
            self.headers = {"content-type": content_type}
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def mock_get(url, **kw):
        call_count[0] += 1
        # 前兩次回 HTML，第三次回 JSON
        if call_count[0] <= 2:
            return FakeResp("text/html; charset=utf-8", {})
        return FakeResp("application/json", {"stat": "OK", "data": []})

    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)
    # 退避時間用 monkeypatch sleep 加速測試
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    result = client._get_legacy("https://test.invalid/foo")
    assert call_count[0] == 3, f"預期 3 次 HTTP（兩次 HTML + 一次 JSON），實際 {call_count[0]}"
    assert result == {"stat": "OK", "data": []}


def test_get_legacy_gives_up_after_max_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """retry 用盡仍回非 JSON → 回空 dict（不拋例外）。"""
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )

    call_count = [0]

    class FakeResp:
        headers = {"content-type": "text/html"}
        def raise_for_status(self): pass
        def json(self): return {}

    def mock_get(url, **kw):
        call_count[0] += 1
        return FakeResp()

    import time as _time

    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    result = client._get_legacy("https://test.invalid/foo")
    # max_retries=2 → 共 3 次嘗試
    assert call_count[0] == 3
    assert result == {}


# ─── TPEX 上櫃法人 ────────────────────────────────────────────────────────────


def test_parse_tpex_institutional_basic():
    """_parse_tpex_institutional 解析正確欄位（含負值）。"""
    data = [
        {
            "Date": "1150520",
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶",
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "500000",
            "SecuritiesInvestmentTrustCompanies-Difference": "100000",
            "Dealers-Difference": "-50000",
            "TotalDifference": "550000",
        },
        {
            "Date": "1150520",
            "SecuritiesCompanyCode": "3105",
            "CompanyName": "穩懋",
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "-200000",
            "SecuritiesInvestmentTrustCompanies-Difference": "0",
            "Dealers-Difference": "0",
            "TotalDifference": "-200000",
        },
    ]
    df = _parse_tpex_institutional(data)
    assert len(df) == 2
    m = {r["stock_id"]: r for r in df.to_dicts()}
    assert m["6488"]["date"] == date(2026, 5, 20)
    assert m["6488"]["foreign_net"] == 500000
    assert m["6488"]["trust_net"] == 100000
    assert m["6488"]["dealer_net"] == -50000
    assert m["6488"]["total_net"] == 550000
    assert m["3105"]["foreign_net"] == -200000
    assert m["3105"]["total_net"] == -200000


def test_parse_tpex_institutional_empty():
    """空 list → 回傳正確 schema 的空 DataFrame。"""
    df = _parse_tpex_institutional([])
    assert df.is_empty()
    assert "total_net" in df.columns
    assert "foreign_net" in df.columns


def test_fetch_otc_institutional_saves_cache(tmp_path: Path, monkeypatch):
    """fetch_otc_institutional 成功時存 institutional_otc_{date}.parquet。"""
    import httpx as _httpx

    otc_data = [
        {
            "Date": "1150519",
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶",
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "200000",
            "SecuritiesInvestmentTrustCompanies-Difference": "50000",
            "Dealers-Difference": "0",
            "TotalDifference": "250000",
        }
    ]

    class MockResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        def raise_for_status(self): pass
        def json(self): return otc_data

    # daily cache so latest_trading_date() works without network
    save_parquet(
        pl.DataFrame({
            "date": [date(2026, 5, 19)],
            "stock_id": ["2330"],
            "name": ["台積電"],
            "trade_volume": [100000],
            "trade_value": [500000000],
            "open": [950.0], "high": [960.0], "low": [945.0], "close": [955.0],
            "change": [5.0], "transaction": [30000],
        }),
        tmp_path / "daily_20260519.parquet",
    )

    monkeypatch.setattr(_httpx, "get", lambda *a, **kw: MockResp())

    client = TWSEClient(
        base_url="https://openapi.twse.com.tw/v1",
        cache_dir=tmp_path,
        ttl_hours=24.0,
        user_agent="test",
        interval_sec=0.0,
    )
    df = client.fetch_otc_institutional()
    assert not df.is_empty()
    cache = tmp_path / "institutional_otc_20260519.parquet"
    assert cache.exists(), "應存 institutional_otc_{date}.parquet"
    row = df.filter(pl.col("stock_id") == "6488").to_dicts()[0]
    assert row["foreign_net"] == 200000
    assert row["total_net"] == 250000


def test_fetch_otc_institutional_cache_hit(tmp_path: Path):
    """已有 institutional_otc_{date}.parquet 時直接讀快取，回傳正確資料。"""
    trade_date = date(2026, 5, 19)
    save_parquet(
        pl.DataFrame({
            "date": [trade_date],
            "stock_id": ["6488"],
            "stock_name": ["環球晶"],
            "foreign_net": [100],
            "trust_net": [0],
            "dealer_net": [0],
            "total_net": [100],
        }),
        tmp_path / "institutional_otc_20260519.parquet",
    )
    # daily cache so latest_trading_date() resolves to 2026-05-19
    save_parquet(
        pl.DataFrame({
            "date": [trade_date],
            "stock_id": ["2330"], "name": ["台積電"],
            "trade_volume": [100000], "trade_value": [500000000],
            "open": [950.0], "high": [960.0], "low": [945.0], "close": [955.0],
            "change": [5.0], "transaction": [30000],
        }),
        tmp_path / "daily_20260519.parquet",
    )
    client = TWSEClient(
        base_url="https://openapi.twse.com.tw/v1",
        cache_dir=tmp_path,
        ttl_hours=24.0,
        user_agent="test",
        interval_sec=0.0,
    )
    df = client.fetch_otc_institutional()
    assert not df.is_empty()
    assert df.filter(pl.col("stock_id") == "6488")["foreign_net"][0] == 100


# ─── load_volume_history ──────────────────────────────────────────────────────


def test_load_volume_history_from_daily(tmp_path: Path):
    """daily_*.parquet 中的 trade_volume 可被 load_volume_history 讀到。"""
    for day in [15, 16, 17, 18, 19]:
        save_parquet(
            pl.DataFrame({
                "date": [date(2026, 5, day)],
                "stock_id": ["2330"],
                "name": ["台積電"],
                "trade_volume": [1000 * day],
                "trade_value": [500000000],
                "open": [950.0], "high": [960.0], "low": [945.0], "close": [955.0],
                "change": [5.0], "transaction": [30000],
            }),
            tmp_path / f"daily_20260{day:02d}.parquet",
        )
    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    df = client.load_volume_history(["2330"], n_days=10)
    assert not df.is_empty()
    assert len(df) == 5
    assert df.filter(pl.col("date") == date(2026, 5, 19))["trade_volume"][0] == 19000


def test_load_volume_history_empty_no_stock_ids(tmp_path: Path):
    """空 stock_ids → 回傳空 DataFrame。"""
    client = TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )
    df = client.load_volume_history([])
    assert df.is_empty()
    assert "trade_volume" in df.columns


# ─── fetch_stock_ohlcv 多源 schema 對齊（R0：daily_all 與 stock_day 合併） ──────


def _make_client(tmp_path: Path) -> TWSEClient:
    return TWSEClient(
        base_url="https://test.invalid", cache_dir=tmp_path,
        ttl_hours=6.0, user_agent="test", interval_sec=0.0,
    )


def test_fetch_stock_ohlcv_mixed_schema_sources(tmp_path: Path):
    """stock_day（完整 OHLCV）+ daily_all（只有 close/volume）合併不 ShapeError。

    daily_all_* 的成交量欄叫 volume（會被 _align 統一成 trade_volume）、
    且缺 open/high/low（補 null）、多 name 欄（丟掉）。
    """
    pl.DataFrame({
        "date": [date(2026, 5, 18)],
        "stock_id": ["2330"],
        "trade_volume": [1000],
        "trade_value": [500000],
        "open": [500.0], "high": [510.0], "low": [495.0], "close": [505.0],
        "change": [5.0],
        "transaction": [800],
    }).write_parquet(tmp_path / "stock_day_2330_202605.parquet")

    pl.DataFrame({
        "date": [date(2026, 5, 18), date(2026, 5, 19)],
        "stock_id": ["2330", "2330"],
        "name": ["台積電", "台積電"],
        "close": [999.0, 508.0],
        "volume": [2000, 2200],
    }).write_parquet(tmp_path / "daily_all_20260519.parquet")

    df = _make_client(tmp_path).fetch_stock_ohlcv("2330", n_days=10)

    assert len(df) == 2
    assert "trade_volume" in df.columns and "volume" not in df.columns
    assert "name" not in df.columns
    # 同日重複：stock_day（完整來源）優先，close 應為 505 而非 daily 的 999
    d0518 = df.filter(pl.col("date") == date(2026, 5, 18))
    assert d0518["close"][0] == 505.0
    assert d0518["open"][0] == 500.0
    # daily-only 的那天：open/high/low 為 null、volume 對齊成 trade_volume
    d0519 = df.filter(pl.col("date") == date(2026, 5, 19))
    assert d0519["close"][0] == 508.0
    assert d0519["open"][0] is None
    assert d0519["trade_volume"][0] == 2200


def test_fetch_stock_ohlcv_daily_only(tmp_path: Path):
    """只有 daily_all 快取（無 stock_day）也能回資料。"""
    pl.DataFrame({
        "date": [date(2026, 5, 19)],
        "stock_id": ["2317"],
        "close": [150.0],
        "volume": [3000],
    }).write_parquet(tmp_path / "daily_all_20260519.parquet")

    df = _make_client(tmp_path).fetch_stock_ohlcv("2317", n_days=10)
    assert len(df) == 1
    assert df["trade_volume"][0] == 3000


# ─── load_institutional_history 上市＋上櫃合併（R0） ───────────────────────────


def test_load_institutional_history_merges_listed_and_otc(tmp_path: Path):
    """glob institutional_*.parquet 同時涵蓋上市與 _otc_ 快取。"""
    inst_schema = {
        "date": pl.Date, "stock_id": pl.Utf8, "stock_name": pl.Utf8,
        "foreign_net": pl.Int64, "trust_net": pl.Int64,
        "dealer_net": pl.Int64, "total_net": pl.Int64,
    }
    pl.DataFrame({
        "date": [date(2026, 6, 9)], "stock_id": ["2330"], "stock_name": ["台積電"],
        "foreign_net": [1000], "trust_net": [100], "dealer_net": [10],
        "total_net": [1110],
    }, schema=inst_schema).write_parquet(tmp_path / "institutional_20260609.parquet")
    pl.DataFrame({
        "date": [date(2026, 6, 9)], "stock_id": ["8299"], "stock_name": ["群聯"],
        "foreign_net": [-500], "trust_net": [200], "dealer_net": [0],
        "total_net": [-300],
    }, schema=inst_schema).write_parquet(tmp_path / "institutional_otc_20260609.parquet")

    df = _make_client(tmp_path).load_institutional_history(n_days=20)
    assert set(df["stock_id"].to_list()) == {"2330", "8299"}
    assert df.filter(pl.col("stock_id") == "8299")["foreign_net"][0] == -500


def test_load_institutional_history_respects_n_days(tmp_path: Path):
    """n_days 取最近 N 個交易日，舊日被切掉。"""
    inst_schema = {
        "date": pl.Date, "stock_id": pl.Utf8, "stock_name": pl.Utf8,
        "foreign_net": pl.Int64, "trust_net": pl.Int64,
        "dealer_net": pl.Int64, "total_net": pl.Int64,
    }
    for i, d in enumerate([date(2026, 6, 5), date(2026, 6, 8), date(2026, 6, 9)]):
        pl.DataFrame({
            "date": [d], "stock_id": ["2330"], "stock_name": ["台積電"],
            "foreign_net": [i], "trust_net": [0], "dealer_net": [0], "total_net": [i],
        }, schema=inst_schema).write_parquet(
            tmp_path / f"institutional_{d.strftime('%Y%m%d')}.parquet"
        )

    df = _make_client(tmp_path).load_institutional_history(n_days=2)
    assert sorted(df["date"].unique().to_list()) == [date(2026, 6, 8), date(2026, 6, 9)]


# ─── TPEX 上櫃全市場日線 + 單季基本面（資料修復 milestone） ─────────────────────


def test_parse_tpex_daily_all():
    from tw_screener.data.twse import _parse_tpex_daily_all

    data = [
        {"Date": "1150612", "SecuritiesCompanyCode": "8299", "CompanyName": "群聯",
         "Close": "2310.00", "Change": "+110.00", "Open": "2250.00", "High": "2330.00",
         "Low": "2240.00", "TradingShares": "4981000", "TransactionAmount": "11650000000",
         "TransactionNumber": "5466"},
        {"Date": "1150612", "SecuritiesCompanyCode": "5274", "CompanyName": "信驊",
         "Close": "--", "Change": "", "Open": "--", "High": "--", "Low": "--",
         "TradingShares": "0", "TransactionAmount": "0", "TransactionNumber": "0"},
    ]
    df = _parse_tpex_daily_all(data)
    assert len(df) == 1  # 無成交（Close='--'）整列略過
    r = df.row(0, named=True)
    assert r["stock_id"] == "8299"
    assert r["date"] == date(2026, 6, 12)
    assert r["close"] == 2310.0
    assert r["trade_volume"] == 4981000
    assert r["change"] == 110.0


def test_fetch_otc_daily_all_cache_hit(tmp_path: Path):
    """TTL 內命中 otc_daily_* 快取不打網；且不污染 fetch_daily_all 的 daily_* glob。"""
    client = _make_client(tmp_path)
    pl.DataFrame({"date": [date(2026, 6, 12)], "stock_id": ["8299"], "name": ["群聯"],
                  "trade_volume": [1000], "trade_value": [100], "open": [1.0], "high": [1.0],
                  "low": [1.0], "close": [1.0], "change": [0.0], "transaction": [1]}
                 ).write_parquet(tmp_path / "otc_daily_20260612.parquet")
    df = client.fetch_otc_daily_all()
    assert df["stock_id"].to_list() == ["8299"]
    # fetch_daily_all 的 _latest_cache_file("daily_*.parquet") 不應撿到 otc_daily_*
    assert client._latest_cache_file("daily_*.parquet") is None


def test_parse_quarterly_fundamentals_merges_four_endpoints():
    from tw_screener.data.twse import _parse_quarterly_fundamentals

    margin_listed = [{"年度": "115", "季別": "1", "公司代號": "2330",
                      "營業收入(百萬元)": "839254.00",
                      "毛利率(%)(營業毛利)/(營業收入)": "58.50",
                      "營業利益率(%)(營業利益)/(營業收入)": "48.50"}]
    eps_listed = [{"年度": "115", "季別": "1", "公司代號": "2330", "基本每股盈餘(元)": "13.94"}]
    margin_otc = [{"Year": "115", "季別": "1", "SecuritiesCompanyCode": "8299",
                   "營業收入百萬元": "15223.60", "毛利率": "32.10", "營業利益率": "20.00"}]
    eps_otc = [{"Year": "115", "季別": "1", "SecuritiesCompanyCode": "8299",
                "基本每股盈餘": "10.50"}]
    df = _parse_quarterly_fundamentals(margin_listed, margin_otc, eps_listed, eps_otc)
    assert df.height == 2
    tsmc = df.filter(pl.col("stock_id") == "2330").row(0, named=True)
    assert tsmc["year"] == 2026 and tsmc["quarter"] == 1
    assert tsmc["gross_margin_pct"] == 58.5 and tsmc["eps"] == 13.94
    phison = df.filter(pl.col("stock_id") == "8299").row(0, named=True)
    assert phison["gross_margin_pct"] == 32.1 and phison["eps"] == 10.5


def test_load_latest_fundamentals(tmp_path: Path):
    client = _make_client(tmp_path)
    assert client.load_latest_fundamentals().is_empty()
    pl.DataFrame({"stock_id": ["2330"], "year": [2026], "quarter": [1],
                  "revenue_m": [1.0], "gross_margin_pct": [58.5],
                  "op_margin_pct": [48.5], "eps": [13.94]}).write_parquet(
        tmp_path / "fundamentals_2026Q1.parquet")
    df = client.load_latest_fundamentals()
    assert df["eps"][0] == 13.94
