"""regime 計算層測試（規劃書 03 V2）：趨勢/廣度/資金 → 進攻/中性/防禦，全離線合成資料。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.analysis.regime import (
    ATTACK,
    DEFENSE,
    INSUFFICIENT,
    NEUTRAL,
    _label,
    compute_flow_score,
    compute_regime,
    compute_trend_score,
    describe_regime,
)
from tw_screener.analysis.rotation import compute_market_index

D0 = date(2026, 1, 1)

CFG = {
    "clip_daily_return_pct": 10.0,
    "trend": {"ma_windows": [20, 60, 120]},
    "breadth": {"ma_window": 60, "position_window": 120, "min_priced": 3},
    "flow": {"windows": [5, 20], "saturate_shares": 100_000},
    "weights": {"trend": 0.4, "breadth": 0.3, "flow": 0.3},
    "thresholds": {"attack": 0.33, "defense": -0.33},
}


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _price_history(n_days: int, daily_ret: float, n_stocks: int = 5) -> pl.DataFrame:
    """每檔自 100 起、每日 ×(1+daily_ret) 的合成全市場日線。"""
    rows = []
    dates = _dates(n_days)
    for s in range(n_stocks):
        sid = f"{1000 + s}"
        close = 100.0 + s  # 略錯開起點，避免完全同值
        for d in dates:
            rows.append({"date": d, "stock_id": sid, "close": close, "volume": 1000})
            close *= 1.0 + daily_ret
    return pl.DataFrame(rows)


def _institutional(n_days: int, daily_net: int, n_stocks: int = 5) -> pl.DataFrame:
    """全市場法人快取：每檔每日固定 total_net。"""
    rows = []
    for s in range(n_stocks):
        sid = f"{1000 + s}"
        for d in _dates(n_days):
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


# ── 端到端三態 ────────────────────────────────────────────────────────────────


def test_bull_market_is_attack() -> None:
    price = _price_history(150, +0.01)
    inst = _institutional(150, +50_000)  # 全市場日均淨流正、5 檔 → 25 萬股/日
    r = compute_regime(price, inst, CFG)
    assert r.regime == ATTACK
    assert r.score is not None and r.score > 0
    # 多頭：三分項全為正
    assert r.trend_score == 1.0
    assert r.breadth_score is not None and r.breadth_score > 0
    assert r.flow_score is not None and r.flow_score > 0


def test_bear_market_is_defense() -> None:
    price = _price_history(150, -0.01)
    inst = _institutional(150, -50_000)
    r = compute_regime(price, inst, CFG)
    assert r.regime == DEFENSE
    assert r.score is not None and r.score < 0
    assert r.trend_score == -1.0
    assert r.breadth_score is not None and r.breadth_score < 0
    assert r.flow_score is not None and r.flow_score < 0


def test_empty_inputs_insufficient() -> None:
    r = compute_regime(pl.DataFrame(), pl.DataFrame(), CFG)
    assert r.regime == INSUFFICIENT
    assert r.score is None
    assert r.trend_score is None and r.breadth_score is None and r.flow_score is None


# ── 分項與邊界 ────────────────────────────────────────────────────────────────


def test_trend_score_alignment() -> None:
    """多頭排列 +1、空頭排列 −1；資料不足回 None。"""
    bull = compute_market_index(_price_history(150, +0.01))
    bear = compute_market_index(_price_history(150, -0.01))
    assert compute_trend_score(bull, [20, 60, 120])[0] == 1.0
    assert compute_trend_score(bear, [20, 60, 120])[0] == -1.0
    short = compute_market_index(_price_history(30, +0.01))  # 不足 MA120
    assert compute_trend_score(short, [20, 60, 120])[0] is None


def test_flow_score_sign_and_saturation() -> None:
    """淨流方向決定正負；超過飽和股數封頂 ±1。"""
    pos = compute_flow_score(_institutional(30, +1000), [5, 20], 100_000)[0]
    neg = compute_flow_score(_institutional(30, -1000), [5, 20], 100_000)[0]
    assert pos is not None and 0 < pos < 1
    assert neg is not None and -1 < neg < 0
    # 5 檔 × 巨量 → 日均遠超 saturate → 封頂
    sat = compute_flow_score(_institutional(30, +10_000_000), [5, 20], 100_000)[0]
    assert sat == 1.0
    # 無法人資料 → None
    assert compute_flow_score(pl.DataFrame(), [5, 20], 100_000)[0] is None


def test_breadth_gate_renormalizes_weights() -> None:
    """有效報價檔數 < min_priced → 廣度 None，regime 仍由 趨勢+資金 正規化合成。"""
    price = _price_history(150, +0.01, n_stocks=2)  # 2 檔 < min_priced=3
    inst = _institutional(150, +50_000, n_stocks=2)
    r = compute_regime(price, inst, CFG)
    assert r.breadth_score is None
    assert r.regime == ATTACK  # 趨勢+1、資金+ → 仍進攻
    assert r.evidence["weights_used"].keys() == {"trend", "flow"}


def test_short_history_flow_only() -> None:
    """歷史過短：趨勢/廣度 None，僅資金可得 → regime 由資金決定。"""
    price = _price_history(30, +0.01)  # < MA60/MA120
    inst = _institutional(30, +50_000)
    r = compute_regime(price, inst, CFG)
    assert r.trend_score is None and r.breadth_score is None
    assert r.flow_score is not None
    assert r.regime == ATTACK


def test_label_thresholds() -> None:
    assert _label(0.5, 0.33, -0.33) == ATTACK
    assert _label(0.33, 0.33, -0.33) == ATTACK
    assert _label(0.0, 0.33, -0.33) == NEUTRAL
    assert _label(-0.33, 0.33, -0.33) == DEFENSE
    assert _label(-0.5, 0.33, -0.33) == DEFENSE


def test_describe_regime_shape() -> None:
    r = compute_regime(_price_history(150, +0.01), _institutional(150, +50_000), CFG)
    desc = describe_regime(r)
    assert desc["regime"] == ATTACK
    assert "大盤姿態" in desc["line"]
    assert desc["advice"]
    assert set(desc) >= {"regime", "score", "as_of", "advice", "line"}
