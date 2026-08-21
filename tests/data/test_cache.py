"""快取載入優化測試（規劃書 01 P1：先按檔名日期過濾再讀）。

select_recent_cache_files 是「安全超集」過濾器——核心契約：
1. 不可漏收任何「落在近 n_days 交易日窗」的檔（漏收＝正確性 bug）。
2. 可超收（呼叫端會精確 filter）。
3. 維持輸入順序（load_market_history 的 unique(keep="first") 依賴 pattern 優先序）。
並驗 load_institutional_history 套用後與「全讀再 filter」逐值等價。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from tw_screener.data.cache import (
    is_fresh_with_columns,
    save_parquet,
    select_prune_candidates,
    select_recent_cache_files,
)
from tw_screener.data.twse import _INSTITUTIONAL_SCHEMA, TWSEClient


def _p(name: str) -> Path:
    return Path("/cache") / name


# ── select_recent_cache_files ──────────────────────────────────────────────


def test_empty_input_returns_empty() -> None:
    assert select_recent_cache_files([], 20) == []


def test_unparseable_tokens_kept_as_fallback() -> None:
    files = [_p("fundamentals_2026Q1.parquet"), _p("weird.parquet")]
    # 無可解析日期 token → 全保留（保守降級）
    assert select_recent_cache_files(files, 20) == files


def test_day_tokens_drop_old_keep_recent() -> None:
    # 錨點 = 最新檔 20260625；n_days=5 → 窗 = 5*8//5+31 = 39 日曆日，cutoff ≈ 05-17
    files = [_p(f"institutional_{d}.parquet") for d in (
        "20260101",  # 遠早於 cutoff → 丟
        "20260601",  # 窗內 → 留
        "20260620",
        "20260625",
    )]
    kept = select_recent_cache_files(files, n_days=5)
    names = {f.name for f in kept}
    assert "institutional_20260101.parquet" not in names
    assert "institutional_20260601.parquet" in names
    assert "institutional_20260625.parquet" in names


def test_listed_and_otc_both_kept_per_date() -> None:
    files = [
        _p("institutional_20260625.parquet"),
        _p("institutional_otc_20260625.parquet"),
        _p("institutional_20260101.parquet"),
        _p("institutional_otc_20260101.parquet"),
    ]
    kept = {f.name for f in select_recent_cache_files(files, n_days=5)}
    assert "institutional_20260625.parquet" in kept
    assert "institutional_otc_20260625.parquet" in kept  # 上市/上櫃同日都保留
    assert "institutional_20260101.parquet" not in kept


def test_month_tokens_use_month_end_upper_bound() -> None:
    # stock_day_<sid>_YYYYMM：單月檔，以月底為上界 → 含窗內日的月份須保留
    anchor = _p("daily_20260625.parquet")
    in_window = _p("stock_day_1477_202606.parquet")   # 6 月底 06-30 ≥ cutoff
    old_month = _p("stock_day_1477_202501.parquet")   # 2025-01 整月早於 cutoff → 丟
    kept = {f.name for f in select_recent_cache_files(
        [anchor, in_window, old_month], n_days=20)}
    assert "stock_day_1477_202606.parquet" in kept
    assert "stock_day_1477_202501.parquet" not in kept


def test_order_is_preserved() -> None:
    files = [
        _p("daily_20260625.parquet"),
        _p("otc_daily_20260625.parquet"),
        _p("stock_day_1477_202606.parquet"),
    ]
    assert select_recent_cache_files(files, 250) == files  # 全在窗內、順序不變


def test_superset_property_no_in_window_file_dropped() -> None:
    """隨機式覆蓋：近 n_days 交易日的每個 day-檔都必須被保留（不可漏收）。"""
    base = date(2026, 6, 25)
    # 造 300 個連續日曆日 day-檔（含週末，模擬寬鬆情形）
    files = [_p(f"daily_{(base - timedelta(days=i)).strftime('%Y%m%d')}.parquet")
             for i in range(300)]
    n_days = 60
    kept = set(select_recent_cache_files(files, n_days))
    # 近 n_days 個「日曆日」必為近 n_days 交易日的超集 → 全部都該在 kept 內
    for i in range(n_days):
        f = _p(f"daily_{(base - timedelta(days=i)).strftime('%Y%m%d')}.parquet")
        assert f in kept


# ── load_institutional_history 等價性 ──────────────────────────────────────


def _make_client(tmp_path: Path) -> TWSEClient:
    return TWSEClient(
        base_url="https://example.test",
        cache_dir=tmp_path,
        ttl_hours=6,
        user_agent="test",
        interval_sec=0,
    )


def _write_inst_day(cache_dir: Path, d: date, otc: bool = False) -> None:
    prefix = "institutional_otc" if otc else "institutional"
    df = pl.DataFrame(
        {
            "date": [d, d],
            "stock_id": (["6669", "1234"] if otc else ["2330", "2317"]),
            "stock_name": ["A", "B"],
            "foreign_net": [100, -50],
            "trust_net": [10, 5],
            "dealer_net": [1, 2],
            "total_net": [111, -43],
        },
        schema=_INSTITUTIONAL_SCHEMA,
    )
    df.write_parquet(cache_dir / f"{prefix}_{d.strftime('%Y%m%d')}.parquet")


def test_load_institutional_history_equivalent_to_full_read(tmp_path: Path) -> None:
    # 造 40 個交易日（每日上市+上櫃各一檔 = 80 檔），請求近 10 日
    days = [date(2026, 6, 25) - timedelta(days=i) for i in range(40)]
    days = [d for d in days if d.weekday() < 5]  # 僅工作日
    for d in days:
        _write_inst_day(tmp_path, d, otc=False)
        _write_inst_day(tmp_path, d, otc=True)

    client = _make_client(tmp_path)
    n = 10
    got = client.load_institutional_history(n_days=n).sort(["date", "stock_id"])

    # 參考：全讀再取近 n 日（即優化前的語意）
    all_files = sorted(tmp_path.glob("institutional_*.parquet"))
    ref_merged = (
        pl.concat([pl.read_parquet(f) for f in all_files])
        .unique(subset=["date", "stock_id"])
        .sort("date")
    )
    recent = ref_merged["date"].unique().sort(descending=True).head(n).to_list()
    ref = ref_merged.filter(pl.col("date").is_in(recent)).sort(["date", "stock_id"])

    assert got.equals(ref)
    assert got["date"].n_unique() == n  # 恰好近 n 個交易日


def test_load_institutional_history_empty_when_no_cache(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    out = client.load_institutional_history(n_days=20)
    assert out.is_empty()


# ── select_prune_candidates（規劃書 01 P2：保留窗清理）────────────────────────

_RETENTION = {
    "daily_days": 400,
    "institutional_days": 400,
    "margin_days": 400,
    "valuation_days": 400,
    "stock_day_keep_all": True,
}


def _touch(cache_dir: Path, name: str) -> Path:
    p = cache_dir / name
    p.write_bytes(b"")
    return p


def test_prune_no_config_keeps_everything(tmp_path: Path) -> None:
    _touch(tmp_path, "institutional_20200101.parquet")
    assert select_prune_candidates(tmp_path, None) == []
    assert select_prune_candidates(tmp_path, {}) == []


def test_prune_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert select_prune_candidates(tmp_path / "nope", _RETENTION) == []


def test_prune_drops_only_over_window_per_family(tmp_path: Path) -> None:
    # 錨 = 各家族最新檔 20260625；400 日 cutoff ≈ 2025-05-21
    over = _touch(tmp_path, "institutional_20250101.parquet")        # 早於 cutoff → 刪
    keep_edge = _touch(tmp_path, "institutional_20250601.parquet")   # 窗內 → 留
    keep_new = _touch(tmp_path, "institutional_20260625.parquet")    # 錨 → 留
    pruned = select_prune_candidates(tmp_path, _RETENTION)
    assert over in pruned
    assert keep_edge not in pruned
    assert keep_new not in pruned


def test_prune_family_anchors_are_independent(tmp_path: Path) -> None:
    # daily 家族最新只到 2025-06（停更）；institutional 到 2026-06。
    # 各家族以自身最新日為錨 → daily 的舊檔不因 institutional 較新而被多刪。
    d_new = _touch(tmp_path, "daily_20250601.parquet")
    d_old = _touch(tmp_path, "daily_20240101.parquet")   # 早於 daily 自身錨 −400 → 刪
    i_new = _touch(tmp_path, "institutional_20260625.parquet")
    pruned = set(select_prune_candidates(tmp_path, _RETENTION))
    assert d_old in pruned
    assert d_new not in pruned   # daily 自身錨 2025-06 → 在窗內
    assert i_new not in pruned


def test_prune_covers_otc_and_all_variants(tmp_path: Path) -> None:
    # daily_/daily_all_/otc_daily_ 同屬 daily 家族；margin_/margin_otc_ 同屬 margin 家族。
    # 每家族需自身的近端錨，舊檔才會超窗（per-family 錨，見上一測試）。
    anchors = [
        _touch(tmp_path, "daily_20260625.parquet"),
        _touch(tmp_path, "margin_20260625.parquet"),
    ]
    over = [
        _touch(tmp_path, "daily_all_20240101.parquet"),
        _touch(tmp_path, "otc_daily_20240101.parquet"),
        _touch(tmp_path, "margin_otc_20240101.parquet"),
    ]
    pruned = set(select_prune_candidates(tmp_path, _RETENTION))
    assert all(f in pruned for f in over)
    assert all(a not in pruned for a in anchors)


def test_prune_keeps_stock_day_and_unclassified_by_default(tmp_path: Path) -> None:
    _touch(tmp_path, "institutional_20260625.parquet")  # 提供錨、本身在窗內
    stock_day = _touch(tmp_path, "stock_day_2330_202001.parquet")    # 預設全留
    unclassified = _touch(tmp_path, "dividend_calendar_20200101.parquet")  # 不在任何家族
    fundamentals = _touch(tmp_path, "fundamentals_2020Q1.parquet")   # 無法解析日期
    pruned = set(select_prune_candidates(tmp_path, _RETENTION))
    assert stock_day not in pruned
    assert unclassified not in pruned
    assert fundamentals not in pruned


def test_prune_stock_day_when_keep_all_false(tmp_path: Path) -> None:
    # stock_day_keep_all=False → 個股月檔改用 daily_days 窗清理
    retention = {**_RETENTION, "stock_day_keep_all": False}
    _touch(tmp_path, "stock_day_2330_202606.parquet")               # 錨（月底 06-30）
    old = _touch(tmp_path, "stock_day_2330_202001.parquet")         # 早於 cutoff → 刪
    pruned = select_prune_candidates(tmp_path, retention)
    assert old in pruned


# ── is_fresh_with_columns ────────────────────────────────────────────────────


def test_is_fresh_with_columns_missing_column_is_stale(tmp_path: Path) -> None:
    """缺必要欄位＝視為 stale，即使還在 TTL 內（防 schema 改版後舊快取被誤判新鮮）。"""
    path = tmp_path / "revenue_202608.parquet"
    save_parquet(pl.DataFrame({"stock_id": ["2330"], "yoy_pct": [20.34]}), path)
    assert not is_fresh_with_columns(path, ttl_hours=24.0, required_columns=["cum_yoy_pct"])


def test_is_fresh_with_columns_present_is_fresh(tmp_path: Path) -> None:
    path = tmp_path / "revenue_202608.parquet"
    save_parquet(
        pl.DataFrame({"stock_id": ["2330"], "yoy_pct": [20.34], "cum_yoy_pct": [19.67]}), path
    )
    assert is_fresh_with_columns(path, ttl_hours=24.0, required_columns=["cum_yoy_pct"])


def test_is_fresh_with_columns_missing_file_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "does_not_exist.parquet"
    assert not is_fresh_with_columns(path, ttl_hours=24.0, required_columns=["cum_yoy_pct"])
