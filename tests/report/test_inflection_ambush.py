"""build_inflection_ambush 的 schema-inference 回歸測試（2026-08-29 實跑觸發）。

`_frame()` 內部 `pl.DataFrame(rows)` 預設 `infer_schema_length=100`，只看前 100
列猜欄位型別；若某欄（如 `theme`）前 100 列全 null、101 列後才出現字串值，polars
會猜成非字串型別，write_csv 時崩潰（同 group_report.py 已修過的成因）。
"""

from __future__ import annotations

import polars as pl

from tw_screener.report.inflection_ambush import build_inflection_ambush

_BASE_COLS = [
    "stock_id", "name", "theme", "fundamental_health", "rev_yoy_pct",
    "base_zone", "dist_low_60d_pct", "foreign_inflection_days",
    "foreign_net_lots", "foreign_flow_diff_5_20", "margin_slim",
    "close", "low_60d", "ma60_dist_pct", "flags",
]


def _make_qualifying_rows(n: int, *, theme_after: int | None = None) -> list[dict]:
    """產生 n 列全部合格（貼底+剛轉買+外資小額）的列；theme_after 之後才給字串值。"""
    rows = []
    for i in range(n):
        theme = None if theme_after is not None and i < theme_after else "主機板"
        rows.append({
            "stock_id": f"{1000 + i}",
            "name": f"股{i}",
            "theme": theme,
            "fundamental_health": "強化",
            "rev_yoy_pct": 30.0,
            "base_zone": "貼底",
            "dist_low_60d_pct": 5.0,
            "foreign_inflection_days": 2,
            "foreign_net_lots": 100.0,
            "foreign_flow_diff_5_20": 10.0,
            "margin_slim": False,
            "close": 50.0,
            "low_60d": 48.0,
            "ma60_dist_pct": 3.0,
            "flags": "",
        })
    return rows


def test_build_inflection_ambush_over_100_rows_late_string_no_crash():
    """>100 列、theme 前 100 列皆 null、101 列後才出現字串——不可崩潰（回歸 2026-08-29）。"""
    rows = _make_qualifying_rows(120, theme_after=100)
    enriched = pl.DataFrame(rows, infer_schema_length=None)
    qualified, _near_miss = build_inflection_ambush(enriched)
    assert qualified.height == 120
    assert qualified.filter(pl.col("theme") == "主機板").height == 20


def test_build_inflection_ambush_empty_enriched_returns_empty():
    enriched = pl.DataFrame(schema={c: pl.Utf8 for c in _BASE_COLS})
    qualified, near_miss = build_inflection_ambush(enriched)
    assert qualified.is_empty()
    assert near_miss.is_empty()


def test_build_inflection_ambush_missing_required_columns_returns_empty():
    enriched = pl.DataFrame({"stock_id": ["1101"], "name": ["台泥"]})
    qualified, near_miss = build_inflection_ambush(enriched)
    assert qualified.is_empty()
    assert near_miss.is_empty()
