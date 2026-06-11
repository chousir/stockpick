"""rotation_report 測試（R3）：四象限 / 校準訊號 / ΔRank / 渲染，全離線合成資料。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis.rotation import compute_fund_flows, compute_subindustry_baskets
from tw_screener.report.rotation_report import (
    Q_COOL,
    Q_DISTRIBUTE,
    Q_NEXT,
    Q_TREND,
    build_rotation_table,
    load_prev_rotation_snapshot,
    render_rotation_report,
)

D0 = date(2026, 1, 1)
N = 80


def _dates(n: int = N) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _membership() -> pl.DataFrame:
    """四個次產業 × 各 1 檔（min_members=1 測試用），對應四象限。"""
    return pl.DataFrame(
        {
            "sub_industry": ["甲流入未漲", "乙流入已漲", "丙流出已漲", "丁流出未漲"],
            "stock_id": ["1111", "2222", "3333", "4444"],
        }
    )


def _price_history() -> pl.DataFrame:
    """甲/丁 全程走平（未漲）；乙/丙 後段 +30%（已漲）。"""
    ds = _dates()
    rows = []
    for sid, flat in [("1111", True), ("2222", False), ("3333", False), ("4444", True)]:
        px = 100.0
        for i, d in enumerate(ds):
            if not flat and i >= N - 20:
                px *= 1.015  # 尾段拉升 → 距 60 日低 > 10%
            rows.append({"date": d, "stock_id": sid, "close": px, "volume": 1000})
    return pl.DataFrame(rows)


def _institutional() -> pl.DataFrame:
    """甲/乙 法人每日買超（流入）；丙/丁 每日賣超（流出）。"""
    rows = []
    for d in _dates():
        for sid, net in [("1111", 500), ("2222", 300), ("3333", -400), ("4444", -200)]:
            rows.append(
                {"date": d, "stock_id": sid, "foreign_net": net, "trust_net": net,
                 "dealer_net": 0, "total_net": net * 2}
            )
    return pl.DataFrame(rows)


@pytest.fixture()
def table() -> pl.DataFrame:
    members = _membership()
    market = _price_history()
    baskets = compute_subindustry_baskets(members, market)
    flows = compute_fund_flows(
        members, _institutional(),
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=5, long_window=20,
    )
    return build_rotation_table(
        flows, baskets, short_window=5, long_window=20,
        entry_signal={"signal": "trust_flow_20d", "mode": "z", "threshold": 1.0,
                      "require_momentum": True,
                      "confirm_signal": "flow_breadth_20d", "confirm_threshold": 0.6},
        position_window=60, position_low_pct=10.0, min_members=1,
    )


def test_quadrant_assignment(table: pl.DataFrame):
    got = {r["sub_industry"]: r["quadrant"] for r in table.iter_rows(named=True)}
    assert got["甲流入未漲"] == Q_NEXT
    assert got["乙流入已漲"] == Q_TREND
    assert got["丙流出已漲"] == Q_DISTRIBUTE
    assert got["丁流出未漲"] == Q_COOL


def test_table_ranked_by_flow(table: pl.DataFrame):
    # 甲日淨 1000 > 乙 600 > 丁 -400 > 丙 -800（20 日滾動同序）
    assert table.sort("radar_rank")["sub_industry"].to_list() == [
        "甲流入未漲", "乙流入已漲", "丁流出未漲", "丙流出已漲",
    ]
    assert table["rank_delta"].null_count() == table.height  # 無 prev → 全 null


def test_lots_conversion(table: pl.DataFrame):
    row = table.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    # total_net=1000 股/日 × 20 日 = 20000 股 = 20 張
    assert row["net_flow_20d_lots"] == 20.0


def test_confirm_flag_set(table: pl.DataFrame):
    # 甲/乙 breadth_20d = 1.0 > 0.6 → 確認鏡頭達標
    row = table.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    assert row["confirm_triggered"] is True
    # 常數流入 → z std=0 → 校準訊號不觸發（誠實）
    assert row["entry_triggered"] is False


def test_render_report_and_csv(table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "2026-W24",
        short_window=5, long_window=20,
        entry_signal={"signal": "trust_flow_20d", "mode": "z", "threshold": 1.0},
        position_low_pct=10.0, top_n=10, data_date="2026-03-21",
    )
    md = md_path.read_text(encoding="utf-8")
    assert "# 2026-W24 次產業資金流向輪動" in md
    assert "🟢 下一棒候選" in md and "甲流入未漲" in md
    assert "出貨警訊" in md and "丙流出已漲" in md
    assert "資料來源與時間" in md
    assert "首週正常" in md  # 無 prev 的誠實標註
    for banned in ("目標價", "強烈建議", "飆股", "保證"):
        assert banned not in md
    csv = pl.read_csv(md_path.parent / "sector_rotation.csv")
    assert {"sub_industry", "radar_rank", "quadrant"}.issubset(csv.columns)


def test_delta_rank_with_prev_snapshot(tmp_path: Path, table: pl.DataFrame):
    # 上週快照：甲排第 3 → 本週第 1 → ΔRank +2
    prev_dir = tmp_path / "2026-W23"
    prev_dir.mkdir(parents=True)
    pl.DataFrame(
        {"sub_industry": ["乙流入已漲", "丙流出已漲", "甲流入未漲"], "radar_rank": [1, 2, 3]}
    ).write_csv(prev_dir / "sector_rotation.csv")

    prev = load_prev_rotation_snapshot(tmp_path, "2026-W24")
    assert prev is not None

    members = _membership()
    market = _price_history()
    flows = compute_fund_flows(
        members, _institutional(), short_window=5, long_window=20,
    )
    t2 = build_rotation_table(
        flows, compute_subindustry_baskets(members, market),
        short_window=5, long_window=20, entry_signal={},
        min_members=1, prev=prev,
    )
    row = t2.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    assert row["rank_delta"] == 2


def test_prev_snapshot_ignores_future_weeks(tmp_path: Path):
    for wk in ("2026-W25", "2026-W30"):
        d = tmp_path / wk
        d.mkdir(parents=True)
        pl.DataFrame({"sub_industry": ["X"], "radar_rank": [1]}).write_csv(
            d / "sector_rotation.csv"
        )
    assert load_prev_rotation_snapshot(tmp_path, "2026-W24") is None


def test_render_no_leading_blank_lines(table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "w",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
    )
    assert md_path.read_text(encoding="utf-8").startswith("# 2026-W24")
