"""WS-H.3 regime 標籤歷史化測試（全離線合成資料）。

驗收條件：(1) 截斷不變性——只用 ≤d 資料算出的 d 日標籤，與餵更長歷史算出的 d 日標籤
一致（backward-looking 的核心保證，防面板前視偏誤）；(2) 缺法人窗遵循 compute_regime
既有降級語義（缺口不是 0、不是整段沒收，鏡射 tail(w) 取「最近 w 筆已發布」）；
(3) 輸出 schema 六欄齊。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.analysis.regime import ATTACK, DEFENSE, INSUFFICIENT
from tw_screener.backtest.regime_history import build_regime_history

D0 = date(2026, 1, 1)

CFG = {
    "clip_daily_return_pct": 10.0,
    "trend": {"ma_windows": [20, 60, 120]},
    "breadth": {"ma_window": 60, "position_window": 120, "min_priced": 3},
    "flow": {"windows": [5, 20], "saturate_shares": 100_000},
    "weights": {"trend": 0.4, "breadth": 0.3, "flow": 0.3},
    "thresholds": {"attack": 0.33, "defense": -0.33},
}

_OUTPUT_COLS = {
    "date",
    "regime_label",
    "regime_score",
    "trend_score",
    "breadth_score",
    "flow_score",
}


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _price_history(n_days: int, daily_ret: float, n_stocks: int = 5) -> pl.DataFrame:
    """每檔自 100 起、每日 ×(1+daily_ret) 的合成全市場日線（同 tests/analysis/test_regime.py）。"""
    rows = []
    dates = _dates(n_days)
    for s in range(n_stocks):
        sid = f"{1000 + s}"
        close = 100.0 + s
        for d in dates:
            rows.append({"date": d, "stock_id": sid, "close": close, "volume": 1000})
            close *= 1.0 + daily_ret
    return pl.DataFrame(rows)


def _institutional(
    n_days: int, daily_net: int, n_stocks: int = 5, skip_dates: set[date] | None = None
) -> pl.DataFrame:
    """全市場法人快取：每檔每日固定 total_net；skip_dates 內的日期整批不出現（模擬缺發布）。"""
    skip = skip_dates or set()
    rows = []
    for s in range(n_stocks):
        sid = f"{1000 + s}"
        for d in _dates(n_days):
            if d in skip:
                continue
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "foreign_net": daily_net,
                    "trust_net": 0,
                    "dealer_net": 0,
                    "total_net": daily_net,
                }
            )
    return pl.DataFrame(rows)


# ── schema ───────────────────────────────────────────────────────────────────


def test_output_schema_six_cols() -> None:
    price = _price_history(150, 0.01)
    inst = _institutional(150, 50_000)
    out = build_regime_history(price, inst, CFG)
    assert set(out.columns) == _OUTPUT_COLS
    # compute_market_index（首日無前一日可算報酬，剔除）→ 日曆比輸入少 1 天
    assert out.height == 149
    assert out["date"].dtype == pl.Date
    assert out["regime_label"].dtype == pl.Utf8
    for c in ("regime_score", "trend_score", "breadth_score", "flow_score"):
        assert out[c].dtype == pl.Float64


def test_empty_input_empty_schema() -> None:
    out = build_regime_history(pl.DataFrame(), pl.DataFrame(), CFG)
    assert out.is_empty()
    assert set(out.columns) == _OUTPUT_COLS


def test_start_filters_output_not_computation() -> None:
    """start 只裁切輸出範圍；暖身段（start 前）仍用於算 start 當天的指標（不裁 warmup）。"""
    price = _price_history(150, 0.01)
    inst = _institutional(150, 50_000)
    start = _dates(150)[100]
    out = build_regime_history(price, inst, CFG, start=start)
    assert out["date"].min() == start
    full = build_regime_history(price, inst, CFG)
    row_out = out.filter(pl.col("date") == start).row(0, named=True)
    row_full = full.filter(pl.col("date") == start).row(0, named=True)
    assert row_out["trend_score"] == row_full["trend_score"]
    assert row_out["regime_label"] == row_full["regime_label"]


# ── (1) 截斷不變性：核心 backward-looking 保證 ──────────────────────────────


def test_truncation_invariance_bull_market() -> None:
    """只餵到第 150 天的資料 vs 餵到第 200 天的資料，第 150 天的標籤必須一致
    （regime_history 逐日只能用 ≤d 資料，未來資料不得改變過去某天的標籤）。
    """
    price_full = _price_history(200, 0.01, n_stocks=5)
    inst_full = _institutional(200, 50_000, n_stocks=5)
    cut = _dates(200)[149]

    price_trunc = price_full.filter(pl.col("date") <= cut)
    inst_trunc = inst_full.filter(pl.col("date") <= cut)

    full = build_regime_history(price_full, inst_full, CFG)
    trunc = build_regime_history(price_trunc, inst_trunc, CFG)

    row_full = full.filter(pl.col("date") == cut).row(0, named=True)
    row_trunc = trunc.filter(pl.col("date") == cut).row(0, named=True)
    assert row_trunc["regime_label"] == row_full["regime_label"] == ATTACK
    for col in ("regime_score", "trend_score", "breadth_score", "flow_score"):
        assert row_trunc[col] == pytest.approx(row_full[col])


def test_truncation_invariance_full_series() -> None:
    """更嚴格版：逐一比對截斷序列與全量序列在重疊日期範圍的每一列（不只抽一天）。"""
    price_full = _price_history(180, -0.008, n_stocks=6)  # 空頭：也驗證非多頭路徑
    inst_full = _institutional(180, -30_000, n_stocks=6)
    cut = _dates(180)[139]

    price_trunc = price_full.filter(pl.col("date") <= cut)
    inst_trunc = inst_full.filter(pl.col("date") <= cut)

    full = build_regime_history(price_full, inst_full, CFG)
    trunc = build_regime_history(price_trunc, inst_trunc, CFG)

    overlap = trunc.join(
        full, on="date", how="inner", suffix="_full"
    ).sort("date")
    assert overlap.height == trunc.height
    assert overlap["regime_label"].to_list() == overlap["regime_label_full"].to_list()
    for col in ("regime_score", "trend_score", "breadth_score", "flow_score"):
        a = overlap[col].to_list()
        b = overlap[f"{col}_full"].to_list()
        for x, y in zip(a, b, strict=True):
            if x is None or y is None:
                assert x is None and y is None
            else:
                assert x == pytest.approx(y)


# ── (2) 缺法人窗降級語義 ─────────────────────────────────────────────────────


def test_flow_gap_day_carries_forward_last_published() -> None:
    """法人某日整批缺發布（如 OTC 未回補窗）→ 該日 flow_score 沿用最近一筆已發布資料，
    不是 0、也不是該日整段 regime 沒收（鏡射 compute_flow_score 吃「已限定 ≤as_of」
    institutional 輸入、tail(w) 只算「已發布」筆數的既有語義）。
    """
    price = _price_history(150, 0.01, n_stocks=5)
    gap_date = _dates(150)[100]
    inst = _institutional(150, 50_000, n_stocks=5, skip_dates={gap_date})

    out = build_regime_history(price, inst, CFG)
    gap_row = out.filter(pl.col("date") == gap_date).row(0, named=True)
    prev_row = out.filter(pl.col("date") == _dates(150)[99]).row(0, named=True)

    assert gap_row["flow_score"] is not None
    assert gap_row["flow_score"] == pytest.approx(prev_row["flow_score"])
    # 資金腿沒被沒收 → regime 仍能合成（趨勢/廣度/資金皆可得的多頭盤）
    assert gap_row["regime_label"] == ATTACK


def test_no_institutional_data_before_first_publish_is_null() -> None:
    """calendar 早於任何已發布法人資料 → flow_score=null（不是 0），regime 由
    trend+breadth 重正規化合成（鏡射 compute_regime「可得分項按權重正規化」）。
    """
    price = _price_history(150, 0.01, n_stocks=5)
    inst_all = _institutional(150, 50_000, n_stocks=5)
    # 只保留第 50 天之後才有法人資料（模擬「該市場段法人快取回補未及」）
    inst_partial = inst_all.filter(pl.col("date") >= _dates(150)[50])

    out = build_regime_history(price, inst_partial, CFG)
    early_row = out.filter(pl.col("date") == _dates(150)[10]).row(0, named=True)
    assert early_row["flow_score"] is None
    assert early_row["trend_score"] is None or math.isfinite(early_row["trend_score"])
    # 趨勢/廣度尚不足暖身時全缺 → 資料不足；一旦暖身足夠但資金仍缺 → 靠趨勢+廣度合成
    late_but_before_flow_row = out.filter(pl.col("date") == _dates(150)[45]).row(0, named=True)
    assert late_but_before_flow_row["flow_score"] is None
    if late_but_before_flow_row["trend_score"] is not None:
        assert late_but_before_flow_row["regime_label"] in (ATTACK, DEFENSE)


def test_no_institutional_at_all_flow_null_everywhere() -> None:
    price = _price_history(150, 0.01, n_stocks=5)
    out = build_regime_history(price, pl.DataFrame(), CFG)
    assert out["flow_score"].null_count() == out.height
    # 全空法人不擋 regime——趨勢/廣度足夠時仍可判（不是全表資料不足）
    late_row = out.filter(pl.col("date") == _dates(150)[149]).row(0, named=True)
    assert late_row["regime_label"] != INSUFFICIENT


def test_breadth_min_priced_gate_null() -> None:
    """有效報價檔數 < min_priced → breadth_score=null（鏡射 compute_breadth_score）。"""
    price = _price_history(150, 0.01, n_stocks=2)  # 2 檔 < CFG min_priced=3
    inst = _institutional(150, 50_000, n_stocks=2)
    out = build_regime_history(price, inst, CFG)
    last = out.filter(pl.col("date") == _dates(150)[149]).row(0, named=True)
    assert last["breadth_score"] is None
    # 趨勢+資金仍可得 → 仍能合成判斷（不因廣度缺席整段沒收）
    assert last["regime_label"] == ATTACK
