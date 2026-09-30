"""tests/backtest/test_intra_pick_ledger_runner.py — M-Pick3c 台帳編排（離線合成快取）。

合成宇宙：1111 價格線性上漲（52 週高點接近度＝1.0）、2222 先漲後跌、
3333 有池列但無任何快取（因子全 null）。
EPS／月營收用可手算的數字（見 _write_finmind）：eps_accel(1111)＝0.5 / P_q、rev_accel(1111)＝0.3。
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from tw_screener.backtest import intra_pick_ledger as il
from tw_screener.backtest.intra_pick_ledger_runner import run_intra_pick_ledger
from tw_screener.data.finmind import _FINANCIALS_WIDE_SCHEMA, _MONTH_REVENUE_SCHEMA
from tw_screener.report.shortlist import SHORTLIST_COLUMNS

DATA_DATE = date(2026, 10, 2)  # W40 週五
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2025, 7, 1), DATA_DATE)
N = len(DAYS)
PEAK = N - 100  # 2222 的高點位置（窗內）


def close_1111(i: int) -> float:
    return 100.0 + 0.1 * i


def close_2222(i: int) -> float:
    return 100.0 + 0.1 * i if i < PEAK else 100.0 + 0.1 * PEAK - 0.5 * (i - PEAK)


def _write_price(cache: Path, sid: str, fn: Any) -> None:
    (cache / "twse").mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "date": DAYS,
            "stock_id": [sid] * N,
            "close": [fn(i) for i in range(N)],
            "volume": [3_000_000] * N,
        },
        schema={"date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64, "volume": pl.Int64},
    ).write_parquet(cache / "twse" / f"stock_day_{sid}_202610.parquet")


def _write_finmind(cache: Path) -> None:
    fm = cache / "finmind"
    fm.mkdir(parents=True, exist_ok=True)
    # EPS：2025Q1 0.5、2025Q2 1.0、2026Q1 1.0、2026Q2 2.0
    #   → [(2.0−1.0) − (1.0−0.5)] / P_q ＝ 0.5 / P_q
    eps = {
        (2025, 1): 0.5,
        (2025, 2): 1.0,
        (2025, 3): 1.0,
        (2025, 4): 1.0,
        (2026, 1): 1.0,
        (2026, 2): 2.0,
    }
    pl.DataFrame(
        {
            "stock_id": ["1111"] * len(eps),
            "year": [k[0] for k in eps],
            "quarter": [k[1] for k in eps],
            "revenue": [1.0] * len(eps),
            "operating_income": [1.0] * len(eps),
            "income_after_tax": [1.0] * len(eps),
            "eps": list(eps.values()),
        },
        schema=_FINANCIALS_WIDE_SCHEMA,
    ).write_parquet(fm / "financials_1111.parquet")
    # 月營收 2025-03…2026-08 共 18 個月：前 12 個月 100、2026-03…05 為 120、2026-06…08 為 150
    #   YoY3(2026-08) ＝ 450/300 − 1 ＝ 0.5；YoY3(2026-05) ＝ 360/300 − 1 ＝ 0.2；rev_accel ＝ 0.3
    months = [(2025 + (2 + k) // 12, (2 + k) % 12 + 1) for k in range(18)]
    values = [100.0] * 12 + [120.0] * 3 + [150.0] * 3
    pl.DataFrame(
        {
            "stock_id": ["1111"] * 18,
            "year": [m[0] for m in months],
            "month": [m[1] for m in months],
            "revenue": values,
            "create_date": [date(2026, 9, 10)] * 18,
        },
        schema=_MONTH_REVENUE_SCHEMA,
    ).write_parquet(fm / "month_revenue_1111.parquet")


POOL = [
    {
        "stock_id": "1111",
        "name": "甲",
        "source": "candidate",
        "sub_industry": "X",
        "tier": "top",
        "rank": 1,
        "ma60_dist_pct": 7.0,
        "amount_million": 500.0,
        "close": close_1111(N - 1),
        "trend_score": 3.0,
        "trend_rank": 1,
        "trend_n": 40,
        "trend_bucket": 1,
    },
    {
        "stock_id": "2222",
        "name": "乙",
        "source": "candidate",
        "sub_industry": "X",
        "tier": "alt",
        "rank": 6,
        "ma60_dist_pct": 3.0,
        "amount_million": 300.0,
        "close": close_2222(N - 1),
        "trend_score": 3.0,
        "trend_rank": 1,
        "trend_n": 40,
        "trend_bucket": 1,
    },
    {
        "stock_id": "3333",
        "name": "丙",
        "source": "watchlist",
        "sub_industry": "Y",
        "tier": "capped",
        "gate_reason": "cap_sub_industry",
        "ma60_dist_pct": 12.5,
        "amount_million": 200.0,
        "close": 30.0,
        "trend_score": 2.0,
        "trend_rank": 5,
        "trend_n": 40,
        "trend_bucket": 1,
    },
]


def _write_shortlist(reports: Path, week: str, data_date: date = DATA_DATE) -> None:
    (reports / week).mkdir(parents=True, exist_ok=True)
    rows = [
        {c: r.get(c) for c in SHORTLIST_COLUMNS} | {"week": week, "data_date": data_date}
        for r in POOL
    ]
    pl.DataFrame(
        rows, schema_overrides={"rank": pl.Int64, "gate_reason": pl.Utf8}, infer_schema_length=None
    ).write_csv(reports / week / "shortlist.csv")


def _settings(tmp: Path, *, start_week: str | None = "2026-W40") -> Path:
    ledger_cfg: dict[str, Any] = {
        "rewrite_days": 7,
        "history_days": 320,
        "ledger_path": str(tmp / "ledger" / "ledger.csv"),
    }
    if start_week is not None:
        ledger_cfg["start_week"] = start_week
    cfg = {
        "paths": {"reports_dir": str(tmp / "reports"), "cache_dir": str(tmp / "cache")},
        "backtest": {"intra_pick": {"calendar_min_names": 2}, "intra_pick_ledger": ledger_cfg},
    }
    path = tmp / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path: Path) -> Path:
    _write_price(tmp_path / "cache", "1111", close_1111)
    _write_price(tmp_path / "cache", "2222", close_2222)
    _write_finmind(tmp_path / "cache")
    _write_shortlist(tmp_path / "reports", "2026-W40")
    return tmp_path


def _ledger(tmp: Path) -> pl.DataFrame:
    return il.read_ledger(tmp / "ledger" / "ledger.csv")


def test_runner_writes_frozen_factor_values(env: Path) -> None:
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 0
    by = {r["stock_id"]: r for r in _ledger(env).iter_rows(named=True)}
    assert sorted(by) == ["1111", "2222", "3333"]

    last = N - 1
    # 1111：單調上漲 → 高點接近度 1.0；mom_6_1 ＝ close[t−21] / close[t−126] − 1
    assert by["1111"]["high52_near"] == pytest.approx(1.0)
    assert by["1111"]["mom_6_1"] == pytest.approx(
        close_1111(last - 21) / close_1111(last - 126) - 1
    )
    # eps_accel ＝ 0.5 / P_q，P_q＝2026Q2 最後一個交易日（2026-06-30）收盤
    i_q = DAYS.index(date(2026, 6, 30))
    assert by["1111"]["eps_accel"] == pytest.approx(0.5 / close_1111(i_q))
    assert by["1111"]["rev_accel"] == pytest.approx(0.3)
    # 2222：先漲後跌，接近度＝現價 / 窗內最高價（250 日窗內的高點＝PEAK 那天）
    window_max = max(close_2222(i) for i in range(last - 249, last + 1))
    assert by["2222"]["high52_near"] == pytest.approx(close_2222(last) / window_max)
    assert by["2222"]["high52_near"] < 1.0
    assert (
        by["2222"]["eps_accel"] is None and by["2222"]["rev_accel"] is None
    )  # 無 FinMind 資料 → null，不補值
    # 3333：有池列、沒有任何快取 → 因子全 null，但列仍記錄（tier／band_dist 是生產值）
    assert all(by["3333"][c] is None for c in il.FACTOR_COLS)
    assert (by["3333"]["tier"], by["3333"]["gate_reason"], by["3333"]["band_dist"]) == (
        "capped",
        "cap_sub_industry",
        2.5,
    )
    assert by["1111"]["band_dist"] == 0.0 and by["2222"]["band_dist"] == 2.0
    # 稽核欄
    assert {r["week"] for r in by.values()} == {"2026-W40"}
    assert {r["data_date"] for r in by.values()} == {DATA_DATE}
    assert {r["recorded_at"] for r in by.values()} == {"2026-10-03T12:00:00Z"}
    assert {r["eps_quarter"] for r in by.values()} == {"2026Q2"}
    assert {r["rev_month"] for r in by.values()} == {"2026-08"}


def test_runner_uses_only_data_date_even_if_cache_has_later_days(env: Path) -> None:
    """日線快取含 data_date 之後的日子（晚幾天補跑）時，因子仍以 data_date 為基準。

    因子是 trailing 窗，不受未來列影響。
    """
    later = DAYS + [date(2026, 10, 5), date(2026, 10, 6)]
    for sid, fn in (("1111", close_1111), ("2222", close_2222)):
        pl.DataFrame(
            {
                "date": later,
                "stock_id": [sid] * len(later),
                "close": [fn(i) for i in range(len(later))],
                "volume": [3_000_000] * len(later),
            },
            schema={"date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64, "volume": pl.Int64},
        ).write_parquet(env / "cache" / "twse" / f"stock_day_{sid}_202610.parquet")
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 0
    row = _ledger(env).filter(pl.col("stock_id") == "1111").row(0, named=True)
    assert row["high52_near"] == pytest.approx(1.0)
    assert row["mom_6_1"] == pytest.approx(close_1111(N - 1 - 21) / close_1111(N - 1 - 126) - 1)


def test_runner_skips_weeks_before_start_week(env: Path) -> None:
    _write_shortlist(env / "reports", "2026-W39", data_date=date(2026, 9, 25))
    assert run_intra_pick_ledger(_settings(env), "2026-W39", now=NOW) == 0
    assert not (env / "ledger" / "ledger.csv").exists()  # 乾淨樣本起點之前不回補


def test_runner_default_week_is_latest_report_dir(env: Path) -> None:
    _write_shortlist(env / "reports", "2026-W39", data_date=date(2026, 9, 25))  # 較舊的週不該被選中
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 0
    assert _ledger(env)["week"].unique().to_list() == ["2026-W40"]


def test_runner_requires_start_week_setting(env: Path) -> None:
    assert run_intra_pick_ledger(_settings(env, start_week=None), None, now=NOW) == 1
    assert not (env / "ledger" / "ledger.csv").exists()


def test_runner_fails_without_shortlist(env: Path) -> None:
    (env / "reports" / "2026-W40" / "shortlist.csv").unlink()
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 1
    assert not (env / "ledger" / "ledger.csv").exists()


def test_runner_fails_when_data_date_not_in_price_calendar(env: Path) -> None:
    _write_shortlist(env / "reports", "2026-W40", data_date=date(2026, 10, 9))  # 日線快取沒有這天
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 1
    assert not (env / "ledger" / "ledger.csv").exists()


def test_runner_rerun_within_window_replaces_then_freezes(env: Path) -> None:
    settings = _settings(env)
    assert run_intra_pick_ledger(settings, None, now=NOW) == 0
    first = _ledger(env)
    assert (
        run_intra_pick_ledger(settings, None, now=NOW + timedelta(days=3)) == 0
    )  # 期限內補跑：整週替換
    second = _ledger(env)
    assert second.height == first.height
    assert set(second["recorded_at"].to_list()) == {"2026-10-06T12:00:00Z"}

    assert (
        run_intra_pick_ledger(settings, None, now=datetime(2026, 10, 10, 0, 1, tzinfo=UTC)) == 1
    )  # deadline 10/09 之後
    assert _ledger(env).equals(second)  # 拒寫，底帳不動


def test_runner_first_write_after_deadline_is_refused_before_loading_caches(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新週逾期才首次寫入 → exit 1、不建檔；且在載入日線快取（約 1 分鐘）之前就 fail-fast。"""

    def must_not_load(*_args: object, **_kwargs: object) -> pl.DataFrame:
        raise AssertionError("逾期寫入應在載入日線快取之前就被拒絕")

    monkeypatch.setattr("tw_screener.analysis.rotation.load_market_history", must_not_load)
    late = datetime(2026, 10, 10, 0, 1, tzinfo=UTC)  # DATA_DATE=10/02 → 期限 10/09
    assert run_intra_pick_ledger(_settings(env), None, now=late) == 1
    assert not (env / "ledger" / "ledger.csv").exists()


def test_runner_rejects_shortlist_whose_week_column_disagrees_with_directory(env: Path) -> None:
    """shortlist.csv 被複製到別週目錄時，不能把它記在錯的週底下。"""
    # 週欄是 W39、但 data_date＝W40 的資料日（期限內）——排除「逾期」這條規則的干擾，只驗週欄交叉檢查
    _write_shortlist(env / "reports", "2026-W39", data_date=DATA_DATE)
    shutil.copy(
        env / "reports" / "2026-W39" / "shortlist.csv",
        env / "reports" / "2026-W40" / "shortlist.csv",
    )
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 1
    assert not (env / "ledger" / "ledger.csv").exists()


def test_runner_reports_unreadable_existing_ledger_without_touching_it(env: Path) -> None:
    ledger_file = env / "ledger" / "ledger.csv"
    ledger_file.parent.mkdir(parents=True)
    ledger_file.write_text("week,stock_id\n2026-W39,1111\n", encoding="utf-8")  # 欄位不符的損毀底帳
    before = ledger_file.read_bytes()
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 1  # 回 1、不丟 traceback
    assert ledger_file.read_bytes() == before


def test_runner_fails_loudly_on_shortlist_type_drift(env: Path) -> None:
    path = env / "reports" / "2026-W40" / "shortlist.csv"
    df = pl.read_csv(path, infer_schema_length=None, schema_overrides={"stock_id": pl.Utf8})
    drifted = df.with_columns(
        pl.when(pl.col("stock_id") == "2222")
        .then(pl.lit("N/A"))
        .otherwise(pl.col("trend_score").cast(pl.Utf8))
        .alias("trend_score")
    )
    drifted.write_csv(path)
    assert run_intra_pick_ledger(_settings(env), None, now=NOW) == 1
    assert not (env / "ledger" / "ledger.csv").exists()  # 寧可少一週，不把 N/A 悄悄變成 null 寫進去
