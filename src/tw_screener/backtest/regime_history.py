"""backtest/regime_history.py — WS-H.3 regime 標籤歷史化（V2 引擎逐日 as-of 重算）。

analysis/regime.py 的 compute_regime 是純函式，但其 IO 包裝 compute_market_regime
只算「最新一天」。研究軌（面板 regime 切片、backtest 分段研究）需要 2022-01-01 起
每個交易日的標籤——本模組向量化重算同一套 趨勢/廣度/資金 三分項，但逐日只用
**≤該日**的資料（backward-looking rolling，不做全樣本正規化），避免面板出現
「用未來資料標記今天 regime」的前視偏誤。

三分項公式**鏡射** analysis/regime.py 的既有語義（含 NaN 誠實化與資料不足降級），
差別只在「逐日重算」vs「算一次最新值」：
    - trend：compute_trend_score 的 `series.tail(window).mean()` ⇒ 逐日版
      `rolling_mean(window_size=w, min_samples=w)`（同一 trailing mean，只是對每天都算）。
    - breadth：compute_breadth_score 的「當前站上 MA 比例」＋「指數 tail(position_window)
      高低位階」⇒ 逐日版用 per-stock rolling_mean（判站上）＋指數 rolling_min/max
      （min_samples=2，鏡射原函式「idx.len()>=2 才計」）。
    - flow：compute_flow_score 的 `tail(w).sum()/tail(w).len()`（吃「已限定 ≤as_of」的
      institutional 輸入，缺發布日直接不進 tail）⇒ 逐日版對「只在有發布的日期存在」的
      法人日淨額序列做 rolling_mean(min_samples=1)，再以 join_asof(backward) 貼回完整
      交易日曆——法人缺發布的日子沿用「最近一筆已發布」數據，语意與原函式一致
      （不是把缺口當 0，也不是整段沒收）。
    - 合成：與 compute_regime 相同的「可得分項按權重正規化、全缺→資料不足」規則；
      逐日只是交易日數量級（~1000+ 列），用 Python 迴圈直接對照原函式邏輯，不必
      為此再堆一層向量化表達式（規格「逐日只做輕量聚合」）。

輸出六欄 parquet（date/regime_label/regime_score/trend_score/breadth_score/flow_score）
到 settings backtest.regime_history.output_path；panel.py build_price_panel 的
regime= 參數純 date join 直接吃這份輸出。

純讀本地快取、不打網。純函式（可用離線合成資料測試）與 IO（cache 載入、parquet
落地）分離：`build_regime_history` 純函式，`run_regime_history` IO 包裝（CLI 呼叫）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

from tw_screener.analysis.regime import INSUFFICIENT, _label
from tw_screener.analysis.rotation import compute_market_index

console = Console()

_OUTPUT_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "regime_label": pl.Utf8,
    "regime_score": pl.Float64,
    "trend_score": pl.Float64,
    "breadth_score": pl.Float64,
    "flow_score": pl.Float64,
}

# 遠超實際快取涵蓋年限的哨兵值：regime 歷史標籤需要長期 warmup（MA120／
# position_window 120 交易日的暖身段），不像其他消費者有「近 N 日」業務語意——
# 直接吃快取現有的全部歷史。非門檻/非業務參數，鐵律 5「限一律進 settings」
# 指的是有調整意義的業務參數，這裡只是「別人為設上限」的哨兵，故不進 config。
_FULL_HISTORY_DAYS = 5000


def _trend_score_series(market_index: pl.DataFrame, ma_windows: list[int]) -> pl.DataFrame:
    """逐日趨勢分數（鏡射 compute_trend_score 逐日版）。回傳 (date, trend_score)。"""
    schema = {"date": pl.Date, "trend_score": pl.Float64}
    if market_index.is_empty() or "market_index" not in market_index.columns:
        return pl.DataFrame(schema=schema)
    windows = sorted(ma_windows)
    ma_cols = [f"_ma{w}" for w in windows]
    idx = market_index.sort("date").with_columns(
        [
            pl.col("market_index").rolling_mean(window_size=w, min_samples=w).alias(f"_ma{w}")
            for w in windows
        ]
    )
    chain_cols = ["market_index", *ma_cols]
    valid = pl.all_horizontal(
        [pl.col(c).is_not_null() & pl.col(c).is_finite() for c in chain_cols]
    )
    pair_exprs = [
        pl.when(pl.col(a) > pl.col(b)).then(1.0).otherwise(-1.0)
        for a, b in zip(chain_cols, chain_cols[1:], strict=False)
    ]
    raw = pl.sum_horizontal(pair_exprs) / float(len(pair_exprs))
    return idx.select("date", pl.when(valid).then(raw).otherwise(None).alias("trend_score"))


def _breadth_score_series(
    price_history: pl.DataFrame,
    market_index: pl.DataFrame,
    ma_window: int,
    position_window: int,
    min_priced: int,
) -> pl.DataFrame:
    """逐日廣度分數（鏡射 compute_breadth_score 逐日版）。回傳 (date, breadth_score)。

    輸出日曆＝market_index 的日期軸（呼叫端保證非空）；n_priced < min_priced →
    該日 breadth_score=null（不論指數位階腿是否可得，鏡射「too_few_priced」全缺降級）。
    """
    schema = {"date": pl.Date, "breadth_score": pl.Float64}
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return pl.DataFrame(schema=schema)
    if market_index.is_empty() or "market_index" not in market_index.columns:
        return pl.DataFrame(schema=schema)

    per_stock = (
        price_history.select("date", "stock_id", "close")
        .sort("stock_id", "date")
        .with_columns(
            pl.col("close")
            .rolling_mean(window_size=ma_window, min_samples=ma_window)
            .over("stock_id")
            .alias("_ma"),
            pl.col("stock_id").cum_count().over("stock_id").alias("_n"),
        )
        .filter(
            (pl.col("_n") >= ma_window)
            & pl.col("close").is_not_null()
            & pl.col("_ma").is_not_null()
        )
    )
    daily_breadth = per_stock.group_by("date").agg(
        (pl.col("close") > pl.col("_ma")).mean().alias("_frac_above_ma"),
        pl.len().alias("_n_priced"),
    )

    idx = (
        market_index.sort("date")
        .with_columns(
            pl.col("market_index")
            .rolling_min(window_size=position_window, min_samples=2)
            .alias("_lo"),
            pl.col("market_index")
            .rolling_max(window_size=position_window, min_samples=2)
            .alias("_hi"),
        )
        .with_columns(
            pl.when(pl.col("_hi") > pl.col("_lo"))
            .then((pl.col("market_index") - pl.col("_lo")) / (pl.col("_hi") - pl.col("_lo")))
            .when(pl.col("_lo").is_not_null())
            .then(0.5)
            .otherwise(None)
            .alias("_idx_pos_raw")
        )
        .with_columns(
            pl.when(
                pl.col("_idx_pos_raw").is_not_null() & pl.col("_idx_pos_raw").is_finite()
            )
            .then(pl.col("_idx_pos_raw"))
            .otherwise(None)
            .alias("_idx_pos")
        )
        .select("date", "_idx_pos")
    )

    combined = idx.join(daily_breadth, on="date", how="left")
    return combined.select(
        "date",
        pl.when(pl.col("_n_priced").is_not_null() & (pl.col("_n_priced") >= min_priced))
        .then(
            pl.when(pl.col("_idx_pos").is_not_null())
            .then(((2 * pl.col("_frac_above_ma") - 1) + (2 * pl.col("_idx_pos") - 1)) / 2.0)
            .otherwise(2 * pl.col("_frac_above_ma") - 1)
        )
        .otherwise(None)
        .alias("breadth_score"),
    ).sort("date")


def _flow_score_series(
    institutional: pl.DataFrame,
    calendar: pl.DataFrame,
    windows: list[int],
    saturate_shares: float,
) -> pl.DataFrame:
    """逐日資金分數（鏡射 compute_flow_score 逐日版）。回傳 (date, flow_score)。

    全市場每日 total_net 加總；法人缺發布的日子不存在於 daily_net 序列，
    join_asof(backward) 貼回 calendar 時沿用「最近一筆已發布」的 rolling 均值
    ——鏡射原函式吃「已限定 ≤as_of」institutional 輸入、tail(w) 取「最近 w 筆
    已發布」而非「最近 w 個日曆/交易日」的既有語義（缺口不是 0、也不是整段沒收）。
    calendar 早於任何已發布法人資料的日期 → null。
    """
    out_cal = calendar.select("date").sort("date")
    if institutional.is_empty() or not {"date", "total_net"}.issubset(institutional.columns):
        return out_cal.with_columns(pl.lit(None, dtype=pl.Float64).alias("flow_score"))
    if saturate_shares <= 0:
        return out_cal.with_columns(pl.lit(None, dtype=pl.Float64).alias("flow_score"))

    daily_net = (
        institutional.group_by("date")
        .agg(pl.col("total_net").sum().cast(pl.Float64).alias("_net"))
        .sort("date")
    )
    if daily_net.is_empty():
        return out_cal.with_columns(pl.lit(None, dtype=pl.Float64).alias("flow_score"))

    windows = sorted(windows)
    avg_cols = [f"_avg{w}" for w in windows]
    daily_net = daily_net.with_columns(
        [
            pl.col("_net").rolling_mean(window_size=w, min_samples=1).alias(f"_avg{w}")
            for w in windows
        ]
    )
    asof = out_cal.join_asof(daily_net.select("date", *avg_cols), on="date", strategy="backward")
    sub_cols = [f"_s{w}" for w in windows]
    asof = asof.with_columns(
        [
            pl.when(pl.col(f"_avg{w}").is_not_null())
            .then((pl.col(f"_avg{w}") / saturate_shares).clip(-1.0, 1.0))
            .otherwise(None)
            .alias(f"_s{w}")
            for w in windows
        ]
    )
    return asof.select("date", pl.mean_horizontal(sub_cols).alias("flow_score"))


def build_regime_history(
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
    cfg: dict,
    start: date | None = None,
) -> pl.DataFrame:
    """逐日 as-of regime 標籤（鏡射 compute_regime 的合成/降級語義；純函式）。

    cfg 同 settings.regime（clip_daily_return_pct/trend/breadth/flow/weights/thresholds）。
    start 給定時只裁切輸出範圍——指標仍用完整載入窗（含暖身段）算完再切，語意同
    panel.py 的 panel_start：不影響任何指標值，只影響輸出的第一列（呼叫端須確保
    price_history/institutional 已含 start 前的暖身段，否則早期日期誠實回「資料不足」）。

    Returns: (date, regime_label, regime_score, trend_score, breadth_score, flow_score)；
    輸入空 → 空表（schema 一致）。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return pl.DataFrame(schema=_OUTPUT_SCHEMA)

    clip = float(cfg.get("clip_daily_return_pct", 10.0))
    market_index = compute_market_index(price_history, clip_daily_return_pct=clip)
    if market_index.is_empty():
        return pl.DataFrame(schema=_OUTPUT_SCHEMA)

    trend_cfg = cfg.get("trend", {})
    breadth_cfg = cfg.get("breadth", {})
    flow_cfg = cfg.get("flow", {})
    weights = cfg.get("weights", {})
    thresholds = cfg.get("thresholds", {})
    w_trend = float(weights.get("trend", 0.4))
    w_breadth = float(weights.get("breadth", 0.3))
    w_flow = float(weights.get("flow", 0.3))
    attack = float(thresholds.get("attack", 0.33))
    defense = float(thresholds.get("defense", -0.33))

    calendar = market_index.select("date").sort("date")

    trend = _trend_score_series(market_index, list(trend_cfg.get("ma_windows", [20, 60, 120])))
    breadth = _breadth_score_series(
        price_history,
        market_index,
        ma_window=int(breadth_cfg.get("ma_window", 60)),
        position_window=int(breadth_cfg.get("position_window", 120)),
        min_priced=int(breadth_cfg.get("min_priced", 200)),
    )
    flow = _flow_score_series(
        institutional,
        calendar,
        list(flow_cfg.get("windows", [5, 20])),
        float(flow_cfg.get("saturate_shares", 100_000_000)),
    )

    merged = (
        calendar.join(trend, on="date", how="left")
        .join(breadth, on="date", how="left")
        .join(flow, on="date", how="left")
        .sort("date")
    )

    # 合成：與 compute_regime 相同的「可得分項按權重正規化、全缺→資料不足」規則。
    # 逐日資料量只到交易日數量級（~1000+ 列）——Python 迴圈直接對照原函式邏輯，
    # 不為此再堆一層向量化表達式（規格「逐日只做輕量聚合」，且避免權重正規化的
    # 條件分支在向量化表達式裡重新推導一次、增加與原函式行為分歧的風險）。
    components = (("trend", w_trend), ("breadth", w_breadth), ("flow", w_flow))
    labels: list[str] = []
    scores: list[float | None] = []
    for row in merged.iter_rows(named=True):
        avail = [
            (row[f"{name}_score"], w)
            for name, w in components
            if row[f"{name}_score"] is not None and w > 0
        ]
        if not avail:
            labels.append(INSUFFICIENT)
            scores.append(None)
            continue
        total_w = sum(w for _, w in avail)
        score = sum(s * w for s, w in avail) / total_w
        labels.append(_label(score, attack, defense))
        scores.append(score)

    out = merged.with_columns(
        pl.Series("regime_label", labels, dtype=pl.Utf8),
        pl.Series("regime_score", scores, dtype=pl.Float64),
    ).select("date", "regime_label", "regime_score", "trend_score", "breadth_score", "flow_score")

    if start is not None:
        out = out.filter(pl.col("date") >= start)
    return out.sort("date")


def run_regime_history(settings: Path) -> Path:
    """WS-H.3 IO 包裝：純讀本地快取（不打網）→ 逐日 regime 標籤 parquet。CLI 呼叫。"""
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rh_cfg = cfg.get("backtest", {}).get("regime_history", {})
    start_raw = rh_cfg.get("start")
    start = date.fromisoformat(str(start_raw)) if start_raw else None
    out_path = Path(rh_cfg.get("output_path", "research/panel/regime_labels.parquet"))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    regime_cfg = cfg.get("regime", {})

    console.print("[bold]載入全部日線／法人快取（regime 歷史需長期 warmup，純讀本地）…[/bold]")
    price = load_market_history(
        cache_dir,
        n_days=_FULL_HISTORY_DAYS,
        patterns=("stock_day_*.parquet", "daily_*.parquet", "otc_daily_*.parquet"),
    )
    if price.is_empty():
        console.print("[red]無日線快取——先跑 make fetch-twse / backfill-universe-history[/red]")
        raise typer.Exit(1)
    institutional = create_client(settings).load_institutional_history(n_days=_FULL_HISTORY_DAYS)
    console.print(f"  價格 {price.height} 列・法人 {institutional.height} 列")

    labels = build_regime_history(price, institutional, regime_cfg, start=start)
    if labels.is_empty():
        console.print("[red]regime 標籤為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels.write_parquet(out_path)

    d0, d1 = str(labels["date"].min()), str(labels["date"].max())
    console.print(
        f"[green]regime 標籤 → {out_path}（{labels.height} 列，{d0}~{d1}）[/green]"
    )
    by_label = labels.group_by("regime_label").agg(pl.len().alias("n")).sort("regime_label")
    for r in by_label.iter_rows(named=True):
        console.print(f"  {r['regime_label']}: {r['n']}")
    return out_path
