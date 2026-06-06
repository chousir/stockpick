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
    """load_concepts（薄殼）：多標籤以「、」串接成單欄。

    用「包含」而非「等於」——concepts.yaml 半自動維護，build-themes 會 merge 概念股，
    故手動次產業標籤恆在、但可能附帶概念股標籤。
    """
    df = load_concepts(Path("config/concepts.yaml"))
    m = {r["stock_id"]: r["sub_industry"] for r in df.iter_rows(named=True)}
    assert "晶圓代工" in m["2330"]  # 台積電：晶圓代工（Wantgoo 匯入曾誤併入 IC生產製造，已正名）
    assert "IC設計服務" in m["3034"] and "面板業" in m["3034"]  # 多標籤串接


def test_load_concepts_missing_file(tmp_path: Path):
    df = load_concepts(tmp_path / "nope.yaml")
    assert df.is_empty()
    assert "sub_industry" in df.columns


def test_load_themes_explodes_multilabel_real_file():
    """load_themes：多標籤一檔展開成多列；次產業標籤 kind=次產業（可能另含概念股列）。"""
    df = load_themes(Path("config/concepts.yaml"))
    rows = df.filter(pl.col("stock_id") == "3034")
    themes = set(rows["theme"].to_list())
    assert {"IC設計服務", "面板業"} <= themes  # 含這兩個次產業（可能再加概念股）
    kinds = {r["theme"]: r["kind"] for r in rows.iter_rows(named=True)}
    assert kinds["IC設計服務"] == SUB_INDUSTRY_KIND
    assert kinds["面板業"] == SUB_INDUSTRY_KIND


def test_load_themes_kind_from_concept_themes(tmp_path: Path):
    """concept_themes 清單內的標籤 → 概念股；其餘 → 次產業（同檔多標籤、各自 kind）。"""
    p = tmp_path / "concepts.yaml"
    p.write_text(
        "concept_themes: [衛星]\n"
        "concepts:\n"
        '  "2330": IC生產製造\n'
        '  "2317": [機殼, 衛星]\n',
        encoding="utf-8",
    )
    df = load_themes(p)
    kinds = {(r["stock_id"], r["theme"]): r["kind"] for r in df.iter_rows(named=True)}
    assert kinds[("2330", "IC生產製造")] == SUB_INDUSTRY_KIND
    assert kinds[("2317", "機殼")] == SUB_INDUSTRY_KIND
    assert kinds[("2317", "衛星")] == CONCEPT_KIND  # 在 concept_themes 內


def test_load_themes_empty_when_missing(tmp_path: Path):
    df = load_themes(tmp_path / "nope.yaml")
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
