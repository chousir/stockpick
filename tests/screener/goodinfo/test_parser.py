"""tests/screener/goodinfo/test_parser.py — parser 單元測試（全離線）。

fixture: screener_result.html 為 2026-05-16 實際抓取之 Goodinfo AJAX 回傳 HTML fragment
條件: 成交筆數>=30000, 股價 15-300, 週MACD↗, 均線多頭排列–日, 股價站上月線, 量增價漲（突破日）
結果: 61 筆
"""

from pathlib import Path

import polars as pl
import pytest

from tw_screener.screener.goodinfo.parser import (
    GoodinfoParseError,
    GoodinfoTooManyResultsError,
    parse_screener_result,
)

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "goodinfo"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ─── 正常解析 ────────────────────────────────────────────────────────────────


def test_parse_row_count():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert len(df) == 61


def test_parse_first_stock_id():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["stock_id"][0] == "00403A"


def test_parse_known_stock_present():
    """台積電（2330）應出現在結果中。"""
    df = parse_screener_result(_fixture("screener_result.html"))
    assert "2330" in df["stock_id"].to_list()


def test_parse_name():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["name"][0] == "主動統一升級50"


def test_parse_market():
    df = parse_screener_result(_fixture("screener_result.html"))
    # 市=上市, 櫃=上櫃
    assert df["market"][0] in ("市", "櫃", "興")


def test_parse_close_is_positive():
    df = parse_screener_result(_fixture("screener_result.html"))
    closes = df["close"].drop_nulls()
    assert (closes > 0).all()


def test_parse_first_close():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["close"][0] == pytest.approx(10.0)


def test_parse_volume_lots_is_positive():
    df = parse_screener_result(_fixture("screener_result.html"))
    vols = df["volume_lots"].drop_nulls()
    assert (vols > 0).all()


def test_parse_first_volume_lots():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["volume_lots"][0] == 1_856_315


# ─── Schema ───────────────────────────────────────────────────────────────────


def test_schema_has_required_columns():
    df = parse_screener_result(_fixture("screener_result.html"))
    for col in (
        "stock_id",
        "name",
        "market",
        "close",
        "change_pct",
        "volume_lots",
        "amount_million",
        "pe_ratio",
        "pb_ratio",
    ):
        assert col in df.columns


def test_close_is_float():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["close"].dtype == pl.Float64


def test_volume_lots_is_int():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["volume_lots"].dtype == pl.Int64


def test_change_pct_is_float():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["change_pct"].dtype == pl.Float64


# ─── 錯誤情況 ────────────────────────────────────────────────────────────────


def test_missing_table_raises():
    html = "<html><body><p>no table here</p></body></html>"
    with pytest.raises(GoodinfoParseError):
        parse_screener_result(html)


def test_too_many_results_raises():
    html = "<p>您的篩選條件範圍過大,<br>合乎條件的股票超過300筆&nbsp;(共計354筆)</p>"
    with pytest.raises(GoodinfoTooManyResultsError) as exc_info:
        parse_screener_result(html)
    assert exc_info.value.count == 354


def test_too_many_results_count_extracted():
    html = "<p>您的篩選條件範圍過大,<br>合乎條件的股票超過300筆&nbsp;(共計500筆)</p>"
    with pytest.raises(GoodinfoTooManyResultsError) as exc_info:
        parse_screener_result(html)
    assert exc_info.value.count == 500


def test_empty_table_returns_empty_df():
    html = """<table id="tblStockList">
    <tr><th>代號</th><th>名稱</th><th>成交</th></tr>
    </table>"""
    df = parse_screener_result(html)
    assert df.is_empty()
    assert "stock_id" in df.columns


def test_empty_table_has_correct_schema():
    html = """<table id="tblStockList">
    <tr><th>代號</th></tr>
    </table>"""
    df = parse_screener_result(html)
    assert "close" in df.columns
    assert df["close"].dtype == pl.Float64


# ─── 整合鏈路：YAML → URL → fixture HTML → DataFrame ────────────────────────


def test_full_chain_yaml_to_df():
    """YAML → build_data_url → 用 fixture HTML → 解析出正確 DataFrame。"""
    from tw_screener.screener.goodinfo.url_builder import build_data_url, load_strategy

    strategy = load_strategy(Path("tests/fixtures/strategies/a_breakout.yaml"))
    url = build_data_url(strategy, "https://goodinfo.tw/tw")

    assert "StockList.asp" in url
    assert "STEP=DATA" in url
    assert "FL_ITEM0" in url

    df = parse_screener_result(_fixture("screener_result.html"))

    assert len(df) == 61
    assert df["stock_id"][0] == "00403A"
    assert "stock_id" in df.columns
    assert "close" in df.columns
