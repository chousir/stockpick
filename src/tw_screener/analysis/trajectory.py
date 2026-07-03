"""analysis/trajectory.py — 回踩品質軌跡欄（規劃書 05 F5，沿舊 07 TR1 設計）。

動機：分析師讀的是單日快照，「健康回踩 vs 下跌第一天」只能靠週對週粗指標猜。
本模組把既有日線歷史蒸餾成軌跡欄，端進 candidates_enriched：

  down_days_streak    連續收黑天數（跌第一天 vs 已跌一段）
  pullback_vol_ratio  回踩期均量/前段均量（縮量回踩＝健康、放量下殺＝危險）
  above_ma20_days     連續站上(+)/跌破(−) MA20 天數（比單日距月線多了持續性）
  pullback_quality    止穩｜觀察｜破線（縮量+守均線→止穩；放量/連跌+破均線→破線）

定位：**啟發式輔助、非買賣訊號**（判斷權在人）；餵 F1-PO3 翻轉解剖與 F2「健康
拉回」判定。歷史不足（新股/上櫃缺口）→ 全 null 不臆造。純函式、不打網。
"""

from __future__ import annotations

import polars as pl

_DEFAULTS: dict[str, float] = {
    "ma_window": 20,
    "pullback_vol_window": 5,   # 回踩期均量窗（交易日）
    "base_vol_window": 20,      # 前段均量窗（回踩期之前）
    "calm_vol_ratio": 0.8,      # ≤ 此＝縮量（止穩要件）
    "danger_vol_ratio": 1.2,    # ≥ 此＝放量（破線要件之一）
    "danger_streak": 3,         # 連跌 ≥ 此天（破線要件之一）
    "min_history_days": 25,     # 歷史 < 此 → 全 null（如實缺）
}

_OUT_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "down_days_streak": pl.Int32,
    "pullback_vol_ratio": pl.Float64,
    "above_ma20_days": pl.Int32,
    "pullback_quality": pl.Utf8,
}


def trajectory_metrics(
    closes: list[float], volumes: list[float | None], params: dict | None = None
) -> dict:
    """單檔軌跡欄（closes/volumes 按日期遞增、同長度；volumes 可含 None）。

    量資料不足 → pullback_vol_ratio null、止穩無法確認縮量 → 判「觀察」（不臆造健康）。
    """
    p = {**_DEFAULTS, **(params or {})}
    out: dict = {
        "down_days_streak": None,
        "pullback_vol_ratio": None,
        "above_ma20_days": None,
        "pullback_quality": None,
    }
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < int(p["min_history_days"]):
        return out

    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            streak += 1
        else:
            break
    out["down_days_streak"] = streak

    w = int(p["ma_window"])
    ma = [
        sum(closes[i - w + 1 : i + 1]) / w if i >= w - 1 else None
        for i in range(len(closes))
    ]
    above_days = 0
    ma_last = ma[-1]
    if ma_last is not None:
        sign = closes[-1] > ma_last
        for i in range(len(closes) - 1, -1, -1):
            ma_i = ma[i]
            if ma_i is None or (closes[i] > ma_i) != sign:
                break
            above_days += 1
        out["above_ma20_days"] = above_days if sign else -above_days

    pw, bw = int(p["pullback_vol_window"]), int(p["base_vol_window"])
    vols = [v for v in (volumes or []) if v is not None and v > 0]
    if len(vols) >= pw + bw:
        recent = sum(vols[-pw:]) / pw
        base = sum(vols[-(pw + bw) : -pw]) / bw
        out["pullback_vol_ratio"] = round(recent / base, 2) if base > 0 else None

    ab, vr = out["above_ma20_days"], out["pullback_vol_ratio"]
    if ab is not None:
        if ab < 0 and (
            streak >= int(p["danger_streak"])
            or (vr is not None and vr >= float(p["danger_vol_ratio"]))
        ):
            out["pullback_quality"] = "破線"
        elif ab > 0 and streak <= 1 and vr is not None and vr <= float(p["calm_vol_ratio"]):
            out["pullback_quality"] = "止穩"
        else:
            out["pullback_quality"] = "觀察"
    return out


def compute_trajectories(
    price_history: pl.DataFrame,
    volume_history: pl.DataFrame | None = None,
    params: dict | None = None,
) -> pl.DataFrame:
    """批次算全部候選股的軌跡欄（join key: stock_id）。

    price_history: (date, stock_id, close[, volume])；volume_history: (date, stock_id,
    trade_volume|volume) 可選——close 同框有 volume 就直接用、否則從 volume_history 補。
    """
    if price_history.is_empty() or "close" not in price_history.columns:
        return pl.DataFrame(schema=_OUT_SCHEMA)

    px = price_history.select(
        pl.col("stock_id").cast(pl.Utf8),
        "date",
        "close",
        *(["volume"] if "volume" in price_history.columns else []),
    )
    if "volume" not in px.columns:
        px = px.with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    # volume_history 補洞（coalesce）：price_history 的 volume 欄可能整欄 null
    # （舊 schema 快取 normalize 補 null），不能只看「欄存不存在」
    if volume_history is not None and not volume_history.is_empty():
        vcol = "trade_volume" if "trade_volume" in volume_history.columns else "volume"
        if vcol in volume_history.columns:
            px = px.join(
                volume_history.select(
                    pl.col("stock_id").cast(pl.Utf8), "date", pl.col(vcol).alias("_vol_vh")
                ).unique(subset=["stock_id", "date"]),
                on=["stock_id", "date"],
                how="left",
            ).with_columns(
                pl.coalesce(pl.col("volume"), pl.col("_vol_vh")).alias("volume")
            ).drop("_vol_vh")

    rows: list[dict] = []
    for sub in px.sort(["stock_id", "date"]).partition_by("stock_id", maintain_order=True):
        m = trajectory_metrics(sub["close"].to_list(), sub["volume"].to_list(), params)
        rows.append({"stock_id": sub["stock_id"][0], **m})
    return pl.DataFrame(rows, schema_overrides=_OUT_SCHEMA) if rows else pl.DataFrame(
        schema=_OUT_SCHEMA
    )
