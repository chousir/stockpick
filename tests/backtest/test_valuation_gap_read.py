"""docs/31 §20.11：val_gap_pct_composite 效度初測 P0–P5 報告格式測試（合成）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.valuation_gap_read import _format_report, _p5_subindustry_ic

_DATES = [date(2026, 7, 3) + timedelta(days=7 * i) for i in range(6)]


def _panel(n_per_date: int = 60) -> pl.DataFrame:
    rows: list[dict] = []
    for di, d in enumerate(_DATES):
        for s in range(n_per_date):
            gap = (s - n_per_date / 2) * 1.0
            rows.append(
                {
                    "date": d,
                    "stock_id": f"{1000 + s}",
                    "sub_industry": "半導體" if s % 2 == 0 else "金融",
                    "val_gap_pct_composite": gap,
                    "val_gap_pct_peer": gap * 0.9,
                    "val_gap_pct_self": gap * 1.1,
                    "val_gap_pct_pb_peer": gap * 0.8,
                    "val_gap_pct_pb_self": gap,
                    "val_gap_pct_yield_peer": gap * 0.7,
                    "val_gap_pct_yield_self": gap * 1.2,
                    "val_composite_n_legs": 6,
                    "ma60_dist_pct": gap * 0.5,
                    "trail_r20": gap * 0.3,
                    "regime": "進攻",
                    # 便宜股（正 gap）→ 正 forward alpha，製造已知方向
                    "alpha10": gap * 0.2 + (di - 2),
                    "alpha20": gap * 0.15,
                    "alpha40": None,
                }
            )
    return pl.DataFrame(rows)


def test_report_has_no_verdict_caveats() -> None:
    md = _format_report(_panel(), horizons=(10, 20, 40), top_quantile=0.2)
    assert "非正式裁決" in md
    assert "P0" in md and "P1" in md and "P2" in md
    assert "P3" in md and "P4" in md and "P5" in md
    assert "同義反覆守門" in md
    # forward-return 全在 n_dates<10 → CI 應標樣本不足
    assert "n_dates<10" in md


def test_empty_panel_reports_blank() -> None:
    md = _format_report(pl.DataFrame(), horizons=(10, 20, 40), top_quantile=0.2)
    assert "面板為空" in md


def test_p5_subindustry_ic_direction() -> None:
    panel = _panel()
    got = _p5_subindustry_ic(panel, horizon=20)
    # alpha20 = gap*0.15 → composite 與 alpha20 完全單調 → 每個次產業 IC ≈ +1
    assert got.height == 2
    assert all(v > 0.9 for v in got["ic"].to_list())
