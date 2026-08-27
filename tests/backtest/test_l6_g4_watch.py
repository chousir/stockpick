"""L6/G4 前瞻累積軌測試（docs/31 §9 item4）。純函式，不打網、不碰真快取。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.backtest.l6_g4_watch import (
    LEDGER_SCHEMA,
    build_distress_snapshot,
    build_l6_g4_snapshot,
    ledger_progress_summary,
    revenue_disclosure_date,
    select_g4_candidates,
    select_l6_candidates,
    select_l6_candidates_distressed,
    upsert_l6_g4_ledger,
)


def _universe(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "stock_id": pl.Utf8, "name": pl.Utf8,
        "market_cap_billion": pl.Float64, "pe_ratio": pl.Float64,
        "cum_rev_yoy_pct": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema)


def _revenue(rows: list[dict]) -> pl.DataFrame:
    schema = {"stock_id": pl.Utf8, "year_month": pl.Utf8, "yoy_pct": pl.Float64}
    return pl.DataFrame(rows, schema=schema)


def test_revenue_disclosure_date_rolls_year() -> None:
    assert revenue_disclosure_date("202607") == date(2026, 8, 10)
    assert revenue_disclosure_date("202612") == date(2027, 1, 10)


def test_revenue_disclosure_date_invalid_input() -> None:
    assert revenue_disclosure_date("") is None
    assert revenue_disclosure_date("2026") is None
    assert revenue_disclosure_date("202613") is None


def test_l6_2cond_hit_without_flow_or_mktcap() -> None:
    """YoY≥20∧PE≤25 命中但投信買超為負、市值<100億 → l6_2cond True、l6_4cond False。"""
    universe = _universe(
        [{"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0,
          "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0}]
    )
    revenue = _revenue([{"stock_id": "1101", "year_month": "202607", "yoy_pct": 10.0}])
    snap = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={"1101": (5.0, 2.0)},
        trust_net_5d={"1101": -100.0}, week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert snap.height == 1
    row = snap.row(0, named=True)
    assert row["l6_2cond"] is True
    assert row["l6_4cond"] is False


def test_l6_4cond_requires_flow_and_mktcap() -> None:
    universe = _universe(
        [{"stock_id": "2330", "name": "台積電", "market_cap_billion": 500.0,
          "pe_ratio": 22.0, "cum_rev_yoy_pct": 30.0}]
    )
    revenue = _revenue([{"stock_id": "2330", "year_month": "202607", "yoy_pct": 15.0}])
    snap = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={}, trust_net_5d={"2330": 1000.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    row = snap.row(0, named=True)
    assert row["l6_2cond"] is True
    assert row["l6_4cond"] is True


def test_g4_requires_all_three_conditions() -> None:
    """yoy_pct>cum_yoy_pct ∧ cum_yoy_pct≥0 ∧ rev_yoy_delta≥0——三缺一不算命中。"""
    universe = _universe(
        [
            {"stock_id": "AAA", "name": "命中", "market_cap_billion": 10.0,
             "pe_ratio": 40.0, "cum_rev_yoy_pct": 5.0},
            {"stock_id": "BBB", "name": "累計為負", "market_cap_billion": 10.0,
             "pe_ratio": 40.0, "cum_rev_yoy_pct": -3.0},
        ]
    )
    revenue = _revenue(
        [
            {"stock_id": "AAA", "year_month": "202607", "yoy_pct": 12.0},
            {"stock_id": "BBB", "year_month": "202607", "yoy_pct": 12.0},
        ]
    )
    snap = build_l6_g4_snapshot(
        universe, revenue,
        yoy_deltas={"AAA": (3.0, 1.0), "BBB": (3.0, 1.0)},
        trust_net_5d={}, week="2026-W34", data_date=date(2026, 8, 22),
    )
    # AAA: yoy(12)>cum(5) ∧ cum≥0 ∧ delta(3)≥0 → g4 True；BBB cum<0 → g4 False（且皆非L6）
    got = {r["stock_id"]: r["g4"] for r in snap.iter_rows(named=True)}
    assert got == {"AAA": True}
    assert "BBB" not in got  # 三式全 False 的列不出現在底帳


def test_no_match_rows_dropped() -> None:
    universe = _universe(
        [{"stock_id": "9999", "name": "都沒中", "market_cap_billion": 10.0,
          "pe_ratio": 60.0, "cum_rev_yoy_pct": -10.0}]
    )
    empty_revenue = pl.DataFrame(
        schema={"stock_id": pl.Utf8, "year_month": pl.Utf8, "yoy_pct": pl.Float64}
    )
    snap = build_l6_g4_snapshot(
        universe, empty_revenue,
        yoy_deltas={}, trust_net_5d={}, week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert snap.is_empty()


def test_select_l6_candidates_filters_to_l6_2cond_only() -> None:
    """l6_2cond命中(YoY≥20∧PE≤25)但l6_4cond不成立(投信買超為負)的列仍要出現——
    docs/31 §9刻意只用l6_2cond讀法，不擅自升級成四條件版。"""
    universe = _universe([
        {"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0,
         "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0},
        {"stock_id": "9999", "name": "都沒中", "market_cap_billion": 10.0,
         "pe_ratio": 60.0, "cum_rev_yoy_pct": -10.0},
    ])
    revenue = _revenue([{"stock_id": "1101", "year_month": "202607", "yoy_pct": 10.0}])
    snap = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={"1101": (5.0, 2.0)},
        trust_net_5d={"1101": -100.0}, week="2026-W34", data_date=date(2026, 8, 22),
    )
    out = select_l6_candidates(snap)
    assert out["stock_id"].to_list() == ["1101"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_l6_candidates_empty_snapshot() -> None:
    out = select_l6_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


# ─── 2026-08-26 population-gate修正：build_distress_snapshot／
# select_l6_candidates_distressed ───


def _market_hist(n_days: int = 100) -> pl.DataFrame:
    """兩檔合成日線：1101一路下跌到接近60日低+跌破MA60+8週負報酬（落難）；
    2330一路上漲、遠離低點+站上MA60+8週正報酬（健康成長，非落難）。"""
    from datetime import timedelta

    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(dates):
        distressed_close = 100.0 - 30.0 * i / (n_days - 1)
        healthy_close = 50.0 + 50.0 * i / (n_days - 1)
        rows.append({"date": d, "stock_id": "1101", "close": distressed_close, "volume": 1000})
        rows.append({"date": d, "stock_id": "2330", "close": healthy_close, "volume": 1000})
    return pl.DataFrame(
        rows, schema={"date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64, "volume": pl.Int64}
    )


def test_build_distress_snapshot_separates_declining_from_rising() -> None:
    out = build_distress_snapshot(_market_hist())
    rows = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    assert rows["1101"]["ma60_dist_pct"] < -8.0
    assert rows["1101"]["low60_dist_pct"] <= 15.0
    assert rows["1101"]["ret_8w_pct"] < 0.0
    assert rows["2330"]["ma60_dist_pct"] > 0.0
    assert rows["2330"]["low60_dist_pct"] > 15.0
    assert rows["2330"]["ret_8w_pct"] > 0.0


def test_build_distress_snapshot_empty_when_missing_columns() -> None:
    assert build_distress_snapshot(pl.DataFrame({"date": [date(2026, 1, 1)]})).is_empty()


def test_select_l6_candidates_distressed_excludes_healthy_growth_stock() -> None:
    """兩檔皆命中l6_2cond(YoY≥20∧PE≤25∧市值達標)，但只有落難的1101該出現——
    這正是本次修法要防的：全市場套YoY/PE會把健康成長股(2330)誤當左側候選。"""
    universe = _universe([
        {"stock_id": "1101", "name": "台泥", "market_cap_billion": 200.0,
         "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0},
        {"stock_id": "2330", "name": "台積電", "market_cap_billion": 200.0,
         "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0},
    ])
    revenue = _revenue([])
    snap = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={}, trust_net_5d={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    distress = build_distress_snapshot(_market_hist())
    out = select_l6_candidates_distressed(snap, distress)
    assert out["stock_id"].to_list() == ["1101"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_l6_candidates_distressed_market_cap_gate() -> None:
    """技術面落難但市值不足100億 → 仍排除（落難週操作定義的市值門檻）。"""
    universe = _universe(
        [{"stock_id": "1101", "name": "小型落難股", "market_cap_billion": 50.0,
          "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0}]
    )
    snap = build_l6_g4_snapshot(
        universe, _revenue([]), yoy_deltas={}, trust_net_5d={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    distress = build_distress_snapshot(_market_hist())
    out = select_l6_candidates_distressed(snap, distress, market_cap_min_billion=100.0)
    assert out.is_empty()


def test_select_l6_candidates_distressed_empty_inputs() -> None:
    empty_snap = pl.DataFrame(schema=LEDGER_SCHEMA)
    empty_dist = pl.DataFrame(
        schema={"stock_id": pl.Utf8, "ma60_dist_pct": pl.Float64,
                "low60_dist_pct": pl.Float64, "ret_8w_pct": pl.Float64}
    )
    assert select_l6_candidates_distressed(empty_snap, empty_dist).is_empty()


def test_select_g4_candidates_filters_to_g4_hits_only() -> None:
    universe = _universe([
        {"stock_id": "AAA", "name": "命中", "market_cap_billion": 10.0,
         "pe_ratio": 40.0, "cum_rev_yoy_pct": 5.0},
        {"stock_id": "BBB", "name": "累計為負", "market_cap_billion": 10.0,
         "pe_ratio": 40.0, "cum_rev_yoy_pct": -3.0},
    ])
    revenue = _revenue([
        {"stock_id": "AAA", "year_month": "202607", "yoy_pct": 12.0},
        {"stock_id": "BBB", "year_month": "202607", "yoy_pct": 12.0},
    ])
    snap = build_l6_g4_snapshot(
        universe, revenue,
        yoy_deltas={"AAA": (3.0, 1.0), "BBB": (3.0, 1.0)},
        trust_net_5d={}, week="2026-W34", data_date=date(2026, 8, 22),
    )
    out = select_g4_candidates(snap)
    assert out["stock_id"].to_list() == ["AAA"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g4_candidates_empty_snapshot() -> None:
    out = select_g4_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


def test_revenue_preview_risk_flag() -> None:
    """202607 公告日估計 2026-08-10——資料日早於公告日 → preview_risk True。"""
    universe = _universe(
        [{"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0,
          "pe_ratio": 20.0, "cum_rev_yoy_pct": 25.0}]
    )
    revenue = _revenue([{"stock_id": "1101", "year_month": "202607", "yoy_pct": 10.0}])
    early = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={}, trust_net_5d={},
        week="2026-W32", data_date=date(2026, 8, 3),
    )
    late = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas={}, trust_net_5d={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert early.row(0, named=True)["revenue_preview_risk"] is True
    assert late.row(0, named=True)["revenue_preview_risk"] is False


def test_upsert_ledger_idempotent_same_week(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    week1 = pl.DataFrame(
        [{"week": "2026-W34", "data_date": date(2026, 8, 22), "stock_id": "1101",
          "name": "台泥", "market_cap_billion": 50.0, "pe_ratio": 20.0,
          "rev_yoy_pct": 10.0, "cum_rev_yoy_pct": 25.0, "rev_yoy_delta": 3.0,
          "revenue_year_month": "202607", "revenue_preview_risk": False,
          "trust_net_5d": 100.0, "l6_2cond": True, "l6_4cond": False, "g4": False}],
        schema=LEDGER_SCHEMA,
    )
    ledger = upsert_l6_g4_ledger(path, week1)
    assert ledger.height == 1

    # 同週重跑（門檻改變導致 l6_4cond 變 True）→ 覆寫該週該股，不重複累加
    week1_rerun = week1.with_columns(pl.lit(True).alias("l6_4cond"))
    ledger2 = upsert_l6_g4_ledger(path, week1_rerun)
    assert ledger2.height == 1
    assert ledger2.row(0, named=True)["l6_4cond"] is True

    week2 = week1.with_columns(
        pl.lit("2026-W35").alias("week"), pl.lit(date(2026, 8, 29)).alias("data_date")
    )
    ledger3 = upsert_l6_g4_ledger(path, week2)
    assert ledger3.height == 2
    assert set(ledger3["week"].to_list()) == {"2026-W34", "2026-W35"}


def test_upsert_ledger_all_null_column_does_not_corrupt_later_weeks(tmp_path: Path) -> None:
    """回歸測試（2026-08-23）：某週寫入時某數值欄全 null（OTC股缺快取的典型情況）——
    該欄寫進 CSV 後只有空白列，若讀回時沒有明帶 schema，polars 會把它推斷成 Utf8。
    下一次 upsert 若該欄出現真實浮點值，`diagonal_relaxed` 會把**整欄**（新舊都算）
    往 Utf8 靠齊，3.0 會變成字串 "3.0"——底帳是唯一資料源，這個污染永久發生、
    無法回頭修，必須在讀檔那一步就用完整 schema_overrides 擋下來。
    """
    path = tmp_path / "ledger.csv"
    # 週1：rev_yoy_delta 全 null（OTC股缺第二個月快取，g4/rev_yoy_delta 天生 None）
    week1 = pl.DataFrame(
        [{"week": "2026-W34", "data_date": date(2026, 8, 22), "stock_id": "6488",
          "name": "環球晶", "market_cap_billion": 800.0, "pe_ratio": 18.0,
          "rev_yoy_pct": 15.0, "cum_rev_yoy_pct": 22.0, "rev_yoy_delta": None,
          "revenue_year_month": "202607", "revenue_preview_risk": False,
          "trust_net_5d": None, "l6_2cond": True, "l6_4cond": False, "g4": False}],
        schema=LEDGER_SCHEMA,
    )
    upsert_l6_g4_ledger(path, week1)

    # 週2：另一檔真的有 rev_yoy_delta 浮點值
    week2 = pl.DataFrame(
        [{"week": "2026-W35", "data_date": date(2026, 8, 29), "stock_id": "1101",
          "name": "台泥", "market_cap_billion": 50.0, "pe_ratio": 20.0,
          "rev_yoy_pct": 10.0, "cum_rev_yoy_pct": 25.0, "rev_yoy_delta": 3.0,
          "revenue_year_month": "202607", "revenue_preview_risk": False,
          "trust_net_5d": 100.0, "l6_2cond": True, "l6_4cond": False, "g4": False}],
        schema=LEDGER_SCHEMA,
    )
    ledger = upsert_l6_g4_ledger(path, week2)

    assert ledger.schema["rev_yoy_delta"] == pl.Float64
    w2_delta = ledger.filter(pl.col("stock_id") == "1101").row(0, named=True)["rev_yoy_delta"]
    assert isinstance(w2_delta, float)
    assert w2_delta == 3.0

    # 重新從磁碟讀一次（模擬下週再 upsert 時的讀檔）——確認寫進磁碟的 CSV 本身沒有被污染
    reread = pl.read_csv(path, schema_overrides=LEDGER_SCHEMA)
    assert reread.schema["rev_yoy_delta"] == pl.Float64


def test_ledger_progress_summary_counts_across_weeks() -> None:
    ledger = pl.DataFrame(
        [
            {"week": "2026-W34", "l6_2cond": True, "l6_4cond": False, "g4": False,
             "revenue_preview_risk": False},
            {"week": "2026-W35", "l6_2cond": True, "l6_4cond": True, "g4": True,
             "revenue_preview_risk": True},
        ],
        schema={
            "week": pl.Utf8, "l6_2cond": pl.Boolean, "l6_4cond": pl.Boolean,
            "g4": pl.Boolean, "revenue_preview_risk": pl.Boolean,
        },
    )
    summary = ledger_progress_summary(ledger)
    assert summary["n_weeks"] == 2
    assert summary["n_l6_2cond"] == 2
    assert summary["n_l6_4cond"] == 1
    assert summary["n_g4"] == 1
    assert summary["n_preview_risk"] == 1


def test_ledger_progress_summary_empty() -> None:
    summary = ledger_progress_summary(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert summary["n_weeks"] == 0
