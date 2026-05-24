"""tests/analysis/test_concepts.py — 主題對照表載入（全離線）。"""

from pathlib import Path

import polars as pl

from tw_screener.analysis.concepts import (
    CONCEPT_KIND,
    SUB_INDUSTRY_KIND,
    load_concepts,
    load_themes,
    unmapped_electronics,
)


def test_load_concepts_real_file():
    """load_concepts（薄殼）：多標籤以「、」串接成單欄。"""
    df = load_concepts(Path("config/concepts.yaml"))
    m = {r["stock_id"]: r["sub_industry"] for r in df.iter_rows(named=True)}
    assert m["2330"] == "IC生產製造"
    assert m["3034"] == "IC設計服務、面板業"  # 多標籤串接


def test_load_concepts_missing_file(tmp_path: Path):
    df = load_concepts(tmp_path / "nope.yaml")
    assert df.is_empty()
    assert "sub_industry" in df.columns


def test_load_themes_explodes_multilabel_real_file():
    """load_themes：多標籤一檔展開成多列、kind=次產業。"""
    df = load_themes(
        concepts_path=Path("config/concepts.yaml"), themes_path=Path("nonexistent.yaml")
    )
    rows = df.filter(pl.col("stock_id") == "3034")
    assert set(rows["theme"].to_list()) == {"IC設計服務", "面板業"}
    assert set(rows["kind"].to_list()) == {SUB_INDUSTRY_KIND}


def test_load_themes_merges_two_sources(tmp_path: Path):
    """concepts.yaml（次產業）＋ themes.yaml（概念股）合併成統一 long table。"""
    cpath = tmp_path / "concepts.yaml"
    cpath.write_text(
        'concepts:\n  "2330": IC生產製造\n  "3017": ["散熱", "AI"]\n', encoding="utf-8"
    )
    tpath = tmp_path / "themes.yaml"
    tpath.write_text(
        'themes:\n  衛星:\n    kind: 概念股\n    members: ["2330", "2314"]\n', encoding="utf-8"
    )
    df = load_themes(concepts_path=cpath, themes_path=tpath)
    # 2330 同時是次產業(IC生產製造) + 概念股(衛星) → 兩列
    assert set(df.filter(pl.col("stock_id") == "2330")["theme"].to_list()) == {
        "IC生產製造",
        "衛星",
    }
    kinds = {r["theme"]: r["kind"] for r in df.iter_rows(named=True)}
    assert kinds["IC生產製造"] == SUB_INDUSTRY_KIND
    assert kinds["衛星"] == CONCEPT_KIND


def test_load_themes_empty_when_both_missing(tmp_path: Path):
    df = load_themes(concepts_path=tmp_path / "a.yaml", themes_path=tmp_path / "b.yaml")
    assert df.is_empty()
    assert set(df.columns) == {"stock_id", "theme", "kind"}


def test_unmapped_electronics_flags_only_untagged_electronics():
    members = pl.DataFrame(
        {
            "stock_id": ["2330", "9999", "2882"],
            "industry_name": ["半導體業", "光電業", "金融保險"],
        }
    )
    themes_long = pl.DataFrame(
        {"stock_id": ["2330"], "theme": ["IC生產製造"], "kind": [SUB_INDUSTRY_KIND]}
    )
    out = unmapped_electronics(members, themes_long)
    assert out == ["9999"]          # 電子且未標次產業 → 提醒
    assert "2330" not in out         # 已標
    assert "2882" not in out         # 非電子大分類，不需標
