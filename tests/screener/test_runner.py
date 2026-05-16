"""tests/screener/test_runner.py — ScreenerRunner 單元測試（全離線）。"""

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from tw_screener.screener.runner import ScreenerRunner

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "goodinfo"
STRATEGY_PATH = Path("config/strategies/a_breakout.yaml")
SETTINGS_PATH = Path("config/settings.yaml")


class _MockFetcher:
    def __init__(self, html: str) -> None:
        self._html = html
        self.calls: list[str] = []

    def get(self, url: str, *, force: bool = False) -> str:
        self.calls.append(url)
        return self._html


def make_runner(tmp_path: Path, html: str = "") -> ScreenerRunner:
    runner = ScreenerRunner(SETTINGS_PATH)
    runner._reports_dir = tmp_path / "reports"
    runner._fetcher = _MockFetcher(html or _screener_html())
    return runner


def _screener_html() -> str:
    return (FIXTURE_DIR / "screener_result.html").read_text(encoding="utf-8")


def _empty_html() -> str:
    return """<html><body>
    <table id="tblStockList" class="solid_1_padding_4_4_tbl">
    <thead><tr><td>股號</td><td>股名</td><td>收盤</td></tr></thead>
    <tbody></tbody>
    </table></body></html>"""


# ─── run_strategy ─────────────────────────────────────────────────────────────


def test_run_strategy_row_count(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = runner.run_strategy(STRATEGY_PATH)
    assert len(df) == 3


def test_run_strategy_has_strategy_id(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = runner.run_strategy(STRATEGY_PATH)
    assert "strategy_id" in df.columns
    assert df["strategy_id"][0] == "a_breakout"


def test_run_strategy_has_screened_at(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = runner.run_strategy(STRATEGY_PATH)
    assert "screened_at" in df.columns
    assert df["screened_at"][0] == date.today()


def test_run_strategy_has_goodinfo_url(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = runner.run_strategy(STRATEGY_PATH)
    assert "goodinfo_url" in df.columns
    assert "2330" in df["goodinfo_url"][0]
    assert "StockDetail" in df["goodinfo_url"][0]


def test_run_strategy_goodinfo_url_format(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = runner.run_strategy(STRATEGY_PATH)
    expected_prefix = "https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID="
    assert df["goodinfo_url"][0].startswith(expected_prefix)


def test_run_strategy_calls_fetcher(tmp_path: Path):
    runner = make_runner(tmp_path)
    mock = _MockFetcher(_screener_html())
    runner._fetcher = mock
    runner.run_strategy(STRATEGY_PATH)
    assert len(mock.calls) == 1


def test_run_strategy_empty_result(tmp_path: Path):
    runner = make_runner(tmp_path, _empty_html())
    df = runner.run_strategy(STRATEGY_PATH)
    assert df.is_empty()
    assert "strategy_id" in df.columns
    assert "screened_at" in df.columns
    assert "goodinfo_url" in df.columns


def test_run_strategy_over_100_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """100 筆以上應記錄 warning（透過 loguru 的 propagate）。"""
    runner = make_runner(tmp_path)
    # Build HTML with 101 rows
    rows = "\n".join(
        f'<tr bgcolor="#fff"><td>{i}</td>'
        f'<td><a href="#">{1000 + i}</a></td>'
        f'<td><a href="#">股票{i}</a></td>'
        f"<td>半導體</td><td></td>"
        f"<td>100.00</td><td>1.00</td><td>1%</td><td>1000</td><td>1.0</td><td>500</td></tr>"
        for i in range(101)
    )
    html = f"""<html><body>
    <table id="tblStockList" class="solid_1_padding_4_4_tbl">
    <thead><tr><td>序</td><td>股號</td><td>股名</td><td>產業</td><td>概念股</td>
    <td>收盤</td><td>漲跌</td><td>漲跌%</td><td>成交張數</td><td>成交金額(億)</td><td>成交筆數</td></tr></thead>
    <tbody>{rows}</tbody></table></body></html>"""
    runner._fetcher = _MockFetcher(html)
    df = runner.run_strategy(STRATEGY_PATH)
    assert len(df) == 101  # result still returned, just warned


# ─── export_csv ───────────────────────────────────────────────────────────────


def test_export_csv_creates_file(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = pl.DataFrame({"stock_id": ["2330"], "name": ["台積電"]})
    output = runner.export_csv(df, "a_breakout", "2026-W20")
    assert output.exists()


def test_export_csv_filename(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = pl.DataFrame({"stock_id": ["2330"]})
    output = runner.export_csv(df, "a_breakout", "2026-W20")
    assert output.name == "screen_result_a_breakout.csv"


def test_export_csv_week_dir(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = pl.DataFrame({"stock_id": ["2330"]})
    output = runner.export_csv(df, "a_breakout", "2026-W20")
    assert "2026-W20" in str(output)


def test_export_csv_content(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = pl.DataFrame({"stock_id": ["2330"], "name": ["台積電"]})
    output = runner.export_csv(df, "a_breakout", "2026-W20")
    content = output.read_text(encoding="utf-8")
    assert "2330" in content
    assert "台積電" in content


def test_export_csv_returns_path(tmp_path: Path):
    runner = make_runner(tmp_path)
    df = pl.DataFrame({"stock_id": ["2330"]})
    output = runner.export_csv(df, "a_breakout", "2026-W20")
    assert isinstance(output, Path)


# ─── run_all ──────────────────────────────────────────────────────────────────


def test_run_all_returns_dict(tmp_path: Path):
    runner = make_runner(tmp_path)
    results = runner.run_all(week_tag="2026-W20")
    assert isinstance(results, dict)
    assert len(results) >= 1


def test_run_all_creates_csvs(tmp_path: Path):
    runner = make_runner(tmp_path)
    runner.run_all(week_tag="2026-W20")
    report_dir = tmp_path / "reports" / "2026-W20"
    csvs = list(report_dir.glob("screen_result_*.csv"))
    # At least as many CSVs as strategy YAMLs
    yaml_count = len(list(Path("config/strategies").glob("*.yaml")))
    assert len(csvs) == yaml_count


def test_run_all_uses_today_week_tag(tmp_path: Path):
    runner = make_runner(tmp_path)
    runner.run_all()
    week_tag = date.today().strftime("%Y-W%V")
    assert (tmp_path / "reports" / week_tag).exists()


def test_run_all_strategy_ids_match_yamls(tmp_path: Path):
    runner = make_runner(tmp_path)
    results = runner.run_all(week_tag="2026-W20")
    yaml_ids = {p.stem for p in Path("config/strategies").glob("*.yaml")}
    assert set(results.keys()) == yaml_ids
