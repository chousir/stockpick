"""tests/backtest/test_panel_tr.py — D6 成員稠密總報酬面板純函式（docs/33 §7；全合成資料）。

期望值皆由測試內手算（註解附算式），不回抄實作。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest import panel_tr as pt

D0 = date(2026, 1, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _prices(closes: dict[str, list[float]], days: list[date]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"date": d, "stock_id": sid, "close": c, "volume": 1_000_000}
            for sid, series in closes.items()
            for d, c in zip(days, series, strict=True)
        ]
    )


def test_build_member_tr_panel_total_return_filters_and_ma60() -> None:
    days = _days(70)
    flat100 = [100.0] * 40 + [95.0] * 30                       # 第 40 列除息，收盤 100 → 95
    price = _prices(
        {"1101": flat100, "2201": [50.0] * 70, "3301": [20.0] * 70,
         "0050": [90.0] * 70, "123456": [10.0] * 70},
        days,
    )
    events = pl.DataFrame(
        {"stock_id": ["1101"], "ex_date": [days[40]], "adj_factor": [100.0 / 95.0]}
    )
    got = pt.build_member_tr_panel(price, events, [2], {"1101", "2201"}, days[10])
    assert got.columns == ["date", "stock_id", "close", "ma60_dist_pct", "r2", "bad_evt_2",
                           "div_covered"]
    # ETF 式代號（0050）與 6 位代號被面板宇宙過濾；起日之前的暖身列不輸出
    assert sorted(got["stock_id"].unique().to_list()) == ["1101", "2201", "3301"]
    assert got["date"].min() == days[10]

    def cell(sid: str, i: int, col: str) -> object:
        return got.filter((pl.col("stock_id") == sid) & (pl.col("date") == days[i]))[col][0]

    # 1101@38：entry＝列 39 收盤 100、exit＝列 41 收盤 95，除息在 (39, 41] → 95×(100/95)/100−1 = 0
    # （純價差會是 −5%）；@39：窗 (40, 42] 不含第 40 列的除息 → 95/95−1 = 0
    assert cell("1101", 38, "r2") == pytest.approx(0.0, abs=1e-9)
    assert cell("1101", 39, "r2") == pytest.approx(0.0, abs=1e-9)
    assert cell("2201", 20, "r2") == pytest.approx(0.0, abs=1e-9)
    # 3301 沒抓過除權息（不在 covered）→ 「沒資料」不等於「沒配息」→ r2 全 null、div_covered 為假
    assert got.filter(pl.col("stock_id") == "3301")["r2"].null_count() == 60
    assert cell("3301", 20, "div_covered") is False and cell("1101", 20, "div_covered") is True
    # 未到期：最後兩列（exit 超出）null
    assert cell("2201", 69, "r2") is None and cell("2201", 67, "r2") is None
    # ma60 @ 第 69 列（第 70 筆）：窗＝列 10..69＝30 筆 100＋30 筆 95 → 均 97.5 → (95/97.5−1)×100
    assert cell("1101", 69, "ma60_dist_pct") == pytest.approx((95 / 97.5 - 1) * 100)
    assert cell("1101", 30, "ma60_dist_pct") is None  # 窗不足 60 筆


def test_window_spans_flags_missing_rows_and_immature_windows() -> None:
    days = _days(10)
    cal = pl.DataFrame({"date": days, "ci": list(range(10))})
    rows = pl.concat([
        pl.DataFrame({"date": days, "stock_id": "A"}),                       # 完整 10 列
        # B 缺日 4
        pl.DataFrame({"date": [d for i, d in enumerate(days) if i != 4], "stock_id": "B"}),
    ])
    got = pt.window_spans(rows, cal, horizon=2).sort("stock_id", "date")
    a = got.filter(pl.col("stock_id") == "A")["span"].to_list()
    b = got.filter(pl.col("stock_id") == "B")["span"].to_list()
    # A：列 i 的 entry＝i+1、exit＝i+3 → span 2；i≥7 的 exit 出界 → null
    assert a == [2, 2, 2, 2, 2, 2, 2, None, None, None]
    # B（缺日 4，列序 0,1,2,3,5,6,7,8,9）：
    # 列 0：entry 1、exit 列序 3＝日 3 → 2；列 1：entry 2、exit 列序 4＝日 5 → 3；
    # 列 2：entry 3、exit 日 6 → 3；列 3：entry 日 5、exit 列序 6＝日 7 → 2；
    # 列 5：entry 6、exit 日 8 → 2；列 6：entry 7、exit 日 9 → 2；其後 null
    assert b == [2, 3, 3, 2, 2, 2, None, None, None]


def test_span_share_by_month_and_row_match() -> None:
    spans = pl.DataFrame({
        "date": [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8),
                 date(2026, 2, 2), date(2026, 2, 3)],
        "stock_id": ["A"] * 6,
        "span": [2, 2, 3, None, 2, 2],
    })
    s = pt.span_share_by_month(spans, 2)
    assert s["ym"].to_list() == ["2026-01", "2026-02"]
    assert s["n_windows"].to_list() == [3, 2]                       # null 不計
    assert s["share_eq"].to_list() == pytest.approx([2 / 3, 1.0])
    assert s["share_gt"].to_list() == pytest.approx([1 / 3, 0.0])

    new = pl.DataFrame({
        "date": [date(2026, 1, 5)] * 4 + [date(2026, 2, 2)] * 2,
        "stock_id": ["a", "b", "c", "d", "a", "b"],
    })
    old = pl.DataFrame({"date": [date(2026, 1, 5)] * 3, "stock_id": ["a", "b", "c"]})
    m = pt.row_match_by_month(new, old)
    assert m["n_new"].to_list() == [4, 2] and m["n_matched"].to_list() == [3, 0]
    assert m["match_rate"].to_list() == pytest.approx([0.75, 0.0])


def test_span_share_by_old_coverage_splits_composition() -> None:
    days = _days(12)
    cal = pl.DataFrame({"date": days, "ci": list(range(12))})
    even = [d for i, d in enumerate(days) if i % 2 == 0]        # Z 只在偶數日成交（列序 0..5）
    new = pl.concat([
        pl.DataFrame({"date": days, "stock_id": "X"}),
        pl.DataFrame({"date": days, "stock_id": "Y"}),
        pl.DataFrame({"date": even, "stock_id": "Z"}),
    ])
    # 舊面板：X 全列收錄、Y 缺日 4、Z 整檔缺席
    old = pl.concat([
        pl.DataFrame({"date": days, "stock_id": "X"}),
        pl.DataFrame({"date": [d for i, d in enumerate(days) if i != 4], "stock_id": "Y"}),
    ])

    def by_grp(got: pl.DataFrame) -> dict[str, dict[str, float]]:
        return {r["grp"]: r for r in got.iter_rows(named=True)}

    # h=2：列 i 的 entry＝列 i+1、exit＝列 i+3，到期需 i+3 ≤ 末列序。
    # X（12 列）：i=0..8 → 9 窗、span 2；Y 同 9 窗（新面板稠密），其中列 4 不在舊面板
    # → 1 窗 missing_row、8 窗 have_row；Z（6 列）：i=0..2 → 3 窗，entry／exit 隔 4 個日曆序
    # → span 4 ≠ 2
    got = by_grp(pt.span_share_by_old_coverage(new, old, cal, 2, date(2027, 1, 1)))
    assert {g: r["n_windows"] for g, r in got.items()} == {
        "have_row": 17, "missing_row": 1, "absent_stock": 3
    }
    assert got["have_row"]["share_eq"] == pytest.approx(1.0)
    assert got["missing_row"]["share_eq"] == pytest.approx(1.0)
    assert got["absent_stock"]["share_eq"] == pytest.approx(0.0)
    assert got["have_row"]["share_rows"] == pytest.approx(17 / 21)          # 21 = 9+9+3
    assert got["absent_stock"]["share_rows"] == pytest.approx(3 / 21)
    assert all(r["year"] == 2026 for r in got.values())

    # before＝日 4：只計 date < 日 4 的列。X、Y 各列 0..3 → 4 窗（Y 缺的列 4 被截掉）have_row；
    # Z 列 0（日 0）、列 1（日 2）→ 2 窗；Z 列 2（日 4）被截掉
    early = by_grp(pt.span_share_by_old_coverage(new, old, cal, 2, days[4]))
    assert {g: r["n_windows"] for g, r in early.items()} == {"have_row": 8, "absent_stock": 2}

    # 跨年：X 單股 8 個連續日（2025-12-29～2026-01-05），h=2 → 5 窗
    # （起日 12-29、30、31、1-1、1-2）；share_rows 是「該年內」各組占比（各年各自加總為 1）
    y_days = [date(2025, 12, 29) + timedelta(days=i) for i in range(8)]
    y_rows = pl.DataFrame({"date": y_days, "stock_id": "X"})
    y_cal = pl.DataFrame({"date": y_days, "ci": list(range(8))})
    yr = pt.span_share_by_old_coverage(y_rows, y_rows, y_cal, 2, date(2027, 1, 1))
    assert yr.select("year", "n_windows", "share_rows").rows() == [(2025, 3, 1.0), (2026, 2, 1.0)]


def test_target_diff_by_period_counts_and_tails() -> None:
    d = date(2026, 3, 2)
    new = pl.DataFrame({"date": [d] * 5, "stock_id": list("abcde"),
                        "r20": [10.0, 5.0, 3.0, None, 7.0]})
    old = pl.DataFrame({"date": [d] * 5, "stock_id": list("abcde"),
                        "r20": [9.0, 5.0, None, 2.0, 1.0]})
    got = pt.target_diff_by_period(new, old, 20).row(0, named=True)
    # 兩邊皆有：a(10−9=1)、b(0)、e(7−1=6) → 差 [1, 0, 6]；c 僅新有（補回）；d 僅舊有（新為 null）
    assert (got["n_both"], got["n_new_only"], got["n_old_only"]) == (3, 1, 1)
    assert got["diff_median"] == pytest.approx(1.0)
    assert got["abs_max"] == pytest.approx(6.0)
    assert (got["n_gt_1pp"], got["n_gt_5pp"]) == (1, 1)   # |差|>1：只有 6；1 不算；>5：只有 6
    # 舊面板只剩 a、b 兩列：新非 null 者＝a、b、c、e；兩邊皆有＝a、b；
    # 僅新有＝c、e（d 新為 null、舊也缺，不計）
    got2 = pt.target_diff_by_period(new, old.head(2), 20).row(0, named=True)
    assert (got2["n_both"], got2["n_new_only"], got2["n_old_only"]) == (2, 2, 0)


def test_span_floor_and_check_gate() -> None:
    share = pl.DataFrame({
        "ym": ["2025-01", "2025-02", "2025-03", "2026-05", "2026-06", "2026-07", "2026-08"],
        "share_eq": [0.95, 0.94, 0.96, 0.97, 0.955, 0.92, 0.94],
    })
    floor = pt.span_floor(share, 2025, 1.0)
    assert floor == pytest.approx(0.93)                           # 2025 最低月 0.94 − 1pp
    assert pt.span_check(share, floor, date(2026, 6, 1)) == (False, ["2026-07"])  # 0.92 < 0.93
    assert pt.span_check(share, floor, date(2026, 8, 1)) == (True, [])
    assert pt.span_check(share, floor, date(2026, 12, 1)) == (None, [])   # 之後無月份 → 無法判定
    assert pt.span_floor(share, 2019, 1.0) is None                        # 基線年無資料
    assert pt.span_check(share, None, date(2026, 6, 1)) == (None, [])


def test_config_from_settings_and_defaults() -> None:
    cfg = pt.PanelTrConfig.from_settings({
        "backtest": {"panel_tr": {
            "start_date": "2023-01-02", "horizons_td": [20], "old_panel_end": date(2026, 8, 28),
            "span_floor_margin_pp": 2.0, "span_check_since": "2026-07-01",
            "diff_month_from": "2026-01", "mpick2_recent_from": "2026-06-01",
            "spot_checks": [["3055", "2026-06-18"], ["2330", "2024-06-06"]],
            "output_name": "x.parquet",
        }}
    })
    assert (cfg.start, cfg.horizons) == (date(2023, 1, 2), (20,))
    assert cfg.old_panel_end == date(2026, 8, 28)
    assert cfg.span_floor_margin_pp == 2.0 and cfg.span_check_since == date(2026, 7, 1)
    assert cfg.spot_checks == (("3055", date(2026, 6, 18)), ("2330", date(2024, 6, 6)))
    assert cfg.output_name == "x.parquet" and cfg.before_price_min_rate == 0.98  # 缺鍵走預設
    assert cfg.diff_month_from == "2026-01" and cfg.mpick2_recent_from == date(2026, 6, 1)
    default = pt.PanelTrConfig.from_settings({})
    assert default.horizons == (10, 20, 40) and default.main_horizon == 20
    assert default.diff_month_from == "2025-07" and default.mpick2_recent_from == date(2026, 5, 1)


def test_spot_check_decomposes_price_and_dividend() -> None:
    days = _days(10)
    price = _prices({"1101": [100.0] * 4 + [95.0] * 6}, days)
    ev = pl.DataFrame({"stock_id": ["1101"], "ex_date": [days[4]], "adj_factor": [100.0 / 95.0]})
    got = pt.spot_check(price, ev, "1101", days[2], 2)
    assert got is not None
    # d＝列 2：entry 列 3 收 100、exit 列 5 收 95；除息列 4 ∈ (3, 5]：純價差 −5%，還原後 0%
    assert (got["entry_date"], got["exit_date"]) == (days[3], days[5])
    assert got["price_ret_pct"] == pytest.approx(-5.0)
    assert got["total_ret_pct"] == pytest.approx(0.0, abs=1e-9)
    assert [e["aligned"] for e in got["events"]] == [days[4]]
    # d＝列 3：entry 列 4、exit 列 6；除息列 4 不在 (4, 6] → 不入窗，報酬 0%
    later = pt.spot_check(price, ev, "1101", days[3], 2)
    assert later is not None and later["events"] == []
    assert later["total_ret_pct"] == pytest.approx(0.0)
    assert pt.spot_check(price, ev, "1101", days[8], 2) is None            # 窗未到期
    assert pt.spot_check(price, ev, "1101", date(2030, 1, 1), 2) is None   # 日期不在該股列


def test_spot_check_aligns_event_on_non_trading_day_forward() -> None:
    days = _days(8)
    keep = [0, 1, 2, 5, 6, 7]                                # 列缺日 3、4（停牌／休市）
    price = _prices({"2201": [10.0, 10.0, 10.0, 9.0, 9.0, 9.0]}, [days[i] for i in keep])
    ev = pl.DataFrame({"stock_id": ["2201"], "ex_date": [days[3]], "adj_factor": [10.0 / 9.0]})
    got = pt.spot_check(price, ev, "2201", days[0], 2)
    assert got is not None
    # entry＝日 1、exit＝列序 3＝日 5；除息日 3 無列 → 對齊到第一個 ≥ 它的列＝日 5，
    # 落在 (日 1, 日 5]
    assert (got["entry_date"], got["exit_date"]) == (days[1], days[5])
    assert [e["aligned"] for e in got["events"]] == [days[5]]
    assert got["price_ret_pct"] == pytest.approx(-10.0)                 # 9/10 − 1
    assert got["total_ret_pct"] == pytest.approx(0.0, abs=1e-9)         # 9/10 × 10/9 − 1


def test_target_diff_by_period_custom_key() -> None:
    a, b = date(2026, 4, 30), date(2026, 5, 4)
    new = pl.DataFrame({"date": [a, b], "stock_id": ["x", "x"], "r20": [4.0, 9.0]})
    old = pl.DataFrame({"date": [a, b], "stock_id": ["x", "x"], "r20": [3.0, 1.0]})
    key = pl.when(pl.col("date") >= date(2026, 5, 1)).then(pl.lit("05+")).otherwise(pl.lit("<05"))
    got = pt.target_diff_by_period(new, old, 20, period=key)
    assert got["period"].to_list() == ["05+", "<05"] or got["period"].to_list() == ["<05", "05+"]
    rows = {r["period"]: r for r in got.iter_rows(named=True)}
    assert rows["<05"]["diff_median"] == pytest.approx(1.0)   # 4−3
    assert rows["05+"]["diff_median"] == pytest.approx(8.0)   # 9−1
