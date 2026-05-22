"""backtest/strategies.py — 策略回測（尚未實作，骨架預留）。

設計意圖：
  讀取 reports/ 下各週的 screen_result_*.csv，
  對比入選當週後 N 週的實際報酬，
  評估各策略（D/E/F/G 為主）的歷史勝率、平均報酬與最大回撤。

需要至少 3 個月（約 12 週）歷史資料才有統計意義。
當前日期：2026-05，預計最早 2026-08 開始實作。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl


def load_historical_screens(reports_dir: Path) -> pl.DataFrame:
    """讀取所有週次的篩選結果，合併成含 week_tag 欄位的 DataFrame。

    Returns:
        schema: week_tag, stock_id, name, close, change_pct, strategy_id
    """
    raise NotImplementedError("回測模組尚未實作，需累積 3 個月以上歷史資料")


def compute_forward_returns(
    screens: pl.DataFrame,
    price_history: pl.DataFrame,
    hold_weeks: int = 4,
) -> pl.DataFrame:
    """計算入選後持有 hold_weeks 週的報酬。

    Args:
        screens: load_historical_screens() 的輸出
        price_history: 全市場日線快取（daily_*.parquet）
        hold_weeks: 持有週數

    Returns:
        schema: week_tag, stock_id, strategy_id, entry_price, exit_price, return_pct
    """
    raise NotImplementedError("回測模組尚未實作，需累積 3 個月以上歷史資料")


def strategy_summary(returns: pl.DataFrame) -> pl.DataFrame:
    """按策略計算勝率、平均報酬、最大回撤。

    Args:
        returns: compute_forward_returns() 的輸出

    Returns:
        schema: strategy_id, win_rate, avg_return_pct, max_drawdown_pct, sample_count
    """
    raise NotImplementedError("回測模組尚未實作，需累積 3 個月以上歷史資料")
