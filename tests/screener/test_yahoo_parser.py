"""tests/screener/test_yahoo_parser.py — Yahoo 類股/概念股解析（全離線）。"""

from pathlib import Path

import pytest

from tw_screener.screener.yahoo.parser import (
    YahooParseError,
    parse_category_members,
    parse_class_index,
)

_FIX = Path("tests/fixtures/yahoo")


def _read(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


def test_parse_class_index_counts_by_label():
    refs = parse_class_index(_read("class_index.html"))
    by_label: dict[str, int] = {}
    for r in refs:
        by_label[r.kind] = by_label.get(r.kind, 0) + 1
    assert by_label["電子產業"] == 35
    assert by_label["概念股"] == 101
    assert by_label["集團股"] == 65
    sat = next(r for r in refs if r.name == "衛星/低軌衛星")
    assert sat.kind == "概念股"
    assert "&amp;" not in sat.href and "category=" in sat.href


def test_parse_class_index_raises_on_garbage():
    with pytest.raises(YahooParseError):
        parse_class_index("<html>no themes here</html>")


def test_parse_category_members_satellite():
    members = parse_category_members(_read("category_satellite.html"))
    ids = [sid for sid, _ in members]
    assert len(ids) == len(set(ids))                 # 去重
    assert len(ids) == 30                             # SSR 前約 30 檔
    assert all(sid.isdigit() and len(sid) == 4 for sid in ids)
    d = dict(members)
    assert d["2317"] == "鴻海"
    assert d["2454"] == "聯發科"
    assert d["4912"] == "聯德控股-KY"                 # -KY 在名稱、股號乾淨


def test_parse_category_members_capped_about_30():
    """蘋果200大供應商實際約 200，但 SSR 只內嵌前約 30（截斷、領頭觀察）。"""
    members = parse_category_members(_read("category_capped.html"))
    assert 25 <= len(members) <= 35


def test_parse_category_members_empty_on_garbage():
    assert parse_category_members("<html></html>") == []
