"""docs/31 §23 Part 4 grid search純函式測試（episode合併/錨點視窗位移/false positive/
多指標合成）。全離線合成資料。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.macro_grid_search import (
    Episode,
    combine_fired,
    count_false_positive_episodes,
    fire_rate,
    first_hit_in_window,
    merge_fired_episodes,
    nth_trading_day_after,
    nth_trading_day_before,
)

D0 = date(2024, 1, 1)


def _dates(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _fired(flags: list[bool], start: date = D0) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": _dates(len(flags), start), "fired": flags},
        schema={"date": pl.Date, "fired": pl.Boolean},
    )


def test_merge_fired_episodes_bridges_short_gap_but_splits_long_gap() -> None:
    # True True False False True (gap=2) → gap_td=2 合併成一段；gap_td=1 切成兩段
    flags = [True, True, False, False, True]
    fired = _fired(flags)
    merged = merge_fired_episodes(fired, gap_td=2)
    assert len(merged) == 1
    assert merged[0] == Episode(_dates(5)[0], _dates(5)[4])

    split = merge_fired_episodes(fired, gap_td=1)
    assert len(split) == 2


def test_merge_fired_episodes_all_false_returns_empty() -> None:
    fired = _fired([False] * 5)
    assert merge_fired_episodes(fired, gap_td=3) == []


def test_nth_trading_day_before_and_after_clamp_at_boundaries() -> None:
    calendar = _dates(10)
    anchor = calendar[5]
    assert nth_trading_day_before(calendar, anchor, 3) == calendar[2]
    assert nth_trading_day_after(calendar, anchor, 3) == calendar[8]
    # 超出範圍夾在邊界，不外插
    assert nth_trading_day_before(calendar, anchor, 100) == calendar[0]
    assert nth_trading_day_after(calendar, anchor, 100) == calendar[9]


def test_first_hit_in_window_picks_earliest_episode_inside_bounds() -> None:
    calendar = _dates(30)
    episodes = [Episode(calendar[5], calendar[5]), Episode(calendar[10], calendar[11])]
    hit = first_hit_in_window(episodes, calendar[3], calendar[8])
    assert hit == episodes[0]
    # 視窗外沒有命中
    assert first_hit_in_window(episodes, calendar[20], calendar[25]) is None


def test_count_false_positive_episodes_excludes_event_window_hits() -> None:
    calendar = _dates(40)
    episodes = [
        Episode(calendar[5], calendar[5]),  # 落在事件窗內
        Episode(calendar[30], calendar[30]),  # 落在事件窗外
    ]
    windows = [(calendar[0], calendar[10])]
    assert count_false_positive_episodes(episodes, windows) == 1


def test_fire_rate_basic_and_empty() -> None:
    assert fire_rate(_fired([True, True, False, False])) == 0.5
    assert fire_rate(_fired([])) != fire_rate(_fired([]))  # nan != nan


def test_combine_fired_union_vs_count_ge() -> None:
    a = _fired([True, False, False, False])
    b = _fired([False, True, False, False])
    c = _fired([False, False, True, False])
    union = combine_fired([a, b, c], rule="union")
    assert union["fired"].to_list() == [True, True, True, False]
    count2 = combine_fired([a, b, c], rule="count_ge", min_count=2)
    assert count2["fired"].to_list() == [False, False, False, False]


def test_combine_fired_inner_joins_to_common_dates_only() -> None:
    a = _fired([True, True, True], start=D0)
    b = _fired([True, True, True], start=D0 + timedelta(days=1))
    combined = combine_fired([a, b], rule="union")
    assert combined.height == 2  # 只剩重疊的兩天
