"""官方族群指數重測「族群前5」測試（docs/31 §10）。純函式合成資料，不打網、不碰真快取。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.official_sector_grid import (
    _INDUSTRY_TO_INDEX_ALIASES,
    _UNMAPPED_INDUSTRIES,
    FLOW_TRIGGER_CELLS,
    RANK_VELOCITY_CELLS,
    build_flow_triggers,
    build_hand_sector_baskets,
    build_hand_sector_membership,
    build_official_sector_baskets,
    build_official_sector_membership,
    compute_rank_velocity,
    compute_subindustry_purity,
    flow_trigger_by_regime,
    flow_trigger_grid,
    official_group_rank_grid,
    rank_velocity_by_regime,
    rank_velocity_grid,
    walk_forward_flow_trigger,
    walk_forward_rank_velocity,
    walk_forward_top5_grid,
)


def test_unmapped_industries_not_in_alias_table() -> None:
    """§10.2 排除清單跟映射表互斥——避免文件與程式碼各說各話。"""
    assert set(_UNMAPPED_INDUSTRIES).isdisjoint(_INDUSTRY_TO_INDEX_ALIASES)


def test_build_official_sector_membership_filters_unmapped() -> None:
    official = pl.DataFrame(
        {
            "sub_industry": ["半導體業", "半導體業", "其他製造業", "存託憑證"],
            "stock_id": ["2330", "2454", "9999", "0001"],
        }
    )
    out = build_official_sector_membership(official)
    assert set(out["stock_id"].to_list()) == {"2330", "2454"}
    assert set(out["sub_industry"].to_list()) == {"半導體業"}


def test_build_official_sector_membership_empty_input() -> None:
    empty = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    assert build_official_sector_membership(empty).is_empty()


def test_build_official_sector_baskets_maps_index_name_to_canonical() -> None:
    sector_index = pl.DataFrame(
        {
            "date": [date(2026, 8, 21), date(2026, 8, 21)],
            "index_name": ["半導體類指數", "光電類指數"],
            "close_index": [500.0, 300.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    row = baskets.filter(pl.col("sub_industry") == "半導體業").row(0, named=True)
    assert row["basket_index"] == 500.0
    assert set(baskets["sub_industry"].to_list()) == {"半導體業", "光電業"}


def test_build_official_sector_baskets_rename_fallback() -> None:
    """觀光跨期改名：新名（觀光餐旅類指數）優先，查無時退回舊名（觀光類指數）。"""
    sector_index = pl.DataFrame(
        {
            "date": [date(2021, 7, 1), date(2026, 8, 21)],
            "index_name": ["觀光類指數", "觀光餐旅類指數"],
            "close_index": [100.0, 200.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    got = {
        r["date"]: r["basket_index"]
        for r in baskets.filter(pl.col("sub_industry") == "觀光事業").iter_rows(named=True)
    }
    assert got == {date(2021, 7, 1): 100.0, date(2026, 8, 21): 200.0}


def test_build_official_sector_baskets_prefers_new_name_when_both_present() -> None:
    """同一天新舊名都存在（理論邊界情況）→ 取候選清單優先序較高的新名。"""
    sector_index = pl.DataFrame(
        {
            "date": [date(2026, 8, 21), date(2026, 8, 21)],
            "index_name": ["觀光類指數", "觀光餐旅類指數"],
            "close_index": [999.0, 200.0],
        }
    )
    baskets = build_official_sector_baskets(sector_index)
    row = baskets.filter(pl.col("sub_industry") == "觀光事業").row(0, named=True)
    assert row["basket_index"] == 200.0


def test_build_official_sector_baskets_empty_input() -> None:
    assert build_official_sector_baskets(
        pl.DataFrame(schema={"date": pl.Date, "index_name": pl.Utf8, "close_index": pl.Float64})
    ).is_empty()


def test_official_group_rank_grid_two_cells_only() -> None:
    """刻意只有2個cell（group_top5 True/False），不是G3的16格——確認結構正確。"""
    dates = [date(2026, 1, d) for d in (2, 3, 4, 5, 6)]
    rows = []
    for d in dates:
        for i, (sub, score) in enumerate(
            [("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 60.0), ("E", 50.0), ("F", 40.0)]
        ):
            rows.append(
                {
                    "date": d, "sub_industry": sub, "trend_score": score,
                    "alpha10": 1.0 if i < 5 else -1.0, "alpha20": 2.0 if i < 5 else -2.0,
                }
            )
    stock_rows = pl.DataFrame(rows)
    grid = official_group_rank_grid(stock_rows, horizons=(10, 20), top_n_groups=5, n_boot=50)
    assert set(grid["group_top5"].to_list()) == {True, False}
    assert set(grid["horizon"].to_list()) == {10, 20}
    assert grid.height == 4  # 2 horizons x 2 cells


def test_official_group_rank_grid_empty_when_missing_columns() -> None:
    grid = official_group_rank_grid(pl.DataFrame({"date": [date(2026, 1, 1)]}))
    assert grid.is_empty()


# ─── §10.6：手標46細分類 + purity-mapped 官方指數 ──────────────────────────────


def _hand_membership(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["sub_industry", "stock_id"], orient="row")


def _official_industry(rows: list[tuple[str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"stock_id": pl.Utf8, "industry_name": pl.Utf8}, orient="row"
    )


def test_compute_subindustry_purity_high_purity_group() -> None:
    hand = _hand_membership([("封測", "2330"), ("封測", "2454"), ("封測", "6488")])
    official = _official_industry(
        [("2330", "半導體業"), ("2454", "半導體業"), ("6488", "光電業")]
    )
    purity = compute_subindustry_purity(hand, official)
    row = purity.filter(pl.col("sub_industry") == "封測").row(0, named=True)
    assert row["dominant_official"] == "半導體業"
    assert row["n_match"] == 2
    assert row["n_total"] == 3
    assert row["purity"] == pytest.approx(2 / 3)


def test_compute_subindustry_purity_all_unmatched_is_zero_not_full() -> None:
    """全員industry_name缺席 → purity=0.0（不是100%）——避免「查無資料」誤讀成「完全純」。"""
    hand = _hand_membership([("安全監控", "1111"), ("安全監控", "2222")])
    official = _official_industry([("1111", None), ("2222", None)])
    purity = compute_subindustry_purity(hand, official)
    row = purity.filter(pl.col("sub_industry") == "安全監控").row(0, named=True)
    assert row["dominant_official"] is None
    assert row["purity"] == 0.0


def test_compute_subindustry_purity_empty_input() -> None:
    empty_hand = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    empty_official = pl.DataFrame(schema={"stock_id": pl.Utf8, "industry_name": pl.Utf8})
    assert compute_subindustry_purity(empty_hand, empty_official).is_empty()


def test_build_hand_sector_membership_filters_by_purity_threshold() -> None:
    hand = _hand_membership(
        [("封測", "2330"), ("封測", "2454"), ("太陽能", "9999"), ("太陽能", "8888")]
    )
    official = _official_industry(
        [("2330", "半導體業"), ("2454", "半導體業"), ("9999", "光電業"), ("8888", None)]
    )
    purity = compute_subindustry_purity(hand, official)
    out = build_hand_sector_membership(hand, purity, min_purity=0.7)
    assert set(out["sub_industry"].to_list()) == {"封測"}  # 太陽能 purity 0.5 < 0.7


def test_build_hand_sector_membership_excludes_unmapped_dominant() -> None:
    """多數官方類別本身查無 MI_INDEX 對應（§10.2排除清單）→ 即使 purity 100% 也不納入。"""
    hand = _hand_membership([("某組", "1111"), ("某組", "2222")])
    official = _official_industry([("1111", "軟體工業"), ("2222", "軟體工業")])
    purity = compute_subindustry_purity(hand, official)
    assert purity.row(0, named=True)["purity"] == 1.0
    out = build_hand_sector_membership(hand, purity, min_purity=0.7)
    assert out.is_empty()


def test_build_hand_sector_baskets_shares_index_across_groups() -> None:
    """封測/晶圓代工皆多數票落在半導體業 → 共用同一條 basket_index 數列（已知副作用）。"""
    hand = _hand_membership(
        [("封測", "2330"), ("晶圓代工", "2454")]
    )
    official = _official_industry([("2330", "半導體業"), ("2454", "半導體業")])
    purity = compute_subindustry_purity(hand, official)
    sector_index = pl.DataFrame(
        {"date": [date(2026, 8, 21)], "index_name": ["半導體類指數"], "close_index": [500.0]}
    )
    baskets = build_hand_sector_baskets(sector_index, purity, min_purity=0.7)
    values = {
        r["sub_industry"]: r["basket_index"] for r in baskets.iter_rows(named=True)
    }
    assert values == {"封測": 500.0, "晶圓代工": 500.0}


def test_build_hand_sector_baskets_empty_input() -> None:
    empty_purity = pl.DataFrame(schema={
        "sub_industry": pl.Utf8, "dominant_official": pl.Utf8,
        "n_total": pl.UInt32, "n_match": pl.UInt32, "purity": pl.Float64,
    })
    assert build_hand_sector_baskets(
        pl.DataFrame(schema={"date": pl.Date, "index_name": pl.Utf8, "close_index": pl.Float64}),
        empty_purity,
    ).is_empty()


# ─── walk_forward_top5_grid（docs/31 §10.13） ──────────────────────────────────


def _weekly_dates(n: int, start: date = date(2023, 1, 2)) -> list[date]:
    return [start + timedelta(weeks=i) for i in range(n)]


def _synthetic_walk_forward_inputs(n_weeks: int = 40) -> dict[str, pl.DataFrame]:
    """兩個100%純度群組（半導體業恆強於光電業），供walk_forward_top5_grid結構性測試
    （不主張統計顯著，只確認流程跑得通、schema正確）。"""
    dates = _weekly_dates(n_weeks)
    semis = ["2330", "2454", "2379"]
    optos = ["3008", "3406", "6116"]

    hand = pl.DataFrame(
        [("半導體業", s) for s in semis] + [("光電業", s) for s in optos],
        schema=["sub_industry", "stock_id"], orient="row",
    )
    official = pl.DataFrame(
        [(s, "半導體業") for s in semis] + [(s, "光電業") for s in optos],
        schema=["stock_id", "industry_name"], orient="row",
    )
    purity = compute_subindustry_purity(hand, official)

    sector_index_rows = []
    price_rows = []
    alpha_rows = []
    for i, d in enumerate(dates):
        sector_index_rows.append(
            {"date": d, "index_name": "半導體類指數", "close_index": 100.0 + i * 2.0}
        )
        sector_index_rows.append(
            {"date": d, "index_name": "光電類指數", "close_index": 100.0 + i * 0.2}
        )
        for s in semis:
            price_rows.append({"date": d, "stock_id": s, "close": 100.0 + i * 2.0})
            alpha_rows.append({"date": d, "stock_id": s, "alpha10": 1.0})
        for s in optos:
            price_rows.append({"date": d, "stock_id": s, "close": 100.0 + i * 0.2})
            alpha_rows.append({"date": d, "stock_id": s, "alpha10": -1.0})

    sector_index_schema = {"date": pl.Date, "index_name": pl.Utf8, "close_index": pl.Float64}
    sector_index = pl.DataFrame(sector_index_rows, schema=sector_index_schema)
    price_schema = {"date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64}
    price = pl.DataFrame(price_rows, schema=price_schema)
    panel_alpha = pl.DataFrame(
        alpha_rows, schema={"date": pl.Date, "stock_id": pl.Utf8, "alpha10": pl.Float64}
    )
    return {
        "hand": hand, "purity": purity, "sector_index": sector_index,
        "price": price, "panel_alpha": panel_alpha,
    }


def test_walk_forward_top5_grid_runs_and_has_expected_schema() -> None:
    data = _synthetic_walk_forward_inputs(n_weeks=40)
    out = walk_forward_top5_grid(
        data["hand"], data["purity"], data["sector_index"], data["price"], data["panel_alpha"],
        purity_thresholds=(0.5, 1.0), horizons=(10,), top_n_groups=1, n_splits=2,
        min_train_frac=0.4, n_boot=50, select_n_boot=20,
    )
    assert set(out.columns) == {
        "horizon", "split_id", "train_end", "test_start", "test_end", "chosen_purity",
        "train_delta_mean", "test_n", "test_n_dates", "test_delta_mean",
        "test_ci_lo", "test_ci_hi",
    }
    assert not out.is_empty()
    assert set(out["horizon"].to_list()) == {10}
    # 半導體業恆強於光電業，前5(top_n_groups預設5，只有2組全入)理論上該偏正——結構性
    # 檢查方向，不主張統計顯著（合成資料本來就是為了測流程，不是測效應量）
    assert (out["test_delta_mean"] > 0).all()


def test_walk_forward_top5_grid_empty_inputs() -> None:
    empty_hand = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    empty_purity = pl.DataFrame(schema={
        "sub_industry": pl.Utf8, "dominant_official": pl.Utf8,
        "n_total": pl.UInt32, "n_match": pl.UInt32, "purity": pl.Float64,
    })
    empty_sector_index = pl.DataFrame(
        schema={"date": pl.Date, "index_name": pl.Utf8, "close_index": pl.Float64}
    )
    empty_price = pl.DataFrame(schema={"date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64})
    empty_alpha = pl.DataFrame(schema={"date": pl.Date, "stock_id": pl.Utf8, "alpha10": pl.Float64})
    out = walk_forward_top5_grid(
        empty_hand, empty_purity, empty_sector_index, empty_price, empty_alpha
    )
    assert out.is_empty()


def test_walk_forward_top5_grid_too_few_weeks_returns_empty() -> None:
    """週數不足以切出任何有效split → 回空表，不硬切。"""
    data = _synthetic_walk_forward_inputs(n_weeks=5)
    out = walk_forward_top5_grid(
        data["hand"], data["purity"], data["sector_index"], data["price"], data["panel_alpha"],
        purity_thresholds=(1.0,), horizons=(10,), n_splits=4, min_train_frac=0.4,
    )
    assert out.is_empty()


# ─── rank_velocity（docs/31 §14：提早卡位訊號） ─────────────────────────────────


def _rank_velocity_fixture() -> pl.DataFrame:
    """3群組×6週：A排名從落後(3)在d2~d3之間急速爬升到第1，C同期急速下滑，B恆定第2——
    可用來檢查 rank_velocity 的方向與lag=2快照的計算是否正確。"""
    dates = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(6)]
    scores = {
        "A": [10, 20, 30, 80, 90, 95],
        "B": [50, 50, 50, 50, 50, 50],
        "C": [100, 90, 80, 20, 10, 5],
    }
    rows = []
    for i, d in enumerate(dates):
        for sub, s in scores.items():
            rows.append({"date": d, "sub_industry": sub, "trend_score": float(s[i])})
    return pl.DataFrame(rows)


def test_compute_rank_velocity_direction_and_lag() -> None:
    base = _rank_velocity_fixture()
    out = compute_rank_velocity(base, lag_snapshots=2)
    dates = sorted(base["date"].unique().to_list())
    # 前2個快照（d0/d1）沒有可比對的t-2快照，該drop不出現，不外插
    assert set(out["date"].unique().to_list()) == set(dates[2:])
    # A 在 d1(rank3) → d3(rank1)：velocity = 3-1 = +2（急速爬升）
    a_d3 = out.filter((pl.col("date") == dates[3]) & (pl.col("sub_industry") == "A"))
    assert a_d3.row(0, named=True)["rank_velocity"] == 2.0
    # C 在 d1(rank1) → d3(rank3)：velocity = 1-3 = -2（急速下滑）
    c_d3 = out.filter((pl.col("date") == dates[3]) & (pl.col("sub_industry") == "C"))
    assert c_d3.row(0, named=True)["rank_velocity"] == -2.0
    # B 恆定第2名：velocity 全程為 0
    assert (out.filter(pl.col("sub_industry") == "B")["rank_velocity"] == 0.0).all()


def test_compute_rank_velocity_empty_when_missing_columns() -> None:
    assert compute_rank_velocity(pl.DataFrame({"date": [date(2026, 1, 1)]})).is_empty()


def test_compute_rank_velocity_too_few_snapshots_returns_empty() -> None:
    base = _rank_velocity_fixture().filter(pl.col("date") <= date(2026, 1, 12))  # 只2週
    assert compute_rank_velocity(base, lag_snapshots=2).is_empty()


def test_rank_velocity_grid_schema_and_cells() -> None:
    base = _rank_velocity_fixture().with_columns(pl.lit(1.0).alias("alpha10"))
    grid = rank_velocity_grid(base, horizons=(10,), top_n_groups=1, n_boot=20)
    assert set(grid.columns) == {
        "horizon", "cell", "n", "n_dates", "mean", "median", "win_rate",
        "delta_mean", "ci_lo", "ci_hi", "mean_h1", "mean_h2",
    }
    assert set(grid["cell"].to_list()).issubset(set(RANK_VELOCITY_CELLS))
    assert not grid.is_empty()


def test_rank_velocity_grid_empty_when_missing_columns() -> None:
    assert rank_velocity_grid(pl.DataFrame({"date": [date(2026, 1, 1)]})).is_empty()


def test_rank_velocity_grid_empty_when_no_alpha_column() -> None:
    grid = rank_velocity_grid(_rank_velocity_fixture(), horizons=(10,))
    assert grid.is_empty()


def test_rank_velocity_by_regime_runs() -> None:
    base = _rank_velocity_fixture().with_columns(
        pl.lit(1.0).alias("alpha10"), pl.lit("進攻").alias("regime")
    )
    out = rank_velocity_by_regime(base, horizons=(10,), top_n_groups=1, n_boot=20)
    assert set(out.columns) == {
        "horizon", "cell", "regime", "n", "n_dates", "mean", "ci_lo", "ci_hi", "thin",
    }
    assert not out.is_empty()


def test_rank_velocity_by_regime_empty_without_regime_column() -> None:
    base = _rank_velocity_fixture().with_columns(pl.lit(1.0).alias("alpha10"))
    assert rank_velocity_by_regime(base, horizons=(10,)).is_empty()


def _rank_velocity_long_fixture(n_weeks: int = 40) -> pl.DataFrame:
    """40週合成資料，供walk_forward_rank_velocity結構性測試（同walk_forward_top5_grid
    慣例：只確認流程/schema正確，不主張統計顯著）。"""
    dates = [date(2023, 1, 2) + timedelta(weeks=i) for i in range(n_weeks)]
    rows = []
    for i, d in enumerate(dates):
        # A/B/C排名逐週循環輪動，製造持續的rank_velocity變化
        cyc = i % 3
        order = ["A", "B", "C"][cyc:] + ["A", "B", "C"][:cyc]
        for sub, s in zip(order, [90.0, 60.0, 30.0], strict=True):
            rows.append(
                {"date": d, "sub_industry": sub, "trend_score": s, "alpha10": 1.0}
            )
    return pl.DataFrame(rows)


def test_walk_forward_rank_velocity_schema() -> None:
    data = _rank_velocity_long_fixture(n_weeks=40)
    out = walk_forward_rank_velocity(
        data, horizons=(10,), top_n_groups=1, n_splits=2, min_train_frac=0.4, n_boot=20,
    )
    assert set(out.columns) == {
        "horizon", "cell", "split_id", "test_start", "test_end",
        "test_n", "test_n_dates", "test_delta_mean", "test_ci_lo", "test_ci_hi",
    }


def test_walk_forward_rank_velocity_empty_inputs() -> None:
    assert walk_forward_rank_velocity(pl.DataFrame({"date": [date(2026, 1, 1)]})).is_empty()


# ─── flow_trigger（docs/31 §17：★投信流校準訊號能否預測未進前5族群的forward報酬） ──


def _institutional_daily_fixture(n_days: int = 150) -> pl.DataFrame:
    """A族群在第70-100天有一段持續投信買超（觸發★訊號），B族群全程平靜。"""
    dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(dates):
        if d.weekday() >= 5:
            continue
        for sub, stock in [("A", "1111"), ("B", "2222")]:
            trust = 500_000 if (sub == "A" and 70 <= i <= 100) else 0
            rows.append(
                {
                    "date": d, "stock_id": stock, "foreign_net": 0,
                    "trust_net": trust, "dealer_net": 0,
                }
            )
    return pl.DataFrame(rows)


def _flow_trigger_membership() -> pl.DataFrame:
    return pl.DataFrame(
        [("A", "1111"), ("B", "2222")], schema=["sub_industry", "stock_id"], orient="row"
    )


def test_build_flow_triggers_fires_only_for_bursting_group() -> None:
    inst = _institutional_daily_fixture()
    triggers = build_flow_triggers(
        inst, _flow_trigger_membership(), z_window=30, z_min_periods=15
    )
    assert not triggers.is_empty()
    assert set(triggers["sub_industry"].to_list()) == {"A"}


def test_build_flow_triggers_empty_when_missing_columns() -> None:
    assert build_flow_triggers(
        pl.DataFrame({"date": [date(2026, 1, 1)]}), _flow_trigger_membership()
    ).is_empty()


def test_build_flow_triggers_empty_when_membership_empty() -> None:
    inst = _institutional_daily_fixture()
    assert build_flow_triggers(
        inst, pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})
    ).is_empty()


def _flow_trigger_stock_rows(n_weeks: int = 10) -> pl.DataFrame:
    dates = [date(2023, 1, 2) + timedelta(weeks=i) for i in range(n_weeks)]
    rows = []
    for d in dates:
        for sub, score in [("A", 30.0), ("B", 50.0), ("C", 10.0)]:
            rows.append(
                {
                    "date": d, "sub_industry": sub, "trend_score": score,
                    "alpha10": 1.0 if sub == "A" else 0.0,
                }
            )
    return pl.DataFrame(rows)


def _flow_trigger_daily_calendar(n_days: int = 70) -> list:
    return [date(2023, 1, 2) + timedelta(days=i) for i in range(n_days)]


def _single_trigger(sub: str, d) -> pl.DataFrame:
    return pl.DataFrame(
        {"sub_industry": [sub], "date": [d]}, schema={"sub_industry": pl.Utf8, "date": pl.Date}
    )


def test_flow_trigger_grid_three_cells_and_schema() -> None:
    """B恆居第1名(top5參照)；A排名第2(未進前5=top_n_groups1)，觸發後一段窗口內
    標記not_top5_triggered，其餘標not_top5_untriggered——3個cell皆須出現。"""
    stock_rows = _flow_trigger_stock_rows()
    daily_dates = _flow_trigger_daily_calendar()
    trig_date = date(2023, 1, 2) + timedelta(days=20)
    triggers = _single_trigger("A", trig_date)
    grid = flow_trigger_grid(
        stock_rows, triggers, daily_dates, horizons=(10,), top_n_groups=1,
        lookback_window=15, n_boot=20,
    )
    assert set(grid.columns) == {
        "horizon", "cell", "n", "n_dates", "mean", "median", "win_rate",
        "delta_mean", "ci_lo", "ci_hi", "mean_h1", "mean_h2",
    }
    assert set(grid["cell"].to_list()) == set(FLOW_TRIGGER_CELLS)
    # A是唯一觸發過的族群，其alpha10恆為1.0——觸發格應全部來自A
    triggered_row = grid.filter(pl.col("cell") == "not_top5_triggered").row(0, named=True)
    assert triggered_row["mean"] == 1.0


def test_flow_trigger_grid_no_triggers_all_untriggered() -> None:
    stock_rows = _flow_trigger_stock_rows()
    daily_dates = _flow_trigger_daily_calendar()
    empty_triggers = pl.DataFrame(schema={"sub_industry": pl.Utf8, "date": pl.Date})
    grid = flow_trigger_grid(
        stock_rows, empty_triggers, daily_dates, horizons=(10,), top_n_groups=1, n_boot=20,
    )
    assert "not_top5_triggered" not in grid["cell"].to_list()
    assert "not_top5_untriggered" in grid["cell"].to_list()


def test_flow_trigger_grid_empty_when_missing_columns() -> None:
    empty_triggers = pl.DataFrame(schema={"sub_industry": pl.Utf8, "date": pl.Date})
    assert flow_trigger_grid(
        pl.DataFrame({"date": [date(2026, 1, 1)]}), empty_triggers, []
    ).is_empty()


def test_flow_trigger_by_regime_runs() -> None:
    stock_rows = _flow_trigger_stock_rows().with_columns(pl.lit("進攻").alias("regime"))
    daily_dates = _flow_trigger_daily_calendar()
    trig_date = date(2023, 1, 2) + timedelta(days=20)
    triggers = _single_trigger("A", trig_date)
    out = flow_trigger_by_regime(
        stock_rows, triggers, daily_dates, horizons=(10,), top_n_groups=1,
        lookback_window=15, n_boot=20,
    )
    assert set(out.columns) == {
        "horizon", "cell", "regime", "n", "n_dates", "mean", "ci_lo", "ci_hi", "thin",
    }
    assert not out.is_empty()


def test_flow_trigger_by_regime_empty_without_regime_column() -> None:
    stock_rows = _flow_trigger_stock_rows()
    daily_dates = _flow_trigger_daily_calendar()
    empty_triggers = pl.DataFrame(schema={"sub_industry": pl.Utf8, "date": pl.Date})
    assert flow_trigger_by_regime(
        stock_rows, empty_triggers, daily_dates, horizons=(10,)
    ).is_empty()


def _flow_trigger_long_fixture(n_weeks: int = 40) -> pl.DataFrame:
    dates = [date(2023, 1, 2) + timedelta(weeks=i) for i in range(n_weeks)]
    rows = []
    for i, d in enumerate(dates):
        cyc = i % 3
        order = ["A", "B", "C"][cyc:] + ["A", "B", "C"][:cyc]
        for sub, s in zip(order, [90.0, 60.0, 30.0], strict=True):
            rows.append(
                {"date": d, "sub_industry": sub, "trend_score": s, "alpha10": 1.0}
            )
    return pl.DataFrame(rows)


def test_walk_forward_flow_trigger_schema() -> None:
    stock_rows = _flow_trigger_long_fixture()
    daily_dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(300)]
    triggers = _single_trigger("A", date(2023, 3, 1))
    out = walk_forward_flow_trigger(
        stock_rows, triggers, daily_dates, horizons=(10,), top_n_groups=1,
        n_splits=2, min_train_frac=0.4, n_boot=20,
    )
    assert set(out.columns) == {
        "horizon", "cell", "split_id", "test_start", "test_end",
        "test_n", "test_n_dates", "test_delta_mean", "test_ci_lo", "test_ci_hi",
    }


def test_walk_forward_flow_trigger_empty_inputs() -> None:
    empty_triggers = pl.DataFrame(schema={"sub_industry": pl.Utf8, "date": pl.Date})
    assert walk_forward_flow_trigger(
        pl.DataFrame({"date": [date(2026, 1, 1)]}), empty_triggers, []
    ).is_empty()
