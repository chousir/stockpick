"""M2 投降洗盤偵測測試（委託書 M2）。

合成融資序列／廣度／資金子分／指數，驗四個子項的命中與**誠實邊界**（薄樣本、缺日、
薄覆蓋一律回 insufficient_data／missing，不假裝算得出來），以及 flag 落檔同日冪等。
不打網、不碰真快取。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.analysis.washout import (
    HIT_BREADTH,
    HIT_FLOW,
    HIT_INDEX,
    HIT_MARGIN,
    SubSignal,
    append_washout_history,
    breadth_washout,
    dense_days,
    detect_market_washout,
    flow_extreme_streak,
    index_deep_deviation,
    margin_capitulation,
    market_margin_series,
    render_washout_block,
)


def _bdays(n: int, end: date = date(2026, 8, 7)) -> list[date]:
    """回 n 個「工作日」（跳過週末），升冪。"""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


# ── 子項 1：融資投降 ──────────────────────────────────────────────────────


def test_market_margin_series_uses_source_daily_change_not_diff():
    """全市場序列要帶 TWSE 自己給的 margin_chg 加總（gap-proof），不是自己 diff 餘額。"""
    df = pl.DataFrame(
        {
            "date": [date(2026, 8, 6), date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 7)],
            "stock_id": ["1101", "1102", "1101", "1102"],
            "margin_balance": [100, 200, 90, 180],
            "margin_chg": [-5, -10, -10, -20],
        }
    )
    s = market_margin_series(df)
    assert s["total_margin_lots"].to_list() == [300, 270]
    # 快取若缺日，自行 diff 餘額會得到跨日變化；來源欄不受影響
    assert s["total_margin_chg_lots"].to_list() == [-15, -30]


def test_market_margin_series_empty_or_missing_cols():
    assert market_margin_series(pl.DataFrame()).is_empty()
    assert market_margin_series(pl.DataFrame({"date": [date(2026, 8, 7)]})).is_empty()


def test_margin_capitulation_fires_on_extreme_drop():
    """平穩序列尾端插一個大減 → z 深負、命中。"""
    days = _bdays(120)
    chg = [0] * 119 + [-500_000]
    s = pl.DataFrame(
        {
            "date": days,
            "total_margin_lots": [9_000_000] * 120,
            "total_margin_chg_lots": chg,
        }
    )
    sig = margin_capitulation(s, z_threshold=-2.0, min_samples=60)
    assert sig.status == "ok"
    assert sig.hit is True
    assert sig.value is not None and sig.value < -2.0


def test_margin_capitulation_thin_sample_is_insufficient_not_false():
    """樣本不足時回 insufficient_data——不得用薄樣本算 z 後宣告「未觸發」。"""
    days = _bdays(10)
    s = pl.DataFrame(
        {
            "date": days,
            "total_margin_lots": [9_000_000] * 10,
            "total_margin_chg_lots": [-1000] * 9 + [-900_000],
        }
    )
    sig = margin_capitulation(s, min_samples=60)
    assert sig.status == "insufficient_data"
    assert sig.hit is False
    assert sig.value is None


def test_margin_capitulation_drops_5d_window_when_cache_has_gaps():
    """尾端 5 列橫跨太多日曆天＝快取缺日 → 5 日窗誠實棄用，只留單日窗。"""
    days = _bdays(100)[:-5] + [
        date(2026, 7, 1), date(2026, 7, 8), date(2026, 7, 15),
        date(2026, 7, 22), date(2026, 8, 7),
    ]
    s = pl.DataFrame(
        {
            "date": sorted(days),
            "total_margin_lots": [9_000_000] * 100,
            "total_margin_chg_lots": [0] * 99 + [-500_000],
        }
    )
    sig = margin_capitulation(s, min_samples=60, window_5d_max_calendar_days=9)
    assert "5日窗棄用" in sig.detail
    assert "單日 z=" in sig.detail


# ── 子項 2：廣度 washout ──────────────────────────────────────────────────


def test_breadth_washout_hit_and_missing():
    assert breadth_washout(0.15, max_frac=0.20).hit is True
    assert breadth_washout(0.35, max_frac=0.20).hit is False
    miss = breadth_washout(None, max_frac=0.20)
    assert miss.status == "missing" and miss.hit is False and miss.value is None


# ── 子項 3：資金分項極端持續 ──────────────────────────────────────────────


def test_flow_extreme_streak_requires_unbroken_tail():
    days = _bdays(20)
    # 尾端 15 日全 < −0.9 → 連 3 週成立
    ok = pl.DataFrame({"date": days, "flow_score": [-0.5] * 5 + [-0.95] * 15})
    assert flow_extreme_streak(ok, max_score=-0.9, min_weeks=3).hit is True
    # 尾段中間有一天回到門檻上 → 中斷、不成立
    broken = pl.DataFrame(
        {"date": days, "flow_score": [-0.95] * 12 + [-0.5] + [-0.95] * 7}
    )
    assert flow_extreme_streak(broken, max_score=-0.9, min_weeks=3).hit is False


def test_flow_extreme_streak_missing_and_short_series():
    miss = flow_extreme_streak(pl.DataFrame(), max_score=-0.9, min_weeks=3)
    assert miss.status == "missing" and miss.hit is False
    short = pl.DataFrame({"date": _bdays(8), "flow_score": [-0.99] * 8})
    sig = flow_extreme_streak(short, max_score=-0.9, min_weeks=3)
    assert sig.status == "insufficient_data" and sig.hit is False


# ── 子項 4：指數深負乖離 ＋ 薄覆蓋防護 ────────────────────────────────────


def test_dense_days_drops_thin_coverage_dates():
    """實測型：正常日 300 檔、異常日只有 3 檔——薄日必須整天剔除。"""
    rows = []
    for d, n in ((date(2026, 8, 5), 300), (date(2026, 8, 6), 3), (date(2026, 8, 7), 300)):
        rows += [{"date": d, "stock_id": f"{i:04d}", "close": 100.0} for i in range(n)]
    px = pl.DataFrame(rows)
    kept = dense_days(px, min_priced=200)
    assert set(kept["date"].unique().to_list()) == {date(2026, 8, 5), date(2026, 8, 7)}


def test_index_deep_deviation_hit_and_short_history():
    days = _bdays(80)
    vals = [100.0] * 60 + [88.0] * 20  # 尾段跌到 MA60 之下夠深
    sig = index_deep_deviation(
        pl.DataFrame({"date": days, "market_index": vals}), ma_window=60, max_dist_pct=-7.0
    )
    assert sig.status == "ok" and sig.hit is True
    short = pl.DataFrame({"date": _bdays(10), "market_index": [100.0] * 10})
    assert index_deep_deviation(short, ma_window=60).status == "insufficient_data"


# ── 組合、分母誠實、落檔冪等 ──────────────────────────────────────────────


def _subs(margin=False, breadth=False, flow=False, index=False, flow_status="ok"):
    return [
        SubSignal(HIT_MARGIN, margin, -3.0 if margin else -1.0, -2.0, "ok"),
        SubSignal(HIT_BREADTH, breadth, 0.1 if breadth else 0.4, 0.2, "ok"),
        SubSignal(HIT_FLOW, flow, -0.95 if flow else -0.3, -0.9, flow_status),
        SubSignal(HIT_INDEX, index, -9.0 if index else 1.0, -7.0, "ok"),
    ]


def test_detect_market_washout_needs_two_hits():
    assert detect_market_washout(_subs(margin=True), min_hits=2).triggered is False
    r = detect_market_washout(_subs(margin=True, breadth=True), min_hits=2)
    assert r.triggered is True
    assert r.n_hit == 2 and set(r.hits) == {HIT_MARGIN, HIT_BREADTH}
    assert "深跌後段・反轉警戒" in r.posture_note


def test_detect_market_washout_denominator_counts_only_evaluable():
    """分母只算 status=ok 的子項（沿 docs/26「已求值 N 項中觸發 M 項」口徑）。"""
    r = detect_market_washout(_subs(margin=True, flow_status="missing"), min_hits=2)
    assert r.n_evaluable == 3
    assert r.n_hit == 1


def test_render_block_always_marks_uncalibrated():
    body = "\n".join(render_washout_block(detect_market_washout(_subs(), min_hits=2)))
    assert "未校準" in body
    assert "已求值" in body


def test_append_washout_history_is_idempotent_per_day(tmp_path):
    """同日重跑不得埋進重複列（沿 macro_regime.append_history 同一冪等模式）。"""
    path = tmp_path / "washout_history.parquet"
    r = detect_market_washout(_subs(margin=True, breadth=True), min_hits=2)
    append_washout_history(path, r, date(2026, 8, 7))
    append_washout_history(path, r, date(2026, 8, 7))
    df = pl.read_parquet(path)
    assert df.height == 1
    assert df["triggered"].item() is True
    assert df["hits"].item() == f"{HIT_MARGIN}|{HIT_BREADTH}"

    append_washout_history(path, r, date(2026, 8, 10))
    assert pl.read_parquet(path).height == 2
