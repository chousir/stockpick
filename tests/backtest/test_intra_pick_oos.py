"""tests/backtest/test_intra_pick_oos.py — M-Pick3a 保留樣本週快照純函式（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.intra_pick_oos import (
    HoldoutConfig,
    adjustment_events,
    before_price_consistency,
    holdout_week_dates,
    official_ratio_events,
    ratio_rounding_error,
    reconcile_events,
    return_agreement,
    total_return_targets,
)
from tw_screener.backtest.regime_history import build_regime_history

D = [date(2021, 3, 1) + timedelta(days=i) for i in range(10)]  # 連續日（交易列由各測試挑）


def _px(sid: str, days: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"date": days, "stock_id": [sid] * len(days), "close": closes})


def _ev(rows: list[tuple[str, date, float | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "adj_factor": pl.Float64},
        orient="row",
    )


# ── adjustment_events ────────────────────────────────────────────────────────


def test_adjustment_events_ratio_cash_stock_and_invalid() -> None:
    div = pl.DataFrame(
        {
            "ex_date": [D[1], D[2], D[3], D[3]],
            "stock_id": ["A", "B", "C", "C"],
            "before_price": [100.0, 110.0, None, 50.0],
            "after_price": [95.0, 100.0, 40.0, 40.0],
        }
    )
    ev = adjustment_events(div)
    got = {r["stock_id"]: r["adj_factor"] for r in ev.iter_rows(named=True)}
    assert got["A"] == pytest.approx(100 / 95)  # 現金股利 5 元
    assert got["B"] == pytest.approx(1.1)  # 股票股利 10%：after＝before／1.1
    assert got["C"] == pytest.approx(1.25)  # 同 (stock, ex_date) 重複 → 留最後一筆
    assert adjustment_events(div.head(3)).filter(pl.col("stock_id") == "C")[
        "adj_factor"
    ].item() is None  # before 缺 → 無效事件（null），不當 1.0


# ── total_return_targets ─────────────────────────────────────────────────────


def test_total_return_no_events_entry_next_row_and_unmatured_null() -> None:
    px = _px("A", D[:4], [10.0, 11.0, 12.0, 13.0])
    out = total_return_targets(px, _ev([]), [1]).sort("date")
    r1 = out["r1"].to_list()
    assert r1[0] == pytest.approx((12 / 11 - 1) * 100)  # entry＝次一列 11、exit＝再後 1 列 12
    assert r1[1] == pytest.approx((13 / 12 - 1) * 100)
    assert r1[2] is None and r1[3] is None  # exit 列不存在＝未到期 → null（不補 0）
    assert out["bad_evt_1"].to_list()[:2] == [False, False]
    assert out["bad_evt_1"].to_list()[2] is None


def test_total_return_cash_dividend_restored_and_entry_row_event_excluded() -> None:
    px = _px("A", D[:4], [100.0, 100.0, 95.0, 95.0])
    out = total_return_targets(px, _ev([("A", D[2], 100 / 95)]), [1]).sort("date")
    r1 = out["r1"].to_list()
    assert r1[0] == pytest.approx(0.0)  # 價跌 5 元＝配 5 元 → 總報酬 0（原始價差會是 −5%）
    assert r1[1] == pytest.approx(0.0)  # entry 當天就是除息日收盤買 → 窗 (e, x] 不含該事件


def test_total_return_stock_dividend_multi_horizon() -> None:
    px = _px("A", D[:4], [110.0, 110.0, 100.0, 102.0])
    out = total_return_targets(px, _ev([("A", D[2], 1.1)]), [2]).sort("date")
    assert out["r2"].to_list()[0] == pytest.approx((102 / 110 * 1.1 - 1) * 100)  # +2%


def test_total_return_event_on_suspended_day_aligned_forward() -> None:
    days = [D[0], D[1], D[3], D[4]]  # D[2] 停牌無列
    px = _px("A", days, [100.0, 100.0, 95.0, 95.0])
    out = total_return_targets(px, _ev([("A", D[2], 100 / 95)]), [1]).sort("date")
    assert out["r1"].to_list()[0] == pytest.approx(0.0)  # 事件後移到 D[3] 那列、落在窗內


def test_total_return_invalid_event_voids_window_only() -> None:
    px = _px("A", D[:5], [100.0, 100.0, 95.0, 96.0, 97.0])
    out = total_return_targets(px, _ev([("A", D[2], None)]), [1]).sort("date")
    assert out["r1"].to_list()[0] is None  # 窗 (D1, D2] 含無效事件 → 作廢
    assert out["bad_evt_1"].to_list()[0] is True
    assert out["r1"].to_list()[2] == pytest.approx((97 / 96 - 1) * 100)  # 不含事件的窗照算
    assert out["bad_evt_1"].to_list()[2] is False


def test_total_return_events_on_same_row_multiply_other_stocks_untouched() -> None:
    days = [D[0], D[1], D[3]]  # D[2] 停牌：D[2]、D[3] 兩個事件都對齊到 D[3] 那列
    px = pl.concat([_px("A", days, [100.0, 100.0, 80.0]), _px("B", days, [10.0, 10.0, 10.0])])
    ev = _ev([("A", D[1], 1.5), ("A", D[2], 1.1), ("A", D[3], 1.2)])
    out = total_return_targets(px, ev, [1]).sort("stock_id", "date")
    a0 = out.filter((pl.col("stock_id") == "A") & (pl.col("date") == D[0]))["r1"].item()
    # D[1] 的事件落在 entry 列（不計）；D[2]、D[3] 同列 → 1.1×1.2 相乘
    assert a0 == pytest.approx((80 / 100 * 1.1 * 1.2 - 1) * 100)
    b0 = out.filter((pl.col("stock_id") == "B") & (pl.col("date") == D[0]))["r1"].item()
    assert b0 == pytest.approx(0.0)


def test_total_return_uncovered_stock_all_null() -> None:
    px = pl.concat([_px("A", D[:3], [10.0, 11.0, 12.0]), _px("B", D[:3], [10.0, 11.0, 12.0])])
    out = total_return_targets(px, _ev([]), [1], covered_ids={"A"})
    assert out.filter(pl.col("stock_id") == "A")["r1"].drop_nulls().len() == 1
    # 沒抓到除權息 → 不假裝沒配
    assert out.filter(pl.col("stock_id") == "B")["r1"].null_count() == 3


def test_total_return_empty_price() -> None:
    out = total_return_targets(pl.DataFrame(), _ev([]), [20, 10])
    assert out.is_empty()
    assert out.columns == ["date", "stock_id", "r10", "r20", "bad_evt_10", "bad_evt_20"]


# ── holdout_week_dates ───────────────────────────────────────────────────────


def _weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_holdout_weeks_iso_boundaries() -> None:
    cal = _weekdays(date(2014, 12, 22), date(2015, 1, 9))
    got = holdout_week_dates(cal, date(2015, 1, 1), date(2021, 12, 31))
    # 2015-W01＝2014-12-29～2015-01-04：快照日 2015-01-02（≥ 起日）納入；2014-W52 不納入
    assert got == [date(2015, 1, 2), date(2015, 1, 9)]


def test_holdout_weeks_compute_on_full_calendar_then_trim() -> None:
    cal = _weekdays(date(2021, 12, 20), date(2022, 1, 7))
    assert holdout_week_dates(cal, date(2015, 1, 1), date(2021, 12, 31))[-1] == date(2021, 12, 31)
    # 迄日落在週中：該週快照日（完整日曆的週五）> 迄日 → 不造出「截到週三」的半週
    got = holdout_week_dates(cal, date(2015, 1, 1), date(2021, 12, 29))
    assert got == [date(2021, 12, 24)]


# ── regime 降級路徑（docs/33 §5 D4：空法人輸入 → 趨勢＋廣度按權重正規化）──────────


def test_build_regime_history_empty_institutional_degrades_to_trend_breadth() -> None:
    days = _weekdays(date(2021, 1, 4), date(2021, 3, 31))
    rows = []
    for k in range(6):
        for i, d in enumerate(days):
            rows.append({"date": d, "stock_id": f"S{k}", "close": 100.0 + i * (1 + k % 3)})
    price = pl.DataFrame(rows)
    cfg = {
        "trend": {"ma_windows": [3, 5]},
        "breadth": {"ma_window": 3, "position_window": 5, "min_priced": 3},
        "flow": {"windows": [2], "saturate_shares": 1_000_000},
        "weights": {"trend": 0.4, "breadth": 0.3, "flow": 0.3},
    }
    out = build_regime_history(price, pl.DataFrame(), cfg)
    assert out["flow_score"].null_count() == out.height  # 法人分項整段缺席
    both = out.drop_nulls(["trend_score", "breadth_score"])
    assert both.height > 0
    expect = (0.4 * both["trend_score"] + 0.3 * both["breadth_score"]) / 0.7
    assert both["regime_score"].to_list() == pytest.approx(expect.to_list())


# ── HoldoutConfig ────────────────────────────────────────────────────────────


def test_holdout_config_from_settings_reads_panel_reconcile_thresholds() -> None:
    cfg = {
        "backtest": {
            "panel": {"reconcile_tol_pct": 0.3, "reconcile_pass_rate": 0.99},
            "intra_pick_oos": {
                "holdout_start": "2016-01-01",
                "holdout_end": "2020-12-31",
                "reconcile_years": [2015, 2020],
                "reconcile_dates_per_year": 2,
                "adj_before_price_tol_pct": 0.2,
                "adj_before_price_min_rate": 0.97,
                "adj_event_match_min_rate": 0.9,
                "adj_cash_amount_min_rate": 0.98,
                "adj_window_diff_p99_max_pp": 2.5,
                "output_dir": "x/y",
            },
        }
    }
    h = HoldoutConfig.from_settings(cfg)
    assert (h.start, h.end) == (date(2016, 1, 1), date(2020, 12, 31))
    assert h.reconcile_years == (2015, 2020) and h.reconcile_dates_per_year == 2
    assert (h.reconcile_tol_pct, h.reconcile_pass_rate) == (0.3, 0.99)
    assert h.output_dir == "x/y"
    assert (h.adj_before_price_tol_pct, h.adj_before_price_min_rate) == (0.2, 0.97)
    assert (h.adj_event_match_min_rate, h.adj_cash_amount_min_rate) == (0.9, 0.98)
    assert h.adj_window_diff_p99_max_pp == 2.5
    d = HoldoutConfig()  # 預設值＝settings.yaml 事前寫定的門檻
    assert (d.adj_before_price_tol_pct, d.adj_before_price_min_rate) == (0.5, 0.98)
    assert (d.adj_event_match_min_rate, d.adj_cash_amount_min_rate) == (0.95, 0.99)
    assert d.adj_window_diff_p99_max_pp == 1.0
    assert HoldoutConfig.from_settings({}) == HoldoutConfig()


# ── reconcile_events ─────────────────────────────────────────────────────────


def _official(rows: list[tuple[str, date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "type": pl.Utf8,
                      "cash_dividend": pl.Float64}, orient="row",
    )


def _fm_events(rows: list[tuple[str, date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "event_type": pl.Utf8,
                      "dividend_value": pl.Float64}, orient="row",
    )


def test_reconcile_events_match_only_sides_near_and_cash_amount() -> None:
    a, b, c, d = D[1], D[2], D[3], D[4]
    official = _official([
        ("A", a, "息", 1.0),      # 配對、金額相同
        ("B", b, "息", 2.0),      # 配對、金額差 0.5 → cash_bad
        ("C", c, "權息", 3.0),    # 配對、含配股 → 不比金額
        ("D", d, "息", 4.0),      # 僅官方；FinMind 有同股差 1 天 → near
        ("E", d, "息", 5.0),      # 僅官方；FinMind 無此股
        ("Z", d, "息", 9.0),      # 不在 stock_ids → 排除
    ])
    fm = _fm_events([
        ("A", a, "除息", 1.0),
        ("B", b, "息", 1.5),
        ("C", c, "除權息", 9.9),
        ("D", d + timedelta(days=1), "息", 4.0),
        ("F", a, "息", 7.0),      # 僅 FinMind
    ])
    r = reconcile_events(official, fm, D[0], D[9], {"A", "B", "C", "D", "E", "F"})
    assert (r["n_official"], r["n_finmind"], r["n_matched"]) == (5, 5, 3)
    assert r["official_only"]["stock_id"].to_list() == ["D", "E"]
    assert r["finmind_only"]["stock_id"].to_list() == ["D", "F"]
    assert r["n_official_only_near"] == 1  # 只有 D 在 FinMind 有 ±3 天內的同股事件
    assert (r["n_cash_pairs"], r["n_cash_ok"]) == (2, 1)  # A、B 為純現金；C 含配股不比
    assert r["cash_bad"]["stock_id"].to_list() == ["B"]


def test_reconcile_events_respects_date_window() -> None:
    official = _official([("A", D[1], "息", 1.0), ("A", D[8], "息", 1.0)])
    fm = _fm_events([("A", D[1], "息", 1.0), ("A", D[8], "息", 1.0)])
    r = reconcile_events(official, fm, D[0], D[5], {"A"})
    assert (r["n_official"], r["n_finmind"], r["n_matched"]) == (1, 1, 1)


# ── return_agreement ─────────────────────────────────────────────────────────


def test_return_agreement_second_order_term_and_group_split() -> None:
    days = D[:5]
    px = pl.concat([
        # A：D[2] 除息（前收 100→參考 95，D=5），之後回升到 104.5
        _px("A", days, [100.0, 100.0, 95.0, 104.5, 104.5]),
        _px("B", days, [50.0] * 5),                       # 無事件
        _px("C", days, [80.0, 80.0, 76.0, 76.0, 76.0]),   # D[2] 除息，但參考端沒有
    ])
    ev = _ev([("A", D[2], 100 / 95), ("C", D[2], 80 / 76)])
    adj = total_return_targets(px, ev, [2])
    raw = total_return_targets(px, _ev([]), [2])
    # 參考＝TWSE 現金股利線性加回：只對 A 在含事件的窗（t＝D[0]）加 D/entry＝5/100＝5.0pp
    ref = raw.with_columns(
        pl.when((pl.col("stock_id") == "A") & (pl.col("date") == D[0]))
        .then(pl.col("r2") + 5.0)
        .otherwise(pl.col("r2"))
        .alias("r2")
    )
    r = return_agreement(adj, raw, ref, 2, stock_div_ids=set())
    # h=2 每檔只有 t＝D[0]、D[1] 兩窗有值（t＝D[2] 的出場列不存在）：共 6 窗
    # 含事件的窗只有 t＝D[0]：A 兩方都看見、C 僅 FinMind；其餘 4 窗無事件
    assert (r["n_windows"], r["n_none"], r["n_both"]) == (6, 4, 1)
    assert (r["n_fm_only"], r["n_ref_only"], r["n_cash"], r["n_both_stock_div"]) == (1, 0, 1, 0)
    # 手算 A@D[0]：entry 100、exit 104.5、比值 100/95
    #   還原 r＝(104.5/100)·(100/95)−1＝10.0%；線性 r＝(104.5−100+5)/100＝9.5%
    #   差＝(D/entry)(exit/after−1)＝0.05·(104.5/95−1)＝0.5pp（二階項，非誤差）
    assert r["diff_median"] == pytest.approx(0.5)
    assert r["diff_abs_max"] == pytest.approx(0.5)


def test_return_agreement_stock_dividend_stocks_excluded_from_cash_stats() -> None:
    days = D[:3]
    px = _px("A", days, [100.0, 100.0, 80.0])
    adj = total_return_targets(px, _ev([("A", D[2], 1.25)]), [1])
    raw = total_return_targets(px, _ev([]), [1])
    ref = raw.with_columns((pl.col("r1") + 3.0).alias("r1"))  # 參考只加回現金（與配股無關）
    r = return_agreement(adj, raw, ref, 1, stock_div_ids={"A"})
    # h=1、3 個交易日：只有 t＝D[0] 一窗有值（t＝D[1] 的出場列不存在）
    assert (r["n_windows"], r["n_both"], r["n_both_stock_div"], r["n_cash"]) == (1, 1, 1, 0)
    assert r["diff_median"] is None  # 配股股票不進現金差值統計


# ── before_price_consistency ─────────────────────────────────────────────────────────────────


def test_before_price_consistency_exact_tolerance_missing_realigned_and_bad() -> None:
    rows = D[:6]
    px = pl.concat([
        _px("A", rows, [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
        _px("B", [D[0], D[1], D[4], D[5]], [10.0, 10.0, 10.0, 10.0]),  # D[2]、D[3] 停牌無列
        _px("C", rows, [5.0] * 6),
        _px("D", rows, [7.0] * 6),
    ])
    ev = pl.DataFrame(
        [
            ("A", D[2], 101.0),   # 前一列 D[1] 收 101 → 分毫不差
            ("A", D[3], 102.3),   # 前一列收 102 → 差 0.29% ≤ 0.5% 但不算分毫不差
            ("A", D[4], 110.0),   # 前一列收 103 → 差 6.8% → 不一致
            ("B", D[2], 10.0),    # 事件日停牌 → 順延到 D[4]、前一列 D[1] 收 10 → 一致且順延
            ("C", D[0], 5.0),     # 事件日就是首列、無前一列 → 不檢查
            ("D", D[2], None),    # 前後價缺（無效事件）→ 不檢查
        ],
        schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "before_price": pl.Float64},
        orient="row",
    )
    r = before_price_consistency(ev, px, tol_pct=0.5)
    assert r["n_events"] == 6 and r["n_invalid_before"] == 1 and r["n_no_prev_row"] == 1
    assert (r["n_checked"], r["n_ok"], r["n_exact"], r["n_realigned"]) == (4, 3, 2, 1)
    worst = r["worst"].row(0, named=True)
    assert (worst["stock_id"], worst["ex_date"]) == ("A", D[4])
    assert worst["diff_pct"] == pytest.approx(abs(110 - 103) / 103 * 100)


def test_before_price_consistency_empty_events() -> None:
    ev = pl.DataFrame(schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "before_price": pl.Float64})
    r = before_price_consistency(ev, _px("A", D[:3], [1.0, 1.0, 1.0]), tol_pct=0.5)
    assert (r["n_events"], r["n_checked"], r["n_ok"]) == (0, 0, 0)


# ── ratio_rounding_error（事後補充診斷）─────────────────────────────────────────────────────


def test_ratio_rounding_error_pure_cash_only_hand_calc() -> None:
    div = pl.DataFrame(
        [
            ("A", D[1], "息", 100.0, 95.0, 5.0),      # 精確：(100−5)/95−1＝0
            ("B", D[1], "除息", 100.0, 94.9, 5.0),    # (95/94.9 − 1)×100＝0.105374…pp
            ("C", D[1], "權息", 100.0, 90.0, 5.0),    # 含配股 → 不算
            ("D", D[1], None, 100.0, 95.0, 5.0),      # 類型缺 → 不算
            ("E", D[1], "息", 100.0, 100.0, 0.0),     # D=0 → 不算
            ("F", D[1], "息", 3.0, 2.0, 5.0),         # before ≤ D → 不算
        ],
        schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "event_type": pl.Utf8,
                "before_price": pl.Float64, "after_price": pl.Float64,
                "dividend_value": pl.Float64},
        orient="row",
    )
    r = ratio_rounding_error(div)
    assert r["n_checked"] == 2
    b_err = (95.0 / 94.9 - 1) * 100
    assert r["err_abs_max"] == pytest.approx(b_err)
    assert r["err_median"] == pytest.approx(b_err / 2)
    assert r["worst"]["stock_id"].to_list()[0] == "B"
    assert (r["flag_pp"], r["n_flagged"]) == (0.2, 0)  # 0.105pp 未超過預設 0.2pp
    assert ratio_rounding_error(div, flag_pp=0.05)["n_flagged"] == 1  # 只有 B 超過 0.05pp


def test_ratio_rounding_error_empty() -> None:
    div = pl.DataFrame(
        schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "event_type": pl.Utf8,
                "before_price": pl.Float64, "after_price": pl.Float64,
                "dividend_value": pl.Float64}
    )
    r = ratio_rounding_error(div)
    assert r["n_checked"] == 0 and r["err_median"] is None and r["err_abs_max"] is None


# ── official_ratio_events ────────────────────────────────────────────────────────────────────


def test_official_ratio_events_uses_prev_close_and_pure_cash_only() -> None:
    px = pl.concat([
        _px("A", D[:4], [100.0, 101.0, 102.0, 103.0]),
        _px("C", D[:4], [50.0, 50.0, 50.0, 50.0]),
        _px("E", D[:4], [10.0, 10.0, 10.0, 10.0]),
    ])
    official = pl.DataFrame(
        [
            ("A", D[2], "息", 2.0),      # 前收（D[1]）101 → 101/(101−2)
            ("B", D[2], "權息", 2.0),    # 含配股 → 不進比值版參考
            ("C", D[2], "息", 200.0),    # 現金 > 前收 → 無效（null）
            ("E", D[0], "息", 1.0),      # 事件在首列、無前收 → 無效（null）
            ("A", D[3], "息", 0.0),      # 現金為 0 → 不算
        ],
        schema={"stock_id": pl.Utf8, "ex_date": pl.Date, "type": pl.Utf8,
                "cash_dividend": pl.Float64},
        orient="row",
    )
    ev = official_ratio_events(official, px)
    got = {r["stock_id"]: r["adj_factor"] for r in ev.iter_rows(named=True)}
    assert set(got) == {"A", "C", "E"}
    assert got["A"] == pytest.approx(101.0 / 99.0)
    assert got["C"] is None and got["E"] is None
