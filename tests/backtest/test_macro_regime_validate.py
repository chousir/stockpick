"""M-Macro2（docs/25 §6 Phase 2）as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 純函式測試。

全離線合成資料（不依賴 gitignored research/macro_regime_screening/raw/），驗收：
(1) build_level_pct_series 逐日呼叫 production compute_level_pct，warmup 不足不產列；
(2) compute_event_labels 前視保護——尾端不足 n_days 排除不補零；
(3) high_risk_lift 在「高風險日事件率必然更高」的構造樣本上算出 lift>1 且 CI 下界 >1；
(4) 完全隨機（高風險與事件無關）樣本上 lift≈1、CI 應涵蓋 1。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.macro_regime_validate import (
    build_level_pct_series,
    compute_event_labels,
    high_risk_lift,
)

D0 = date(2020, 1, 1)


def _dates(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_build_level_pct_series_warmup_and_values() -> None:
    """遞增序列：warmup（<min_obs）前不產列；warmup 後每列 level_pct 皆為當日在窗內的百分位。"""
    n = 100
    df = pl.DataFrame(
        {"date": _dates(n), "value": [float(i) for i in range(n)]},
        schema={"date": pl.Date, "value": pl.Float64},
    )
    out = build_level_pct_series(df, lookback_days=756, min_obs=30)
    assert out.height == n - 30 + 1  # 第 30 筆（index 29）起才達 min_obs
    # 嚴格遞增序列的百分位恆為 1.0（當日值是窗內最大值）
    assert all(v == 1.0 for v in out["score"].to_list())


def test_compute_event_labels_excludes_unexpired_tail() -> None:
    """尾端不足 n_days 期的樣本直接排除，不補零（防前視偏誤）。"""
    n = 50
    n_days = 10
    # 構造：index 5 之後價格腰斬（跌 50%），其餘持平
    values = [100.0] * n
    for i in range(6, n):
        values[i] = 50.0
    df = pl.DataFrame(
        {"date": _dates(n), "value": values}, schema={"date": pl.Date, "value": pl.Float64}
    )
    events = compute_event_labels(df, n_days=n_days, drawdown_pct=0.15)
    # 尾端 n_days 筆不產列（前視保護）
    assert events.height == n - n_days
    # index 0..5（跌價發生在 index 6 之前）forward window 內會看到腰斬 → event=1
    early = events.filter(pl.col("date") <= _dates(n)[5])["event"].to_list()
    assert all(e == 1 for e in early)
    # index 6 之後（已經跌完，之後平盤）forward window 內不再有新跌幅 → event=0
    late = events.filter(pl.col("date") > _dates(n)[5])["event"].to_list()
    assert all(e == 0 for e in late)


def test_high_risk_lift_detects_planted_signal() -> None:
    """構造「高風險日之後必發生事件」的樣本 → lift 明顯 >1 且 CI 下界 >1。"""
    n = 400
    dates = _dates(n)
    rng = random.Random(7)
    # score：每 10 天一個高峰（score=0.95，其餘均勻分布在 [0,0.7]）
    scores = []
    events = []
    for i in range(n):
        is_high = i % 10 == 0
        scores.append(0.95 if is_high else rng.uniform(0.0, 0.7))
        # 高風險日 90% 觸發事件；其餘日子只有 10% 背景事件率
        events.append(1 if rng.random() < (0.9 if is_high else 0.10) else 0)
    score_df = pl.DataFrame(
        {"date": dates, "score": scores}, schema={"date": pl.Date, "score": pl.Float64}
    )
    event_df = pl.DataFrame(
        {"date": dates, "event": events}, schema={"date": pl.Date, "event": pl.Int8}
    )
    result = high_risk_lift(score_df, event_df, quintile=0.80, block_len=20, n_boot=500, seed=1)
    assert result.lift is not None
    assert result.lift > 3.0
    assert result.ci_lo is not None and result.ci_lo > 1.0


def test_high_risk_lift_no_signal_when_independent() -> None:
    """高風險與事件互相獨立的隨機樣本 → lift 應貼近 1、CI 涵蓋 1（不誤判有訊號）。"""
    n = 600
    dates = _dates(n)
    rng = random.Random(11)
    scores = [rng.uniform(0.0, 1.0) for _ in range(n)]
    events = [1 if rng.random() < 0.15 else 0 for _ in range(n)]
    score_df = pl.DataFrame(
        {"date": dates, "score": scores}, schema={"date": pl.Date, "score": pl.Float64}
    )
    event_df = pl.DataFrame(
        {"date": dates, "event": events}, schema={"date": pl.Date, "event": pl.Int8}
    )
    result = high_risk_lift(score_df, event_df, quintile=0.80, block_len=20, n_boot=500, seed=2)
    assert result.lift is not None
    assert result.ci_lo is not None and result.ci_hi is not None
    assert result.ci_lo < 1.0 < result.ci_hi


def test_high_risk_lift_empty_input_returns_none() -> None:
    empty_score = pl.DataFrame(schema={"date": pl.Date, "score": pl.Float64})
    empty_event = pl.DataFrame(schema={"date": pl.Date, "event": pl.Int8})
    result = high_risk_lift(empty_score, empty_event, quintile=0.8, block_len=20)
    assert result.lift is None
    assert result.n_all == 0
