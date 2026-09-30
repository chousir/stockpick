"""tests/backtest/test_intra_pick_ledger.py — M-Pick3c 前瞻台帳純函式（docs/35）。

期望值皆手算：偏好帶預設 (5, 10)，帶外距離＝下緣減值／值減上緣；
凍結規則邊界（deadline 當日可寫、隔日拒絕）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_ledger as il
from tw_screener.report.shortlist import SHORTLIST_COLUMNS, ShortlistConfig

D = date(2026, 10, 2)  # W40 的資料日（週五）
WEEK = "2026-W40"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CFG = ip.IntraPickConfig()  # 出貨預設：eps 期限 5/31、9/1、11/30、次年 4/1；營收次月 11 日起可用
SL = ShortlistConfig()  # 偏好帶 (5.0, 10.0)


def make_shortlist(
    rows: list[dict[str, object]], week: str = WEEK, data_date: date | None = D
) -> pl.DataFrame:
    """依 SHORTLIST_COLUMNS 補齊未指定欄（None），型別交給 polars 推斷後由函式內 cast。"""
    full = [
        {c: r.get(c) for c in SHORTLIST_COLUMNS} | {"week": week, "data_date": data_date}
        for r in rows
    ]
    return pl.DataFrame(
        full,
        schema_overrides={
            "week": pl.Utf8,
            "data_date": pl.Date,
            "stock_id": pl.Utf8,
            "rank": pl.Int64,
            "tier": pl.Utf8,
            "gate_reason": pl.Utf8,
            "sub_industry": pl.Utf8,
            "name": pl.Utf8,
            "source": pl.Utf8,
            "ma60_dist_pct": pl.Float64,
            "amount_million": pl.Float64,
            "close": pl.Float64,
            "trend_score": pl.Float64,
            "trend_rank": pl.Int64,
            "trend_n": pl.Int64,
            "trend_bucket": pl.Int64,
        },
        infer_schema_length=None,
    )


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
        "close": 100.0,
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
        "close": 50.0,
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
    {
        "stock_id": "4444",
        "name": "丁",
        "source": "candidate",
        "sub_industry": None,
        "tier": "gated",
        "gate_reason": "ext_unknown",
        "ma60_dist_pct": None,
        "amount_million": 90.0,
        "close": 20.0,
    },
]


def frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    feats = pl.DataFrame(
        {
            "date": [D, date(2026, 9, 25), D],
            "stock_id": ["1111", "1111", "9999"],  # 舊日列與池外股票都不得進台帳
            "mom_6_1": [0.10, 0.99, 0.5],
            "high52_near": [0.95, 0.11, 0.6],
        }
    )
    eps = pl.DataFrame({"date": [D, D], "stock_id": ["1111", "3333"], "eps_accel": [0.01, -0.02]})
    rev = pl.DataFrame({"date": [D], "stock_id": ["1111"], "rev_accel": [0.05]})
    return feats, eps, rev


def build(pool: list[dict[str, object]] | None = None) -> pl.DataFrame:
    feats, eps, rev = frames()
    return il.build_ledger_rows(
        make_shortlist(POOL if pool is None else pool),
        feats,
        eps,
        rev,
        sl_cfg=SL,
        ip_cfg=CFG,
        recorded_at="2026-10-03T04:00:00Z",
    )


# ─── 週次閘與期別標籤 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("week", "start", "expected"),
    [
        ("2026-W39", "2026-W40", True),
        ("2026-W40", "2026-W40", False),
        ("2026-W41", "2026-W40", False),
        ("2027-W01", "2026-W40", False),
        ("2025-W52", "2026-W01", True),
    ],
)
def test_is_before_start(week: str, start: str, expected: bool) -> None:
    assert il.is_before_start(week, start) is expected


@pytest.mark.parametrize("bad", ["2026-W4", "2026W40", "26-W40", "2026-w40", ""])
def test_is_before_start_rejects_malformed_week(bad: str) -> None:
    with pytest.raises(ValueError):
        il.is_before_start(bad, "2026-W40")
    with pytest.raises(ValueError):
        il.is_before_start("2026-W40", bad)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-W40", True),
        ("2026-W4", False),
        ("2026-w40", False),
        ("", False),
        ("2026-W401", False),
    ],
)
def test_is_week_tag(value: str, expected: bool) -> None:
    assert il.is_week_tag(value) is expected


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2026, 10, 2), "2026Q2"),  # Q2 期限次日 9/1 起可用；Q3 要到 11/30
        (date(2026, 9, 1), "2026Q2"),  # 期限當日起可用（<=）
        (date(2026, 8, 31), "2026Q1"),  # 前一日仍是 Q1
        (date(2026, 12, 1), "2026Q3"),
        (date(2027, 1, 15), "2026Q3"),  # Q4 要到次年 4/1
        (date(2027, 4, 1), "2026Q4"),
    ],
)
def test_eps_quarter_label(d: date, expected: str) -> None:
    assert il.eps_quarter_label(d, CFG) == expected


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2026, 10, 2), "2026-08"),  # 9 月營收要到 10/11 才可用
        (date(2026, 10, 10), "2026-08"),
        (date(2026, 10, 11), "2026-09"),
        (date(2026, 1, 5), "2025-11"),  # 跨年
        (date(2026, 1, 20), "2025-12"),
    ],
)
def test_rev_month_label(d: date, expected: str) -> None:
    assert il.rev_month_label(d, CFG) == expected


def test_labels_agree_with_intra_pick_asof_functions() -> None:
    """標籤是 intra_pick.eps_asof／revenue_asof 查找規則的鏡射；兩邊若漂移，這裡先紅。

    做法：讓查找表的值＝期別索引本身，as-of 函式回傳的值就是它實際查了哪一期。
    """
    days = [date(2026, m, d) for m in range(1, 13) for d in (1, 10, 11, 12, 28)] + [
        date(2027, 4, 1)
    ]
    keys = pl.DataFrame({"date": days, "stock_id": ["A"] * len(days)})

    qtable = pl.DataFrame(
        {
            "stock_id": ["A"] * 40,
            "qi": list(range(2020 * 4, 2020 * 4 + 40)),
            "eps_accel": [float(q) for q in range(2020 * 4, 2020 * 4 + 40)],
        }
    )
    got_q = ip.eps_asof(keys, qtable, CFG).sort("date")
    for d, qi in zip(got_q["date"], got_q["eps_accel"], strict=True):
        assert il.eps_quarter_label(d, CFG) == f"{int(qi) // 4}Q{int(qi) % 4 + 1}"

    mtable = pl.DataFrame(
        {
            "stock_id": ["A"] * 100,
            "mi": list(range(2020 * 12, 2020 * 12 + 100)),
            "rev_accel": [float(m) for m in range(2020 * 12, 2020 * 12 + 100)],
        }
    )
    got_m = ip.revenue_asof(keys, mtable, CFG.revenue_available_day).sort("date")
    for d, mi in zip(got_m["date"], got_m["rev_accel"], strict=True):
        assert il.rev_month_label(d, CFG) == f"{int(mi) // 12}-{int(mi) % 12 + 1:02d}"


def _fin(year: int, quarter: int) -> pl.DataFrame:
    return pl.DataFrame({"stock_id": ["A"], "year": [year], "quarter": [quarter], "eps": [1.0]})


def _rev(year: int, month: int) -> pl.DataFrame:
    return pl.DataFrame({"stock_id": ["A"], "year": [year], "month": [month], "revenue": [1.0]})


def test_cache_staleness_fresh_caches_give_no_notes() -> None:
    # 10/02：F3 應查 2026Q2、F4 應查 2026-08
    assert il.cache_staleness(_fin(2026, 2), _rev(2026, 8), D, CFG) == []


def test_cache_staleness_revenue_falls_behind_after_the_11th() -> None:
    notes = il.cache_staleness(
        _fin(2026, 2), _rev(2026, 8), date(2026, 10, 12), CFG
    )  # 9 月營收 10/11 起可用
    assert len(notes) == 1
    assert (
        "2026-09" in notes[0]
        and "最新月 2026-08" in notes[0]
        and "backfill-finmind-revenue" in notes[0]
    )


def test_cache_staleness_financials_fall_behind_after_the_q3_deadline() -> None:
    notes = il.cache_staleness(
        _fin(2026, 2), _rev(2026, 10), date(2026, 12, 1), CFG
    )  # Q3 自 11/30 起可用
    assert len(notes) == 1
    assert (
        "2026Q3" in notes[0]
        and "最新季 2026Q2" in notes[0]
        and "backfill-finmind-financials" in notes[0]
    )


def test_cache_staleness_empty_caches() -> None:
    empty_fin = pl.DataFrame(schema={"stock_id": pl.Utf8, "year": pl.Int64, "quarter": pl.Int64})
    empty_rev = pl.DataFrame(schema={"stock_id": pl.Utf8, "year": pl.Int64, "month": pl.Int64})
    notes = il.cache_staleness(empty_fin, empty_rev, D, CFG)
    assert len(notes) == 2 and all("為空" in n for n in notes)


def test_cache_staleness_any_stock_at_the_period_counts_as_fresh() -> None:
    """快取層級判斷：只要有任一檔到了應查期就算新鮮（逐檔缺值由覆蓋率警告負責）。"""
    fin = pl.concat([_fin(2025, 4), _fin(2026, 2)])
    rev = pl.concat([_rev(2026, 5), _rev(2026, 8)])
    assert il.cache_staleness(fin, rev, D, CFG) == []


# ─── build_ledger_rows ───────────────────────────────────────────────────────


def test_build_ledger_rows_shape_and_dtypes() -> None:
    out = build()
    assert out.columns == list(il.LEDGER_COLUMNS)
    assert dict(out.schema) == il.LEDGER_SCHEMA
    assert out["stock_id"].to_list() == [
        "1111",
        "2222",
        "3333",
        "4444",
    ]  # 全部 tier 都記（含 gated）
    assert out["week"].unique().to_list() == [WEEK]
    assert out["data_date"].unique().to_list() == [D]
    assert out["recorded_at"].unique().to_list() == ["2026-10-03T04:00:00Z"]
    assert out["eps_quarter"].unique().to_list() == ["2026Q2"]
    assert out["rev_month"].unique().to_list() == ["2026-08"]


def test_build_ledger_rows_factor_values_and_date_filter() -> None:
    out = build().sort("stock_id")
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    # 1111：只取 data_date 當日（0.10／0.95），不取 9/25 的舊列（0.99／0.11）
    assert (by["1111"]["mom_6_1"], by["1111"]["high52_near"]) == (0.10, 0.95)
    assert (by["1111"]["eps_accel"], by["1111"]["rev_accel"]) == (0.01, 0.05)
    # 2222：三個因子表都沒有它 → 全 null（不補值）
    assert all(by["2222"][c] is None for c in il.FACTOR_COLS)
    # 3333：只有 EPS 加速有值
    assert by["3333"]["eps_accel"] == -0.02
    assert by["3333"]["mom_6_1"] is None and by["3333"]["rev_accel"] is None
    # 池外的 9999 不入台帳
    assert "9999" not in out["stock_id"].to_list()


def test_build_ledger_rows_band_dist_hand_computed() -> None:
    by = {r["stock_id"]: r["band_dist"] for r in build().iter_rows(named=True)}
    assert by["1111"] == 0.0  # 7.0 在 [5, 10] 帶內
    assert by["2222"] == 2.0  # 5 − 3.0
    assert by["3333"] == 2.5  # 12.5 − 10
    assert by["4444"] is None  # ma60_dist_pct 缺


def test_build_ledger_rows_keeps_production_columns() -> None:
    by = {r["stock_id"]: r for r in build().iter_rows(named=True)}
    assert (by["1111"]["tier"], by["1111"]["rank"], by["1111"]["gate_reason"]) == ("top", 1, None)
    assert (by["3333"]["tier"], by["3333"]["gate_reason"], by["3333"]["source"]) == (
        "capped",
        "cap_sub_industry",
        "watchlist",
    )
    assert by["2222"]["amount_million"] == 300.0 and by["2222"]["ma60_dist_pct"] == 3.0
    assert by["4444"]["sub_industry"] is None


def test_build_ledger_rows_accepts_string_data_date_like_csv_load() -> None:
    """shortlist.csv 經 load_shortlist 讀入時 data_date 是字串——與 Date 輸入結果須一致。"""
    feats, eps, rev = frames()
    as_str = make_shortlist(POOL).with_columns(pl.col("data_date").cast(pl.Utf8))
    assert as_str.schema["data_date"] == pl.Utf8
    out = il.build_ledger_rows(
        as_str, feats, eps, rev, sl_cfg=SL, ip_cfg=CFG, recorded_at="2026-10-03T04:00:00Z"
    )
    assert out.equals(build())
    assert out.schema["data_date"] == pl.Date


def test_build_ledger_rows_rejects_mixed_or_undated_shortlist() -> None:
    feats, eps, rev = frames()
    two_weeks = pl.concat([make_shortlist(POOL[:1]), make_shortlist(POOL[1:2], week="2026-W41")])
    with pytest.raises(ValueError, match="單一週次"):
        il.build_ledger_rows(two_weeks, feats, eps, rev, sl_cfg=SL, ip_cfg=CFG, recorded_at="t")
    with pytest.raises(ValueError, match="data_date"):
        il.build_ledger_rows(
            make_shortlist(POOL, data_date=None),
            feats,
            eps,
            rev,
            sl_cfg=SL,
            ip_cfg=CFG,
            recorded_at="t",
        )


def test_build_ledger_rows_rejects_missing_source_columns() -> None:
    feats, eps, rev = frames()
    with pytest.raises(ValueError, match="shortlist 缺欄"):
        il.build_ledger_rows(
            make_shortlist(POOL).drop("tier"),
            feats,
            eps,
            rev,
            sl_cfg=SL,
            ip_cfg=CFG,
            recorded_at="t",
        )


def test_build_ledger_rows_type_drift_fails_loudly_instead_of_nulling() -> None:
    """上游數值欄出現字串時要報錯，而不是靜默轉成 null 進凍結證據。"""
    feats, eps, rev = frames()
    drifted = make_shortlist(POOL).with_columns(
        pl.when(pl.col("stock_id") == "2222")
        .then(pl.lit("N/A"))
        .otherwise(pl.col("trend_score").cast(pl.Utf8))
        .alias("trend_score")
    )
    assert drifted.schema["trend_score"] == pl.Utf8
    with pytest.raises(pl.exceptions.PolarsError):
        il.build_ledger_rows(drifted, feats, eps, rev, sl_cfg=SL, ip_cfg=CFG, recorded_at="t")


def test_build_ledger_rows_rejects_duplicate_factor_keys() -> None:
    feats, eps, rev = frames()
    dup = pl.concat([feats, feats.filter((pl.col("stock_id") == "1111") & (pl.col("date") == D))])
    with pytest.raises(ValueError, match="重複鍵"):
        il.build_ledger_rows(
            make_shortlist(POOL), dup, eps, rev, sl_cfg=SL, ip_cfg=CFG, recorded_at="t"
        )


# ─── upsert_ledger（凍結規則）────────────────────────────────────────────────


def ledger_rows(
    week: str, data_date: date, ids: tuple[str, ...] = ("1111", "2222"), eps: float | None = 0.01
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "week": [week] * len(ids),
            "data_date": [data_date] * len(ids),
            "recorded_at": ["t"] * len(ids),
            "stock_id": list(ids),
            "name": ["n"] * len(ids),
            "source": ["candidate"] * len(ids),
            "sub_industry": ["X"] * len(ids),
            "tier": ["top"] * len(ids),
            "rank": list(range(1, len(ids) + 1)),
            "gate_reason": [None] * len(ids),
            "trend_score": [1.0] * len(ids),
            "trend_rank": [1] * len(ids),
            "trend_n": [40] * len(ids),
            "trend_bucket": [1] * len(ids),
            "close": [10.0] * len(ids),
            "ma60_dist_pct": [6.0] * len(ids),
            "amount_million": [100.0] * len(ids),
            "band_dist": [0.0] * len(ids),
            "mom_6_1": [0.1] * len(ids),
            "high52_near": [0.9] * len(ids),
            "eps_accel": [eps] * len(ids),
            "rev_accel": [None] * len(ids),
            "eps_quarter": ["2026Q2"] * len(ids),
            "rev_month": ["2026-08"] * len(ids),
        },
        schema=il.LEDGER_SCHEMA,
    )


def test_upsert_creates_and_appends_weeks(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "ledger.csv"
    il.upsert_ledger(path, ledger_rows("2026-W40", D), now=NOW, rewrite_days=7)
    merged = il.upsert_ledger(
        path, ledger_rows("2026-W41", D + timedelta(days=7)), now=NOW, rewrite_days=7
    )
    assert merged["week"].to_list() == ["2026-W40"] * 2 + ["2026-W41"] * 2
    assert il.read_ledger(path).equals(merged)
    assert not list(tmp_path.glob("sub/*.tmp"))  # 不留暫存檔


def test_upsert_same_week_within_window_replaces_only_that_week(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    il.upsert_ledger(
        path,
        ledger_rows("2026-W39", D - timedelta(days=7)),
        now=NOW - timedelta(days=30),
        rewrite_days=7,
    )
    il.upsert_ledger(path, ledger_rows("2026-W40", D), now=NOW, rewrite_days=7)
    before_w39 = il.read_ledger(path).filter(pl.col("week") == "2026-W39")

    rerun = ledger_rows(
        "2026-W40", D, ids=("1111", "2222", "3333"), eps=0.5
    )  # 補跑後多一檔、值不同
    merged = il.upsert_ledger(
        path, rerun, now=datetime(2026, 10, 9, 23, 0, tzinfo=UTC), rewrite_days=7
    )

    w40 = merged.filter(pl.col("week") == "2026-W40")
    assert w40["stock_id"].to_list() == ["1111", "2222", "3333"]
    assert w40["eps_accel"].to_list() == [0.5, 0.5, 0.5]
    assert merged.filter(pl.col("week") == "2026-W39").equals(before_w39)  # 他週不動


def test_upsert_freeze_boundary(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    il.upsert_ledger(
        path, ledger_rows("2026-W40", D), now=NOW, rewrite_days=7
    )  # deadline = 2026-10-09
    assert il.rewrite_deadline(D, 7) == date(2026, 10, 9)

    il.upsert_ledger(
        path,
        ledger_rows("2026-W40", D, eps=0.2),
        now=datetime(2026, 10, 9, 23, 59, tzinfo=UTC),
        rewrite_days=7,
    )
    frozen_snapshot = il.read_ledger(path)
    assert frozen_snapshot["eps_accel"].to_list() == [0.2, 0.2]  # deadline 當日仍可寫

    with pytest.raises(il.LedgerFrozenError, match="2026-W40 重寫已逾期"):
        il.upsert_ledger(
            path,
            ledger_rows("2026-W40", D, eps=0.9),
            now=datetime(2026, 10, 10, 0, 1, tzinfo=UTC),
            rewrite_days=7,
        )
    assert il.read_ledger(path).equals(frozen_snapshot)  # 拒寫後底帳不動


def test_upsert_first_write_after_deadline_is_refused(tmp_path: Path) -> None:
    """新週逾期才首次寫入也拒絕——不得事後（看到結果後）才決定要記哪幾週。"""
    path = tmp_path / "ledger.csv"
    late = datetime(2026, 10, 10, 0, 1, tzinfo=UTC)  # D=10/02 → 期限 10/09
    with pytest.raises(il.LedgerFrozenError, match="2026-W40 首次寫入已逾期"):
        il.upsert_ledger(path, ledger_rows("2026-W40", D), now=late, rewrite_days=7)
    assert not path.exists()  # 拒寫且不建檔


def test_upsert_first_write_on_the_deadline_day_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    edge = datetime(2026, 10, 9, 23, 59, tzinfo=UTC)
    merged = il.upsert_ledger(path, ledger_rows("2026-W40", D), now=edge, rewrite_days=7)
    assert merged.height == 2


def test_upsert_a_frozen_week_does_not_block_another_week(tmp_path: Path) -> None:
    """期限逐週各算各的：W40 已凍結不影響仍在自己期限內的 W41。"""
    path = tmp_path / "ledger.csv"
    il.upsert_ledger(path, ledger_rows("2026-W40", D), now=NOW, rewrite_days=7)
    on_w41 = datetime(
        2026, 10, 12, tzinfo=UTC
    )  # W40 已逾期（10/09），W41 的 data_date=10/09 → 期限 10/16
    with pytest.raises(il.LedgerFrozenError):
        il.upsert_ledger(path, ledger_rows("2026-W40", D), now=on_w41, rewrite_days=7)
    merged = il.upsert_ledger(
        path, ledger_rows("2026-W41", D + timedelta(days=7)), now=on_w41, rewrite_days=7
    )
    assert merged["week"].unique(maintain_order=True).to_list() == ["2026-W40", "2026-W41"]


def test_upsert_fails_closed_when_deadline_cannot_be_determined(tmp_path: Path) -> None:
    """既有列 data_date 全缺（手改 CSV）或新列無 data_date → 視為已凍結，而不是永遠可寫。"""
    path = tmp_path / "ledger.csv"
    undated = ledger_rows("2026-W40", D).with_columns(
        pl.lit(None, dtype=pl.Date).alias("data_date")
    )
    with pytest.raises(il.LedgerFrozenError, match="無法判定期限"):
        il.upsert_ledger(path, undated, now=NOW, rewrite_days=7)  # 新週、新列無 data_date
    assert not path.exists()

    il.upsert_ledger(path, ledger_rows("2026-W40", D), now=NOW, rewrite_days=7)
    tampered = il.read_ledger(path).with_columns(pl.lit(None, dtype=pl.Date).alias("data_date"))
    tampered.write_csv(path)
    before = path.read_bytes()
    with pytest.raises(il.LedgerFrozenError, match="無法判定期限"):
        il.upsert_ledger(path, ledger_rows("2026-W40", D, eps=0.9), now=NOW, rewrite_days=7)
    assert path.read_bytes() == before


def test_write_window_uses_the_earliest_data_date_of_the_week() -> None:
    """同週列若含多個 data_date（異常情形），以最早者為基準——寧嚴勿鬆。"""
    old = pl.concat(
        [ledger_rows("2026-W40", D), ledger_rows("2026-W40", D + timedelta(days=4), ids=("3333",))]
    )
    day = datetime(
        2026, 10, 10, tzinfo=UTC
    )  # 以最早 D=10/02 → 期限 10/09 已過；若誤取 10/06 → 期限 10/13 會放行
    with pytest.raises(il.LedgerFrozenError, match="重寫已逾期"):
        il.check_write_window(old, "2026-W40", D, now=day, rewrite_days=7)


def test_check_write_window_matches_upsert_rules() -> None:
    """runner 用 check_write_window 在載入快取前 fail-fast；它與 upsert_ledger 是同一條規則。"""
    empty = pl.DataFrame(schema=il.LEDGER_SCHEMA)
    il.check_write_window(empty, "2026-W40", D, now=NOW, rewrite_days=7)  # 期限內
    with pytest.raises(il.LedgerFrozenError):
        il.check_write_window(
            empty, "2026-W40", D, now=datetime(2026, 10, 10, tzinfo=UTC), rewrite_days=7
        )
    with pytest.raises(il.LedgerFrozenError, match="無法判定期限"):
        il.check_write_window(empty, "2026-W40", None, now=NOW, rewrite_days=7)
    existing = ledger_rows("2026-W40", D)
    il.check_write_window(existing, "2026-W40", D, now=NOW, rewrite_days=7)  # 重寫、期限內
    with pytest.raises(il.LedgerFrozenError, match="重寫已逾期"):
        il.check_write_window(
            existing, "2026-W40", D, now=datetime(2026, 10, 10, tzinfo=UTC), rewrite_days=7
        )


def test_upsert_empty_and_multiweek_inputs(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    assert il.upsert_ledger(
        path, pl.DataFrame(schema=il.LEDGER_SCHEMA), now=NOW, rewrite_days=7
    ).is_empty()
    assert not path.exists()  # 空輸入不建檔
    two = pl.concat([ledger_rows("2026-W40", D), ledger_rows("2026-W41", D)])
    with pytest.raises(ValueError, match="一次只 upsert 一週"):
        il.upsert_ledger(path, two, now=NOW, rewrite_days=7)


def test_ledger_roundtrip_keeps_all_null_float_columns_float(tmp_path: Path) -> None:
    """某週某因子全 null（例如快取落後）時，重讀不得被推斷成字串而污染跨週 concat。"""
    path = tmp_path / "ledger.csv"
    il.upsert_ledger(path, ledger_rows("2026-W40", D, eps=None), now=NOW, rewrite_days=7)
    reread = il.read_ledger(path)
    assert reread.schema["eps_accel"] == pl.Float64 and reread.schema["rev_accel"] == pl.Float64
    assert reread["data_date"].dtype == pl.Date
    merged = il.upsert_ledger(
        path, ledger_rows("2026-W41", D + timedelta(days=7), eps=0.3), now=NOW, rewrite_days=7
    )
    assert merged["eps_accel"].to_list() == [None, None, 0.3, 0.3]


# ─── week_summary ────────────────────────────────────────────────────────────


def test_week_summary_counts_only_main_tiers() -> None:
    rows = build()  # top 1111（sub X）、alt 2222（sub X）、capped 3333（sub Y）、gated 4444
    s = il.week_summary(rows, min_coverage=0.70)
    assert (s["n_rows"], s["n_main"], s["n_top"]) == (4, 3, 1)
    assert (s["n_groups"], s["n_groups_ge2"]) == (2, 1)  # X 有 2 檔、Y 只有 1 檔
    cov = s["coverage"]
    assert isinstance(cov, dict)
    # 合格成員 3 檔：mom_6_1 只有 1111 有 → 1/3；eps_accel 1111、3333 → 2/3；
    # rev_accel 只有 1111 → 1/3
    assert cov["mom_6_1"] == pytest.approx(1 / 3)
    assert cov["high52_near"] == pytest.approx(1 / 3)
    assert cov["eps_accel"] == pytest.approx(2 / 3)
    assert cov["rev_accel"] == pytest.approx(1 / 3)
    assert s["low_coverage"] == ["eps_accel", "high52_near", "mom_6_1", "rev_accel"]  # 全部 < 0.70


def test_week_summary_gated_rows_do_not_drag_coverage() -> None:
    """gated／held 列因子再空也不算進覆蓋率分母。"""
    rows = build()
    only_main = rows.filter(pl.col("tier").is_in(il.MAIN_TIERS))
    assert il.week_summary(rows, 0.5)["coverage"] == il.week_summary(only_main, 0.5)["coverage"]


def test_week_summary_without_main_members() -> None:
    rows = build().filter(pl.col("tier") == "gated")
    s = il.week_summary(rows, min_coverage=0.70)
    assert (s["n_main"], s["n_groups"], s["n_groups_ge2"], s["coverage"], s["low_coverage"]) == (
        0,
        0,
        0,
        {},
        [],
    )
