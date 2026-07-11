"""WS-K margin_factors 因子 build＋裁決邏輯單元測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.margin_factors import (
    FACTOR_SPECS,
    build_margin_factors,
    classify_verdict,
)

_D0 = date(2026, 1, 5)


def _dates(n: int) -> list[date]:
    return [_D0 + timedelta(days=i) for i in range(n)]


def _base_panel(
    n_days: int,
    margin: list[float | None],
    volume: list[float | None] | None = None,
    tdcc_rows: dict[int, float] | None = None,
    stock_id: str = "1111",
) -> pl.DataFrame:
    """組最小面板（date/stock_id/margin_balance_lots/big_holder_pct/volume）。

    tdcc_rows：{day_offset: big_holder_pct} —— 只在指定 offset 有值，其餘 null
    （鏡射面板「僅 TDCC 資料日有值」的真實語義）。
    """
    days = _dates(n_days)
    vol = volume if volume is not None else [1_000_000.0] * n_days
    tdcc_rows = tdcc_rows or {}
    big_holder = [tdcc_rows.get(i) for i in range(n_days)]
    return pl.DataFrame(
        {
            "date": days,
            "stock_id": [stock_id] * n_days,
            "margin_balance_lots": margin,
            "big_holder_pct": big_holder,
            "volume": vol,
        }
    )


# ── (1) margin_slim：shift 無前視 ──────────────────────────────────────────


def test_margin_slim_value_and_sign() -> None:
    """遞增餘額序列：margin_slim = −1×(current−prev20)/prev20，餘額成長 → 本欄為負。"""
    n = 30
    margin = [1000.0 + 10.0 * i for i in range(n)]  # 遞增（融資餘額擴張，非減肥）
    panel = _base_panel(n, margin)
    out = build_margin_factors(panel).sort("date")
    row = out.row(25, named=True)  # prev=第 5 列=1050、current=第25列=1250
    prev, cur = 1000.0 + 10.0 * 5, 1000.0 + 10.0 * 25
    expected = -1.0 * (cur - prev) / prev
    assert abs(row["margin_slim"] - expected) < 1e-9
    assert row["margin_slim"] < 0  # 餘額擴張（非減肥）→ 負值，驗證方向定義


def test_margin_slim_no_lookahead() -> None:
    """row i 的 margin_slim 只吃 row i 與 row i−20，改動未來列不影響過去值（無前視）。"""
    n = 30
    margin_a = [1000.0 + 10.0 * i for i in range(n)]
    margin_b = list(margin_a)
    margin_b[26:] = [99999.0] * (n - 26)  # 只改第 25 列之後的未來資料
    out_a = build_margin_factors(_base_panel(n, margin_a)).sort("date")
    out_b = build_margin_factors(_base_panel(n, margin_b)).sort("date")
    assert out_a.row(25, named=True)["margin_slim"] == out_b.row(25, named=True)["margin_slim"]


def test_margin_slim_null_before_warmup() -> None:
    """前 20 列（20 交易日前資料不存在）→ null，不臆造。"""
    n = 25
    margin = [1000.0 + 10.0 * i for i in range(n)]
    out = build_margin_factors(_base_panel(n, margin)).sort("date")
    assert out.row(19, named=True)["margin_slim"] is None  # shift(20) 剛好取不到（index<20）
    assert out.row(20, named=True)["margin_slim"] is not None  # index20 起可取 index0


# ── (2) big_holder_wow：只在 TDCC 日有值＋跳週 null ────────────────────────


def test_big_holder_wow_consecutive_weeks() -> None:
    """相鄰 TDCC 週（間隔 7 曆日）→ WoW＝兩週差；非 TDCC 日全 null。"""
    n = 30
    tdcc_rows = {0: 40.0, 7: 41.0, 14: 42.5, 21: 42.0}
    panel = _base_panel(n, [None] * n, tdcc_rows=tdcc_rows)
    out = build_margin_factors(panel).sort("date")
    assert out.row(0, named=True)["big_holder_wow"] is None  # 第一個 TDCC 日無前一週可比
    assert abs(out.row(7, named=True)["big_holder_wow"] - (41.0 - 40.0)) < 1e-9
    assert abs(out.row(14, named=True)["big_holder_wow"] - (42.5 - 41.0)) < 1e-9
    assert abs(out.row(21, named=True)["big_holder_wow"] - (42.0 - 42.5)) < 1e-9
    # 非 TDCC 日（big_holder_pct 本身 null）→ big_holder_wow 亦 null
    assert out.row(3, named=True)["big_holder_wow"] is None


def test_big_holder_wow_skip_week_null() -> None:
    """與前一個 TDCC 資料日間隔 >14 曆日（跳週／資料缺口）→ null，不硬接。"""
    n = 50
    tdcc_rows = {0: 40.0, 7: 41.0, 14: 42.5, 21: 42.0, 42: 45.0}  # 21→42 間隔 21 天
    panel = _base_panel(n, [None] * n, tdcc_rows=tdcc_rows)
    out = build_margin_factors(panel).sort("date")
    assert out.row(42, named=True)["big_holder_wow"] is None


# ── (3) margin_to_vol：張→股單位換算 ───────────────────────────────────────


def test_margin_to_vol_unit_conversion() -> None:
    """margin_balance_lots（張）×1000 對齊 panel volume（股）：500 張、20 日均量 10 萬股 → 5.0。"""
    n = 30
    margin = [500.0] * n
    volume = [100_000.0] * n
    panel = _base_panel(n, margin, volume=volume)
    out = build_margin_factors(panel).sort("date")
    row = out.row(25, named=True)  # 窗已滿（>=20 列）
    assert abs(row["margin_to_vol"] - (500.0 * 1000 / 100_000.0)) < 1e-9


# ── (4) null 不填 0 ─────────────────────────────────────────────────────────


def test_null_not_filled_zero() -> None:
    """margin_balance_lots 該列 null → margin_slim／margin_to_vol 皆 null（非 0）。"""
    n = 30
    margin = [1000.0 + 10.0 * i for i in range(n)]
    margin[25] = None  # 當期餘額缺
    panel = _base_panel(n, margin, volume=[100_000.0] * n)
    out = build_margin_factors(panel).sort("date")
    row = out.row(25, named=True)
    assert row["margin_slim"] is None
    assert row["margin_to_vol"] is None

    margin2 = [1000.0 + 10.0 * i for i in range(n)]
    margin2[5] = None  # 20 日前（shift 來源）缺
    panel2 = _base_panel(n, margin2, volume=[100_000.0] * n)
    out2 = build_margin_factors(panel2).sort("date")
    assert out2.row(25, named=True)["margin_slim"] is None


def test_factor_columns_created() -> None:
    n = 25
    out = build_margin_factors(_base_panel(n, [1000.0] * n))
    for c in FACTOR_SPECS:
        assert c in out.columns


# ── (5) classify_verdict：三選一＋樣本不足 ─────────────────────────────────


def test_verdict_direction_confirmed() -> None:
    v = classify_verdict(
        predicted_sign=1,
        mean_ic=0.05,
        bs_ci=(0.02, 0.08),
        split_ics=[0.04, 0.05, 0.06, 0.03, 0.05],
        n_dates=40,
        t=50,
    )
    assert v == "方向成立"


def test_verdict_reverse_significant() -> None:
    v = classify_verdict(
        predicted_sign=1,
        mean_ic=-0.05,
        bs_ci=(-0.08, -0.02),
        split_ics=[-0.04, -0.05, -0.06, -0.03, -0.05],
        n_dates=40,
        t=50,
    )
    assert v == "反向顯著"


def test_verdict_no_evidence_small_effect() -> None:
    v = classify_verdict(
        predicted_sign=1,
        mean_ic=0.01,  # 低於 0.03 效應量底線
        bs_ci=(0.005, 0.015),
        split_ics=[0.01, 0.01, 0.01, 0.01, 0.01],
        n_dates=40,
        t=50,
    )
    assert v == "無證據"


def test_verdict_no_evidence_ci_crosses_zero() -> None:
    v = classify_verdict(
        predicted_sign=1,
        mean_ic=0.05,
        bs_ci=(-0.01, 0.09),  # 跨零
        split_ics=[0.04, 0.05, 0.06, 0.03, 0.05],
        n_dates=40,
        t=50,
    )
    assert v == "無證據"


def test_verdict_no_evidence_wf_inconsistent() -> None:
    v = classify_verdict(
        predicted_sign=1,
        mean_ic=0.05,
        bs_ci=(0.02, 0.08),
        split_ics=[0.05, -0.02, -0.01, 0.04, 0.05],  # 5 段僅 3 同號 < ceil(0.8*5)=4
        n_dates=40,
        t=50,
    )
    assert v == "無證據"


def test_verdict_insufficient_sample() -> None:
    # n_dates < 30
    assert (
        classify_verdict(
            predicted_sign=1,
            mean_ic=0.05,
            bs_ci=(0.02, 0.08),
            split_ics=[0.05, 0.05, 0.05, 0.05, 0.05],
            n_dates=10,
            t=50,
        )
        == "樣本不足不可判"
    )
    # T < 10
    assert (
        classify_verdict(
            predicted_sign=1,
            mean_ic=0.05,
            bs_ci=(0.02, 0.08),
            split_ics=[0.05, 0.05, 0.05, 0.05, 0.05],
            n_dates=40,
            t=5,
        )
        == "樣本不足不可判"
    )
