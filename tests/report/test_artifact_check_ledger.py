"""tests/report/test_artifact_check_ledger.py — week-check 的 M-Pick3c 前瞻台帳檢查（docs/35）。

week-check 在 Makefile 沒有 `-` 前綴，檢查函式**不得 raise**（否則會中斷 make week）——
損毀底帳、欄位不符、設定錯誤都要轉成警告句。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml

from tw_screener.backtest.intra_pick_ledger import LEDGER_SCHEMA
from tw_screener.report import artifact_check
from tw_screener.report.artifact_check import check_intra_ledger, run_artifact_check

MACHINE = ["candidates_enriched.csv", "group_analysis.md"]
ANALYST = ["pick.md", "picks.csv"]
POOL_FILE = "shortlist.csv"  # 有池才有東西可記


def _make_week(reports_dir: Path, week: str, files: list[str]) -> None:
    week_dir = reports_dir / week
    week_dir.mkdir(parents=True)
    for name in files:
        (week_dir / name).write_text("x", encoding="utf-8")


def _ledger_frame(week: str, tiers_and_f2: list[tuple[str, float | None]]) -> pl.DataFrame:
    """最小底帳：每列 (tier, high52_near)，其餘因子固定有值；欄位齊全（read_ledger 須能吃）。"""
    n = len(tiers_and_f2)
    cols = {
        "week": [week] * n,
        "data_date": [date(2026, 10, 2)] * n,
        "recorded_at": ["t"] * n,
        "stock_id": [f"{1000 + i}" for i in range(n)],
        "name": ["n"] * n,
        "source": ["candidate"] * n,
        "sub_industry": ["X"] * n,
        "tier": [t for t, _ in tiers_and_f2],
        "rank": [None] * n,
        "gate_reason": [None] * n,
        "trend_score": [1.0] * n,
        "trend_rank": [1] * n,
        "trend_n": [40] * n,
        "trend_bucket": [1] * n,
        "close": [10.0] * n,
        "ma60_dist_pct": [6.0] * n,
        "amount_million": [100.0] * n,
        "band_dist": [0.0] * n,
        "mom_6_1": [0.1] * n,
        "high52_near": [f for _, f in tiers_and_f2],
        "eps_accel": [0.01] * n,
        "rev_accel": [0.02] * n,
        "eps_quarter": ["2026Q2"] * n,
        "rev_month": ["2026-08"] * n,
    }
    return pl.DataFrame(cols, schema=LEDGER_SCHEMA)


def _write_ledger(path: Path, *frames: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.concat(list(frames)).write_csv(path)


def _reports(tmp_path: Path, weeks: dict[str, bool]) -> Path:
    """建 reports/<週>/；weeks[週]＝該週是否有 shortlist.csv。"""
    reports = tmp_path / "reports"
    for week, has_pool in weeks.items():
        _make_week(reports, week, MACHINE + ANALYST + ([POOL_FILE] if has_pool else []))
    return reports


def _write_settings(tmp_path: Path, *, with_ledger: bool, start_week: str = "2026-W40") -> Path:
    cfg: dict[str, object] = {
        "paths": {"reports_dir": str(tmp_path / "reports")},
        "report": {"artifact_check": {"machine": MACHINE, "analyst": ANALYST}},
    }
    if with_ledger:
        cfg["backtest"] = {
            "intra_pick_ledger": {
                "start_week": start_week,
                "ledger_path": str(tmp_path / "ledger.csv"),
            }
        }
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return settings


def test_before_start_week_is_silent(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W39": True})
    assert check_intra_ledger(tmp_path / "none.csv", reports, "2026-W40", 0.7) == []


def test_no_week_dirs_is_silent(tmp_path: Path) -> None:
    assert check_intra_ledger(tmp_path / "none.csv", tmp_path / "nope", "2026-W40", 0.7) == []


def test_latest_week_missing_rows_warns(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    msg = check_intra_ledger(tmp_path / "none.csv", reports, "2026-W40", 0.7)  # 檔案不存在
    assert len(msg) == 1 and "2026-W40" in msg[0] and "make intra-pick-ledger" in msg[0]
    path = tmp_path / "ledger.csv"
    _write_ledger(path, _ledger_frame("2026-W41", [("top", 0.9)]))  # 只有別週的列
    assert len(check_intra_ledger(path, reports, "2026-W40", 0.7)) == 1


def test_older_week_with_pool_but_no_ledger_rows_warns(tmp_path: Path) -> None:
    """W40 在沒有記錄器的分支上跑完、之後才 merge：W41 的 week-check 仍要點名 W40 缺列。"""
    reports = _reports(tmp_path, {"2026-W40": True, "2026-W41": True})
    path = tmp_path / "ledger.csv"
    _write_ledger(path, _ledger_frame("2026-W41", [("top", 0.9), ("alt", 0.8)]))
    msg = check_intra_ledger(path, reports, "2026-W40", 0.7)
    assert len(msg) == 1
    assert "2026-W40" in msg[0] and "逾補記期限" in msg[0] and "WEEK=2026-W40" in msg[0]


def test_weeks_without_a_pool_file_are_not_flagged(tmp_path: Path) -> None:
    """沒有 shortlist.csv 的週沒有池可記（shortlist 步驟失敗由機器產物檢查另外點名）。"""
    reports = _reports(tmp_path, {"2026-W40": False, "2026-W41": False})
    assert check_intra_ledger(tmp_path / "none.csv", reports, "2026-W40", 0.7) == []


def test_healthy_latest_week_is_silent(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    path = tmp_path / "ledger.csv"
    frame = _ledger_frame(
        "2026-W40", [("top", 0.9), ("alt", 0.8), ("capped", 0.7), ("gated", None)]
    )
    _write_ledger(path, frame)
    # gated 列的 null 不計入覆蓋率
    assert check_intra_ledger(path, reports, "2026-W40", 0.7) == []


def test_low_coverage_names_factor_and_rate(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    path = tmp_path / "ledger.csv"
    # 合格成員 4 檔、high52_near 只有 1 檔有值 → 25%
    frame = _ledger_frame(
        "2026-W40", [("top", 0.9), ("alt", None), ("capped", None), ("capped", None)]
    )
    _write_ledger(path, frame)
    msg = check_intra_ledger(path, reports, "2026-W40", 0.7)
    assert len(msg) == 1 and "high52_near 25%" in msg[0] and "mom_6_1" not in msg[0]


def test_only_latest_week_coverage_is_checked(tmp_path: Path) -> None:
    """舊週覆蓋率再低也已凍結、無從補救，week-check 不重複糾纏；只看最新週。"""
    reports = _reports(tmp_path, {"2026-W40": True, "2026-W41": True})
    path = tmp_path / "ledger.csv"
    _write_ledger(
        path,
        _ledger_frame("2026-W40", [("top", None), ("alt", None)]),
        _ledger_frame("2026-W41", [("top", 0.9), ("alt", 0.8)]),
    )
    assert check_intra_ledger(path, reports, "2026-W40", 0.7) == []


@pytest.mark.parametrize(
    "content",
    [
        "week,stock_id\n2026-W40,1111\n",  # 欄位不符（verifier 的重現案例）
        "",  # 空檔
        "\x00\x01 not a csv at all",  # 二進位垃圾
    ],
)
def test_unreadable_ledger_becomes_a_warning_not_an_exception(tmp_path: Path, content: str) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    path = tmp_path / "ledger.csv"
    path.write_text(content, encoding="utf-8")
    msg = check_intra_ledger(path, reports, "2026-W40", 0.7)  # 不得 raise
    assert len(msg) == 1 and "無法讀取" in msg[0]


def test_malformed_start_week_disables_the_check_with_a_warning(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    msg = check_intra_ledger(tmp_path / "none.csv", reports, "2026W40", 0.7)
    assert len(msg) == 1 and "start_week" in msg[0] and "停用" in msg[0]


def test_non_week_directories_are_ignored(tmp_path: Path) -> None:
    reports = _reports(tmp_path, {"2026-W40": True})
    (reports / "notes-Wip").mkdir()  # 名稱含 -W 但不是週次標籤
    path = tmp_path / "ledger.csv"
    _write_ledger(path, _ledger_frame("2026-W40", [("top", 0.9), ("alt", 0.8)]))
    assert check_intra_ledger(path, reports, "2026-W40", 0.7) == []


def test_run_artifact_check_includes_ledger_warning(tmp_path: Path) -> None:
    _reports(tmp_path, {"2026-W40": True})
    settings = _write_settings(tmp_path, with_ledger=True)

    report = run_artifact_check(settings)  # 台帳檔不存在 → 缺列
    assert len(report.ledger_warnings) == 1 and report.has_warnings

    _write_ledger(tmp_path / "ledger.csv", _ledger_frame("2026-W40", [("top", 0.9), ("alt", 0.8)]))
    report = run_artifact_check(settings)
    assert report.ledger_warnings == [] and not report.has_warnings


def test_run_artifact_check_survives_a_corrupt_ledger(tmp_path: Path) -> None:
    """week-check 沒有 make 的 `-` 前綴：壞台帳絕不能讓它丟例外而中斷 make week。"""
    _reports(tmp_path, {"2026-W40": True})
    (tmp_path / "ledger.csv").write_text("week,stock_id\n2026-W40,1111\n", encoding="utf-8")
    report = run_artifact_check(_write_settings(tmp_path, with_ledger=True))
    assert report.has_warnings and "無法讀取" in report.ledger_warnings[0]


def test_run_artifact_check_survives_a_bug_inside_the_ledger_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最後防線：台帳檢查本身出任何意外，都轉成一句警告，不擋流程。"""
    _reports(tmp_path, {"2026-W40": True})

    def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(artifact_check, "_ledger_warnings", boom)
    report = run_artifact_check(_write_settings(tmp_path, with_ledger=True))
    assert report.has_warnings
    assert (
        "前瞻台帳檢查本身失敗" in report.ledger_warnings[0] and "boom" in report.ledger_warnings[0]
    )


def test_run_artifact_check_without_ledger_settings_skips_check(tmp_path: Path) -> None:
    _reports(tmp_path, {"2026-W40": True})
    settings = _write_settings(tmp_path, with_ledger=False)
    assert run_artifact_check(settings).ledger_warnings == []
