"""pick 閉環計算測試（規劃書 05 F1 PO2–PO4）。

合成 picks／excluded／價格序列，驗算 到期快照（entry 次日、除息加回、市場中位、
族群超額、路徑回撤）、分層彙總、偽陰性帳、翻轉解剖。不打網、不碰真快取。
"""

from __future__ import annotations

import datetime as _dt
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import typer
import yaml

from tw_screener.backtest.picks_outcome import (
    compute_todate_returns,
    counterfactual_summary,
    layer_summary,
    median_window_return,
    render_outcome_report,
    render_weekly_brief,
    week_over_week_diff,
    weekly_layer_table,
)
from tw_screener.backtest.picks_outcome_runner import run_picks_brief
from tw_screener.report.pick_store import upsert_pick


def _price_series(stock_id: str, start: date, closes: list[float]) -> pl.DataFrame:
    dates = []
    d = start
    while len(dates) < len(closes):
        if d.weekday() < 5:
            dates.append(d)
        d += _dt.timedelta(days=1)
    return pl.DataFrame(
        {"date": dates, "stock_id": [stock_id] * len(closes), "close": closes}
    )


def _picks(rows: list[dict]) -> pl.DataFrame:
    base = {
        "week": "2026-W21",
        "data_date": date(2026, 5, 4),  # 週一
        "sub_industry": None,
        "layer": "core",
        "ext_ma60_pct": None,
    }
    return pl.DataFrame([{**base, **r} for r in rows]).with_columns(
        pl.col("data_date").cast(pl.Date)
    )


# ─── compute_todate_returns ───────────────────────────────────────────────────


def test_todate_entry_next_day_exit_last_close_and_market_median():
    # 目標股 100→110（entry 週二 100、exit 最後日 110 = +10%）；市場兩檔持平 → 中位 0%
    target = _price_series("2610", date(2026, 5, 4), [95, 100, 105, 102, 108, 110])
    flat1 = _price_series("f1", date(2026, 5, 4), [50] * 6)
    flat2 = _price_series("f2", date(2026, 5, 4), [80] * 6)
    px = pl.concat([target, flat1, flat2])
    picks = _picks([{"stock_id": "2610", "name": "華航"}])
    out = compute_todate_returns(picks, px)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["status"] == "matured"
    assert row["entry_date"] == date(2026, 5, 5)  # 次一交易日，防前視
    assert row["entry_price"] == 100.0
    assert row["exit_price"] == 110.0
    assert abs(row["return_pct"] - 10.0) < 1e-6
    # 市場中位：2610 +10%、f1 0%、f2 0% → 中位 0%；α = +10pp
    assert abs(row["market_return_pct"] - 0.0) < 1e-6
    assert abs(row["alpha_market_pct"] - 10.0) < 1e-6
    # 路徑最深回撤：窗內最低收盤 100（entry 當日）→ 0%…實際 102 為窗內低於 entry？
    # 收盤序列 entry 起 [100,105,102,108,110]，min=100 → path_min = 0%
    assert abs(row["path_min_return_pct"] - 0.0) < 1e-6


def test_todate_exit_date_cutoff_and_path_drawdown():
    # exit_date 截在第 4 交易日（102）；路徑 min=98 → 回撤 −2%
    target = _price_series("2610", date(2026, 5, 4), [95, 100, 98, 102, 120, 130])
    px = pl.concat([target, _price_series("f1", date(2026, 5, 4), [50] * 6)])
    picks = _picks([{"stock_id": "2610", "name": "華航"}])
    out = compute_todate_returns(picks, px, exit_date=date(2026, 5, 7))
    row = out.row(0, named=True)
    assert row["exit_date"] == date(2026, 5, 7)
    assert abs(row["return_pct"] - 2.0) < 1e-6
    assert abs(row["path_min_return_pct"] - (-2.0)) < 1e-6


def test_todate_dividend_addback_recorded_separately():
    # 除息 5 元落在 (entry, exit] → return 加回、div_addback_pct 揭露（基準不加回）
    target = _price_series("2603", date(2026, 5, 4), [100, 100, 99, 97, 96, 95])
    px = pl.concat([target, _price_series("f1", date(2026, 5, 4), [50] * 6)])
    divs = pl.DataFrame(
        {"stock_id": ["2603"], "ex_date": [date(2026, 5, 7)], "cash_dividend": [5.0]}
    )
    picks = _picks([{"stock_id": "2603", "name": "長榮"}])
    out = compute_todate_returns(picks, px, dividends=divs)
    row = out.row(0, named=True)
    assert abs(row["div_addback_pct"] - 5.0) < 1e-6
    assert abs(row["return_pct"] - 0.0) < 1e-6  # −5% 價損 + 5% 股利


def test_todate_subindustry_alpha_with_membership():
    # 族群三檔：成員漲 +20/+10/0 → 中位 +10%；pick 本身 +20 → 族群 α +10pp
    a = _price_series("A", date(2026, 5, 4), [100, 100, 110, 115, 118, 120])
    b = _price_series("B", date(2026, 5, 4), [100, 100, 104, 106, 108, 110])
    c = _price_series("C", date(2026, 5, 4), [100, 100, 100, 100, 100, 100])
    other = _price_series("Z", date(2026, 5, 4), [50] * 6)
    px = pl.concat([a, b, c, other])
    membership = pl.DataFrame(
        {"sub_industry": ["航空", "航空", "航空"], "stock_id": ["A", "B", "C"]}
    )
    picks = _picks([{"stock_id": "A", "name": "甲", "sub_industry": "航空"}])
    out = compute_todate_returns(picks, px, membership=membership, min_subind_members=3)
    row = out.row(0, named=True)
    assert abs(row["return_pct"] - 20.0) < 1e-6
    assert row["subind_n"] == 3
    assert abs(row["subind_return_pct"] - 10.0) < 1e-6
    assert abs(row["alpha_subind_pct"] - 10.0) < 1e-6


def test_todate_subindustry_below_min_members_null():
    a = _price_series("A", date(2026, 5, 4), [100, 100, 120, 120, 120, 120])
    px = pl.concat([a, _price_series("Z", date(2026, 5, 4), [50] * 6)])
    membership = pl.DataFrame({"sub_industry": ["航空"], "stock_id": ["A"]})
    picks = _picks([{"stock_id": "A", "name": "甲", "sub_industry": "航空"}])
    out = compute_todate_returns(picks, px, membership=membership, min_subind_members=3)
    row = out.row(0, named=True)
    assert row["alpha_subind_pct"] is None


def test_todate_no_entry_when_data_date_beyond_cache():
    px = _price_series("A", date(2026, 5, 4), [100, 100, 100])
    picks = _picks([{"stock_id": "A", "name": "甲", "data_date": date(2026, 6, 1)}])
    out = compute_todate_returns(picks, px)
    assert out.row(0, named=True)["status"] == "no_entry"


def test_median_window_return_requires_both_endpoints():
    a = _price_series("A", date(2026, 5, 4), [100, 110, 120])
    b = _price_series("B", date(2026, 5, 4), [100, 100])  # 缺 exit 端 → 剔除
    px = pl.concat([a, b])
    med, n = median_window_return(px, date(2026, 5, 4), date(2026, 5, 6))
    assert n == 1
    assert abs(med - 20.0) < 1e-6


# ─── 彙總層 ───────────────────────────────────────────────────────────────────


def _todate_frame(rows: list[dict]) -> pl.DataFrame:
    base = {
        "week": "2026-W21",
        "stock_id": "X",
        "name": "X",
        "layer": "core",
        "return_pct": 0.0,
        "alpha_market_pct": 0.0,
        "market_return_pct": 0.0,
        "alpha_subind_pct": None,
        "path_min_return_pct": -1.0,
        "status": "matured",
    }
    return pl.DataFrame([{**base, **r} for r in rows], infer_schema_length=None)


def test_layer_summary_and_weekly_table():
    todate = _todate_frame([
        {"stock_id": "a", "return_pct": 10.0, "alpha_market_pct": 8.0},
        {"stock_id": "b", "return_pct": -10.0, "alpha_market_pct": -12.0,
         "path_min_return_pct": -15.0},
        {"stock_id": "c", "layer": "pool", "return_pct": 5.0, "alpha_market_pct": 3.0},
    ])
    layers = layer_summary(todate)
    core = layers.filter(pl.col("layer") == "core").row(0, named=True)
    assert core["n"] == 2
    assert abs(core["win_rate"] - 0.5) < 1e-9
    assert abs(core["avg_return_pct"] - 0.0) < 1e-9
    assert abs(core["worst_path_min_pct"] - (-15.0)) < 1e-9
    assert layers["layer"].to_list() == ["core", "pool"]  # core 排前

    weekly = weekly_layer_table(todate, layer="core")
    row = weekly.row(0, named=True)
    assert row["n"] == 2 and row["wins"] == 1
    assert abs(row["avg_alpha_pct"] - (-2.0)) < 1e-9


def test_counterfactual_summary_by_reason():
    ex = _todate_frame([
        {"stock_id": "2327", "name": "國巨", "return_pct": 44.0, "alpha_market_pct": 45.0},
        {"stock_id": "x1", "name": "乙", "return_pct": -4.0, "alpha_market_pct": -3.0},
    ]).drop("layer").with_columns(
        pl.Series("reason", ["過熱", "過熱"]), pl.Series("detail", [None, None], dtype=pl.Utf8)
    )
    cf = counterfactual_summary(ex)
    row = cf.row(0, named=True)
    assert row["reason"] == "過熱" and row["n"] == 2
    assert abs(row["avg_return_pct"] - 20.0) < 1e-9
    assert abs(row["beat_market_rate"] - 0.5) < 1e-9
    assert row["best_stock"] == "國巨"


def test_week_over_week_diff_downgrade_with_signals():
    picks = pl.DataFrame(
        {
            "week": ["2026-W25", "2026-W25", "2026-W27", "2026-W27"],
            "data_date": [date(2026, 6, 18)] * 2 + [date(2026, 6, 30)] * 2,
            "stock_id": ["2337", "2610", "2610", "9999"],
            "name": ["旺宏", "華航", "華航", "新股"],
            "layer": ["core", "pool", "core", "core"],
        }
    )
    enriched = {
        "2026-W27": pl.DataFrame(
            {
                "stock_id": ["2337"],
                "ma20_dist_pct": [-5.0],
                "ma60_dist_pct": [2.0],
                "foreign_net_5d_lots": [-39451.0],
                "foreign_net_lots": [-20000.0],
                "trust_net_5d_lots": [-11575.0],
                "flags": ["土洋對作"],
            }
        )
    }
    diff = week_over_week_diff(picks, enriched)
    # 旺宏 core→除名（降級、帶訊號）；華航 pool→core 是升級不列
    assert diff.height == 1
    row = diff.row(0, named=True)
    assert row["stock_id"] == "2337"
    assert row["to_layer"] == "（除名）"
    assert abs(row["foreign_net_5d_lots"] - (-39451.0)) < 1e-9
    assert row["flags"] == "土洋對作"


def test_render_outcome_report_sections():
    todate = _todate_frame([
        {"stock_id": "a", "return_pct": 10.0, "alpha_market_pct": 8.0},
    ])
    md = render_outcome_report(
        todate,
        layer_summary(todate),
        weekly_layer_table(todate),
        pl.DataFrame(),
        pl.DataFrame(),
        pl.DataFrame(),
        missing_weeks=["2026-W26"],
        exit_date=date(2026, 7, 1),
        data_range=(date(2026, 1, 5), date(2026, 7, 1)),
        diff=pl.DataFrame(),
        min_sample_warn=20,
    )
    assert "核心層逐週 α" in md
    assert "2026-W26" in md  # 斷供如實標
    assert "偽陰性" in md
    assert "方向性" in md  # 樣本小警語
    assert "翻轉解剖" in md


# ─── render_weekly_brief：WS-L 進場日欄＋late_entry 標記 ──────────────────────


def _picks_returns_frame(rows: list[dict], with_late_col: bool = True) -> pl.DataFrame:
    base = {
        "week_tag": "2026-W27",
        "stock_id": "X",
        "name": "X",
        "strategy_id": "core",
        "hold_weeks": 1,
        "screened_at": date(2026, 6, 30),
        "entry_date": date(2026, 7, 1),
        "exit_date": date(2026, 7, 8),
        "entry_price": 100.0,
        "exit_price": 110.0,
        "return_pct": 10.0,
        "market_return_pct": 2.0,
        "excess_return_pct": 8.0,
        "status": "matured",
        "late_entry": False,
    }
    df = pl.DataFrame([{**base, **r} for r in rows])
    return df if with_late_col else df.drop("late_entry")


def test_render_weekly_brief_includes_entry_date_column():
    picks_r = _picks_returns_frame([{"stock_id": "2610", "name": "華航"}])
    md = render_weekly_brief(picks_r, pl.DataFrame(), "2026-W27", date(2026, 6, 30))
    assert "進場日" in md
    assert "2026-07-01" in md


def test_render_weekly_brief_marks_late_entry_with_footnote():
    picks_r = _picks_returns_frame([
        {"stock_id": "2610", "name": "華航"},
        {
            "stock_id": "3293", "name": "鈊象",
            "entry_date": date(2026, 7, 3), "late_entry": True,
        },
    ])
    md = render_weekly_brief(picks_r, pl.DataFrame(), "2026-W27", date(2026, 6, 30))
    assert "2026-07-03*" in md  # late 列加星號
    assert "2026-07-01*" not in md  # 未標 late 的列不加星號
    assert "late_entry" in md  # footnote 說明


def test_render_weekly_brief_backward_compatible_without_late_entry_column():
    """picks_r 沒有 late_entry 欄（舊呼叫端／未 join）仍可正常渲染，不炸、不冒出註腳。"""
    picks_r = _picks_returns_frame([{"stock_id": "2610", "name": "華航"}], with_late_col=False)
    md = render_weekly_brief(picks_r, pl.DataFrame(), "2026-W27", date(2026, 6, 30))
    assert "進場日" in md
    assert "late_entry" not in md


def test_render_weekly_brief_missing_entry_date_shows_dash():
    picks_r = _picks_returns_frame([{"stock_id": "2610", "name": "華航", "entry_date": None}])
    md = render_weekly_brief(picks_r, pl.DataFrame(), "2026-W27", date(2026, 6, 30))
    assert "| — |" in md


# ─── run_picks_brief：WS-L --week 指定週重產 ──────────────────────────────────


def _build_reports_and_cache(tmp_path: Path) -> tuple[Path, Path]:
    """兩週（2026-W19／2026-W20）皆有到期 pick＋涵蓋兩週窗的合成日線快取。

    回傳 (settings, reports_dir)。
    """
    reports = tmp_path / "reports"
    cache_root = tmp_path / "cache"
    twse_cache = cache_root / "twse"
    twse_cache.mkdir(parents=True)

    closes = [100.0 + i * 0.5 for i in range(30)]  # 30 個交易日，兩週 entry+hold 窗都夠長
    _price_series("2610", date(2026, 6, 1), closes).write_parquet(
        twse_cache / "daily_all_20260601.parquet"
    )

    def _row(week: str, data_date: date) -> dict:
        return {
            "week": week, "data_date": data_date, "stock_id": "2610", "name": "華航",
            "layer": "core", "sub_industry": None, "entry_zone": None, "stop": None,
            "ext_ma60_pct": None, "thesis_tag": None,
        }

    upsert_pick(reports / "2026-W19", _row("2026-W19", date(2026, 6, 1)))
    upsert_pick(reports / "2026-W20", _row("2026-W20", date(2026, 6, 8)))

    settings = tmp_path / "settings.yaml"
    settings.write_text(
        yaml.safe_dump(
            {
                "paths": {"reports_dir": str(reports), "cache_dir": str(cache_root)},
                "backtest": {"picks": {}},
            }
        ),
        encoding="utf-8",
    )
    return settings, reports


def test_run_picks_brief_week_param_writes_to_specified_week_dir(tmp_path):
    settings, reports = _build_reports_and_cache(tmp_path)
    run_picks_brief(settings, week="2026-W19")
    out = reports / "2026-W19" / "pick_outcome_brief.md"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "2026-W19" in content
    assert "進場日" in content
    assert not (reports / "2026-W20" / "pick_outcome_brief.md").exists()


def test_run_picks_brief_default_unaffected_by_week_param(tmp_path):
    """不指定 week＝現行預設行為：評估底帳中 r+5 已到期最近一週，寫最新週報目錄。"""
    settings, reports = _build_reports_and_cache(tmp_path)
    run_picks_brief(settings)
    out = reports / "2026-W20" / "pick_outcome_brief.md"  # dirs[-1]，且 W20 也是到期最近一週
    assert out.exists()
    assert "2026-W20" in out.read_text(encoding="utf-8")


def test_run_picks_brief_invalid_week_exits(tmp_path):
    settings, _reports = _build_reports_and_cache(tmp_path)
    with pytest.raises(typer.Exit):
        run_picks_brief(settings, week="2026-W99")


# ── M3.1 停損延遲帳（委託書 M3）────────────────────────────────────────────


def test_parse_stop_price_handles_real_ledger_wordings():
    """底帳實際四種寫法都要抽對——MA60/low60 標籤數字不得被當成停損價。"""
    from tw_screener.backtest.picks_outcome import parse_stop_price

    assert parse_stop_price("收盤跌破41.5(MA60)、隔日未收復出場") == 41.5
    assert parse_stop_price("收盤確認跌破 MA60 20.35、隔日未收復出場") == 20.35
    assert parse_stop_price("MA60 10.29 高於收盤不可用→收盤跌破 low60 9.90") == 9.90
    assert (
        parse_stop_price("收盤跌破 215.0、隔日未收復出場（MA60 236.87 高於收盤不可用）")
        == 215.0
    )
    assert parse_stop_price("收盤破21.1") == 21.1


def test_parse_stop_price_returns_none_when_no_absolute_price():
    """沒有絕對價（只寫「跌破季線」）＝抽不到，回 None 不猜——M7 patch-2 要修的正是這型。"""
    from tw_screener.backtest.picks_outcome import parse_stop_price

    assert parse_stop_price("跌破季線停損") is None
    assert parse_stop_price("") is None
    assert parse_stop_price(None) is None
    # 純百分比/天數不算絕對價
    assert parse_stop_price("回檔破 5% 出場") is None


def _px(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )


def test_stop_delay_ledger_measures_weekly_review_cost():
    """訊號日掛條件單 vs 等下週報覆核——延遲成本＝兩個執行價的差。"""
    from tw_screener.backtest.picks_outcome import stop_delay_ledger

    picks = pl.DataFrame(
        {
            "week": ["2026-W19", "2026-W20"],
            "data_date": [date(2026, 6, 1), date(2026, 6, 8)],
            "stock_id": ["1101", "1102"],
            "name": ["甲", "乙"],
            "layer": ["core", "core"],
            "stop": ["收盤跌破 95.0、隔日未收復出場", "收盤跌破 90.0、隔日未收復出場"],
        }
    )
    # 1101：6/2 進場 100；6/3 收 94 觸發（<95）；6/4 條件單執行 92；
    #       下一個覆核資料日＝6/8（W20 data_date），其次一交易日 6/9 執行 80 → 延遲成本 −13.04%
    px = _px(
        [
            ("1101", date(2026, 6, 1), 101.0),
            ("1101", date(2026, 6, 2), 100.0),
            ("1101", date(2026, 6, 3), 94.0),
            ("1101", date(2026, 6, 4), 92.0),
            ("1101", date(2026, 6, 5), 88.0),
            ("1101", date(2026, 6, 8), 84.0),
            ("1101", date(2026, 6, 9), 80.0),
            # 1102：從未跌破 90 → not_triggered
            ("1102", date(2026, 6, 8), 100.0),
            ("1102", date(2026, 6, 9), 99.0),
            ("1102", date(2026, 6, 10), 98.0),
        ]
    )
    led = stop_delay_ledger(picks, px)
    a = led.filter(pl.col("stock_id") == "1101").row(0, named=True)
    assert a["status"] == "measured"
    assert a["stop_price"] == 95.0
    assert a["signal_date"] == date(2026, 6, 3)
    assert a["cond_exec_date"] == date(2026, 6, 4)
    assert a["cond_exec_price"] == 92.0
    assert a["review_date"] == date(2026, 6, 8)
    assert a["weekly_exec_date"] == date(2026, 6, 9)
    assert a["delay_cost_pct"] == pytest.approx((80.0 / 92.0 - 1) * 100, abs=1e-6)
    assert a["delay_td"] == 3

    b = led.filter(pl.col("stock_id") == "1102").row(0, named=True)
    assert b["status"] == "not_triggered"
    assert b["delay_cost_pct"] is None


def test_stop_delay_ledger_marks_unparsed_and_implausible():
    """抽不到價、或抽到高於進場價的不合理值 → unparsed，不算假帳。"""
    from tw_screener.backtest.picks_outcome import stop_delay_ledger

    picks = pl.DataFrame(
        {
            "week": ["2026-W19", "2026-W19"],
            "data_date": [date(2026, 6, 1), date(2026, 6, 1)],
            "stock_id": ["1101", "1102"],
            "name": ["甲", "乙"],
            "layer": ["core", "core"],
            "stop": ["跌破季線停損", "收盤跌破 999.0"],  # 後者遠高於進場價 100
        }
    )
    px = _px(
        [
            ("1101", date(2026, 6, 2), 100.0),
            ("1101", date(2026, 6, 3), 90.0),
            ("1102", date(2026, 6, 2), 100.0),
            ("1102", date(2026, 6, 3), 90.0),
        ]
    )
    led = stop_delay_ledger(picks, px)
    assert set(led["status"].to_list()) == {"unparsed"}
    assert led.filter(pl.col("stock_id") == "1102").row(0, named=True)["stop_price"] is None


def test_stop_delay_summary_and_section_render_without_samples():
    """無可量測樣本時：成本欄 None（不印 0%）、報表段仍印出 status 計數。"""
    from tw_screener.backtest.picks_outcome import (
        render_stop_delay_section,
        stop_delay_ledger,
        stop_delay_summary,
    )

    picks = pl.DataFrame(
        {
            "week": ["2026-W19"],
            "data_date": [date(2026, 6, 1)],
            "stock_id": ["1101"],
            "name": ["甲"],
            "layer": ["core"],
            "stop": ["跌破季線停損"],
        }
    )
    px = _px([("1101", date(2026, 6, 2), 100.0)])
    s = stop_delay_summary(stop_delay_ledger(picks, px))
    assert s["n_measured"] == 0
    assert s["avg_cost_pct"] is None
    assert s["counts"]["unparsed"] == 1
    body = "\n".join(render_stop_delay_section(stop_delay_ledger(picks, px)))
    assert "停損延遲帳" in body
    assert "停損價無法解析 1" in body


# ── M1.6 左側解禁回收條款（委託書 M1.6）──────────────────────────────────


def _recall_picks() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "week": ["2026-W31", "2026-W31", "2026-W32"],
            "stock_id": ["1101", "1102", "1103"],
            "layer": ["opportunity", "opportunity", "core"],
            "thesis_tag": ["左側M-BR1 賣壓熄＋貼低", "外資三窗同買", "D+E 主升"],
        }
    )


def _recall_returns(left_alpha: float, gen_alpha: float, left_ret: float) -> pl.DataFrame:
    """兩筆 opportunity：1101＝左側票、1102＝一般機會層。"""
    return pl.DataFrame(
        {
            "week_tag": ["2026-W31", "2026-W31"],
            "stock_id": ["1101", "1102"],
            "strategy_id": ["opportunity", "opportunity"],
            "status": ["matured", "matured"],
            "return_pct": [left_ret, 5.0],
            "excess_return_pct": [left_alpha, gen_alpha],
        }
    )


def test_contrarian_recall_warns_only_when_alpha_and_winrate_both_worse():
    from tw_screener.backtest.picks_outcome import contrarian_recall_check

    by_hold = {2: _recall_returns(-4.0, 3.0, -6.0), 4: _recall_returns(-5.0, 2.0, -8.0)}
    # 門檻用 min_picks=1 逼到 eligible；α 與勝率皆較差 → 警告
    chk = contrarian_recall_check(_recall_picks(), by_hold, min_picks=1)
    assert chk["n_left"] == 1
    assert chk["eligible"] is True
    assert chk["warn"] is True

    # α 較差但勝率不差（左側票也是正報酬）→ 不警告（兩者須同時劣化）
    by_hold_win = {2: _recall_returns(-4.0, 3.0, 2.0), 4: _recall_returns(-5.0, 2.0, 1.0)}
    assert contrarian_recall_check(_recall_picks(), by_hold_win, min_picks=1)["warn"] is False


def test_contrarian_recall_not_eligible_below_threshold():
    """未達 10 筆／12 週門檻 → 即使數字難看也不宣告回收（避免小樣本亂殺）。"""
    from tw_screener.backtest.picks_outcome import contrarian_recall_check

    by_hold = {2: _recall_returns(-4.0, 3.0, -6.0)}
    chk = contrarian_recall_check(_recall_picks(), by_hold, min_picks=10, min_weeks=12)
    assert chk["eligible"] is False
    assert chk["warn"] is False


def test_contrarian_recall_does_not_judge_on_empty_group():
    """一般機會層無到期樣本 → 不判定，不用空集合宣告左側劣化。"""
    from tw_screener.backtest.picks_outcome import (
        contrarian_recall_check,
        render_contrarian_recall_section,
    )

    only_left = pl.DataFrame(
        {
            "week_tag": ["2026-W31"],
            "stock_id": ["1101"],
            "strategy_id": ["opportunity"],
            "status": ["matured"],
            "return_pct": [-9.0],
            "excess_return_pct": [-9.0],
        }
    )
    chk = contrarian_recall_check(_recall_picks(), {2: only_left}, min_picks=1)
    assert chk["warn"] is False
    assert "不判定" in str(chk["note"])
    body = "\n".join(render_contrarian_recall_section(chk))
    assert "兩條件桶" in body  # 證據狀態必須印在報表上，不得寫成「未驗證」
    assert "未驗證" not in body
