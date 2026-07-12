"""WS-C rotation_efficacy 單元測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.rotation_efficacy import (
    Q_DISTRIBUTE,
    Q_NEXT,
    Q_TREND,
    basket_forward_returns,
    basket_position_series,
    category_lift_table,
    compare_with_production,
    rotation_signal_panel,
    weekly_snapshot_dates,
)

_D0 = date(2026, 1, 5)  # 週一


def _days(n: int) -> list[date]:
    """連續平日（跳過週末）＝合成交易日曆。"""
    out, d = [], _D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_weekly_snapshot_dates_takes_last_trading_day() -> None:
    ds = _days(12)  # 兩週半
    weekly = weekly_snapshot_dates(ds)
    assert all(d.weekday() == 4 for d in weekly[:-1])  # 完整週取到週五
    assert weekly == sorted(weekly)


def test_basket_position_series_rolling_low() -> None:
    ds = _days(4)
    baskets = pl.DataFrame(
        {
            "sub_industry": ["半導體"] * 4,
            "date": ds,
            "basket_index": [100.0, 90.0, 99.0, 108.0],
        }
    )
    pos = basket_position_series(baskets, position_window=3)
    vals = pos.sort("date")["above_low_pct"].to_list()
    assert vals[0] == 0.0  # 首日即窗低
    assert abs(vals[1]) < 1e-9  # 新低
    assert abs(vals[2] - 10.0) < 1e-9  # 99/90−1
    assert abs(vals[3] - 20.0) < 1e-9  # 108/90−1（窗含 90）


def test_rotation_signal_panel_quadrant_and_turn() -> None:
    ds = [date(2026, 1, 9)]  # 單一快照日
    flows_z = pl.DataFrame(
        {
            "sub_industry": ["A", "B", "C", "D"],
            "date": ds * 4,
            "members": [6, 6, 6, 2],
            "net_flow_20d": [100, 100, -100, 100],
            "net_flow_5d": [-50, 50, 50, 50],
            "flow_momentum": [1.0, -1.0, 0.0, 1.0],
            "trust_flow_20d_z": [1.5, 0.5, None, 2.0],
        }
    )
    position = pl.DataFrame(
        {
            "sub_industry": ["A", "B", "C", "D"],
            "date": ds * 4,
            "above_low_pct": [15.0, 5.0, 20.0, 3.0],
        }
    )
    trend = pl.DataFrame(
        {
            "sub_industry": ["A", "B", "C", "D"],
            "date": ds * 4,
            "trend_score": [80.0, 60.0, 40.0, 90.0],
        }
    )
    out = rotation_signal_panel(
        flows_z, position, trend, ds, min_members=5
    ).sort("sub_industry")
    rows = {r["sub_industry"]: r for r in out.iter_rows(named=True)}
    # A：流入×已漲＝主升；20d>0 且 5d<0 ＝退潮；momentum>0＝加速；z>1＝★
    assert rows["A"]["quadrant"] == Q_TREND
    assert rows["A"]["flow_turn"] == "退潮"
    assert rows["A"]["freshness"] == "加速"
    assert rows["A"]["entry_triggered"] is True
    # B：流入×未漲＝下一棒；同號無 turn；momentum<0＝放緩
    assert rows["B"]["quadrant"] == Q_NEXT
    assert rows["B"]["flow_turn"] is None
    assert rows["B"]["freshness"] == "放緩"
    # C：流出×已漲＝出貨警訊；20d<0 且 5d>0＝資金回流；momentum=0 → freshness null
    assert rows["C"]["quadrant"] == Q_DISTRIBUTE
    assert rows["C"]["flow_turn"] == "資金回流"
    assert rows["C"]["freshness"] is None
    # D：members=2 → 榜外（radar_rank null）；quadrant 照算
    assert rows["D"]["on_board"] is False
    assert rows["D"]["radar_rank"] is None
    # 上榜者按 trend_score 排：A(80)=1、B(60)=2、C(40)=3
    assert rows["A"]["radar_rank"] == 1 and rows["C"]["radar_rank"] == 3


def test_basket_forward_returns_min_members_null() -> None:
    ds = [date(2026, 1, 9)]
    panel = pl.DataFrame(
        {
            "date": ds * 4,
            "stock_id": ["1111", "2222", "3333", "4444"],
            "r5": [10.0, 20.0, None, 5.0],
            "alpha5": [1.0, 2.0, None, -1.0],
        }
    )
    membership = pl.DataFrame(
        {
            "sub_industry": ["甲", "甲", "甲", "乙"],
            "stock_id": ["1111", "2222", "3333", "4444"],
        }
    )
    fwd = basket_forward_returns(panel, membership, horizons=(5,), min_members=2)
    rows = {r["sub_industry"]: r for r in fwd.iter_rows(named=True)}
    assert abs(rows["甲"]["basket_r5"] - 15.0) < 1e-9  # null 成員不計
    assert rows["甲"]["n_ret5"] == 2
    assert rows["乙"]["basket_r5"] is None  # 1 檔 < min_members


def test_category_lift_table_baseline_and_null_class() -> None:
    df = pl.DataFrame(
        {
            "flow_turn": ["退潮"] * 12 + [None] * 12,
            "y": [-2.0] * 12 + [3.0] * 12,
        }
    )
    tbl = category_lift_table(df, "flow_turn", "y", n_boot=200, include_null_as="同號")
    cats = tbl["category"].to_list()
    assert cats[0] == "（全體）" and set(cats[1:]) == {"退潮", "同號"}
    rows = {r["category"]: r for r in tbl.iter_rows(named=True)}
    assert rows["退潮"]["mean"] == -2.0 and rows["退潮"]["ci_lo"] is not None
    assert rows["（全體）"]["n"] == 24


def test_compare_with_production_match_rates() -> None:
    d = date(2026, 7, 9)
    recon = pl.DataFrame(
        {
            "sub_industry": ["A", "B"],
            "date": [d, d],
            "trend_score": [80.0, 60.0],
            "quadrant": [Q_TREND, Q_NEXT],
            "freshness": ["加速", None],
            "flow_turn": [None, "退潮"],
        }
    )
    prod = pl.DataFrame(
        {
            "sub_industry": ["A", "B"],
            "date": [d, d],
            "trend_score": [82.0, 60.0],
            "quadrant": [Q_TREND, Q_TREND],  # B 不合
            "freshness": ["加速", None],
            "flow_turn": [None, "退潮"],
        }
    )
    cmp_df = compare_with_production(recon, prod)
    r = cmp_df.row(0, named=True)
    assert r["n"] == 2
    assert abs(r["trend_mad"] - 1.0) < 1e-9
    assert abs(r["quadrant_match"] - 0.5) < 1e-9
    assert r["freshness_match"] == 1.0  # 雙 null＝一致


# ── WS-H.4a：週訊號 regime 切片（runner 第 5 段的純函式路徑）─────────────────


def test_quadrant_lift_regime_slices_weekly() -> None:
    """主升續勢子集 join regime 後三切片：列數/同向數正確（鏡射 runner 5.2 流程）。"""
    import random

    from tw_screener.backtest.factor_lab import (
        regime_alignment_verdict,
        regime_mean_slices,
    )

    rng = random.Random(31)
    # 36 個週訊號日：進攻/中性/防禦各 12 週，每週 3 個族群、全部主升續勢
    rows = []
    d = _D0
    for w in range(36):
        while d.weekday() != 4:
            d += timedelta(days=1)
        regime = ("進攻", "中性", "防禦")[w // 12]
        sign = 1.0 if regime in ("進攻", "中性") else -1.0
        for g in range(3):
            rows.append(
                {
                    "date": d, "sub_industry": f"g{g}", "quadrant": Q_TREND,
                    "regime": regime,
                    "basket_alpha20": sign * 3.0 + rng.gauss(0, 0.3),
                }
            )
        d += timedelta(days=7)
    sig = pl.DataFrame(rows)

    sub = sig.filter(pl.col("quadrant") == Q_TREND)
    slices = regime_mean_slices(sub, target="basket_alpha20", block_len=5, min_n_dates=10)
    assert slices.height == 3
    r = {x["regime"]: x for x in slices.iter_rows(named=True)}
    assert r["進攻"]["n_dates"] == 12 and r["進攻"]["n"] == 36
    assert r["進攻"]["mean"] > 2.0 and r["中性"]["mean"] > 2.0 and r["防禦"]["mean"] < -2.0
    assert all(not x["thin"] for x in slices.iter_rows(named=True))
    # 全樣本 per-週 mean 為正（2/3 段為 +3）→ 進攻/中性同向 → 跨 regime 穩健
    verdict, same, present = regime_alignment_verdict(slices, 1, value_col="mean")
    assert verdict == "跨 regime 穩健" and sorted(same) == ["中性", "進攻"] and present == 3


def test_regime_slices_all_null_label_skips() -> None:
    """regime 欄全 null（標籤未產）→ 切片空表＝runner 誠實跳過路徑，不炸。"""
    from tw_screener.backtest.factor_lab import regime_mean_slices

    sig = pl.DataFrame(
        {
            "date": [date(2026, 1, 9)] * 3,
            "sub_industry": ["a", "b", "c"],
            "regime": pl.Series([None] * 3, dtype=pl.Utf8),
            "basket_alpha20": [1.0, 2.0, 3.0],
        }
    )
    assert regime_mean_slices(sig, target="basket_alpha20", block_len=5).is_empty()
