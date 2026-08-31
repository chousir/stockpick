"""docs/31 §20.11：val_gap_pct_composite 重建面板測試。

合成測試驗證公式複製與 ISO 週去重；另有一個條件式 anchor（`data/cache` +
`reports/2026-W35/candidates_enriched.csv` 存在時才跑）逐檔對表生產值——
不一致＝重建作廢（§23.4 script-anchor 紀律）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from tw_screener.backtest.redesign_dimension_grid import build_valuation_gap_cells
from tw_screener.backtest.valuation_gap_panel import (
    build_ledger_snapshot,
    compute_gap_legs_for_snapshot,
    iso_week_snapshot_dates,
    upsert_valuation_gap_ledger,
)

_VR_SCHEMA = {
    "date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
    "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64,
}


def _history(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=_VR_SCHEMA)


def test_iso_week_snapshot_dates_keeps_one_per_iso_week() -> None:
    # 2026-06-22/23/24 同 ISO 週（W26），只留最後一個（06-24）
    hist = _history(
        [
            {"date": d, "stock_id": "1101", "market": "上市",
             "pe": 10.0, "pbr": 1.0, "dividend_yield": 3.0}
            for d in (date(2026, 6, 22), date(2026, 6, 23), date(2026, 6, 24),
                      date(2026, 6, 30))
        ]
    )
    got = iso_week_snapshot_dates(hist)
    assert got == [date(2026, 6, 24), date(2026, 6, 30)]


def test_gap_legs_replicate_ratio_median_formula() -> None:
    # 5 檔同產業、PE 分別 10/20/30/40/50 → 中位數 30。stock A PE=10 →
    # 同儕 PE 腿 gap% = (30/10 - 1)*100 ≈ +200%（implied = close*(30/10)）。
    members = [f"S{i}" for i in range(5)]
    hist = _history(
        [
            {"date": date(2026, 7, 3), "stock_id": s, "market": "上市",
             "pe": pe, "pbr": 2.0, "dividend_yield": 4.0}
            for s, pe in zip(members, [10.0, 20.0, 30.0, 40.0, 50.0], strict=True)
        ]
    )
    membership = pl.DataFrame(
        {"sub_industry": ["ind"] * 5, "stock_id": members}
    )
    broad = pl.DataFrame({"sub_industry": ["產業別:x"] * 5, "stock_id": members})
    close_map = {s: 100.0 for s in members}
    # 自身歷史：給每檔 8 筆一樣的 PE（=當前值）→ 自身腿 gap ≈ 0
    self_hist = _history(
        [
            {"date": date(2026, 6, 1), "stock_id": s, "market": "上市",
             "pe": pe, "pbr": 2.0, "dividend_yield": 4.0}
            for s, pe in zip(members, [10.0, 20.0, 30.0, 40.0, 50.0], strict=True)
            for _ in range(8)
        ]
    )
    out = compute_gap_legs_for_snapshot(
        hist, close_map, membership, broad, self_hist, min_peers=5, min_snapshots=8
    )
    row = out.filter(pl.col("stock_id") == "S0").row(0, named=True)
    # implied = round(100 * (30/10), 2) = 300.0 → gap = (300/100 - 1)*100 = +200.0
    assert row["val_gap_pct_peer"] == pytest.approx(200.0, abs=0.1)
    # 自身 PE 中位數 = 10（8 筆都是 10）→ implied = 100*(10/10)=100 → gap 0
    assert row["val_gap_pct_self"] == pytest.approx(0.0, abs=0.1)
    # composite = 6 腿中位數；n_legs 應為 6（peer PE/PB/yield + self PE/PB/yield 都算得出）
    assert row["val_composite_n_legs"] == 6


def test_composite_is_median_not_mean() -> None:
    # 直接驗 compute_composite_valuation_gap 的行為已在 valuation 測試涵蓋；
    # 這裡確認面板層 n_legs 隨可用腿數變動：自身歷史不足 → 只剩同儕 3 腿
    members = [f"S{i}" for i in range(5)]
    hist = _history(
        [
            {"date": date(2026, 6, 12), "stock_id": s, "market": "上市",
             "pe": pe, "pbr": 2.0, "dividend_yield": 4.0}
            for s, pe in zip(members, [10.0, 20.0, 30.0, 40.0, 50.0], strict=True)
        ]
    )
    membership = pl.DataFrame({"sub_industry": ["ind"] * 5, "stock_id": members})
    broad = pl.DataFrame({"sub_industry": ["產業別:x"] * 5, "stock_id": members})
    out = compute_gap_legs_for_snapshot(
        hist, {s: 100.0 for s in members}, membership, broad, hist,
        min_peers=5, min_snapshots=8,
    )
    row = out.filter(pl.col("stock_id") == "S0").row(0, named=True)
    assert row["val_gap_pct_self"] is None  # 只有 1 筆歷史 < min_snapshots=8
    assert row["val_composite_n_legs"] == 3  # 同儕 PE/PB/殖利率


def test_build_valuation_gap_cells_two_tails() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2026, 7, 3)] * 10,
            "stock_id": [f"S{i}" for i in range(10)],
            "val_gap_pct_composite": [float(i) for i in range(-5, 5)],
            "alpha10": [0.0] * 10,
        }
    )
    cells = build_valuation_gap_cells(panel, top_quantile=0.2)
    # 10 檔 × 20% = 2 檔一尾。最高 gap（S9=+4, S8=+3）= cheap；最低（S0=-5, S1=-4）= rich
    cheap = set(cells.filter(pl.col("cell") == "cheap")["stock_id"].to_list())
    rich = set(cells.filter(pl.col("cell") == "rich")["stock_id"].to_list())
    assert cheap == {"S8", "S9"}
    assert rich == {"S0", "S1"}
    assert cells.height == 4  # 中間 6 檔不分派


def test_ledger_snapshot_and_upsert(tmp_path: Path) -> None:
    cand_rows = [
        {"stock_id": "1101", "name": "台泥", "close": 40.0, "ma60_dist_pct": 1.0,
         "val_metric": "PE", "val_gap_pct_peer": 5.0, "val_gap_pct_self": 3.0,
         "val_gap_pct_pb_peer": None, "val_gap_pct_pb_self": None,
         "val_gap_pct_yield_peer": None, "val_gap_pct_yield_self": None,
         "val_gap_pct_composite": 4.0, "val_composite_n_legs": 2},
        {"stock_id": "9999", "name": "無估值", "close": 10.0, "ma60_dist_pct": None,
         "val_metric": None, "val_gap_pct_composite": None, "val_composite_n_legs": 0},
    ]
    snap = build_ledger_snapshot(cand_rows, "2026-W35", date(2026, 8, 28))
    assert snap.height == 1  # 只留有 composite 的
    p = tmp_path / "ledger.csv"
    upsert_valuation_gap_ledger(p, snap)
    upsert_valuation_gap_ledger(p, snap)  # 重跑同週 → 去重
    back = pl.read_csv(p)
    assert back.height == 1
    assert back["val_gap_pct_composite"][0] == 4.0


# --- 條件式 anchor（真實快取存在時才跑）------------------------------------
_CE = Path("reports/2026-W35/candidates_enriched.csv")
_CACHE = Path("data/cache/twse")


@pytest.mark.skipif(
    not (_CE.exists() and any(_CACHE.glob("valuation_ratios_*.parquet"))),
    reason="需要 data/cache + reports/2026-W35（gitignored，本地驗證用）",
)
def test_w35_anchor_matches_production() -> None:
    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import (
        build_broad_industry_membership,
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.backtest.panel import build_price_panel
    from tw_screener.backtest.valuation_gap_panel import build_valuation_gap_panel
    from tw_screener.data.twse import create_client

    client = create_client(Path("config/settings.yaml"))
    val_history = client.load_valuation_ratios_history()
    ind = load_industry_mapping(_CACHE)
    panel = build_valuation_gap_panel(
        val_history,
        build_peer_membership(list_subindustries(), ind),
        build_broad_industry_membership(ind),
        build_price_panel(load_market_history(_CACHE, n_days=320), horizons=(10, 20, 40)),
        subindustry_map=list_subindustries(),
    )
    w35 = panel.filter(pl.col("date") == val_history["date"].max()).select(
        "stock_id", "val_gap_pct_composite"
    )
    ce = pl.read_csv(_CE, infer_schema_length=2000).select(
        pl.col("stock_id").cast(pl.Utf8),
        pl.col("val_gap_pct_composite").alias("prod"),
    ).drop_nulls("prod")
    j = ce.join(w35, on="stock_id", how="left")
    # 生產有值的每一檔，重建必須一致（容忍 0.05 的最末位 round 差）
    bad = j.filter(
        pl.col("val_gap_pct_composite").is_null()
        | ((pl.col("val_gap_pct_composite") - pl.col("prod")).abs() > 0.05)
    )
    assert bad.height == 0, f"{bad.height} 檔重建與生產不一致：{bad.head(10)}"
