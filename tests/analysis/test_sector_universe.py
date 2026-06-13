"""sector_universe 測試（R0）：次產業 membership 與 28 類對照純讀。"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from tw_screener.analysis.sector_universe import (
    audit_priceless_members,
    list_subindustries,
    load_industry_mapping,
)

_CONCEPTS_YAML = """\
concept_themes:
- 5G
- AI人工智慧
concepts:
  '2330': 晶圓代工
  '2454':
  - IC設計
  - 5G
  '3034':
  - IC設計
  - AI人工智慧
  '8299': 記憶體模組
  '00878': 晶圓代工
  '2330Y': IC設計
"""


def _write_concepts(tmp_path: Path) -> Path:
    p = tmp_path / "concepts.yaml"
    p.write_text(_CONCEPTS_YAML, encoding="utf-8")
    return p


def test_list_subindustries_happy_path(tmp_path: Path):
    df = list_subindustries(_write_concepts(tmp_path))
    assert df.columns == ["sub_industry", "stock_id"]
    # 概念股主題（5G / AI人工智慧）不出現在次產業
    assert "5G" not in df["sub_industry"].to_list()
    assert "AI人工智慧" not in df["sub_industry"].to_list()
    # 手標次產業都在
    ic = df.filter(pl.col("sub_industry") == "IC設計")["stock_id"].to_list()
    assert "2454" in ic and "3034" in ic


def test_list_subindustries_filters_etf_and_warrant(tmp_path: Path):
    df = list_subindustries(_write_concepts(tmp_path))
    ids = df["stock_id"].to_list()
    assert "00878" not in ids  # ETF（00 開頭）
    assert "2330Y" not in ids  # 權證（含非數字字元）
    assert "2330" in ids


def test_list_subindustries_missing_file(tmp_path: Path):
    df = list_subindustries(tmp_path / "nope.yaml")
    assert df.is_empty()
    assert df.columns == ["sub_industry", "stock_id"]


def _industry_df(rows: list[tuple[str, str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema=["stock_id", "stock_name", "industry_code", "industry_name"],
        orient="row",
    )


def test_load_industry_mapping_merges_listed_and_otc(tmp_path: Path):
    _industry_df([("2330", "台積電", "24", "半導體業")]).write_parquet(
        tmp_path / "industry_202606.parquet"
    )
    _industry_df([("8299", "群聯", "24", "半導體業")]).write_parquet(
        tmp_path / "otc_industry_202606.parquet"
    )
    df = load_industry_mapping(tmp_path)
    assert set(df["stock_id"].to_list()) == {"2330", "8299"}


def test_load_industry_mapping_uses_latest_month(tmp_path: Path):
    _industry_df([("2330", "台積電", "99", "舊分類")]).write_parquet(
        tmp_path / "industry_202605.parquet"
    )
    _industry_df([("2330", "台積電", "24", "半導體業")]).write_parquet(
        tmp_path / "industry_202606.parquet"
    )
    df = load_industry_mapping(tmp_path)
    assert df.height == 1
    assert df["industry_name"][0] == "半導體業"


def test_load_industry_mapping_empty_dir(tmp_path: Path):
    df = load_industry_mapping(tmp_path)
    assert df.is_empty()
    assert "industry_name" in df.columns


# ─── audit_priceless_members ──────────────────────────────────────────────────


def _members(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["sub_industry", "stock_id"], orient="row")


def test_audit_priceless_filters_to_no_price():
    members = _members([("IC設計", "2330"), ("IC設計", "8888"), ("記憶體", "9999")])
    # 2330 有價；8888/9999 無價（興櫃/下市/誤標）
    out = audit_priceless_members(members, priced_ids={"2330", "2454"})
    assert set(out["stock_id"].to_list()) == {"8888", "9999"}


def test_audit_priceless_all_priced_returns_empty():
    members = _members([("IC設計", "2330"), ("記憶體", "8299")])
    out = audit_priceless_members(members, priced_ids=["2330", "8299"])
    assert out.is_empty()


def test_audit_priceless_keeps_multi_label_rows():
    # 一檔多標籤 → 無價時每個 (sub_industry, stock_id) 列都保留，供逐檔併列
    members = _members([("IC設計", "8888"), ("5G", "8888")])
    out = audit_priceless_members(members, priced_ids=set())
    assert out.height == 2
    assert sorted(out["sub_industry"].to_list()) == ["5G", "IC設計"]


def test_audit_priceless_empty_members():
    empty = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    assert audit_priceless_members(empty, priced_ids={"2330"}).is_empty()
