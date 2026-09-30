"""tests/data/test_stock_day_month_cache.py — 個股月檔快取規則（R2；docs/35 §5）。

規則：過去月份的 `stock_day_{sid}_{YYYYMM}.parquet` 只有在「月結後才寫入」（mtime 日期 ≥ 次月 1 日）
才算最終版；月中寫入的暫定檔（缺該月後半段）月底後必須重抓一次。當月維持 TTL 行為。
重抓不到東西時沿用暫定檔，不讓該月消失。上市（TWSE STOCK_DAY）與上櫃（TPEX tradingStock）
同一條規則。

日期一律相對 date.today() 計算，測試不隨執行日期變化；不打網（_get_legacy 一律替身）。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from tw_screener.data.cache import is_month_file_final
from tw_screener.data.twse import TWSEClient, _months_back

TODAY = date.today()
CUR_YM = TODAY.strftime("%Y%m")
PAST = _months_back(TODAY, 2)  # 兩個月前的 1 日（過去月份）
PAST_YM = PAST.strftime("%Y%m")
PREV = _months_back(TODAY, 1)  # 上個月 1 日（＝ PAST 的次月 1 日）
PREV_YM = PREV.strftime("%Y%m")
MID_OF_PAST = datetime.combine(PAST.replace(day=15), time(12, 0))  # 月中寫入
AFTER_PAST = datetime.combine(PREV, time(0, 30))  # 次月 1 日 00:30 寫入＝月結後


def _stamp(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _rows(ym: str, days: list[int], close: float = 10.0) -> pl.DataFrame:
    y, m = int(ym[:4]), int(ym[4:])
    n = len(days)
    return pl.DataFrame(
        {
            "date": [date(y, m, d) for d in days],
            "stock_id": ["2330"] * n,
            "trade_volume": [1000] * n,
            "trade_value": [10000] * n,
            "open": [close] * n,
            "high": [close] * n,
            "low": [close] * n,
            "close": [close] * n,
            "change": [0.0] * n,
            "transaction": [10] * n,
        }
    )


def _seed(
    tmp_path: Path, sid: str, ym: str, days: list[int], when: datetime, close: float = 10.0
) -> Path:
    path = tmp_path / f"stock_day_{sid}_{ym}.parquet"
    _rows(ym, days, close).with_columns(pl.lit(sid).alias("stock_id")).write_parquet(path)
    _stamp(path, when)
    return path


def _twse_payload(ym: str, days: list[int]) -> dict[str, Any]:
    y, m = int(ym[:4]), int(ym[4:])
    return {
        "stat": "OK",
        "fields": [
            "日期",
            "成交股數",
            "成交金額",
            "開盤價",
            "最高價",
            "最低價",
            "收盤價",
            "漲跌價差",
            "成交筆數",
        ],
        "data": [
            [
                f"{y - 1911}/{m:02d}/{d:02d}",
                "1,000",
                "10,000",
                "20.00",
                "20.00",
                "20.00",
                "20.00",
                "0.00",
                "10",
            ]
            for d in days
        ],
    }


def _tpex_payload(ym: str, days: list[int]) -> dict[str, Any]:
    y, m = int(ym[:4]), int(ym[4:])
    return {
        "stat": "ok",
        "tables": [
            {
                "fields": [
                    "日 期",
                    "成交張數",
                    "成交仟元",
                    "開盤",
                    "最高",
                    "最低",
                    "收盤",
                    "漲跌",
                    "筆數",
                ],
                "data": [
                    [
                        f"{y - 1911}/{m:02d}/{d:02d}",
                        "1",
                        "10",
                        "20.00",
                        "20.00",
                        "20.00",
                        "20.00",
                        "0.00",
                        "10",
                    ]
                    for d in days
                ],
            }
        ],
    }


class Net:
    """_get_legacy 替身：依 URL 內的 YYYYMM 回對應 payload；記錄呼叫。"""

    def __init__(
        self, twse: dict[str, list[int]], tpex: dict[str, list[int]] | None = None
    ) -> None:
        self.twse, self.tpex = twse, tpex or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        if "STOCK_DAY" in url:  # TWSE：date=YYYYMM01
            ym = url.split("date=")[1][:6]
            return _twse_payload(ym, self.twse[ym]) if ym in self.twse else {}
        ym = url.split("date=")[1][:7].replace("/", "")  # TPEX：date=YYYY/MM/01
        return _tpex_payload(ym, self.tpex[ym]) if ym in self.tpex else {}


def _client(tmp_path: Path, net: Net, otc: set[str] | None = None) -> TWSEClient:
    client = TWSEClient(
        base_url="https://test.invalid",
        cache_dir=tmp_path,
        ttl_hours=6.0,
        user_agent="test",
        interval_sec=0.0,
    )
    client._otc_ids = otc or set()  # type: ignore[attr-defined] — 避免觸發 ISIN 抓取
    client._get_legacy = net  # type: ignore[method-assign]
    return client


# ─── is_month_file_final：判準與邊界 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("ym", "mtime", "expected"),
    [
        ("202607", datetime(2026, 7, 31, 23, 59), False),  # 月底當天寫入＝仍是月結前（保守）
        ("202607", datetime(2026, 8, 1, 0, 0), True),  # 次月 1 日 00:00 起算月結後
        ("202607", datetime(2026, 8, 20, 9, 0), True),
        ("202607", datetime(2026, 7, 10, 9, 0), False),  # 月中暫定檔
        ("202512", datetime(2025, 12, 31, 23, 59), False),  # 跨年
        ("202512", datetime(2026, 1, 1, 0, 0), True),
        ("202502", datetime(2025, 2, 28, 12, 0), False),  # 二月
        ("202502", datetime(2025, 3, 1, 0, 0), True),
    ],
)
def test_is_month_file_final_boundaries(
    tmp_path: Path, ym: str, mtime: datetime, expected: bool
) -> None:
    f = tmp_path / f"stock_day_2330_{ym}.parquet"
    f.write_bytes(b"x")
    _stamp(f, mtime)
    assert is_month_file_final(f, ym) is expected


def test_is_month_file_final_missing_file_is_false(tmp_path: Path) -> None:
    assert is_month_file_final(tmp_path / "stock_day_2330_202607.parquet", "202607") is False


# ─── 上市（TWSE STOCK_DAY）─────────────────────────────────────────────────────


def test_twse_final_past_month_is_cache_hit_without_network(tmp_path: Path) -> None:
    _seed(tmp_path, "2330", PAST_YM, [2, 3], AFTER_PAST)  # 月結後寫入＝最終版
    net = Net({})
    df = _client(tmp_path, net).fetch_stock_history("2330", months=1, anchor=PAST)
    assert net.urls == []  # 最終版過去月份不重抓
    assert sorted(df["date"].to_list()) == [PAST.replace(day=2), PAST.replace(day=3)]


def test_twse_provisional_past_month_is_refetched_and_becomes_final(tmp_path: Path) -> None:
    path = _seed(tmp_path, "2330", PAST_YM, [2, 3], MID_OF_PAST)  # 月中寫入：只有前兩天
    full = [2, 3, 4, 5, 8, 9, 10]
    net = Net({PAST_YM: full})
    client = _client(tmp_path, net)

    df = client.fetch_stock_history("2330", months=1, anchor=PAST)
    assert len(net.urls) == 1 and f"date={PAST_YM}01" in net.urls[0]
    assert sorted(df["date"].to_list()) == [PAST.replace(day=d) for d in full]
    assert pl.read_parquet(path).height == len(full)  # 檔案被完整版取代
    assert is_month_file_final(path, PAST_YM)  # 重抓寫入的 mtime 已在次月 → 最終版

    net.urls.clear()
    client.fetch_stock_history("2330", months=1, anchor=PAST)  # 第二次：冪等，不再打網
    assert net.urls == []


def test_twse_provisional_month_falls_back_when_refetch_is_empty(tmp_path: Path) -> None:
    path = _seed(tmp_path, "2330", PAST_YM, [2, 3], MID_OF_PAST)
    before = path.read_bytes()
    net = Net({})  # 網路失敗／回空：_get_legacy 回 {}
    client = _client(tmp_path, net)

    df = client.fetch_stock_history("2330", months=1, anchor=PAST)
    assert len(net.urls) == 1
    assert sorted(df["date"].to_list()) == [PAST.replace(day=2), PAST.replace(day=3)]  # 沿用暫定檔
    assert path.read_bytes() == before  # 檔案不動（不誤標為最終版）
    assert not is_month_file_final(path, PAST_YM)

    client.fetch_stock_history("2330", months=1, anchor=PAST)  # 沒有負快取：下次仍會重試
    assert len(net.urls) == 2


def _mid(month_first: date) -> datetime:
    return datetime.combine(month_first.replace(day=15), time(12, 0))


def test_twse_fallback_resets_consecutive_empty_counter(tmp_path: Path) -> None:
    """序列：空月 → 暫定月（沿用）→ 空月 → 暫定月。沿用必須把 consecutive_empty 歸零，
    否則第二個空月就湊滿「連續 2 月空」而 break，把最後一個暫定月丟掉。"""
    m4 = _months_back(TODAY, 4)  # 序列：PREV(空)→PAST(暫定)→前3月(空)→m4(暫定)
    _seed(tmp_path, "2330", PAST_YM, [2], _mid(PAST))  # n=1（PREV 為 n=0：無檔、回空）
    _seed(tmp_path, "2330", m4.strftime("%Y%m"), [3], _mid(m4))  # n=3（n=2 那個月：無檔、回空）
    net = Net({})
    df = _client(tmp_path, net).fetch_stock_history("2330", months=4, anchor=PREV)
    assert len(net.urls) == 4  # 四個月都走到，沒有提早終止
    assert sorted(df["date"].to_list()) == [m4.replace(day=3), PAST.replace(day=2)]


def test_twse_refetch_that_is_a_subset_never_overwrites_the_provisional_file(
    tmp_path: Path,
) -> None:
    """官方回傳非空、但缺暫定檔已有的日期（子集／部分重疊）→ 不覆蓋、不標最終版、沿用暫定檔。

    覆蓋成子集會靜默丟資料且立刻成為「最終版」而無法回復。"""
    path = _seed(tmp_path, "2330", PAST_YM, [2, 3, 4, 5, 8], MID_OF_PAST)
    before = path.read_bytes()
    net = Net({PAST_YM: [2, 3, 9]})  # 缺 4、5、8；多了 9（部分重疊也算不完整）
    client = _client(tmp_path, net)

    df = client.fetch_stock_history("2330", months=1, anchor=PAST)
    assert sorted(df["date"].to_list()) == [
        PAST.replace(day=d) for d in (2, 3, 4, 5, 8)
    ]  # 沿用暫定檔
    assert path.read_bytes() == before and not is_month_file_final(path, PAST_YM)

    client.fetch_stock_history("2330", months=1, anchor=PAST)  # 仍非最終版 → 下次照樣重試
    assert len(net.urls) == 2


def test_twse_refetch_with_more_rows_but_missing_old_dates_is_not_a_superset(
    tmp_path: Path,
) -> None:
    """判準是「日期涵蓋」而不是列數：新資料列數更多、但缺了舊日期，仍不可覆蓋。"""
    path = _seed(tmp_path, "2330", PAST_YM, [2, 3, 4, 5, 8], MID_OF_PAST)
    before = path.read_bytes()
    net = Net({PAST_YM: [2, 3, 9, 10, 11, 12]})  # 6 列 > 5 列，但缺 4、5、8
    df = _client(tmp_path, net).fetch_stock_history("2330", months=1, anchor=PAST)
    assert sorted(df["date"].to_list()) == [PAST.replace(day=d) for d in (2, 3, 4, 5, 8)]
    assert path.read_bytes() == before and not is_month_file_final(path, PAST_YM)


def test_twse_refetch_superset_with_changed_values_still_overwrites(tmp_path: Path) -> None:
    """護欄只擋「缺日期」：新資料涵蓋所有舊日期時，共同日期的數值以官方新值為準。"""
    path = _seed(tmp_path, "2330", PAST_YM, [2, 3], MID_OF_PAST, close=10.0)
    net = Net({PAST_YM: [2, 3, 4]})  # Net 的 TWSE payload 收盤價為 20.0
    _client(tmp_path, net).fetch_stock_history("2330", months=1, anchor=PAST)
    got = pl.read_parquet(path).sort("date")
    assert got["date"].to_list() == [PAST.replace(day=d) for d in (2, 3, 4)]
    assert got["close"].to_list() == [20.0, 20.0, 20.0]
    assert is_month_file_final(path, PAST_YM)


def test_twse_current_month_keeps_ttl_behavior(tmp_path: Path) -> None:
    cur = _seed(tmp_path, "2330", CUR_YM, [1], datetime.now())  # 剛寫：TTL 內
    net = Net({CUR_YM: [1, 2]})
    client = _client(tmp_path, net)
    client.fetch_stock_history("2330", months=1)
    assert net.urls == []  # TTL 內 → 命中快取

    _stamp(cur, datetime.now() - timedelta(hours=7))  # 超過 ttl_hours=6 → 過期
    client.fetch_stock_history("2330", months=1)
    assert len(net.urls) == 1 and f"date={CUR_YM}01" in net.urls[0]


def test_twse_only_the_provisional_month_hits_the_network(tmp_path: Path) -> None:
    """months=3：當月新鮮、PREV 最終版、PAST 暫定 → 只有 PAST 那一個月打網。"""
    _seed(tmp_path, "2330", CUR_YM, [1], datetime.now())
    _seed(tmp_path, "2330", PREV_YM, [2], datetime.now())  # 現在寫入 ≥ PREV 次月 1 日 → 最終版
    _seed(tmp_path, "2330", PAST_YM, [2], MID_OF_PAST)
    net = Net({PAST_YM: [2, 3, 4]})
    df = _client(tmp_path, net).fetch_stock_history("2330", months=3)
    assert len(net.urls) == 1 and f"date={PAST_YM}01" in net.urls[0]
    assert PAST.replace(day=4) in df["date"].to_list()


# ─── 上櫃（TPEX tradingStock）─────────────────────────────────────────────────


def test_tpex_final_past_month_is_cache_hit_without_network(tmp_path: Path) -> None:
    _seed(tmp_path, "3293", CUR_YM, [1], datetime.now())
    _seed(tmp_path, "3293", PREV_YM, [2], datetime.now())
    _seed(tmp_path, "3293", PAST_YM, [2, 3], AFTER_PAST)
    net = Net({}, {})
    df = _client(tmp_path, net, otc={"3293"}).fetch_stock_history("3293", months=3)
    assert net.urls == []
    assert df.height == 4


def test_tpex_provisional_past_month_is_refetched_and_becomes_final(tmp_path: Path) -> None:
    _seed(tmp_path, "3293", CUR_YM, [1], datetime.now())
    _seed(tmp_path, "3293", PREV_YM, [2], datetime.now())
    path = _seed(tmp_path, "3293", PAST_YM, [2, 3], MID_OF_PAST)
    full = [2, 3, 4, 5, 8]
    net = Net({}, {PAST_YM: full})
    client = _client(tmp_path, net, otc={"3293"})

    df = client.fetch_stock_history("3293", months=3)
    assert len(net.urls) == 1 and "tradingStock" in net.urls[0]
    assert f"date={PAST.strftime('%Y/%m')}/01" in net.urls[0]
    assert pl.read_parquet(path).height == len(full)
    assert is_month_file_final(path, PAST_YM)
    assert PAST.replace(day=8) in df["date"].to_list()

    net.urls.clear()
    client.fetch_stock_history("3293", months=3)
    assert net.urls == []


def test_tpex_fallback_resets_consecutive_empty_counter(tmp_path: Path) -> None:
    """同上，上櫃路徑（無 anchor，從當月往回走）：空月 → 暫定月 → 空月 → 暫定月。"""
    older = _months_back(TODAY, 3)
    _seed(tmp_path, "3293", PREV_YM, [2], _mid(PREV))  # n=1（當月為 n=0：無檔、回空）
    _seed(
        tmp_path, "3293", older.strftime("%Y%m"), [3], _mid(older)
    )  # n=3（PAST 為 n=2：無檔、回空）
    net = Net({}, {})
    df = _client(tmp_path, net, otc={"3293"}).fetch_stock_history("3293", months=4)
    assert len(net.urls) == 4
    assert sorted(df["date"].to_list()) == [older.replace(day=3), PREV.replace(day=2)]


def test_tpex_refetch_that_is_a_subset_never_overwrites_the_provisional_file(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, "3293", CUR_YM, [1], datetime.now())
    _seed(tmp_path, "3293", PREV_YM, [2], datetime.now())
    path = _seed(tmp_path, "3293", PAST_YM, [2, 3, 4, 5], MID_OF_PAST)
    before = path.read_bytes()
    net = Net({}, {PAST_YM: [2]})  # 官方只回 1 天（非空但不完整）
    df = _client(tmp_path, net, otc={"3293"}).fetch_stock_history("3293", months=3)
    assert len(net.urls) == 1
    assert sorted(d.day for d in df["date"].to_list() if d.strftime("%Y%m") == PAST_YM) == [
        2,
        3,
        4,
        5,
    ]
    assert path.read_bytes() == before and not is_month_file_final(path, PAST_YM)


def test_tpex_provisional_month_falls_back_when_refetch_is_empty(tmp_path: Path) -> None:
    _seed(tmp_path, "3293", CUR_YM, [1], datetime.now())
    _seed(tmp_path, "3293", PREV_YM, [2], datetime.now())
    path = _seed(tmp_path, "3293", PAST_YM, [2, 3], MID_OF_PAST)
    before = path.read_bytes()
    net = Net({}, {})
    df = _client(tmp_path, net, otc={"3293"}).fetch_stock_history("3293", months=3)
    assert len(net.urls) == 1
    assert PAST.replace(day=3) in df["date"].to_list()  # 沿用暫定檔
    assert path.read_bytes() == before and not is_month_file_final(path, PAST_YM)
