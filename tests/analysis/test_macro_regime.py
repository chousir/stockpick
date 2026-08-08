"""tests/analysis/test_macro_regime.py — 總經燈號計分層測試（docs/25 v2，全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from tw_screener.analysis.macro_regime import (
    GREEN,
    INSUFFICIENT,
    NO_PREV,
    RED,
    YELLOW,
    append_history,
    append_panel_history,
    classify_light,
    compute_dual_risk,
    compute_indicator_reading,
    compute_level_pct,
    compute_macro_light,
    compute_panel_deltas,
    compute_speed_pct,
    describe_macro_light,
    latest_value,
    load_prev_color,
    to_detail_frame,
    to_panel_history_frame,
)

D0 = date(2026, 1, 1)


def _series(n: int, start_val: float, step: float, start: date = D0) -> pl.DataFrame:
    """自 start_val 起，每日 +step 的合成序列（連續遞增，方便驗證百分位性質）。"""
    dates = [start + timedelta(days=i) for i in range(n)]
    values = [start_val + step * i for i in range(n)]
    return pl.DataFrame(
        {"date": dates, "value": values}, schema={"date": pl.Date, "value": pl.Float64}
    )


def _series_with_null(n: int, start_val: float, step: float, null_at: int) -> pl.DataFrame:
    df = _series(n, start_val, step)
    values = df["value"].to_list()
    values[null_at] = None
    return df.with_columns(pl.Series("value", values, dtype=pl.Float64))


# ── level_pct / speed_pct / dual_risk ───────────────────────────────────────


def test_level_pct_monotonic_series_latest_is_top_percentile() -> None:
    df = _series(60, 100.0, 1.0)
    as_of = df["date"].max()
    pct = compute_level_pct(df, as_of, lookback_days=60)
    assert pct == 1.0  # 嚴格遞增序列，最新值＝窗內最大值


def test_level_pct_insufficient_history_returns_none() -> None:
    df = _series(10, 100.0, 1.0)  # < _MIN_OBS=30
    pct = compute_level_pct(df, df["date"].max(), lookback_days=60)
    assert pct is None


def test_level_pct_no_look_ahead() -> None:
    """look-ahead 迴歸測試：as_of 之後多附加的資料不能改變 as_of 當天的分數。"""
    df_60 = _series(60, 100.0, 1.0)
    as_of = D0 + timedelta(days=39)
    score_truncated = compute_level_pct(df_60.filter(pl.col("date") <= as_of), as_of, 60)

    # 附加 20 筆「未來」資料（含極端值，若有 look-ahead 會被污染）
    future = pl.DataFrame(
        {
            "date": [as_of + timedelta(days=i) for i in range(1, 21)],
            "value": [9999.0] * 20,
        },
        schema={"date": pl.Date, "value": pl.Float64},
    )
    df_with_future = pl.concat([df_60, future])
    score_with_future = compute_level_pct(df_with_future, as_of, 60)

    assert score_truncated == score_with_future


def test_level_pct_ignores_null_values() -> None:
    df = _series_with_null(60, 100.0, 1.0, null_at=59)  # 最新一筆為 null
    as_of = df["date"].max()
    # 最新一筆是 null → latest_value 會回退到前一筆非 null（見 compute_indicator_reading 測試）；
    # 這裡直接測 compute_level_pct 對「as_of 當天無值」時仍應忽略 null 列運算前面的窗
    pct = compute_level_pct(df, as_of, lookback_days=60)
    assert pct is not None  # 前面 59 筆非 null，仍夠 min_obs


def test_speed_pct_big_recent_jump_is_high_percentile() -> None:
    # 前 50 天持平，最後 10 天陡升 → 最新一筆的 20 日變化量應是窗內最大變化
    flat = [100.0] * 50
    jump = [100.0 + i * 5 for i in range(1, 11)]
    values = flat + jump
    dates = [D0 + timedelta(days=i) for i in range(60)]
    df = pl.DataFrame(
        {"date": dates, "value": values}, schema={"date": pl.Date, "value": pl.Float64}
    )
    pct = compute_speed_pct(df, df["date"].max(), lookback_days=60, delta_days=10)
    assert pct is not None
    assert pct > 0.9


def test_dual_risk_direction_agnostic() -> None:
    """急漲與急跌的 dual_risk 應相近（只看急動幅度，不看方向）。"""
    dates = [D0 + timedelta(days=i) for i in range(60)]
    up = pl.DataFrame(
        {"date": dates, "value": [100.0] * 50 + [100.0 + i * 5 for i in range(1, 11)]},
        schema={"date": pl.Date, "value": pl.Float64},
    )
    down = pl.DataFrame(
        {"date": dates, "value": [100.0] * 50 + [100.0 - i * 5 for i in range(1, 11)]},
        schema={"date": pl.Date, "value": pl.Float64},
    )
    risk_up = compute_dual_risk(up, up["date"].max(), lookback_days=60, delta_days=10)
    risk_down = compute_dual_risk(down, down["date"].max(), lookback_days=60, delta_days=10)
    assert risk_up is not None and risk_down is not None
    assert risk_up > 0.9  # 2×|p-0.5|，p∈[0,1]→dual_risk∈[0,1]，急動應貼近上界 1.0
    assert risk_down > 0.9


# ── latest_value / compute_indicator_reading ────────────────────────────────


def test_latest_value_skips_trailing_null() -> None:
    df = _series_with_null(10, 100.0, 1.0, null_at=9)
    value, d = latest_value(df, df["date"].max())
    assert value == 108.0  # 倒數第二筆（index 8）
    assert d == D0 + timedelta(days=8)


def test_compute_indicator_reading_stale() -> None:
    df = _series(60, 100.0, 1.0)  # 最新一筆 = D0+59
    today = D0 + timedelta(days=100)  # 距最新觀測超過 stale_days
    reading = compute_indicator_reading(df, "TEST", "level_pct", today, 60, 20, stale_days=10)
    assert reading.stale is True
    assert reading.score is None


def test_compute_indicator_reading_raw_transform_no_score() -> None:
    df = _series(60, 100.0, 1.0)
    today = df["date"].max()
    reading = compute_indicator_reading(df, "TEST", "raw", today, 60, 20, stale_days=10)
    assert reading.stale is False
    assert reading.raw_value is not None
    assert reading.score is None  # raw＝純揭露，不計分


# ── classify_light（燈色＋遲滯帶）───────────────────────────────────────────


def test_classify_light_no_memory_uses_base_thresholds() -> None:
    assert classify_light(50.0, None, green_max=60, red_min=80, hysteresis=3) == GREEN
    assert classify_light(70.0, None, green_max=60, red_min=80, hysteresis=3) == YELLOW
    assert classify_light(85.0, None, green_max=60, red_min=80, hysteresis=3) == RED
    assert classify_light(None, None, green_max=60, red_min=80, hysteresis=3) == INSUFFICIENT


def test_classify_light_hysteresis_holds_within_band() -> None:
    # 上週黃，這週 82（<83=red_min+hysteresis）→ 維持黃，不因剛過 80 就跳紅
    assert classify_light(82.0, YELLOW, green_max=60, red_min=80, hysteresis=3) == YELLOW
    # 這週 84（>=83）→ 真的突破，跳紅
    assert classify_light(84.0, YELLOW, green_max=60, red_min=80, hysteresis=3) == RED


def test_classify_light_hysteresis_red_to_yellow() -> None:
    # 上週紅，78（>=77=red_min-hysteresis）→ 維持紅
    assert classify_light(78.0, RED, green_max=60, red_min=80, hysteresis=3) == RED
    # 76（<77 但 >=57=green_max-hysteresis）→ 降到黃
    assert classify_light(76.0, RED, green_max=60, red_min=80, hysteresis=3) == YELLOW
    # 50（<57）→ 直接降到綠
    assert classify_light(50.0, RED, green_max=60, red_min=80, hysteresis=3) == GREEN


def test_classify_light_green_to_yellow_hysteresis() -> None:
    # 上週綠，62（<63=green_max+hysteresis）→ 維持綠
    assert classify_light(62.0, GREEN, green_max=60, red_min=80, hysteresis=3) == GREEN
    # 64（>=63）→ 跳黃
    assert classify_light(64.0, GREEN, green_max=60, red_min=80, hysteresis=3) == YELLOW


# ── compute_macro_light（端到端純函式）──────────────────────────────────────

CFG = {
    "primary_series": "BAA10Y",
    "primary_transform": "level_pct",
    "disclosure_series": {
        "DGS20": "level_pct",
        "VIXCLS": "speed_pct",
        "DCOILWTICO": "dual_risk",
        "DGS10": "raw",
    },
    "lookback_days": 60,
    "delta_days": 10,
    "stale_days": 10,
    "thresholds": {"green_max": 60, "red_min": 80},
    "hysteresis": 3,
}


def test_compute_macro_light_missing_primary_is_insufficient() -> None:
    light = compute_macro_light({}, CFG, D0 + timedelta(days=59), prev_color=None)
    assert light.color == INSUFFICIENT
    assert light.risk_score is None


def test_compute_macro_light_populates_disclosure_panel() -> None:
    series_data = {
        "BAA10Y": _series(60, 100.0, 1.0),
        "DGS20": _series(60, 50.0, 0.5),
        "VIXCLS": _series(60, 20.0, 0.2),
        "DCOILWTICO": _series(60, 70.0, -0.3),
        # DGS10 缺席 → 該欄 stale/未取得，不影響主訊號
    }
    today = D0 + timedelta(days=59)
    light = compute_macro_light(series_data, CFG, today, prev_color=None)
    assert light.color == RED  # 嚴格遞增序列 → level_pct=1.0 → risk_score=100 → 紅
    assert light.risk_score == 100.0
    assert len(light.disclosure) == 4
    dgs10 = next(d for d in light.disclosure if d.series_id == "DGS10")
    assert dgs10.stale is True  # 缺資料


def test_describe_macro_light_change_line() -> None:
    series_data = {"BAA10Y": _series(60, 100.0, 1.0)}
    today = D0 + timedelta(days=59)
    light = compute_macro_light(series_data, CFG, today, prev_color=GREEN)
    desc = describe_macro_light(light)
    assert desc["change_line"] == "綠 → 紅"
    assert "系統性風險" in desc["advice"]


def test_to_detail_frame_row_count() -> None:
    series_data = {"BAA10Y": _series(60, 100.0, 1.0)}
    today = D0 + timedelta(days=59)
    light = compute_macro_light(series_data, CFG, today, prev_color=None)
    detail = to_detail_frame(light)
    assert detail.height == 1 + len(CFG["disclosure_series"])  # primary + 揭露面板


# ── history.parquet round-trip ──────────────────────────────────────────────


def test_append_history_and_load_prev_color(tmp_path: Path) -> None:
    hpath = tmp_path / "macro_regime" / "history.parquet"
    series = _series(120, 100.0, 1.0)
    light = compute_macro_light(
        {"BAA10Y": series}, CFG, D0 + timedelta(days=59), prev_color=None
    )
    assert load_prev_color(hpath) is None  # 尚未寫入

    append_history(hpath, light)
    assert load_prev_color(hpath) == light.color

    # 同一天重跑（模擬手動重試/CLI 多跑一次）：冪等，不應重複寫入
    append_history(hpath, light)
    df = pl.read_parquet(hpath)
    assert df.height == 1

    # 不同一天（模擬下週再跑）：才是真的累積成 2 列
    light_next = compute_macro_light(
        {"BAA10Y": series}, CFG, D0 + timedelta(days=66), prev_color=light.color
    )
    append_history(hpath, light_next)
    df = pl.read_parquet(hpath)
    assert df.height == 2


# ── panel_history + 變化追蹤（docs/26 A案）─────────────────────────────────────

_PANEL_SERIES = {
    "BAA10Y": _series(120, 100.0, 1.0),  # level_pct=1.0 → score_pct=100
    "DGS10": _series(120, 4.0, 0.01),  # raw transform（不計分，退用相對變化）
}
_PANEL_TODAY = D0 + timedelta(days=119)


def _panel_light(prev_color: str | None = None) -> object:
    return compute_macro_light(_PANEL_SERIES, CFG, _PANEL_TODAY, prev_color=prev_color)


def _prev_panel_row(
    run_as_of: date,
    series_id: str,
    transform: str,
    raw_value: float | None,
    score_pct: float | None,
) -> dict:
    return {
        "run_as_of": run_as_of,
        "role": "primary" if series_id == CFG["primary_series"] else "disclosure",
        "series_id": series_id,
        "transform": transform,
        "as_of": run_as_of,
        "raw_value": raw_value,
        "score_pct": score_pct,
        "stale": False,
    }


def test_to_panel_history_frame_shape() -> None:
    light = _panel_light()
    frame = to_panel_history_frame(light)
    assert frame.height == 1 + len(CFG["disclosure_series"])
    assert "run_as_of" in frame.columns
    assert "source" not in frame.columns  # 明細 CSV 才需要來源欄，歷史檔不重複存常數
    assert frame["run_as_of"].unique().to_list() == [light.as_of]


def test_append_panel_history_idempotent_then_accumulates(tmp_path: Path) -> None:
    ppath = tmp_path / "macro_regime" / "panel_history.parquet"
    light = _panel_light()
    n_rows = 1 + len(CFG["disclosure_series"])

    append_panel_history(ppath, light)
    assert pl.read_parquet(ppath).height == n_rows

    # 同一輪重跑（make macro 手動多跑一次）：整批跳過，不得埋進重複列
    append_panel_history(ppath, light)
    assert pl.read_parquet(ppath).height == n_rows

    # 下一輪（序列真的有新觀測日）：才真的累積。注意只把 today 往後推是不夠的——
    # 沒有新觀測就是同一個 run_as_of，仍會被冪等擋下（這是刻意的：沒有新資料就不該新增一列）
    light_next = compute_macro_light(
        {"BAA10Y": _series(127, 100.0, 1.0), "DGS10": _series(127, 4.0, 0.01)},
        CFG,
        _PANEL_TODAY + timedelta(days=7),
        prev_color=light.color,
    )
    assert light_next.as_of != light.as_of
    append_panel_history(ppath, light_next)
    df = pl.read_parquet(ppath)
    assert df.height == 2 * n_rows
    assert df["run_as_of"].n_unique() == 2


def test_compute_panel_deltas_empty_history_is_no_prev() -> None:
    light = _panel_light()
    deltas = compute_panel_deltas(pl.DataFrame(), light, deadband_pct=2.0, deadband_rel=0.005)
    assert deltas["BAA10Y"].arrow == NO_PREV
    assert deltas["BAA10Y"].delta_score_pct is None


def test_compute_panel_deltas_ignores_own_run() -> None:
    """歷史裡只有本輪自己 → 仍是無前次（嚴格早於才算基準，append 順序不影響結果）。"""
    light = _panel_light()
    history = to_panel_history_frame(light)
    deltas = compute_panel_deltas(history, light, deadband_pct=2.0, deadband_rel=0.005)
    assert deltas["BAA10Y"].arrow == NO_PREV


def test_compute_panel_deltas_score_arrow_up_and_deadband() -> None:
    light = _panel_light()
    prev_run = _PANEL_TODAY - timedelta(days=7)

    rising = pl.DataFrame(
        [_prev_panel_row(prev_run, "BAA10Y", "level_pct", 200.0, 90.0)]
    )
    d = compute_panel_deltas(rising, light, deadband_pct=2.0, deadband_rel=0.005)["BAA10Y"]
    assert d.arrow == "↑"
    assert d.delta_score_pct == 10.0  # 90 → 100
    assert d.prev_as_of == prev_run

    flat = pl.DataFrame([_prev_panel_row(prev_run, "BAA10Y", "level_pct", 218.0, 99.0)])
    assert (
        compute_panel_deltas(flat, light, deadband_pct=2.0, deadband_rel=0.005)["BAA10Y"].arrow
        == "→"
    )  # Δ+1p 在 deadband 內＝持平，不亂標箭頭

    # ↓：本次水位低（遞減序列 → 當前值是窗內最小）而前次高
    light_low = compute_macro_light(
        {"BAA10Y": _series(120, 200.0, -1.0)}, CFG, _PANEL_TODAY, prev_color=None
    )
    assert light_low.risk_score is not None and light_low.risk_score < 5
    falling = pl.DataFrame([_prev_panel_row(prev_run, "BAA10Y", "level_pct", 150.0, 50.0)])
    assert (
        compute_panel_deltas(falling, light_low, deadband_pct=2.0, deadband_rel=0.005)[
            "BAA10Y"
        ].arrow
        == "↓"
    )


def test_compute_panel_deltas_raw_transform_uses_relative_deadband() -> None:
    """raw 揭露序列（無 score_pct）退用相對變化：單位各異，絕對 deadband 無意義。"""
    light = _panel_light()
    prev_run = _PANEL_TODAY - timedelta(days=7)
    cur_raw = next(d for d in light.disclosure if d.series_id == "DGS10").raw_value
    assert cur_raw is not None

    moved = pl.DataFrame([_prev_panel_row(prev_run, "DGS10", "raw", cur_raw * 0.9, None)])
    assert (
        compute_panel_deltas(moved, light, deadband_pct=2.0, deadband_rel=0.005)["DGS10"].arrow
        == "↑"
    )

    barely = pl.DataFrame([_prev_panel_row(prev_run, "DGS10", "raw", cur_raw * 0.999, None)])
    assert (
        compute_panel_deltas(barely, light, deadband_pct=2.0, deadband_rel=0.005)["DGS10"].arrow
        == "→"
    )


def test_compute_panel_deltas_stale_current_reading_is_no_prev() -> None:
    """本次未取得 → 不拿舊值算變化（鐵律 2：寧缺勿假）。"""
    light = compute_macro_light(_PANEL_SERIES, CFG, _PANEL_TODAY, prev_color=None)
    vix = next(d for d in light.disclosure if d.series_id == "VIXCLS")
    assert vix.stale is True  # VIXCLS 未在 _PANEL_SERIES 提供
    prev_run = _PANEL_TODAY - timedelta(days=7)
    history = pl.DataFrame([_prev_panel_row(prev_run, "VIXCLS", "speed_pct", 20.0, 30.0)])
    deltas = compute_panel_deltas(history, light, deadband_pct=2.0, deadband_rel=0.005)
    assert deltas["VIXCLS"].arrow == NO_PREV
    assert deltas["VIXCLS"].delta_score_pct is None


def test_describe_macro_light_delta_absent_when_not_supplied() -> None:
    """向後相容：不傳 deltas → 每個讀值的 delta 為 None，模板不渲染變化欄。"""
    desc = describe_macro_light(_panel_light())
    assert desc["primary"]["delta"] is None
    assert all(d["delta"] is None for d in desc["disclosure"])


def test_describe_macro_light_delta_rendered_fields() -> None:
    light = _panel_light()
    prev_run = _PANEL_TODAY - timedelta(days=7)
    history = pl.DataFrame([_prev_panel_row(prev_run, "BAA10Y", "level_pct", 200.0, 90.0)])
    deltas = compute_panel_deltas(history, light, deadband_pct=2.0, deadband_rel=0.005)
    desc = describe_macro_light(light, deltas)
    assert desc["primary"]["delta"]["arrow"] == "↑"
    assert desc["primary"]["delta"]["score_pct"] == 10.0
    assert desc["primary"]["delta"]["prev_as_of"] == prev_run.isoformat()
    # 揭露面板缺前次 → 老實顯示無前次，不用主訊號的基準去套
    assert desc["disclosure"][0]["delta"]["arrow"] == NO_PREV
