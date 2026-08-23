"""官方族群前5前瞻累積軌測試（docs/31 §12）。純函式合成資料，不打網、不碰真快取。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.backtest.official_sector_watch import (
    LEDGER_SCHEMA,
    latest_top5_snapshot,
    ledger_progress_summary,
    upsert_ledger,
)


def _membership(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["sub_industry", "stock_id"], orient="row")


def _trend(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"sub_industry": pl.Utf8, "date": pl.Date, "trend_score": pl.Float64},
        orient="row",
    )


def test_latest_top5_snapshot_only_uses_latest_date() -> None:
    membership = _membership([("半導體業", "2330"), ("光電業", "3008")])
    trend = _trend(
        [
            ("半導體業", date(2026, 8, 14), 10.0),   # 較舊日期，不該被採用
            ("半導體業", date(2026, 8, 21), 90.0),
            ("光電業", date(2026, 8, 21), 10.0),
        ]
    )
    snap = latest_top5_snapshot(
        membership, trend, names={"2330": "台積電", "3008": "大立光"},
        week="2026-W34", data_date=date(2026, 8, 21), purity_used=0.5, top_n_groups=1,
    )
    assert snap["stock_id"].to_list() == ["2330"]
    assert snap.row(0, named=True)["group_rank"] == 1
    assert snap.row(0, named=True)["trend_score"] == 90.0


def test_latest_top5_snapshot_multi_label_stock_gets_multiple_rows() -> None:
    membership = _membership([("半導體業", "9999"), ("電子零組件業", "9999")])
    trend = _trend(
        [
            ("半導體業", date(2026, 8, 21), 90.0),
            ("電子零組件業", date(2026, 8, 21), 80.0),
            ("光電業", date(2026, 8, 21), 10.0),
        ]
    )
    snap = latest_top5_snapshot(
        membership, trend, names={"9999": "多標籤股"},
        week="2026-W34", data_date=date(2026, 8, 21), purity_used=0.5, top_n_groups=2,
    )
    assert snap.height == 2
    assert set(snap["sub_industry"].to_list()) == {"半導體業", "電子零組件業"}


def test_latest_top5_snapshot_empty_inputs() -> None:
    empty_membership = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    empty_trend = pl.DataFrame(
        schema={"sub_industry": pl.Utf8, "date": pl.Date, "trend_score": pl.Float64}
    )
    assert latest_top5_snapshot(
        empty_membership, empty_trend, {}, "2026-W34", date(2026, 8, 21), 0.5
    ).is_empty()


def test_upsert_ledger_allows_multiple_rows_per_stock_same_week(tmp_path: Path) -> None:
    """key含sub_industry——同一股票同週可以有多列（多標籤同時屬於多個前5群組）。"""
    path = tmp_path / "ledger.csv"
    week1 = pl.DataFrame(
        [
            {"week": "2026-W34", "data_date": date(2026, 8, 21), "stock_id": "9999",
             "name": "多標籤股", "sub_industry": "半導體業", "trend_score": 90.0,
             "group_rank": 1, "purity_used": 0.5},
            {"week": "2026-W34", "data_date": date(2026, 8, 21), "stock_id": "9999",
             "name": "多標籤股", "sub_industry": "電子零組件業", "trend_score": 80.0,
             "group_rank": 2, "purity_used": 0.5},
        ],
        schema=LEDGER_SCHEMA,
    )
    ledger = upsert_ledger(path, week1)
    assert ledger.height == 2
    assert ledger.filter(pl.col("stock_id") == "9999").height == 2


def test_upsert_ledger_same_week_rerun_replaces_not_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    week1 = pl.DataFrame(
        [{"week": "2026-W34", "data_date": date(2026, 8, 21), "stock_id": "2330",
          "name": "台積電", "sub_industry": "半導體業", "trend_score": 90.0,
          "group_rank": 1, "purity_used": 0.5}],
        schema=LEDGER_SCHEMA,
    )
    upsert_ledger(path, week1)
    ledger2 = upsert_ledger(path, week1)
    assert ledger2.height == 1


def test_upsert_ledger_all_null_column_does_not_corrupt_later_weeks(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    week1 = pl.DataFrame(
        [{"week": "2026-W34", "data_date": date(2026, 8, 21), "stock_id": "9999",
          "name": None, "sub_industry": "半導體業", "trend_score": None,
          "group_rank": 1, "purity_used": 0.5}],
        schema=LEDGER_SCHEMA,
    )
    upsert_ledger(path, week1)
    week2 = pl.DataFrame(
        [{"week": "2026-W35", "data_date": date(2026, 8, 28), "stock_id": "2330",
          "name": "台積電", "sub_industry": "半導體業", "trend_score": 88.0,
          "group_rank": 1, "purity_used": 0.5}],
        schema=LEDGER_SCHEMA,
    )
    ledger = upsert_ledger(path, week2)
    assert ledger.schema["trend_score"] == pl.Float64
    w2 = ledger.filter(pl.col("stock_id") == "2330").row(0, named=True)["trend_score"]
    assert isinstance(w2, float)
    assert w2 == 88.0


def test_ledger_progress_summary_counts() -> None:
    ledger = pl.DataFrame(
        [
            {"week": "2026-W34", "stock_id": "2330"},
            {"week": "2026-W34", "stock_id": "3008"},
            {"week": "2026-W35", "stock_id": "2330"},
        ],
        schema={"week": pl.Utf8, "stock_id": pl.Utf8},
    )
    summary = ledger_progress_summary(ledger)
    assert summary["n_weeks"] == 2
    assert summary["n_rows"] == 3
    assert summary["n_unique_stocks"] == 2


def test_ledger_progress_summary_empty() -> None:
    summary = ledger_progress_summary(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert summary["n_weeks"] == 0
