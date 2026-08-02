"""data/valuation_history.py — 台股全市場估值中位數逐日累積（docs/25 §2.4 fallback B 前置管線）。

**只做累積，不做驗證、不做計分**（使用者 2026-08-02 拍板範圍）。docs/25 §2.4 原規劃要把全市場
PE/PBR 中位數收進總經燈號，但驗證需要跟 BAA10Y 同規格的 3 年滾動窗＋60 日 block bootstrap——
`data/cache/twse/valuation_ratios_*.parquet` 從 2026-06-12 才開始累積（~7 週），且官方端點
`BWIBBU_d`／`peratio_analysis` 皆「只回最新一交易日、不可回補」（docs/02），沒有任何辦法補回
更早的歷史。這代表驗證只能等資料隨時間自然養夠（~750 個交易日≈3 年），沒有捷徑；本模組只負責
「不要讓這 7 週的起點浪費掉」——每次 `fetch-twse` 跑，把當天全市場 PE/PBR/殖利率中位數
append 進一份**不受快取保留窗清理**的獨立 parquet，養到 Phase 2b（暫定）門檻後才有下一步。

**為什麼落地路徑刻意選在 `data/cache/` 之外**：`data/cache/twse/valuation_ratios_*.parquet`
（逐日快照原始檔）受 `cache.retention.valuation_days`（現 400 天）管制，`tw-screener
data prune-cache` 會依此窗砍舊檔。這份原始資料**砍了就永遠拿不回來**（上游不可回補），一旦
之後有人為了省空間跑 prune、又剛好把窗設得比累積年限短，3 年的养成就會被腰斬。中位數摘要本身
體積極小（一天一列，養 3 年也才 ~750 列），獨立存在 `data/macro_regime/` 底下（同一份不受
cache prune 掃描的目錄，比照 `macro_regime/history.parquet` 前例）＝即使原始快照之後被清理，
累積進度依然保留。**未來若有人想把這份檔案「整理」回 `data/cache/` 底下，先重讀這段。**
"""

from __future__ import annotations

import statistics
from datetime import date
from pathlib import Path

import polars as pl
from loguru import logger

_SUMMARY_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "median_pe": pl.Float64,
    "n_pe": pl.Int64,
    "median_pbr": pl.Float64,
    "n_pbr": pl.Int64,
    "median_dividend_yield": pl.Float64,
    "n_dividend_yield": pl.Int64,
}


def daily_valuation_summary(df: pl.DataFrame) -> dict[str, object] | None:
    """單日全市場估值快照 → 中位數摘要（純函式）。

    `df` 為單一 `valuation_ratios_{YYYYMMDD}.parquet` 的內容（`_parse_valuation_ratios` 產出，
    同一份快照內所有列共用同一個 `date`）。空表（整天完全沒抓到資料，如端點掛掉）→ 回 None，
    不產列——沒有日期可掛、也沒有「這天有資料但缺值」的語意，跟「有資料但全部缺值」是兩回事，
    後者仍要誠實產一列（median=None、n=0），不能混為一談悄悄跳過。
    各欄中位數計算**排除 null**（虧損股 PE、極少數缺 PBR/殖利率的列），不當 0 處理（誠實原則）。
    """
    if df.is_empty():
        return None
    d = df["date"].drop_nulls()
    if d.is_empty():
        return None
    as_of: date = d[0]

    def _median_and_n(col: str) -> tuple[float | None, int]:
        vals = [float(v) for v in df[col].drop_nulls().to_list()]
        if not vals:
            return None, 0
        return statistics.median(vals), len(vals)

    median_pe, n_pe = _median_and_n("pe")
    median_pbr, n_pbr = _median_and_n("pbr")
    median_dy, n_dy = _median_and_n("dividend_yield")
    return {
        "date": as_of,
        "median_pe": median_pe,
        "n_pe": n_pe,
        "median_pbr": median_pbr,
        "n_pbr": n_pbr,
        "median_dividend_yield": median_dy,
        "n_dividend_yield": n_dy,
    }


def append_valuation_history(df: pl.DataFrame, history_path: Path) -> pl.DataFrame:
    """把當日估值中位數摘要 append 進累積 parquet（冪等：同一 `date` 已存在則不重複寫入）。

    冪等性刻意比照 `analysis/macro_regime.append_history` *理應*有、但實際沒做到的行為——
    `make week`/`fetch-twse` 同一天可能因重試或手動多跑一次，沒有這道檢查會在累積序列裡
    埋進同日重複列，往後的滾動窗計算會被悄悄污染。回傳完整累積後的 DataFrame（供呼叫端印
    累積深度）；`daily_valuation_summary` 回 None（整天無資料）時，直接回傳既有累積內容、
    不寫入。
    """
    summary = daily_valuation_summary(df)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        pl.read_parquet(history_path)
        if history_path.exists()
        else pl.DataFrame(schema=_SUMMARY_SCHEMA)
    )
    if summary is None:
        logger.warning("估值快照為空，本日不計入累積歷史")
        return existing
    if not existing.is_empty() and (existing["date"] == summary["date"]).any():
        logger.info(f"估值歷史已有 {summary['date']} 這天，跳過重複 append")
        return existing
    new_row = pl.DataFrame([summary], schema=_SUMMARY_SCHEMA)
    combined = pl.concat([existing, new_row], how="diagonal_relaxed").sort("date")
    combined.write_parquet(history_path)
    logger.info(f"估值歷史累積 → {history_path}（{len(combined)} 個交易日）")
    return combined


def accumulation_depth_message(history: pl.DataFrame, target_days: int) -> str:
    """累積深度提示——只講「養了多少天」，不算任何百分位/訊號（範圍外）。"""
    n = len(history)
    if n == 0:
        return "估值歷史累積：0 個交易日（尚無資料）"
    remain = max(0, target_days - n)
    if remain == 0:
        return (
            f"估值歷史累積：{n} 個交易日（已達 {target_days} 日目標——"
            "仍需另立 milestone 走 BAA10Y 同規格驗證才能收進燈號，本模組不做驗證）"
        )
    return (
        f"估值歷史累積：{n} 個交易日（距 {target_days} 日目標還差 {remain} 日，尚不足以驗證）"
    )
