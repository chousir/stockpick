"""backtest/macro_regime_validate.py — M-Macro2/M-Macro3（docs/25 §6 Phase 2/3）：

as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 重測（Phase 2）＋燈號 vs V2 regime 共振/背離
讀法實測驗證（Phase 3），共用同一套量尺（避免各自造輪子造成方法論漂移，逐項對齊三輪研究
report_2026-08-01*.md 的設定）：

1. **as-of 回放**：直接呼叫 `analysis/macro_regime.compute_level_pct`（正式生產函式，不重寫
   百分位公式）逐日重算 BAA10Y level_pct，驗證跟研究階段（round2 headline lift
   2.26〔1.61–3.06〕，對 NASDAQCOM 事件、N=60/X=15%）在誤差範圍內一致——這是「研究用計算
   邏輯」vs「production 用計算邏輯」有無微妙落差的最後把關（docs/25 §6 Phase 2 動機）。
2. **門檻敏感度**：紅燈切點（quintile，對齊 red_min/100）在小網格上重跑同一套 lift/CI。
3. **DEXJPUS tail-event 重測**：round1 用 top-quintile（20%）門檻算 dual_risk lift 得到
   無證據結論（1.15〔0.80–1.49〕）；docs/25 假設稀有肥尾事件（如 2024-08 carry unwind）
   被 20% 這麼寬的門檻稀釋——重跑更窄的尾端門檻（top 10%/5%/2%）看訊號會不會浮現。
4. **共振/背離讀法驗證（Phase 3）**：`build_light_color_series` 逐日重放**實際生產燈色**
   （含遲滯帶跨日記憶，呼叫 `classify_light` 序列化——不是 Phase 2 那種可獨立重算的單日
   quintile 門檻，遲滯帶本身就是「昨天的顏色」這個狀態，必須逐日 carry `prev_color` 才重放
   得出production 實際會顯示的顏色）；`bucket_lift` 把任意布林條件（如「紅」∧「V2 防禦」的
   交集）對事件目標算 lift＋CI，供共振桶跟單獨訊號比較用。

方法論對齊 report_2026-08-01.md：headline 事件＝`min(price[t+1..t+N])/price[t]` 跌幅 ≥X，
尾端不足 N 期樣本直接排除不補零；高風險＝當日分數（已是百分位∈[0,1]）≥ 分位門檻；
lift＝高風險日事件發生率 ÷ 全樣本事件發生率；CI＝N 個交易日一塊的 block bootstrap
（1000 次重抽，重用 `factor_lab.moving_block_bootstrap_ci`，不重寫 bootstrap 邏輯）。
嚴格因果（compute_level_pct/compute_dual_risk/classify_light 本身即因果／序列化因果）；
playbook/20 §6 研究軌判準。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from tw_screener.analysis.macro_regime import classify_light, compute_dual_risk, compute_level_pct

_EVENT_SCHEMA: dict[str, type[pl.DataType]] = {"date": pl.Date, "event": pl.Int8}
_SCORE_SCHEMA: dict[str, type[pl.DataType]] = {"date": pl.Date, "score": pl.Float64}


def build_level_pct_series(df: pl.DataFrame, lookback_days: int, min_obs: int = 30) -> pl.DataFrame:
    """逐日 level_pct：對每個觀測日呼叫 production `compute_level_pct(df, as_of=該日, …)`。

    warmup 不足（< min_obs 觀測）的日期不產列，語意對齊 compute_level_pct 的 None 回傳。
    刻意逐日呼叫既有純函式（而非另外向量化重寫），因為本模組的目的正是驗證「重放出來的
    序列」與「production 實際會算出來的值」逐日等價——重寫等於繞過了要驗證的東西。
    """
    if df.is_empty():
        return pl.DataFrame(schema=_SCORE_SCHEMA)
    rows: list[dict[str, object]] = []
    for d in df.sort("date")["date"].to_list():
        score = compute_level_pct(df, d, lookback_days, min_obs)
        if score is not None:
            rows.append({"date": d, "score": score})
    return pl.DataFrame(rows, schema=_SCORE_SCHEMA)


def build_dual_risk_series(
    df: pl.DataFrame, lookback_days: int, delta_days: int, min_obs: int = 30
) -> pl.DataFrame:
    """逐日 dual_risk（雙尾變速；直接呼叫 production `compute_dual_risk`）。"""
    if df.is_empty():
        return pl.DataFrame(schema=_SCORE_SCHEMA)
    rows: list[dict[str, object]] = []
    for d in df.sort("date")["date"].to_list():
        score = compute_dual_risk(df, d, lookback_days, delta_days, min_obs)
        if score is not None:
            rows.append({"date": d, "score": score})
    return pl.DataFrame(rows, schema=_SCORE_SCHEMA)


def build_light_color_series(
    df: pl.DataFrame,
    lookback_days: int,
    green_max: float,
    red_min: float,
    hysteresis: float,
    min_obs: int = 30,
) -> pl.DataFrame:
    """逐日**生產燈色**（含遲滯帶跨日記憶；直接呼叫 production `compute_level_pct`＋
    `classify_light`，不重寫任一邏輯）。

    跟 `build_level_pct_series`（Phase 2，回傳連續分數）的差別：這裡回傳的是遲滯帶處理過的
    **顏色**，因為 M-Macro3 測的是「使用者實際會在報表上看到的燈色」有沒有共振/背離的預測力
    ——遲滯帶讓顏色有跨日狀態（換色需突破門檻±hysteresis），用單日 quintile 門檻重算會漏掉
    這個狀態，等於測了一個 production 不會真的顯示的替代訊號。`prev_color` 逐日 carry，
    warmup 不足（score=None）的日期不產列但仍推進 `prev_color`（= INSUFFICIENT）——鏡射
    production `classify_light` 對 `prev_light=INSUFFICIENT` 的既有語意（無記憶可依附，
    下一個有效讀值直接用基準門檻判定、不套遲滯），不是另外發明的行為。
    """
    if df.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "color": pl.Utf8})
    rows: list[dict[str, object]] = []
    prev_color: str | None = None
    for d in df.sort("date")["date"].to_list():
        score = compute_level_pct(df, d, lookback_days, min_obs)
        risk_score = score * 100 if score is not None else None
        color = classify_light(risk_score, prev_color, green_max, red_min, hysteresis)
        if score is not None:
            rows.append({"date": d, "color": color})
        prev_color = color
    return pl.DataFrame(rows, schema={"date": pl.Date, "color": pl.Utf8})


def compute_event_labels(price_df: pl.DataFrame, n_days: int, drawdown_pct: float) -> pl.DataFrame:
    """event[t]=1 若 `min(price[t+1..t+n_days])/price[t]` 跌幅 ≥ drawdown_pct，否則 0。

    尾端不足 n_days 期的樣本直接排除、不補零（playbook/20 §6「未到期樣本排除不補零」）。
    """
    if price_df.is_empty():
        return pl.DataFrame(schema=_EVENT_SCHEMA)
    p = price_df.sort("date")
    dates = p["date"].to_list()
    values = p["value"].to_list()
    n = len(values)
    rows: list[dict[str, object]] = []
    for i in range(n - n_days):
        base = values[i]
        if base is None or base <= 0:
            continue
        window = values[i + 1 : i + 1 + n_days]
        if any(v is None for v in window):
            continue
        drawdown = 1 - (min(window) / base)
        rows.append({"date": dates[i], "event": 1 if drawdown >= drawdown_pct else 0})
    return pl.DataFrame(rows, schema=_EVENT_SCHEMA)


def _encode(high_risk: bool, event: bool) -> float:
    """單日 (high_risk, event) 編碼成單一 float，供通用 bootstrap helper 重用。

    解碼見 `_lift_stat`。
    """
    return (2.0 if high_risk else 0.0) + (1.0 if event else 0.0)


def _lift_stat(resampled: list[float]) -> float:
    """v∈{0,1,2,3}：bit1=high_risk、bit0=event。lift=P(event|high_risk)/P(event)；算不出→nan。"""
    n = len(resampled)
    if n == 0:
        return float("nan")
    n_high = sum(1 for v in resampled if v >= 2)
    n_high_event = sum(1 for v in resampled if v == 3)
    n_event = sum(1 for v in resampled if v in (1, 3))
    if n_high == 0 or n_event == 0:
        return float("nan")
    return (n_high_event / n_high) / (n_event / n)


@dataclass(frozen=True)
class LiftResult:
    lift: float | None
    ci_lo: float | None
    ci_hi: float | None
    n_high_risk: int
    n_all: int
    n_high_risk_events: int
    n_all_events: int
    n_blocks: int


def bucket_lift(
    bucket: pl.DataFrame,
    events: pl.DataFrame,
    block_len: int,
    n_boot: int = 1000,
    seed: int = 42,
    bucket_col: str = "in_bucket",
) -> LiftResult:
    """任意布林條件（`bucket_col`，非僅 quintile 門檻）對 `events` 算 lift＋block-bootstrap CI。

    共用 `_encode`/`_lift_stat`/`moving_block_bootstrap_ci`（不重寫 bootstrap 邏輯）——
    `high_risk_lift` 的「score≥quintile」只是這個更通用比較的一個特例（見其實作直接委派
    到這裡）。M-Macro3 用它比較「BAA10Y 紅」「V2 防禦」「兩者交集（共振）」「紅∧進攻（背離）」
    四種桶對同一事件目標的 lift，桶的定義可以是任意布林運算式，不限於單一分數門檻。
    """
    from tw_screener.backtest.factor_lab import moving_block_bootstrap_ci

    merged = bucket.join(events, on="date", how="inner").sort("date")
    if merged.is_empty():
        return LiftResult(None, None, None, 0, 0, 0, 0, 0)
    encoded = [
        _encode(bool(b), bool(e))
        for b, e in zip(merged[bucket_col].to_list(), merged["event"].to_list(), strict=True)
    ]
    lift = _lift_stat(encoded)
    ci_lo, ci_hi = moving_block_bootstrap_ci(
        encoded, block_len=block_len, n_boot=n_boot, seed=seed, stat=_lift_stat
    )
    n = len(encoded)
    n_high = sum(1 for v in encoded if v >= 2)
    n_high_event = sum(1 for v in encoded if v == 3)
    n_event = sum(1 for v in encoded if v in (1, 3))
    return LiftResult(
        lift=None if lift != lift else lift,  # nan 自比較恆 False，此為標準 nan 判定寫法
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        n_high_risk=n_high,
        n_all=n,
        n_high_risk_events=n_high_event,
        n_all_events=n_event,
        n_blocks=-(-n // block_len) if block_len > 0 else 0,
    )


def high_risk_lift(
    scores: pl.DataFrame,
    events: pl.DataFrame,
    quintile: float,
    block_len: int,
    n_boot: int = 1000,
    seed: int = 42,
) -> LiftResult:
    """score/event 依 date 對齊後算 lift＋block-bootstrap CI（`bucket_lift` 的 quintile 特例）。

    `score` 欄本身已是百分位∈[0,1]（compute_level_pct/compute_dual_risk 回傳值），
    「score ≥ quintile」即「該日落在近 3 年窗的前 (1-quintile) 分位」，不需再另算一次分位。
    """
    if scores.is_empty():
        return LiftResult(None, None, None, 0, 0, 0, 0, 0)
    bucket = scores.with_columns((pl.col("score") >= quintile).alias("in_bucket")).select(
        "date", "in_bucket"
    )
    return bucket_lift(bucket, events, block_len, n_boot=n_boot, seed=seed)
