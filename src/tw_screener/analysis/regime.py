"""analysis/regime.py — 大盤 regime 總控閘門（規劃書 03 V2）。

設計意圖（規劃書 03 §V2）：
  在 picks/族群/輪動報表上補一個「市場層多空/位階訊號」，空頭或高位期提示降低總曝險。
  用既有等權全市場指數（rotation.compute_market_index）＋全市場法人快取，算三分項：

    1. 趨勢：等權指數 vs MA20/MA60/MA120 多空排列（指數 > MA20 > MA60 > MA120＝滿分多頭）。
    2. 廣度：個股站上自身 MA60 的比例（位階廣度）＋ 等權指數在自身 120 日高低區間的位階。
    3. 資金：全市場三大法人近 5/20 日 total_net 的日均淨流方向（股）。

  三分項各正規化到 [-1, 1]，加權合成連續分數 regime_score，門檻切 進攻 / 中性 / 防禦。

關鍵約定：
  - 評分為純函式；IO（讀價量/法人快取）集中在 compute_market_regime 便利包裝
    （供報告/CLI 共用），沿用既有 loader。
  - 所有視窗、權重、門檻由 settings.regime 傳入，不寫死。
  - 定位＝**輔助姿態揭露**，不硬性 gate 掉訊號（守 docs CLAUDE.md Part 3「由人決策」）。
  - 任一分項資料不足回 None；合成時只對「可得分項」按權重正規化；全缺→regime「資料不足」。

明文修正（對規劃書）：規劃書 §V2 廣度寫「上漲家數比、距低位階」。單日上漲家數比噪音大，
  改用截面位階廣度（站上自身 MA60 比例）＋指數自身位階（距低/距高），語意一致且穩健。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

import polars as pl

from tw_screener.analysis.rotation import compute_market_index

ATTACK = "進攻"
NEUTRAL = "中性"
DEFENSE = "防禦"
INSUFFICIENT = "資料不足"


@dataclass(frozen=True)
class RegimeResult:
    """大盤 regime 判定結果（連續分數＋三分項＋透明依據）。"""

    as_of: date | None
    regime: str
    score: float | None  # 合成分數 ∈ [-1, 1]；全分項缺→None
    trend_score: float | None
    breadth_score: float | None
    flow_score: float | None
    evidence: dict[str, object] = field(default_factory=dict)


_ADVICE = {
    ATTACK: "可正常布局、倉位上限偏高；順勢操作。",
    NEUTRAL: "標準倉位、分批布局；選股重於擇時。",
    DEFENSE: "降低總曝險、嚴設停損；現金為王、減少新進場。",
    INSUFFICIENT: "資料不足，無法判定大盤姿態（請確認日線／法人快取）。",
}


def describe_regime(r: RegimeResult) -> dict[str, object]:
    """把 RegimeResult 轉成報表/CLI 共用的顯示 dict（一行摘要＋姿態建議＋分項）。"""

    def _fmt(x: float | None) -> str:
        return f"{x:+.2f}" if x is not None else "—"

    score_str = f"{r.score:+.2f}" if r.score is not None else "—"
    line = (
        f"大盤姿態：{r.regime}（分數 {score_str}）"
        f"｜趨勢 {_fmt(r.trend_score)}・廣度 {_fmt(r.breadth_score)}・資金 {_fmt(r.flow_score)}"
    )
    return {
        "regime": r.regime,
        "score": r.score,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "trend_score": r.trend_score,
        "breadth_score": r.breadth_score,
        "flow_score": r.flow_score,
        "advice": _ADVICE.get(r.regime, ""),
        "line": line,
    }


def _num(x: object) -> float:
    """Polars 聚合回傳（mean/min/max）轉 float；呼叫端已保證非 None 數值。"""
    return cast(float, x)


def _latest_ma(series: pl.Series, window: int) -> float | None:
    """series 最後 window 個值的均值；不足 window → None。series 須已依日期排序。"""
    if series.len() < window:
        return None
    return _num(series.tail(window).mean())


def compute_trend_score(
    market_index: pl.DataFrame, ma_windows: list[int]
) -> tuple[float | None, dict[str, object]]:
    """等權指數 vs 多條均線的多空排列分數 ∈ [-1, 1]。

    把 [指數, MA(短), MA(中), MA(長)] 串成由快到慢的鏈，每對相鄰「前 > 後」記 +1、否則 −1，
    取均值。多頭排列（指數 > MA20 > MA60 > MA120）→ +1；空頭全反向 → −1。

    需最長 MA 的資料量；不足 → (None, 依據)。
    """
    if market_index.is_empty() or "market_index" not in market_index.columns:
        return None, {"reason": "no_market_index"}
    idx = market_index.sort("date")["market_index"]
    windows = sorted(ma_windows)
    if idx.len() < max(windows):
        return None, {"reason": "insufficient_history", "n_days": idx.len()}
    latest = float(idx.tail(1).item())
    mas = {w: _latest_ma(idx, w) for w in windows}
    # 鏈：指數 → MA(短) → … → MA(長)（資料足夠時 mas 全非 None）
    chain: list[float] = [latest] + [m for w in windows if (m := mas[w]) is not None]
    # NaN 毒化的指數會讓 Python 比較全 False → 趨勢假裝 −1.0——誠實回「未取得」
    if not all(math.isfinite(v) for v in chain):
        return None, {"reason": "non_finite_index"}
    pairs = [1 if a > b else -1 for a, b in zip(chain, chain[1:], strict=False)]
    score = sum(pairs) / len(pairs)
    return score, {
        "index": round(latest, 2),
        "ma": {f"ma{w}": round(v, 2) for w, v in mas.items() if v is not None},
    }


def compute_breadth_score(
    price_history: pl.DataFrame,
    market_index: pl.DataFrame,
    ma_window: int,
    position_window: int,
    min_priced: int,
) -> tuple[float | None, dict[str, object]]:
    """廣度分數 ∈ [-1, 1]＝（個股站上自身 MA 比例 ＋ 指數自身位階）各映射後取均值。

    - 位階廣度：最新交易日，收盤 > 自身 ma_window 日均線的個股佔比 ∈ [0, 1]。
    - 指數位階：等權指數最新值在自身 position_window 高低區間的位置 ∈ [0, 1]（距低=0、距高=1）。
    兩者皆 frac，映射 2*frac − 1 到 [-1, 1] 後平均。

    有效報價個股數 < min_priced → 廣度視為不可信，回 (None, 依據)。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return None, {"reason": "no_price_history"}

    df = price_history.select(["date", "stock_id", "close"]).sort(["stock_id", "date"])
    # 每股最新一筆收盤 vs 自身 ma_window 均線（資料足夠者才計入分母）
    per_stock = (
        df.with_columns(
            pl.col("close").rolling_mean(ma_window).over("stock_id").alias("_ma"),
            (pl.col("stock_id").cum_count().over("stock_id")).alias("_n"),
        )
        .group_by("stock_id")
        .agg(pl.all().last())
        .filter(
            (pl.col("_n") >= ma_window)
            & pl.col("close").is_not_null()
            & pl.col("_ma").is_not_null()
        )
    )
    n_priced = per_stock.height
    if n_priced < min_priced:
        return None, {"reason": "too_few_priced", "n_priced": n_priced}
    frac_above_ma = _num((per_stock["close"] > per_stock["_ma"]).mean())

    # 指數位階（距低=0、距高=1）
    idx_pos: float | None = None
    if not market_index.is_empty() and "market_index" in market_index.columns:
        idx = market_index.sort("date")["market_index"]
        if idx.len() >= 2:
            window = idx.tail(position_window)
            lo, hi = _num(window.min()), _num(window.max())
            latest = float(idx.tail(1).item())
            idx_pos = (latest - lo) / (hi - lo) if hi > lo else 0.5
            if not math.isfinite(idx_pos):  # 指數含 NaN → 位階分項誠實缺席
                idx_pos = None

    sub = [2 * frac_above_ma - 1]
    if idx_pos is not None:
        sub.append(2 * idx_pos - 1)
    score = sum(sub) / len(sub)
    return score, {
        "frac_above_ma": round(frac_above_ma, 3),
        "n_priced": n_priced,
        "index_position": round(idx_pos, 3) if idx_pos is not None else None,
    }


def compute_flow_score(
    institutional: pl.DataFrame, windows: list[int], saturate_shares: float
) -> tuple[float | None, dict[str, object]]:
    """全市場三大法人淨流方向分數 ∈ [-1, 1]。

    各窗取近 N 交易日全市場 total_net 加總 ÷ N ＝日均淨流（股），除以 saturate_shares 並夾
    [-1, 1]，多窗取均值。無法人資料或無有效交易日 → (None, 依據)。
    """
    if institutional.is_empty() or not {"date", "total_net"}.issubset(institutional.columns):
        return None, {"reason": "no_institutional"}
    if saturate_shares <= 0:
        return None, {"reason": "bad_saturate"}
    # 全市場每日淨流（所有個股 total_net 加總）
    daily = (
        institutional.group_by("date").agg(pl.col("total_net").sum().alias("net")).sort("date")
    )
    if daily.is_empty():
        return None, {"reason": "no_trading_days"}
    net_series = daily["net"]
    sub: list[float] = []
    evidence: dict[str, object] = {}
    for w in sorted(windows):
        tail = net_series.tail(w)
        n = tail.len()
        if n == 0:
            continue
        avg_daily = float(tail.sum()) / n
        s = max(-1.0, min(1.0, avg_daily / saturate_shares))
        sub.append(s)
        evidence[f"avg_daily_net_{w}d"] = int(avg_daily)
    if not sub:
        return None, {"reason": "no_window"}
    return sum(sub) / len(sub), evidence


def _label(score: float, attack: float, defense: float) -> str:
    if score >= attack:
        return ATTACK
    if score <= defense:
        return DEFENSE
    return NEUTRAL


def compute_regime(
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
    cfg: dict,
) -> RegimeResult:
    """合成大盤 regime（趨勢/廣度/資金 → 進攻/中性/防禦）。

    cfg＝settings.regime（clip_daily_return_pct / trend / breadth / flow / weights / thresholds）。
    可得分項按 weights 正規化加權；全缺→regime「資料不足」、score None。純函式，IO 由呼叫端載入。
    """
    clip = float(cfg.get("clip_daily_return_pct", 10.0))
    market_index = compute_market_index(price_history, clip_daily_return_pct=clip)

    trend_cfg = cfg.get("trend", {})
    breadth_cfg = cfg.get("breadth", {})
    flow_cfg = cfg.get("flow", {})

    trend_score, trend_ev = compute_trend_score(
        market_index, list(trend_cfg.get("ma_windows", [20, 60, 120]))
    )
    breadth_score, breadth_ev = compute_breadth_score(
        price_history,
        market_index,
        ma_window=int(breadth_cfg.get("ma_window", 60)),
        position_window=int(breadth_cfg.get("position_window", 120)),
        min_priced=int(breadth_cfg.get("min_priced", 200)),
    )
    flow_score, flow_ev = compute_flow_score(
        institutional,
        list(flow_cfg.get("windows", [5, 20])),
        float(flow_cfg.get("saturate_shares", 100_000_000)),
    )

    # NaN 分項一律視為「未取得」：NaN 進 _label 兩邊比較皆 False 會靜默輸出「中性」，
    # 壞掉的指標不得用正常口吻給倉位建議（W27 門面曾印「分數 +nan → 中性」）
    def _finite_or_none(s: float | None) -> float | None:
        return s if s is not None and math.isfinite(s) else None

    trend_score = _finite_or_none(trend_score)
    breadth_score = _finite_or_none(breadth_score)
    flow_score = _finite_or_none(flow_score)

    weights = cfg.get("weights", {})
    components = {
        "trend": (trend_score, float(weights.get("trend", 0.4))),
        "breadth": (breadth_score, float(weights.get("breadth", 0.3))),
        "flow": (flow_score, float(weights.get("flow", 0.3))),
    }
    avail = {k: (s, w) for k, (s, w) in components.items() if s is not None and w > 0}

    as_of = (
        market_index.sort("date")["date"].tail(1).item()
        if not market_index.is_empty()
        else None
    )
    evidence: dict[str, object] = {"trend": trend_ev, "breadth": breadth_ev, "flow": flow_ev}

    if not avail:
        return RegimeResult(
            as_of=as_of,
            regime=INSUFFICIENT,
            score=None,
            trend_score=trend_score,
            breadth_score=breadth_score,
            flow_score=flow_score,
            evidence=evidence,
        )

    total_w = sum(w for _, w in avail.values())
    score = sum(s * w for s, w in avail.values()) / total_w
    thresholds = cfg.get("thresholds", {})
    regime = _label(
        score, float(thresholds.get("attack", 0.33)), float(thresholds.get("defense", -0.33))
    )
    evidence["weights_used"] = {k: round(w / total_w, 3) for k, (_, w) in avail.items()}
    return RegimeResult(
        as_of=as_of,
        regime=regime,
        score=score,
        trend_score=trend_score,
        breadth_score=breadth_score,
        flow_score=flow_score,
        evidence=evidence,
    )


def compute_market_regime(
    cfg: dict,
    settings_path: Path,
    institutional: pl.DataFrame | None = None,
) -> RegimeResult:
    """IO 便利包裝：載入全市場日線＋法人快取後呼叫純函式 compute_regime（規劃書 03 V2）。

    供 group 報告與 market regime CLI 共用。institutional 可由呼叫端（group/rotation 已載過）
    傳入避免重複讀；否則自行載最近窗。純讀快取、不打網（法人 loader 只讀本地 parquet）。
    """
    from tw_screener.analysis.rotation import load_market_history

    rcfg = cfg.get("regime", {})
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    market = load_market_history(cache_dir, n_days=int(rcfg.get("history_days", 250)))
    if institutional is None:
        from tw_screener.data.twse import create_client

        flow_windows = [int(w) for w in rcfg.get("flow", {}).get("windows", [5, 20])]
        institutional = create_client(settings_path).load_institutional_history(
            n_days=max(flow_windows) if flow_windows else 20
        )
    return compute_regime(market, institutional, rcfg)
