"""tests/screener/goodinfo/test_parser.py — parser 單元測試（全離線）。"""

from pathlib import Path

import polars as pl
import pytest

from tw_screener.screener.goodinfo.parser import GoodinfoParseError, parse_screener_result

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "goodinfo"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ─── 正常解析 ────────────────────────────────────────────────────────────────


def test_parse_row_count():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert len(df) == 3


def test_parse_stock_id():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["stock_id"][0] == "2330"
    assert df["stock_id"][1] == "2454"
    assert df["stock_id"][2] == "3034"


def test_parse_name():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["name"][0] == "台積電"
    assert df["name"][1] == "聯發科"


def test_parse_industry():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["industry"][0] == "半導體"


def test_parse_concept():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert "AI/HPC" in df["concept"][0]
    assert "台灣50" in df["concept"][0]


def test_parse_close():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["close"][0] == pytest.approx(1075.0)
    assert df["close"][1] == pytest.approx(1200.0)


def test_parse_volume():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["volume"][0] == 18560
    assert df["volume"][1] == 8320


def test_parse_amount():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["amount"][0] == "199.24"


# ─── Schema ───────────────────────────────────────────────────────────────────


def test_schema_has_required_columns():
    df = parse_screener_result(_fixture("screener_result.html"))
    for col in ("stock_id", "name", "industry", "concept", "close", "volume", "amount"):
        assert col in df.columns


def test_close_is_float():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["close"].dtype == pl.Float64


def test_volume_is_int():
    df = parse_screener_result(_fixture("screener_result.html"))
    assert df["volume"].dtype == pl.Int64


# ─── 錯誤情況 ────────────────────────────────────────────────────────────────


def test_missing_table_raises():
    html = "<html><body><p>no table here</p></body></html>"
    with pytest.raises(GoodinfoParseError):
        parse_screener_result(html)


def test_empty_table_returns_empty_df():
    html = """<html><body>
    <table id="tblStockList" class="solid_1_padding_4_4_tbl">
    <thead>
    <tr><td>股號</td><td>股名</td><td>收盤</td></tr>
    </thead>
    <tbody></tbody>
    </table></body></html>"""
    df = parse_screener_result(html)
    assert df.is_empty()
    assert "stock_id" in df.columns


def test_empty_table_has_correct_schema():
    html = """<html><body>
    <table id="tblStockList" class="solid_1_padding_4_4_tbl">
    <thead><tr><td>股號</td></tr></thead>
    <tbody></tbody>
    </table></body></html>"""
    df = parse_screener_result(html)
    assert "close" in df.columns
    assert df["close"].dtype == pl.Float64


# ─── 整合鏈路：YAML → URL → fixture HTML → DataFrame ────────────────────────


def test_full_chain_yaml_to_df():
    """YAML → build URL → 用 fixture HTML → 解析出正確 DataFrame。"""
    from tw_screener.screener.goodinfo.url_builder import build_screener_url, load_strategy

    strategy = load_strategy(Path("config/strategies/a_breakout.yaml"))
    url = build_screener_url(strategy, "https://goodinfo.tw/tw")

    assert "StockList.asp" in url
    assert "FL_ITEM0" in url

    df = parse_screener_result(_fixture("screener_result.html"))

    assert len(df) == 3
    assert df["stock_id"][0] == "2330"
    assert "stock_id" in df.columns
    assert "close" in df.columns
