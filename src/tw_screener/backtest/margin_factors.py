"""backtest/margin_factors.py — WS-K 籌碼因子首驗（個股層，融資／TDCC 大戶）。

docs/23-backtest-r2-w28.md §3 預註冊三因子（首驗跑前寫死，跑後不得改假設語彙）：

- **margin_slim**（K1，預註冊正向）＝ −1 ×（margin_balance_lots 相對 20 個交易日前
  （per stock 實際交易日序列的 row-order shift(20)，非日曆日——面板每列即一交易日，
  同 panel.py ma20_dist_pct 的窗慣例）的變化率）；分母（20 日前餘額）為 0 或 null
  → null。融資餘額下降（減肥）時本欄為正，機制：浮動槓桿籌碼洗出、上方賣壓沉澱減輕。
- **big_holder_wow**（K2，預註冊正向）＝ big_holder_pct − 該股「前一個 TDCC 資料日」
  的 big_holder_pct；只在 TDCC 資料日（面板 big_holder_pct 非 null 的列）有值；
  與前一個 TDCC 資料日間隔 >14 曆日（跳週／資料缺口）→ null，不強行接續補值。
- **margin_to_vol**（K3，預註冊負向）＝ margin_balance_lots×1000（張→股，對齊 panel
  volume 單位＝股——twse.py trade_volume 全線統一股、panel.py 直接沿用該欄，
  見 `_normalize`／「daily_all_* 快取的成交量欄叫 volume，統一成 trade_volume（同單位：股）」）
  ÷ 20 個交易日均量（row-order rolling；窗不足／均量 0／margin null → null）。

全部 backward-looking（shift 只往過去，無前視）；null 不填 0，語義同面板
ground-truth 慣例（vol_ratio／ma*_dist_pct 同款：缺就是缺，不臆造）。

`classify_verdict()`：docs/23 §3 判準三選一＋樣本不足標記，純函式（runner／
測試共用，不吃 IO）。

純函式；IO（面板讀取、factor_lab.evaluate 呼叫、報告落地）由
margin_factors_runner 負責。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import polars as pl

#: 因子欄 → (中文說明, 預註冊方向；+1=正向 −1=負向；docs/23 §3)
FACTOR_SPECS: dict[str, tuple[str, int]] = {
    "margin_slim": ("K1 融資減肥：−1×20 交易日融資餘額變化率", 1),
    "big_holder_wow": ("K2 大戶 WoW：≥400 張大戶佔比週增（pp）", 1),
    "margin_to_vol": ("K3 融資餘額/量：融資餘額股數÷20 交易日均量", -1),
}

_LOOKBACK_TD = 20
_WOW_MAX_GAP_DAYS = 14


def build_margin_factors(panel: pl.DataFrame, lookback_td: int = _LOOKBACK_TD) -> pl.DataFrame:
    """在面板上加 margin_slim／big_holder_wow／margin_to_vol 三欄（模組 docstring 定義）。

    需要欄：date/stock_id/margin_balance_lots/big_holder_pct/volume；任一缺席
    → 原樣回傳（不新增欄，呼叫端自行檢查並如實標註）。
    """
    need = {"date", "stock_id", "margin_balance_lots", "big_holder_pct", "volume"}
    if panel.is_empty() or not need.issubset(panel.columns):
        return panel

    df = panel.sort("stock_id", "date")

    # K1：margin_slim（row-order shift＝實際交易日序；分母 0/null → null，不硬除）
    prev_margin = pl.col("margin_balance_lots").shift(lookback_td).over("stock_id")
    margin_slim = (
        pl.when(prev_margin.is_not_null() & (prev_margin != 0))
        .then(-1 * (pl.col("margin_balance_lots") - prev_margin) / prev_margin)
        .otherwise(None)
        .alias("margin_slim")
    )

    # K3：margin_to_vol（張→股：×1000 對齊 panel volume 單位＝股）
    vol_avg = (
        pl.col("volume")
        .rolling_mean(window_size=lookback_td, min_samples=lookback_td)
        .over("stock_id")
    )
    margin_to_vol = (
        pl.when(
            vol_avg.is_not_null()
            & (vol_avg > 0)
            & pl.col("margin_balance_lots").is_not_null()
        )
        .then(pl.col("margin_balance_lots") * 1000 / vol_avg)
        .otherwise(None)
        .alias("margin_to_vol")
    )

    df = df.with_columns(margin_slim, margin_to_vol)

    # K2：big_holder_wow（只在 TDCC 資料日算；跳週(>14 曆日) → null，不硬接續）
    tdcc = (
        df.filter(pl.col("big_holder_pct").is_not_null())
        .select("date", "stock_id", "big_holder_pct")
        .sort("stock_id", "date")
        .with_columns(
            pl.col("big_holder_pct").shift(1).over("stock_id").alias("_prev_pct"),
            pl.col("date").shift(1).over("stock_id").alias("_prev_date"),
        )
        .with_columns(
            (pl.col("date") - pl.col("_prev_date")).dt.total_days().alias("_gap_days")
        )
        .with_columns(
            pl.when(
                pl.col("_prev_pct").is_not_null() & (pl.col("_gap_days") <= _WOW_MAX_GAP_DAYS)
            )
            .then(pl.col("big_holder_pct") - pl.col("_prev_pct"))
            .otherwise(None)
            .alias("big_holder_wow")
        )
        .select("date", "stock_id", "big_holder_wow")
    )
    df = df.join(tdcc, on=["date", "stock_id"], how="left")

    return df


def classify_verdict(
    predicted_sign: int,
    mean_ic: float | None,
    bs_ci: tuple[float | None, float | None],
    split_ics: Sequence[float | None],
    n_dates: int,
    t: int,
    min_effect: float = 0.03,
) -> str:
    """docs/23 §3 判準三選一＋樣本不足標記（純函式；runner／測試共用，不吃 IO）。

    優先序（先看樣本夠不夠，再看方向）：
    1. 樣本不足（非重疊子樣本 n_dates<30 或 per-date IC 序列長度 T<10，bootstrap
       重抽不出可信分布）→ **「樣本不足不可判」**，不論其餘條件如何。
    2. 否則同時滿足 |mean_IC|≥min_effect ＋ bs_CI95 不含 0 ＋ walk-forward 段同號
       比例 ≥4/5（段數<5 依比例下修、math.ceil(0.8×段數)，至少 3 段可判——
       docs/23 §1(a)「3 段需 3/3」同語彙）→ 依 mean_IC 符號對照 predicted_sign：
       同號＝**「方向成立」**、反號＝**「反向顯著」**（照實記錄，不得事後改機制說法）。
    3. 其餘＝**「無證據」**。
    """
    if n_dates < 30 or t < 10:
        return "樣本不足不可判"
    if mean_ic is None:
        return "無證據"
    bs_lo, bs_hi = bs_ci
    ci_ok = bs_lo is not None and bs_hi is not None and not (bs_lo < 0 < bs_hi)
    effect_ok = abs(mean_ic) >= min_effect
    valid = [v for v in split_ics if v is not None]
    pos = sum(1 for v in valid if v > 0)
    neg = sum(1 for v in valid if v < 0)
    required = math.ceil(0.8 * len(valid)) if valid else 0
    wf_ok = len(valid) >= 3 and max(pos, neg) >= required
    wf_sign = 1 if pos > neg else (-1 if neg > pos else 0)
    mean_sign = 1 if mean_ic > 0 else -1
    if effect_ok and ci_ok and wf_ok and wf_sign == mean_sign:
        return "方向成立" if mean_sign == predicted_sign else "反向顯著"
    return "無證據"
