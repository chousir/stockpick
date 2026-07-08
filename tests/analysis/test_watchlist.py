"""analysis/watchlist.py 純讀檔 helper＋enrich 回補觸發單測（自 cli.py 下沉後可獨立測）。"""

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from tw_screener.analysis.watchlist import (
    enrich_named_list,
    load_latest_screener_results,
    read_holdings_csv,
    read_watchlist_csv,
)


class _FakeClient:
    """假 TWSEClient：快取先回 short_rows 根，fetch_stock_history 後回 full_rows 根。"""

    def __init__(self, short_rows: int, full_rows: int = 80) -> None:
        self.history_calls: list[str] = []
        self._rows = short_rows
        self._full = full_rows

    def _frame(self, sid: str, n: int) -> pl.DataFrame:
        if n == 0:
            return pl.DataFrame(
                schema={
                    "stock_id": pl.Utf8, "date": pl.Date,
                    "trade_volume": pl.Int64, "trade_value": pl.Int64,
                    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                    "close": pl.Float64, "change": pl.Float64, "transaction": pl.Int64,
                }
            )
        base = date(2026, 1, 5)
        return pl.DataFrame(
            {
                "stock_id": [sid] * n,
                "date": [base + timedelta(days=i) for i in range(n)],
                "trade_volume": [1000] * n,
                "trade_value": [100_000] * n,
                "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
                "close": [10.0] * n, "change": [0.1] * n, "transaction": [10] * n,
            }
        )

    def fetch_stock_ohlcv(self, sid: str, n_days: int = 100) -> pl.DataFrame:
        return self._frame(sid, min(self._rows, n_days))

    def fetch_stock_history(self, sid: str, months: int = 6) -> pl.DataFrame:
        self.history_calls.append(sid)
        self._rows = self._full
        return self._frame(sid, self._full)


def test_enrich_backfills_when_cache_below_ma60_window() -> None:
    """快取僅 19 根（< MA60 視窗）→ 觸發單檔回補（W28 觀察清單均線盲區修復）。"""
    client = _FakeClient(short_rows=19, full_rows=80)
    members, _ = enrich_named_list(client, ["2317"], None, pl.DataFrame(), None)
    assert client.history_calls == ["2317"]
    assert not members.is_empty()


def test_enrich_backfills_when_cache_empty() -> None:
    """快取全空（舊行為）仍觸發回補，行為不回退。"""
    client = _FakeClient(short_rows=0, full_rows=80)
    members, _ = enrich_named_list(client, ["2317"], None, pl.DataFrame(), None)
    assert client.history_calls == ["2317"]
    assert not members.is_empty()


def test_enrich_skips_backfill_when_cache_sufficient() -> None:
    """快取 ≥ 60 根 → 不重抓（過去月份永久快取、不浪費請求）。"""
    client = _FakeClient(short_rows=100)
    members, _ = enrich_named_list(client, ["2330"], None, pl.DataFrame(), None)
    assert client.history_calls == []
    assert not members.is_empty()


def test_read_watchlist_csv_preserves_leading_zero(tmp_path: Path) -> None:
    p = tmp_path / "watchlist.csv"
    p.write_text("stock_id,note\n0050,ETF\n2330,tsmc\n", encoding="utf-8")
    assert read_watchlist_csv(p) == ["0050", "2330"]


def test_read_watchlist_csv_missing_file_or_column(tmp_path: Path) -> None:
    assert read_watchlist_csv(tmp_path / "nope.csv") == []
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    assert read_watchlist_csv(bad) == []


def test_read_holdings_csv_parses_buy_price_and_shares(tmp_path: Path) -> None:
    p = tmp_path / "holdings.csv"
    p.write_text(
        'stock_id,buy_price,shares,note\n2330,"1,000.5",2000,核心\n6231,,,空值\n',
        encoding="utf-8",
    )
    out = read_holdings_csv(p)
    assert out["2330"] == {"buy_price": 1000.5, "shares": 2000.0}
    assert out["6231"] == {"buy_price": None, "shares": None}


def test_read_holdings_csv_missing_file(tmp_path: Path) -> None:
    assert read_holdings_csv(tmp_path / "nope.csv") == {}


def test_load_latest_screener_results_picks_latest_week(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "2026-W20").mkdir(parents=True)
    (reports / "2026-W21").mkdir(parents=True)
    (reports / "2026-W21" / "screen_result_d.csv").write_text(
        "stock_id,close\n2330,1000\n", encoding="utf-8"
    )
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"paths:\n  reports_dir: {reports}\n", encoding="utf-8")

    week_tag, results = load_latest_screener_results(settings)
    assert week_tag == "2026-W21"
    assert set(results) == {"d"}
    assert results["d"]["stock_id"].to_list() == [2330]


def test_load_latest_screener_results_no_reports_dir(tmp_path: Path) -> None:
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"paths:\n  reports_dir: {tmp_path / 'missing'}\n", encoding="utf-8")
    assert load_latest_screener_results(settings) == ("", {})


def test_enrich_keeps_etf_rows_with_industry_etf() -> None:
    """持股 ETF（00 開頭）產輕量列：不再被無聲丟棄、industry 標 ETF（docs/21 M-ETF1）。"""
    client = _FakeClient(short_rows=100)
    members, synth = enrich_named_list(
        client, ["2330", "0050", "00981A"], None, pl.DataFrame(), None
    )
    assert set(members["stock_id"].to_list()) == {"2330", "0050", "00981A"}
    ind = dict(zip(members["stock_id"].to_list(), members["industry_name"].to_list()))
    assert ind["0050"] == "ETF"
    assert ind["00981A"] == "ETF"
    assert ind["2330"] != "ETF"
    assert synth["_list"].height == 3
