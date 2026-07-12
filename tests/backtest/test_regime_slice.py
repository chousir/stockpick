"""WS-H.4b regime 切片機制測試（regime_slice.py）：全離線合成資料。

驗收三件套：(1) regime 檔缺 → 跳過不炸；(2) 合成三 regime 事件 → 切片 n 與同向數正確；
(3) trigger_outcomes 與 evaluate_triggers 同判定（事件級鏡射不漂移）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from tw_screener.backtest.regime_slice import (
    UNLABELED,
    block_len_for_horizon,
    load_regime_labels,
    regime_base_rates,
    regime_slice_table,
    render_regime_slice_section,
    trigger_outcomes,
)
from tw_screener.backtest.rotation_calib import compute_base_rate, evaluate_triggers

D0 = date(2024, 1, 2)


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _episodes(key: str, ds: list[date]) -> pl.DataFrame:
    return pl.DataFrame(
        {"sub_industry": [key] * len(ds), "start_date": ds},
        schema={"sub_industry": pl.Utf8, "start_date": pl.Date},
    )


# ── block 長規格 ──────────────────────────────────────────────────────────────


def test_block_len_daily_and_weekly():
    assert block_len_for_horizon(15) == 16  # 日頻 h+1
    assert block_len_for_horizon(12, weekly=True) == 4  # ceil(12/5)+1


# ── (1) regime 檔缺 → 空表、段落誠實跳過、不炸 ─────────────────────────────────


def test_load_regime_labels_missing_file_returns_empty(tmp_path: Path):
    assert load_regime_labels(tmp_path / "nope.parquet").is_empty()


def test_load_regime_labels_wrong_columns_returns_empty(tmp_path: Path):
    p = tmp_path / "bad.parquet"
    pl.DataFrame({"foo": [1]}).write_parquet(p)
    assert load_regime_labels(p).is_empty()


def test_missing_regime_labels_all_unlabeled_no_verdict():
    cal = _dates(60)
    outcomes = pl.DataFrame(
        {
            "sub_industry": ["A"] * 20,
            "date": cal[:20],
            "hit": [True] * 10 + [False] * 10,
        }
    )
    empty_labels = pl.DataFrame(
        schema={"date": pl.Date, "regime_label": pl.Utf8}
    )
    table, verdict = regime_slice_table(
        outcomes, empty_labels, {UNLABELED: 0.5}, horizon_days=15
    )
    assert table.height == 1 and table["regime"][0] == UNLABELED
    assert verdict == "樣本不足"  # 未標桶不進裁決
    section = render_regime_slice_section(
        "## regime 切片（WS-H.4b）", table, verdict, "span", 15, "membership"
    )
    text = "\n".join(section)
    assert "未標" in text and "樣本期間" in text  # 照列＋footer 三行仍在


def test_empty_outcomes_section_skips_honestly():
    table, verdict = regime_slice_table(
        pl.DataFrame(schema={"sub_industry": pl.Utf8, "date": pl.Date, "hit": pl.Boolean}),
        pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8}),
        {},
        horizon_days=15,
    )
    assert table.is_empty() and verdict == "樣本不足"
    section = render_regime_slice_section("## X", table, verdict, "—", 15, "m")
    assert any("誠實跳過" in ln for ln in section)


# ── (2) 合成三 regime 事件：切片 n 與同向數正確 ────────────────────────────────


def _three_regime_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    """120 日曆：0-39 進攻、40-79 中性、80-119 防禦；每日一事件。

    進攻/中性全命中、防禦全未命中 → 同向數 2（lift>1）＋防禦反向。
    """
    cal = _dates(120)
    labels = pl.DataFrame(
        {
            "date": cal,
            "regime_label": ["進攻"] * 40 + ["中性"] * 40 + ["防禦"] * 40,
        }
    )
    outcomes = pl.DataFrame(
        {
            "sub_industry": ["A"] * 120,
            "date": cal,
            "hit": [True] * 80 + [False] * 40,
        }
    )
    return outcomes, labels


def test_three_regime_slice_counts_and_direction():
    outcomes, labels = _three_regime_fixture()
    base_rates = {"進攻": 0.5, "中性": 0.5, "防禦": 0.5}
    table, verdict = regime_slice_table(outcomes, labels, base_rates, horizon_days=15)
    by = {r["regime"]: r for r in table.iter_rows(named=True)}
    assert set(by) == {"進攻", "中性", "防禦"}
    assert by["進攻"]["n_events"] == 40 and by["防禦"]["n_events"] == 40
    assert by["進攻"]["lift"] == 2.0 and by["中性"]["lift"] == 2.0  # 1.0/0.5
    assert by["防禦"]["lift"] == 0.0
    # ≥2 桶 lift>1 同向 → 跨 regime 穩健；防禦反向照列
    assert verdict.startswith("跨 regime 穩健")
    assert "lift>1 同向" in verdict and "防禦" in verdict
    # 每桶 40 日 ≥10 → bs_CI 可算（常數序列 → CI 緊貼點估計）
    assert by["進攻"]["bs_ci95_lo"] is not None


def test_bull_only_when_only_attack_qualified():
    cal = _dates(120)
    labels = pl.DataFrame(
        {"date": cal, "regime_label": ["進攻"] * 40 + ["中性"] * 40 + ["防禦"] * 40}
    )
    # 進攻 40 事件全命中；中性僅 10 事件（<30 樣本不足，不進裁決）
    outcomes = pl.DataFrame(
        {
            "sub_industry": ["A"] * 50,
            "date": cal[:40] + cal[40:50],
            "hit": [True] * 40 + [True] * 10,
        }
    )
    table, verdict = regime_slice_table(
        outcomes, labels, {"進攻": 0.5, "中性": 0.5}, horizon_days=15
    )
    assert verdict == "bull-only"
    by = {r["regime"]: r for r in table.iter_rows(named=True)}
    assert by["中性"]["n_events"] == 10  # 樣本不足桶照列


def test_all_buckets_below_min_n_is_insufficient():
    cal = _dates(30)
    labels = pl.DataFrame({"date": cal, "regime_label": ["進攻"] * 15 + ["防禦"] * 15})
    outcomes = pl.DataFrame(
        {"sub_industry": ["A"] * 10, "date": cal[:10], "hit": [True] * 10}
    )
    table, verdict = regime_slice_table(outcomes, labels, {"進攻": 0.5}, horizon_days=15)
    assert verdict == "樣本不足"
    assert table.height == 1  # 只有出現過的桶照列


# ── (3) trigger_outcomes ＝ evaluate_triggers 的事件級鏡射（同判定不漂移）──────


def test_trigger_outcomes_mirrors_evaluate_triggers():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    triggers = pl.DataFrame(
        {
            "sub_industry": ["A"] * 4,
            "date": [cal[45], cal[10], cal[55], cal[95]],
            # 45=命中(領先5)、10=誤報、55=occupy 剔除、95=尾端剔除
        }
    )
    outcomes = trigger_outcomes(triggers, episodes, cal, lead_window=15, occupy_days=15)
    stats = evaluate_triggers(triggers, episodes, cal, lead_window=15, occupy_days=15)
    assert outcomes.height == stats["n_triggers"] == 2
    assert outcomes["hit"].sum() == stats["hits"] == 1
    assert set(outcomes["date"].to_list()) == {cal[45], cal[10]}


def test_regime_base_rates_unlabeled_equals_global():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    empty_labels = pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8})
    rates = regime_base_rates(episodes, cal, {"A"}, empty_labels, 15, 15, 0)
    global_rate = compute_base_rate(episodes, cal, {"A"}, 15, 15, 0)
    assert set(rates) == {UNLABELED}
    assert abs(rates[UNLABELED] - global_rate) < 1e-12


def test_regime_base_rates_split_by_day_label():
    cal = _dates(100)
    episodes = _episodes("A", [cal[50]])
    labels = pl.DataFrame(
        {"date": cal, "regime_label": ["進攻"] * 50 + ["防禦"] * 50}
    )
    rates = regime_base_rates(episodes, cal, {"A"}, labels, 15, 15, 0)
    # 起漲點在 pos50（防禦首日）：命中窗 [35,50] 橫跨兩桶；兩桶皆應有合格日
    assert set(rates) == {"進攻", "防禦"}
    assert rates["進攻"] > 0  # pos35-49（進攻日）可攔截 pos50 事件
