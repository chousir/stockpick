"""analysis/watchlist.py 純讀檔 helper 單測（自 cli.py 下沉後可獨立測）。"""

from pathlib import Path

from tw_screener.analysis.watchlist import (
    load_latest_screener_results,
    read_holdings_csv,
    read_watchlist_csv,
)


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
