"""M-Diag1「抓太晚」診斷計算測試（WS1）。

合成候選宇宙＋價格序列，驗算 build_candidate_screens／forward_returns_long／
extension_curve／signal_ic_table／rank_ic_table，及 Spearman/Fisher-z 純函式。不打網。
"""

from __future__ import annotations

import datetime as _dt
from datetime import date

import polars as pl

from tw_screener.backtest.diagnostic import (
    _fisher_ci,
    _spearman,
    build_candidate_screens,
    crossref_launches,
    detect_missed_launches,
    extension_curve,
    forward_returns_long,
    liquid_missed_table,
    missed_launch_summary,
    rank_ic_table,
    render_late_entry_report,
    render_missed_launch_report,
    signal_ic_table,
)


def _weekday_dates(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def _market(stock_returns: dict[str, float], start: date) -> pl.DataFrame:
    """每股 entry(idx1)=100、entry+5td(idx6)=100*(1+r)，其餘 100。8 個交易日。"""
    dates = _weekday_dates(start, 8)
    frames = []
    for sid, r in stock_returns.items():
        closes = [100.0] * 8
        closes[6] = 100.0 * (1 + r)
        frames.append(
            pl.DataFrame({
                "date": dates,
                "stock_id": [sid] * 8,
                "close": closes,
                "volume": [1000] * 8,
            })
        )
    return pl.concat(frames)


def _enriched(week: str, stocks: list[tuple[str, float, float, int]]) -> pl.DataFrame:
    """stocks: (stock_id, ma60_dist_pct, momentum_5d_pct, rank_in_group)。"""
    return pl.DataFrame({
        "stock_id": [s[0] for s in stocks],
        "name": [f"股{s[0]}" for s in stocks],
        "strategy": ["d_quality_leader"] * len(stocks),
        "rank_in_group": [s[3] for s in stocks],
        "ma60_dist_pct": [s[1] for s in stocks],
        "momentum_5d_pct": [s[2] for s in stocks],
        "ret_10d_pct": [None] * len(stocks),
        "foreign_net_5d_lots": [None] * len(stocks),
        "inst_pct20d": [10.0] * len(stocks),
        "vol_ratio": [1.0] * len(stocks),
    })


def test_spearman_and_fisher_ci() -> None:
    df = pl.DataFrame({"x": [1.0, 2, 3, 4, 5], "y": [2.0, 4, 6, 8, 10]})
    rho, n = _spearman(df, "x", "y")
    assert n == 5 and rho is not None and abs(rho - 1.0) < 1e-9
    # 反向完全單調 → -1
    df2 = pl.DataFrame({"x": [1.0, 2, 3], "y": [3.0, 2, 1]})
    rho2, _ = _spearman(df2, "x", "y")
    assert rho2 is not None and abs(rho2 + 1.0) < 1e-9
    # n<11 不給 CI；n 夠大給合理區間
    assert _fisher_ci(0.5, 5) == (None, None)
    lo, hi = _fisher_ci(0.5, 50)
    assert lo is not None and hi is not None and lo < 0.5 < hi


def test_ws1_pipeline_extension_and_ic() -> None:
    # 延伸度越高、前瞻報酬越低（追高）：ext 與 return 負相關
    stocks = [
        ("1001", -2.0, -1.0, 4),   # 貼低、前瞻好
        ("1002", 3.0, 1.0, 3),
        ("1003", 12.0, 5.0, 2),
        ("1004", 25.0, 9.0, 1),    # 延伸、前瞻差
    ]
    rets = {"1001": 0.06, "1002": 0.03, "1003": -0.02, "1004": -0.06}
    start = date(2026, 5, 1)  # Fri → entry Mon 5/4
    market = _market(rets, start)
    enr = {"2026-W21": _enriched("2026-W21", stocks)}
    week_to_date = {"2026-W21": date(2026, 5, 1)}

    screens, feats = build_candidate_screens(enr, week_to_date)
    assert screens.height == 4 and feats.height == 4
    assert set(screens.columns) >= {"week_tag", "screened_at", "stock_id", "strategy_id"}

    ret_long = forward_returns_long(screens, market, None, horizons_td=(5,))
    assert not ret_long.is_empty()
    assert set(ret_long["horizon_td"].unique().to_list()) == {5}

    joined = feats.join(
        ret_long.select("week_tag", "stock_id", "horizon_td",
                        "return_pct", "market_return_pct", "excess_return_pct"),
        on=["week_tag", "stock_id"], how="inner",
    )
    assert joined.height == 4

    curve = extension_curve(joined, target_col="return_pct")
    assert not curve.is_empty()
    # 低延伸桶中位 > 高延伸桶中位（單調遞減）
    lo_bucket = curve.filter(pl.col("bucket") == "<0%")["median"][0]
    hi_bucket = curve.filter(pl.col("bucket") == "20–30%")["median"][0]
    assert lo_bucket > hi_bucket

    ic = signal_ic_table(joined, feature_cols=("ma60_dist_pct",), target_col="return_pct")
    row = ic.filter(pl.col("feature") == "ma60_dist_pct").row(0, named=True)
    assert row["ic"] is not None and row["ic"] < 0  # 追高：負 IC

    rank = rank_ic_table(joined, target_col="return_pct", min_group=3)
    # rank_in_group 小=排前，前瞻好 → skill 為正
    assert not rank.is_empty()

    md = render_late_entry_report(curve, ic, rank, {5: 4}, "原始報酬", 15.0, 20)
    assert "抓太晚" in md and "距季線" in md


def test_empty_inputs_safe() -> None:
    empty = pl.DataFrame()
    s, f = build_candidate_screens({}, {})
    assert s.is_empty() and f.is_empty()
    assert forward_returns_long(empty, empty, None, horizons_td=(5,)).is_empty()
    assert extension_curve(empty).is_empty()
    assert signal_ic_table(empty).is_empty()
    assert rank_ic_table(empty).is_empty()
    assert detect_missed_launches(empty, {}, [], forward_td=5).is_empty()
    assert crossref_launches(empty, empty, {}).is_empty()
    assert missed_launch_summary(empty).is_empty()


def _mkt_stock(sid: str, dates: list[date], closes: list[float], vol: int) -> pl.DataFrame:
    return pl.DataFrame({
        "date": dates, "stock_id": [sid] * len(closes),
        "close": closes, "volume": [vol] * len(closes),
    })


def test_detect_missed_launches_filters_extended_and_flat() -> None:
    dates = _weekday_dates(date(2025, 12, 1), 80)
    d_idx = 65  # data_date；MA60 用 bars 6..65、pullback 用 bars 55..65、forward 用 66..
    week_to_date = {"2026-W21": dates[d_idx]}
    # 回檔起漲：base 100、末段回檔到 97、entry 後 5td 衝 +29%
    launcher = [100.0] * 60 + [99, 98, 98, 97, 97, 97.0] + [97, 100, 105, 112, 120, 125] + [
        125.0
    ] * 8
    # 延伸股：高位（距 MA60 很遠），之後也漲 → 應被 ma_dist 上限剔除
    extended = [100.0] * 63 + [138, 139, 140.0] + [140, 145, 150, 158, 168, 178] + [178.0] * 8
    # 回檔但沒起漲：末段回檔，forward 平 → 不達 launch 門檻
    flat = [100.0] * 60 + [99, 98, 98, 97, 97, 97.0] + [97, 98, 97, 98, 99, 98.0] + [98.0] * 8
    market = pl.concat([
        _mkt_stock("AAA", dates, launcher, 2_000_000),
        _mkt_stock("EXT", dates, extended, 2_000_000),
        _mkt_stock("FLAT", dates, flat, 2_000_000),
    ])
    got = detect_missed_launches(
        market, week_to_date, ["2026-W21"],
        forward_td=5, launch_pct=20.0, pullback_max_pct=0.0, ma_dist_ceiling_pct=10.0,
    )
    ids = got["stock_id"].to_list()
    assert "AAA" in ids  # 回檔起漲被抓到
    assert "EXT" not in ids  # 延伸股被剔
    assert "FLAT" not in ids  # 沒起漲被剔
    assert got.filter(pl.col("stock_id") == "AAA")["amount_m"][0] > 100  # 成交額(百萬)


def test_crossref_three_way_and_liquidity() -> None:
    launched = pl.DataFrame({
        "week_tag": ["2026-W21"] * 3,
        "stock_id": ["AAA", "BBB", "CCC"],
        "screened_at": [date(2026, 5, 22)] * 3,
        "ret_pullback_pct": [-3.0, -5.0, -4.0],
        "ma_dist_pct": [-2.0, -1.0, 0.0],
        "amount_m": [500.0, 5.0, 300.0],  # BBB 微型
        "launch_return_pct": [30.0, 40.0, 25.0],
    })
    picks = pl.DataFrame({"week": ["2026-W21"], "stock_id": ["AAA"]})
    enriched = {"2026-W21": pl.DataFrame({
        "stock_id": ["CCC"], "name": ["測CCC"], "foreign_net_5d_lots": [-100.0],
        "flags": ["土洋對作"],
    })}
    excluded = pl.DataFrame({
        "week": ["2026-W21"], "stock_id": ["CCC"], "reason": ["土洋對作"],
    })
    xr = crossref_launches(
        launched, picks, enriched, excluded, name_map={"BBB": "測BBB"},
        watchlist_ids={"BBB"}, held_ids=set(),
    )
    st = dict(zip(xr["stock_id"].to_list(), xr["status"].to_list()))
    # BBB 在觀察清單 → watchlisted（優先於 never_surfaced）
    assert st == {"AAA": "acted", "BBB": "watchlisted", "CCC": "considered"}
    ccc = xr.filter(pl.col("stock_id") == "CCC").row(0, named=True)
    assert ccc["exclude_reason"] == "土洋對作" and ccc["name"] == "測CCC"
    assert xr.filter(pl.col("stock_id") == "BBB")["name"][0] == "測BBB"

    summ = missed_launch_summary(xr, min_amount_m=100.0)
    row = summ.row(0, named=True)
    assert row["acted"] == 1 and row["considered"] == 1 and row["watchlisted"] == 1

    liq = liquid_missed_table(xr, min_amount_m=100.0)
    # CCC(considered) + BBB(watchlisted) 都算可行動漏抓；AAA(acted) 不算
    assert set(liq["stock_id"].to_list()) == {"CCC", "BBB"}

    # held 優先：若 AAA 改標持股 → held（非 acted）
    xr2 = crossref_launches(launched, picks, enriched, excluded, held_ids={"AAA"})
    assert xr2.filter(pl.col("stock_id") == "AAA")["status"][0] == "held"

    md = render_missed_launch_report(
        pl.DataFrame([{
            "config": "≥20%/10d", "events": 3, "held": 0, "acted": 1,
            "considered": 1, "watchlisted": 1, "never_liquid": 0, "never_illiquid": 0,
        }]),
        liq, "≥20%/10d", 100.0, "2025-05-29 ~ 2026-06-09",
    )
    assert "漏掉起漲股" in md and "測CCC" in md
