"""G1/G2/G5 前瞻累積軌測試（docs/31 §11）。純函式合成資料，不打網、不碰真快取。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.backtest.g1_g2_g5_watch import (
    LEDGER_SCHEMA,
    build_g1_g2_g5_snapshot,
    ledger_progress_summary,
    select_f2prime_candidates,
    select_g1_candidates,
    select_g2_candidates,
    select_g5_candidates,
    upsert_ledger,
)


def _universe(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "stock_id": pl.Utf8, "name": pl.Utf8,
        "market_cap_billion": pl.Float64, "cum_rev_yoy_pct": pl.Float64,
        "pe_ratio": pl.Float64,  # F2'（§20.10）需要；缺 key 的舊測試列自動填 null
    }
    return pl.DataFrame(rows, schema=schema)


def _fundamentals(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "stock_id": pl.Utf8, "quarter_label": pl.Utf8,
        "net_margin_pct": pl.Float64, "delta_net_margin_pct": pl.Float64,
        "op_margin_pct": pl.Float64, "delta_op_margin_pct": pl.Float64,
        "gross_margin_pct": pl.Float64, "roe_q_pct": pl.Float64,
        "debt_ratio_pct": pl.Float64, "current_ratio": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema)


def test_g1_hit_requires_all_four_conditions() -> None:
    universe = _universe(
        [{"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0, "cum_rev_yoy_pct": 5.0}]
    )
    fundamentals = _fundamentals(
        [{"stock_id": "1101", "quarter_label": "2026Q2", "net_margin_pct": 8.0,
          "delta_net_margin_pct": 2.0, "op_margin_pct": 10.0, "delta_op_margin_pct": 0.5,
          "gross_margin_pct": 20.0, "roe_q_pct": 1.0, "debt_ratio_pct": 70.0,
          "current_ratio": 0.5}]
    )
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, empty_peer, empty_val,
        ma60_map={"1101": 5.0}, amount_map={"1101": 100.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    row = snap.row(0, named=True)
    assert row["g1"] is True
    assert row["g2"] is False  # roe/debt/current都不過
    assert row["g5"] is False  # 無val_pctile/peer_median


def test_g1_fails_when_ma60_too_extended() -> None:
    universe = _universe(
        [{"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0, "cum_rev_yoy_pct": 5.0}]
    )
    fundamentals = _fundamentals(
        [{"stock_id": "1101", "quarter_label": "2026Q2", "net_margin_pct": 8.0,
          "delta_net_margin_pct": 2.0, "op_margin_pct": 10.0, "delta_op_margin_pct": 0.5,
          "gross_margin_pct": 20.0, "roe_q_pct": None, "debt_ratio_pct": None,
          "current_ratio": None}]
    )
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, empty_peer, empty_val,
        ma60_map={"1101": 20.0}, amount_map={"1101": 100.0},  # 距季線+20%，超過G1上限15%
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert snap.is_empty()


def test_g2_hit_pure_snapshot_no_delta_needed() -> None:
    universe = _universe(
        [{"stock_id": "2330", "name": "台積電",
          "market_cap_billion": 500.0, "cum_rev_yoy_pct": None}]
    )
    fundamentals = _fundamentals(
        [{"stock_id": "2330", "quarter_label": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": None, "delta_op_margin_pct": None,
          "gross_margin_pct": None, "roe_q_pct": 5.0, "debt_ratio_pct": 40.0,
          "current_ratio": 2.0}]
    )
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, empty_peer, empty_val,
        ma60_map={}, amount_map={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    row = snap.row(0, named=True)
    assert row["g1"] is False
    assert row["g2"] is True
    assert row["g5"] is False


def test_g5_requires_gross_margin_above_peer_median() -> None:
    universe = _universe(
        [{"stock_id": "3006", "name": "晶豪科",
          "market_cap_billion": 30.0, "cum_rev_yoy_pct": None}]
    )
    fundamentals = _fundamentals(
        [{"stock_id": "3006", "quarter_label": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": 15.0, "delta_op_margin_pct": 1.0,
          "gross_margin_pct": 35.0, "roe_q_pct": None, "debt_ratio_pct": None,
          "current_ratio": None}]
    )
    peer = pl.DataFrame(
        [{"stock_id": "3006", "subind_median": 30.0}],
        schema={"stock_id": pl.Utf8, "subind_median": pl.Float64},
    )
    val = pl.DataFrame(
        [{"stock_id": "3006", "val_pctile": 20.0}],
        schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64},
    )
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, peer, val,
        ma60_map={}, amount_map={"3006": 500.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    row = snap.row(0, named=True)
    assert row["g5"] is True

    # 反例：毛利率低於同業中位 → 不命中
    fundamentals_low = _fundamentals(
        [{"stock_id": "3006", "quarter_label": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": 15.0, "delta_op_margin_pct": 1.0,
          "gross_margin_pct": 25.0, "roe_q_pct": None, "debt_ratio_pct": None,
          "current_ratio": None}]
    )
    snap2 = build_g1_g2_g5_snapshot(
        universe, fundamentals_low, peer, val,
        ma60_map={}, amount_map={"3006": 500.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert snap2.is_empty()


def test_no_match_rows_dropped() -> None:
    universe = _universe(
        [{"stock_id": "9999", "name": "都沒中",
          "market_cap_billion": 1.0, "cum_rev_yoy_pct": -10.0}]
    )
    empty_fund = pl.DataFrame(schema={
        "stock_id": pl.Utf8, "quarter_label": pl.Utf8,
        "net_margin_pct": pl.Float64, "delta_net_margin_pct": pl.Float64,
        "op_margin_pct": pl.Float64, "delta_op_margin_pct": pl.Float64,
        "gross_margin_pct": pl.Float64, "roe_q_pct": pl.Float64,
        "debt_ratio_pct": pl.Float64, "current_ratio": pl.Float64,
    })
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, empty_fund, empty_peer, empty_val,
        ma60_map={}, amount_map={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    assert snap.is_empty()


def test_select_g2_candidates_filters_to_g2_hits_only() -> None:
    universe = _universe([
        {"stock_id": "2330", "name": "台積電",
         "market_cap_billion": 500.0, "cum_rev_yoy_pct": None},
        {"stock_id": "1234", "name": "不合格",
         "market_cap_billion": 1.0, "cum_rev_yoy_pct": None},
    ])
    fundamentals = _fundamentals([
        {"stock_id": "2330", "quarter_label": "2026Q2", "net_margin_pct": None,
         "delta_net_margin_pct": None, "op_margin_pct": None, "delta_op_margin_pct": None,
         "gross_margin_pct": None, "roe_q_pct": 5.0, "debt_ratio_pct": 40.0, "current_ratio": 2.0},
        {"stock_id": "1234", "quarter_label": "2026Q2", "net_margin_pct": None,
         "delta_net_margin_pct": None, "op_margin_pct": None, "delta_op_margin_pct": None,
         "gross_margin_pct": None, "roe_q_pct": 0.1, "debt_ratio_pct": 90.0, "current_ratio": 0.5},
    ])
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, empty_peer, empty_val,
        ma60_map={}, amount_map={},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    out = select_g2_candidates(snap)
    assert out["stock_id"].to_list() == ["2330"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g2_candidates_empty_snapshot() -> None:
    out = select_g2_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g1_candidates_filters_to_g1_hits_only() -> None:
    universe = _universe([
        {"stock_id": "1101", "name": "台泥", "market_cap_billion": 50.0, "cum_rev_yoy_pct": 5.0},
        {"stock_id": "9999", "name": "都沒中", "market_cap_billion": 1.0, "cum_rev_yoy_pct": -10.0},
    ])
    fundamentals = _fundamentals([
        {"stock_id": "1101", "quarter_label": "2026Q2", "net_margin_pct": 8.0,
         "delta_net_margin_pct": 2.0, "op_margin_pct": 10.0, "delta_op_margin_pct": 0.5,
         "gross_margin_pct": 20.0, "roe_q_pct": 1.0, "debt_ratio_pct": 70.0,
         "current_ratio": 0.5},
    ])
    empty_peer = pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    empty_val = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, empty_peer, empty_val,
        ma60_map={"1101": 5.0}, amount_map={"1101": 100.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    out = select_g1_candidates(snap)
    assert out["stock_id"].to_list() == ["1101"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g1_candidates_empty_snapshot() -> None:
    out = select_g1_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g5_candidates_filters_to_g5_hits_only() -> None:
    universe = _universe(
        [{"stock_id": "3006", "name": "晶豪科",
          "market_cap_billion": 30.0, "cum_rev_yoy_pct": None}]
    )
    fundamentals = _fundamentals(
        [{"stock_id": "3006", "quarter_label": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": 15.0, "delta_op_margin_pct": 1.0,
          "gross_margin_pct": 35.0, "roe_q_pct": None, "debt_ratio_pct": None,
          "current_ratio": None}]
    )
    peer = pl.DataFrame(
        [{"stock_id": "3006", "subind_median": 30.0}],
        schema={"stock_id": pl.Utf8, "subind_median": pl.Float64},
    )
    val = pl.DataFrame(
        [{"stock_id": "3006", "val_pctile": 20.0}],
        schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64},
    )
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, peer, val,
        ma60_map={}, amount_map={"3006": 500.0},
        week="2026-W34", data_date=date(2026, 8, 22),
    )
    out = select_g5_candidates(snap)
    assert out["stock_id"].to_list() == ["3006"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_g5_candidates_empty_snapshot() -> None:
    out = select_g5_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


# --- F2'（成長優質股，§7.2/§20.10）---------------------------------------------

def _f2_fundamentals(gross: float, delta_op: float) -> pl.DataFrame:
    return _fundamentals(
        [{"stock_id": "2382", "quarter_label": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": 5.0, "delta_op_margin_pct": delta_op,
          "gross_margin_pct": gross, "roe_q_pct": None, "debt_ratio_pct": None,
          "current_ratio": None}]
    )


_F2_PEER = pl.DataFrame(
    [{"stock_id": "2382", "subind_median": 10.0}],
    schema={"stock_id": pl.Utf8, "subind_median": pl.Float64},
)
_EMPTY_VAL = pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})


def _f2_snap(pe: float | None, mktcap: float, gross: float = 12.0,
             delta_op: float = 0.0) -> pl.DataFrame:
    universe = _universe([{"stock_id": "2382", "name": "廣達",
                           "market_cap_billion": mktcap, "cum_rev_yoy_pct": None,
                           "pe_ratio": pe}])
    return build_g1_g2_g5_snapshot(
        universe, _f2_fundamentals(gross, delta_op), _F2_PEER, _EMPTY_VAL,
        ma60_map={}, amount_map={},
        week="2026-W35", data_date=date(2026, 8, 29),
    )


def test_f2_hit_requires_pe_band_peer_op_and_mktcap() -> None:
    snap = _f2_snap(pe=22.0, mktcap=500.0, gross=12.0, delta_op=0.0)
    row = snap.row(0, named=True)
    assert row["f2"] is True
    assert row["pe_ratio"] == 22.0


def test_f2_pe_band_edges_inclusive() -> None:
    assert _f2_snap(pe=15.0, mktcap=500.0).row(0, named=True)["f2"] is True
    assert _f2_snap(pe=30.0, mktcap=500.0).row(0, named=True)["f2"] is True
    assert _f2_snap(pe=14.9, mktcap=500.0).is_empty()   # 深度價值 → 不是 F2'
    assert _f2_snap(pe=30.1, mktcap=500.0).is_empty()   # 太貴 → 不是 F2'


def test_f2_requires_gross_margin_strictly_above_peer() -> None:
    # 等於同儕中位（10.0）→ 不命中（§7.2「優於」＝嚴格大於，跟 G5 的 >= 不同）
    assert _f2_snap(pe=22.0, mktcap=500.0, gross=10.0).is_empty()
    assert _f2_snap(pe=22.0, mktcap=500.0, gross=10.01).row(0, named=True)["f2"] is True


def test_f2_requires_op_margin_not_deteriorating() -> None:
    assert _f2_snap(pe=22.0, mktcap=500.0, delta_op=-0.1).is_empty()
    assert _f2_snap(pe=22.0, mktcap=500.0, delta_op=0.0).row(0, named=True)["f2"] is True


def test_f2_market_cap_floor_300() -> None:
    assert _f2_snap(pe=22.0, mktcap=299.0).is_empty()
    assert _f2_snap(pe=22.0, mktcap=300.0).row(0, named=True)["f2"] is True


def test_f2_null_inputs_do_not_hit() -> None:
    assert _f2_snap(pe=None, mktcap=500.0).is_empty()
    # peer median 為 null（同儕樣本不足）→ 不命中、不崩
    universe = _universe([{"stock_id": "2382", "name": "廣達",
                           "market_cap_billion": 500.0, "cum_rev_yoy_pct": None,
                           "pe_ratio": 22.0}])
    snap = build_g1_g2_g5_snapshot(
        universe, _f2_fundamentals(12.0, 0.0),
        pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64}),
        _EMPTY_VAL, ma60_map={}, amount_map={},
        week="2026-W35", data_date=date(2026, 8, 29),
    )
    assert snap.is_empty()


def test_select_f2prime_candidates_filters_to_f2_hits_only() -> None:
    universe = _universe([
        {"stock_id": "2382", "name": "廣達", "market_cap_billion": 500.0,
         "cum_rev_yoy_pct": None, "pe_ratio": 22.0},
        {"stock_id": "9999", "name": "小型深度價值", "market_cap_billion": 5.0,
         "cum_rev_yoy_pct": None, "pe_ratio": 8.0},
    ])
    fundamentals = _fundamentals([
        {"stock_id": "2382", "quarter_label": "2026Q2", "net_margin_pct": None,
         "delta_net_margin_pct": None, "op_margin_pct": 5.0, "delta_op_margin_pct": 0.5,
         "gross_margin_pct": 15.0, "roe_q_pct": None, "debt_ratio_pct": None,
         "current_ratio": None},
        {"stock_id": "9999", "quarter_label": "2026Q2", "net_margin_pct": None,
         "delta_net_margin_pct": None, "op_margin_pct": 5.0, "delta_op_margin_pct": 0.5,
         "gross_margin_pct": 15.0, "roe_q_pct": None, "debt_ratio_pct": None,
         "current_ratio": None},
    ])
    peer = pl.DataFrame(
        [{"stock_id": "2382", "subind_median": 10.0},
         {"stock_id": "9999", "subind_median": 10.0}],
        schema={"stock_id": pl.Utf8, "subind_median": pl.Float64},
    )
    snap = build_g1_g2_g5_snapshot(
        universe, fundamentals, peer, _EMPTY_VAL,
        ma60_map={}, amount_map={},
        week="2026-W35", data_date=date(2026, 8, 29),
    )
    out = select_f2prime_candidates(snap)
    assert out["stock_id"].to_list() == ["2382"]
    assert set(out.columns) == {"stock_id", "name"}


def test_select_f2prime_candidates_empty_snapshot() -> None:
    out = select_f2prime_candidates(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert out.is_empty()
    assert set(out.columns) == {"stock_id", "name"}


def test_upsert_ledger_all_null_column_does_not_corrupt_later_weeks(tmp_path: Path) -> None:
    """同l6_g4_watch的回歸測試場景：某週某數值欄全null不可污染後續週的真實值。"""
    path = tmp_path / "ledger.csv"
    week1 = pl.DataFrame(
        [{"week": "2026-W34", "data_date": date(2026, 8, 22), "stock_id": "2330",
          "name": "台積電", "market_cap_billion": 500.0, "cum_rev_yoy_pct": None,
          "ma60_dist_pct": None, "amount_million": None, "val_pctile": None,
          "fundamentals_quarter": "2026Q2", "net_margin_pct": None,
          "delta_net_margin_pct": None, "op_margin_pct": None, "delta_op_margin_pct": None,
          "gross_margin_pct": None, "gross_margin_peer_median": None,
          "roe_q_pct": 5.0, "debt_ratio_pct": 40.0, "current_ratio": 2.0,
          "g1": False, "g2": True, "g5": False}],
        schema=LEDGER_SCHEMA,
    )
    upsert_ledger(path, week1)

    week2 = pl.DataFrame(
        [{"week": "2026-W35", "data_date": date(2026, 8, 29), "stock_id": "1101",
          "name": "台泥", "market_cap_billion": 50.0, "cum_rev_yoy_pct": 3.0,
          "ma60_dist_pct": 5.0, "amount_million": 100.0, "val_pctile": 20.0,
          "fundamentals_quarter": "2026Q2", "net_margin_pct": 8.0,
          "delta_net_margin_pct": 2.0, "op_margin_pct": 10.0, "delta_op_margin_pct": 0.5,
          "gross_margin_pct": 20.0, "gross_margin_peer_median": 15.0,
          "roe_q_pct": None, "debt_ratio_pct": None, "current_ratio": None,
          "g1": True, "g2": False, "g5": False}],
        schema=LEDGER_SCHEMA,
    )
    ledger = upsert_ledger(path, week2)
    assert ledger.schema["cum_rev_yoy_pct"] == pl.Float64
    w2_val = ledger.filter(pl.col("stock_id") == "1101").row(0, named=True)["cum_rev_yoy_pct"]
    assert isinstance(w2_val, float)
    assert w2_val == 3.0


def test_ledger_progress_summary_counts() -> None:
    ledger = pl.DataFrame(
        [
            {"week": "2026-W34", "g1": True, "g2": False, "g5": False, "f2": False},
            {"week": "2026-W35", "g1": True, "g2": True, "g5": True, "f2": True},
        ],
        schema={"week": pl.Utf8, "g1": pl.Boolean, "g2": pl.Boolean, "g5": pl.Boolean,
                "f2": pl.Boolean},
    )
    summary = ledger_progress_summary(ledger)
    assert summary["n_weeks"] == 2
    assert summary["n_g1"] == 2
    assert summary["n_g2"] == 1
    assert summary["n_g5"] == 1
    assert summary["n_f2"] == 1


def test_ledger_progress_summary_empty() -> None:
    summary = ledger_progress_summary(pl.DataFrame(schema=LEDGER_SCHEMA))
    assert summary["n_weeks"] == 0
