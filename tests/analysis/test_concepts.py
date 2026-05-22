"""tests/analysis/test_concepts.py — 次產業對照表載入（全離線）。"""

from pathlib import Path

import polars as pl

from tw_screener.analysis.concepts import load_concepts, unmapped_electronics


def test_load_concepts_real_file():
    """讀正式 config/concepts.yaml：半導體龍頭次產業正確。"""
    df = load_concepts(Path("config/concepts.yaml"))
    m = {r["stock_id"]: r["sub_industry"] for r in df.iter_rows(named=True)}
    assert m["2330"] == "晶圓代工"
    assert m["8299"] == "記憶體模組"   # 群聯
    assert m["2408"] == "記憶體IC"     # 南亞科
    assert m["3034"] == "IC設計"       # 聯詠


def test_load_concepts_missing_file(tmp_path: Path):
    df = load_concepts(tmp_path / "nope.yaml")
    assert df.is_empty()
    assert "sub_industry" in df.columns


def test_unmapped_electronics_flags_only_untagged_electronics():
    members = pl.DataFrame(
        {
            "stock_id": ["2330", "9999", "2882"],
            "industry_name": ["半導體業", "光電業", "金融保險"],
            "sub_industry": ["晶圓代工", None, None],
        }
    )
    out = unmapped_electronics(members)
    assert out == ["9999"]          # 電子且未標 → 提醒
    assert "2330" not in out         # 已標
    assert "2882" not in out         # 非電子大分類，不需標
