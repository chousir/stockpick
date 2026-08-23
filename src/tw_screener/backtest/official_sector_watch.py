"""backtest/official_sector_watch.py — docs/31 §12：官方族群前5前瞻累積軌每週快照。

跟`l6_g4_watch`/`g1_g2_g5_watch`同一種「先記錄、樣本夠了再判讀」的理由，但這條軌
訊號在**群組層**（先算官方對應群組的trend_score排名，再回推哪些個股屬於前5名
群組），不是個股層候選——多一層「先分組排名再展開回個股」的計算。本檔只做最新
一天的排名快照，不逐週回測（回測見§10的`official_sector_grid.py`／`_runner.py`）。

**purity門檻定案0.5**（§10.10已示範「單一最佳值」不穩定，建議穩健區間0.4-0.6，
取中點當前瞻累積軌的代表性設定，非重新調參選出來的）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

LEDGER_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "sub_industry": pl.Utf8,
    "trend_score": pl.Float64,
    "group_rank": pl.Int64,
    "purity_used": pl.Float64,
}


def latest_top5_snapshot(
    membership: pl.DataFrame,
    trend: pl.DataFrame,
    names: dict[str, str | None],
    week: str,
    data_date: date,
    purity_used: float,
    top_n_groups: int = 5,
) -> pl.DataFrame:
    """把最新一天的官方群組trend_score排名前N，展開回個股列。

    Args:
        membership: `official_sector_grid.build_hand_sector_membership()` 輸出
            （sub_industry, stock_id；多標籤股可能屬於多個群組，各自展開一列）。
        trend: `rotation_efficacy.trend_score_series()` 輸出（sub_industry, date,
            trend_score）——只取其中日期最大的那一天。
        names: {stock_id: name}，呼叫端算好傳入。

    Returns:
        LEDGER_SCHEMA；同一股票若屬於多個前5名群組，會出現多列（各自不同
        sub_industry，不去重——多標籤本身是既有語意，見`list_subindustries()`）。
    """
    if membership.is_empty() or trend.is_empty():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    latest_date = trend["date"].max()
    latest = trend.filter(pl.col("date") == latest_date).drop_nulls("trend_score")
    if latest.is_empty():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    ranked = latest.with_columns(
        pl.col("trend_score").rank(method="min", descending=True).alias("_rank")
    ).filter(pl.col("_rank") <= top_n_groups)
    if ranked.is_empty():
        return pl.DataFrame(schema=LEDGER_SCHEMA)

    joined = membership.join(
        ranked.select("sub_industry", "trend_score", pl.col("_rank").alias("group_rank")),
        on="sub_industry",
        how="inner",
    )
    if joined.is_empty():
        return pl.DataFrame(schema=LEDGER_SCHEMA)

    rows: list[dict] = []
    for r in joined.iter_rows(named=True):
        rows.append(
            {
                "week": week,
                "data_date": data_date,
                "stock_id": r["stock_id"],
                "name": names.get(r["stock_id"]),
                "sub_industry": r["sub_industry"],
                "trend_score": r["trend_score"],
                "group_rank": int(r["group_rank"]),
                "purity_used": purity_used,
            }
        )
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


def _read_ledger(path: Path) -> pl.DataFrame:
    """讀既有底帳 CSV，全欄位明帶 `schema_overrides=LEDGER_SCHEMA`。

    同`l6_g4_watch._read_ledger`修法：全欄位明帶schema，避免某週某數值欄全null
    時被polars推斷成Utf8、下次concat把整欄污染成字串。
    """
    if not path.exists():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    return pl.read_csv(path, try_parse_dates=True, schema_overrides=LEDGER_SCHEMA)


def upsert_ledger(path: Path, new_rows: pl.DataFrame) -> pl.DataFrame:
    """把本週快照併入底帳（以 (week, stock_id, sub_industry) 去重、冪等）。

    key含`sub_industry`（跟L6/G4/G1/G2/G5的(week, stock_id)不同）——本表允許
    同一股票同週出現多列（多標籤股同時屬於多個前5名群組），不能只用(week, stock_id)
    去重，否則會誤刪合法的第二列。
    """
    if new_rows.is_empty():
        return _read_ledger(path)
    existing = _read_ledger(path)
    if not existing.is_empty():
        week = new_rows["week"][0]
        existing = existing.filter(pl.col("week") != week)
    merged = pl.concat([existing, new_rows], how="diagonal_relaxed").sort(
        ["week", "stock_id", "sub_industry"]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_csv(path)
    return merged


def ledger_progress_summary(ledger: pl.DataFrame) -> dict[str, object]:
    """底帳累積進度一行摘要（runner 印給人看，不是統計裁決）。"""
    if ledger.is_empty():
        return {"n_weeks": 0, "weeks": [], "n_rows": 0, "n_unique_stocks": 0}
    return {
        "n_weeks": ledger["week"].n_unique(),
        "weeks": sorted(ledger["week"].unique().to_list()),
        "n_rows": ledger.height,
        "n_unique_stocks": ledger["stock_id"].n_unique(),
    }
