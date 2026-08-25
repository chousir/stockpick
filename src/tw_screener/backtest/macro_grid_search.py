"""backtest/macro_grid_search.py — docs/31 §23 Part 4：宏觀指標視窗/門檻/組合grid search，
對2022空頭／2024-08-05崩盤／2025-04關稅衝擊3個已知事件測「有沒有一組參數提前反應」
（§23.4 pre-registration已寫死grid/錨點/候選天花板，本模組是純函式實作，不重複規格文字）。

跟`macro_regime_validate.py`的關係：沿用其`build_level_pct_series`/`build_speed_pct_series`
產生逐日分數（不重寫百分位計算），本模組只加「分數序列 → episode → 對錨點算命中/false
positive/fire_rate」這段§22系列研究從未做過的事件視窗分析，是新的分析面而非重工。

核心概念：
  - fired：某日score≥threshold（或多指標布林合成）。
  - episode：連續fired的日子，允許中斷≤gap_td個交易日仍算同一段（避免門檻附近來回刷新
    灌水episode數，§23.4已定gap_td=10）。
  - 錨點視窗一律用「交易日位移」而非日曆天，位移基準是呼叫端傳入的`calendar`（§23.4取
    BAA10Y全歷史date欄當統一交易日曆，4指標日曆非100%相同但足夠接近，跟本專案既有
    `compute_event_labels`用序列自身row序列當交易日的簡化一致，不追求跨指標日曆精確對齊）。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date

import polars as pl


@dataclass(frozen=True)
class Episode:
    """一段fired區間（含合併過門檻附近雜訊的中斷）。"""

    start: date
    end: date


def nth_trading_day_before(calendar: list[date], anchor: date, n: int) -> date:
    """`calendar`（已排序）上，anchor（或最近一個≤anchor的交易日）往前數n個交易日的日期。

    anchor早於calendar[0]或n超出範圍時夾在calendar[0]（不外插）。
    """
    idx = bisect.bisect_right(calendar, anchor) - 1
    idx = max(0, idx)
    return calendar[max(0, idx - n)]


def nth_trading_day_after(calendar: list[date], anchor: date, n: int) -> date:
    """同`nth_trading_day_before`，往後數n個交易日；夾在calendar[-1]（不外插）。"""
    idx = bisect.bisect_left(calendar, anchor)
    idx = min(len(calendar) - 1, idx)
    return calendar[min(len(calendar) - 1, idx + n)]


def merge_fired_episodes(fired: pl.DataFrame, gap_td: int) -> list[Episode]:
    """`fired`（已依date排序，欄位date/fired）→ episode列表。

    中斷（fired=False的連續列數）≤gap_td仍算同一段episode（跨中斷延伸end）；
    超過gap_td才切成新episode。fired全False回傳空列表。
    """
    dates = fired["date"].to_list()
    flags = fired["fired"].to_list()
    episodes: list[Episode] = []
    cur_start: date | None = None
    cur_end: date | None = None
    gap = 0
    for d, f in zip(dates, flags, strict=True):
        if f:
            if cur_start is None:
                cur_start = d
            cur_end = d
            gap = 0
        elif cur_start is not None:
            gap += 1
            if gap > gap_td:
                episodes.append(Episode(cur_start, cur_end))  # type: ignore[arg-type]
                cur_start = None
                cur_end = None
                gap = 0
    if cur_start is not None:
        episodes.append(Episode(cur_start, cur_end))  # type: ignore[arg-type]
    return episodes


def first_hit_in_window(
    episodes: list[Episode], window_start: date, window_end: date
) -> Episode | None:
    """episodes裡start落在[window_start, window_end]（含端點）者，回傳start最早的一個；無則None。"""
    candidates = [e for e in episodes if window_start <= e.start <= window_end]
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.start)


def count_false_positive_episodes(
    episodes: list[Episode], event_windows: list[tuple[date, date]]
) -> int:
    """episodes裡start不落在任一event_windows區間內的數量（§23.4候選天花板②）。"""
    return sum(
        1 for e in episodes if not any(w0 <= e.start <= w1 for w0, w1 in event_windows)
    )


def fire_rate(fired: pl.DataFrame) -> float:
    """fired（欄位含bool `fired`）裡True比例；空輸入回傳nan（誠實：算不出不當作0）。"""
    flags = fired["fired"].to_list()
    if not flags:
        return float("nan")
    return sum(flags) / len(flags)


def combine_fired(fired_frames: list[pl.DataFrame], rule: str, min_count: int = 1) -> pl.DataFrame:
    """多指標fired欄（各含date/fired）inner join對齊到全部指標皆有效讀值的日期後合成。

    `rule="union"`：任一指標fired即fired（等同`count_ge`＋min_count=1，用不同名字讓
    呼叫端grid定義讀起來對得上§23.4「聯集/交集」用語）。
    `rule="count_ge"`：≥min_count個指標同時fired。
    inner join故warmup/stale較長的指標會讓合成序列的可用日期窗縮小，屬預期行為
    （§23.4未特別放寬，缺一指標讀值那天不能算「這組合有沒有fired」）。
    """
    if not fired_frames:
        return pl.DataFrame(schema={"date": pl.Date, "fired": pl.Boolean})
    merged = fired_frames[0].rename({"fired": "fired_0"})
    for i, f in enumerate(fired_frames[1:], start=1):
        merged = merged.join(f.rename({"fired": f"fired_{i}"}), on="date", how="inner")
    cols = [f"fired_{i}" for i in range(len(fired_frames))]
    count_expr = sum((pl.col(c).cast(pl.Int32) for c in cols), start=pl.lit(0, dtype=pl.Int32))
    threshold = 1 if rule == "union" else min_count
    if rule not in ("union", "count_ge"):
        raise ValueError(f"未知rule {rule!r}，只支援 'union'/'count_ge'")
    return merged.select("date", (count_expr >= threshold).alias("fired"))
