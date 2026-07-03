"""report/artifact_check.py — make week 尾段產物完整性檢查（規劃書 05 F4）。

動機（規劃書 05 §F4#3，承舊 09 RQ3）：make week 對 rotation／cp-value-candidates
等步驟容錯（Makefile `-` 前綴，失敗不擋主流程），產物缺了沒人知道——
cp_candidates.md 曾連續數週無聲斷供、W26 整週 pick 斷供也是事後人工才發現。
本模組比對 settings `report.artifact_check` 的應產出清單，缺者印 WARNING、
不擋流程（永遠 exit 0）。

兩類產物分開判：
- machine：make week 自己該產的（篩選 CSV／group_analysis／輪動／CP 候選）
  ——只查最新週，缺＝該步驟壞了。
- analyst：分析師事後補的（pick.md／picks.csv）——最新週剛篩完、本來就還沒寫，
  缺只印提醒；往週缺＝真斷供（W26 型），WARNING。
  excluded.csv 不查：當週可能真的沒有旗標剔除，缺檔合法（F1-PO4 底帳自願制）。

純檔案存在性檢查，不打網、不讀檔內容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich.console import Console

from tw_screener.report.pick_store import week_dirs

console = Console()


@dataclass
class ArtifactReport:
    """單次完整性檢查結果（latest_week 為空字串＝reports/ 下無週次目錄）。"""

    latest_week: str = ""
    missing_machine: list[str] = field(default_factory=list)
    pending_analyst: list[str] = field(default_factory=list)
    stale_weeks: dict[str, list[str]] = field(default_factory=dict)

    @property
    def has_warnings(self) -> bool:
        return bool(self.missing_machine or self.stale_weeks)


def _missing_in(week_dir: Path, expected: list[str]) -> list[str]:
    """expected 中在 week_dir 找不到的檔名（支援 glob，如 screen_result_*.csv）。"""
    return [
        name
        for name in expected
        if not (any(week_dir.glob(name)) if "*" in name else (week_dir / name).exists())
    ]


def check_week_artifacts(
    reports_dir: Path, machine: list[str], analyst: list[str]
) -> ArtifactReport:
    """比對最新週的機器產物＋全部往週的分析師產物，回傳缺漏清單。

    往週只查「有篩選產物（screen_result_*.csv）」的週——沒篩過的目錄不算斷供
    （與 pick_store.weeks_without_picks 同判準）。
    """
    dirs = week_dirs(reports_dir)
    if not dirs:
        return ArtifactReport()

    latest = dirs[-1]
    report = ArtifactReport(
        latest_week=latest.name,
        missing_machine=_missing_in(latest, machine),
        pending_analyst=_missing_in(latest, analyst),
    )
    for d in dirs[:-1]:
        if not any(d.glob("screen_result_*.csv")):
            continue
        if missing := _missing_in(d, analyst):
            report.stale_weeks[d.name] = missing
    return report


def run_artifact_check(settings: Path) -> ArtifactReport:
    """CLI 進口：讀 settings、跑檢查、印結果。缺漏只 WARNING、不 raise（不擋 make week）。"""
    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    check_cfg = cfg.get("report", {}).get("artifact_check", {})
    report = check_week_artifacts(
        Path(cfg["paths"]["reports_dir"]),
        machine=check_cfg.get("machine", []),
        analyst=check_cfg.get("analyst", []),
    )

    if not report.latest_week:
        console.print(
            "[yellow]⚠️ WARNING：reports/ 下沒有任何週次目錄——尚未跑過 make week？[/yellow]"
        )
        return report

    console.print(f"[bold]產物完整性檢查：{report.latest_week}[/bold]")
    for name in report.missing_machine:
        console.print(
            f"[yellow]⚠️ WARNING：{report.latest_week} 缺 {name}——"
            f"對應步驟可能無聲失敗（make week 容錯步驟不擋流程），請回看本次輸出[/yellow]"
        )
    for week, names in sorted(report.stale_weeks.items()):
        console.print(
            f"[yellow]⚠️ WARNING：{week} 缺 {'、'.join(names)}——"
            f"往週產物斷供（W26 型），pick 閉環（make pick-outcome）少這週的帳[/yellow]"
        )
    for name in report.pending_analyst:
        console.print(
            f"[dim]⏳ 提醒：{report.latest_week} 尚無 {name}"
            "（分析師定稿後跑 picks record 補齊）[/dim]"
        )
    if not report.has_warnings and not report.pending_analyst:
        console.print("[green]✓ 產物齊全（機器產物＋歷週 pick 底帳）[/green]")
    return report
