"""產物完整性檢查測試（規劃書 05 F4）。

tmp_path 上驗 缺機器產物 WARNING／往週 pick 斷供 WARNING／最新週僅提醒，不碰真 reports/。
"""

from __future__ import annotations

from pathlib import Path

from tw_screener.report.artifact_check import check_week_artifacts

MACHINE = [
    "screen_result_d_quality_leader.csv",
    "candidates_enriched.csv",
    "group_analysis.md",
    "cp_candidates.md",
]
ANALYST = ["pick.md", "picks.csv"]


def _make_week(reports_dir: Path, week: str, files: list[str]) -> Path:
    week_dir = reports_dir / week
    week_dir.mkdir(parents=True)
    for name in files:
        (week_dir / name).write_text("x", encoding="utf-8")
    return week_dir


def test_all_present_no_warnings(tmp_path):
    _make_week(tmp_path, "2026-W26", MACHINE + ANALYST)
    _make_week(tmp_path, "2026-W27", MACHINE + ANALYST)
    report = check_week_artifacts(tmp_path, MACHINE, ANALYST)
    assert report.latest_week == "2026-W27"
    assert not report.has_warnings
    assert report.missing_machine == []
    assert report.pending_analyst == []
    assert report.stale_weeks == {}


def test_missing_machine_artifact_warns(tmp_path):
    files = [f for f in MACHINE if f != "cp_candidates.md"] + ANALYST
    _make_week(tmp_path, "2026-W27", files)
    report = check_week_artifacts(tmp_path, MACHINE, ANALYST)
    assert report.has_warnings
    assert report.missing_machine == ["cp_candidates.md"]


def test_stale_week_missing_picks_warns_but_latest_only_pending(tmp_path):
    # 往週有篩選產物但沒 picks.csv/pick.md ＝ W26 型斷供 → WARNING
    _make_week(tmp_path, "2026-W26", ["screen_result_d_quality_leader.csv"])
    # 最新週剛篩完、分析師還沒定稿 → 只提醒不 WARNING
    _make_week(tmp_path, "2026-W27", MACHINE)
    report = check_week_artifacts(tmp_path, MACHINE, ANALYST)
    assert report.stale_weeks == {"2026-W26": ["pick.md", "picks.csv"]}
    assert report.pending_analyst == ["pick.md", "picks.csv"]
    assert report.missing_machine == []
    assert report.has_warnings


def test_stale_week_without_screen_results_not_counted(tmp_path):
    # 沒篩過的週次目錄（如手建資料夾）不算斷供
    _make_week(tmp_path, "2026-W25", [])
    _make_week(tmp_path, "2026-W27", MACHINE + ANALYST)
    report = check_week_artifacts(tmp_path, MACHINE, ANALYST)
    assert report.stale_weeks == {}
    assert not report.has_warnings


def test_glob_pattern_matches(tmp_path):
    _make_week(tmp_path, "2026-W27", ["screen_result_d_quality_leader.csv"] + ANALYST)
    report = check_week_artifacts(tmp_path, ["screen_result_*.csv"], ANALYST)
    assert report.missing_machine == []


def test_empty_reports_dir(tmp_path):
    report = check_week_artifacts(tmp_path, MACHINE, ANALYST)
    assert report.latest_week == ""
    assert not report.has_warnings


def test_nonexistent_reports_dir(tmp_path):
    report = check_week_artifacts(tmp_path / "nope", MACHINE, ANALYST)
    assert report.latest_week == ""
    assert not report.has_warnings
