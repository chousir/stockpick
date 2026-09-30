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

純本地檢查、不打網：週次目錄產物只查檔案存在、不讀內容；M-Pick3c 前瞻台帳（docs/35）另讀底帳 CSV，
點名最新週缺列或因子覆蓋不足（台帳寫入是 make week 尾段的容錯步驟，無聲失敗會少一週乾淨樣本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
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
    ledger_warnings: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.missing_machine or self.stale_weeks or self.ledger_warnings)


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


def check_intra_ledger(
    ledger_path: Path, reports_dir: Path, start_week: str, min_coverage: float
) -> list[str]:
    """M-Pick3c 前瞻台帳（docs/35）：起算週之後每個有 shortlist.csv 的週次都該在底帳裡。

    回傳 WARNING 句（空 list＝正常、尚未起算或無週次）：
    - 底帳無法讀取（損毀／欄位不符）→ 一句警告；
    - 最新週缺列 → 提示補跑（限 data_date+7 日內）；更早的週缺列 → 多半已逾補記期限，
      該週不會進乾淨樣本；
    - 最新週有列但合格成員內任一因子覆蓋率 < min_coverage → 點名因子與比率。
    台帳靠 make week 尾段容錯步驟寫入，無聲失敗會少一週乾淨樣本，故在這裡點名（同本模組動機）。
    week-check 沒有 make 的 `-` 前綴，所以本函式**不得 raise**（讀檔錯誤一律轉成警告句）。
    """
    from tw_screener.backtest import intra_pick_ledger as il
    from tw_screener.report.shortlist import SHORTLIST_FILENAME

    if not il.is_week_tag(start_week):
        return [f"settings 的台帳 start_week={start_week!r} 不是 YYYY-Www 格式——台帳檢查停用"]
    dirs = [d for d in week_dirs(reports_dir) if il.is_week_tag(d.name)]
    if not dirs:
        return []
    latest = dirs[-1].name
    try:
        ledger = il.read_ledger(ledger_path)
    except (OSError, pl.exceptions.PolarsError) as e:
        return [f"前瞻台帳無法讀取（{ledger_path}）：{e}——檔案損毀或欄位不符，請人工檢查"]
    recorded = set(ledger["week"].to_list())

    out: list[str] = []
    for d in dirs:
        week = d.name
        if il.is_before_start(week, start_week) or week in recorded:
            continue
        if not (d / SHORTLIST_FILENAME).is_file():
            continue  # 沒有池可記（shortlist 步驟失敗另有機器產物檢查）
        if week == latest:
            out.append(
                f"{week} 前瞻台帳缺列（{ledger_path}）——make intra-pick-ledger 可補跑"
                "（限 data_date+7 日內，逾期拒寫）"
            )
        else:
            out.append(
                f"{week} 有 {SHORTLIST_FILENAME} 但前瞻台帳無此週——多半已逾補記期限"
                f"（data_date+7 日），該週不會進乾淨樣本；"
                f"期限內仍可 make intra-pick-ledger WEEK={week}"
            )
    if latest in recorded:
        summary = il.week_summary(ledger.filter(pl.col("week") == latest), min_coverage)
        cov, low = summary["coverage"], summary["low_coverage"]
        if isinstance(cov, dict) and isinstance(low, list) and low:
            out.append(
                f"{latest} 前瞻台帳因子覆蓋不足："
                + "・".join(f"{c} {cov[c]:.0%}" for c in low)
                + f"（< {min_coverage:.0%}；原因與處置見 make intra-pick-ledger 輸出與 docs/35 §5）"
            )
    return out


def _ledger_warnings(cfg: dict, reports_dir: Path) -> list[str]:
    """從 settings 取台帳設定後檢查；未設定 backtest.intra_pick_ledger（舊設定檔）→ 不檢查。"""
    from tw_screener.backtest.intra_pick import IntraPickConfig

    lc = (cfg.get("backtest") or {}).get("intra_pick_ledger") or {}
    if not lc.get("start_week"):
        return []
    return check_intra_ledger(
        Path(lc.get("ledger_path", "research/intra_pick_ledger/ledger.csv")),
        reports_dir,
        str(lc["start_week"]),
        IntraPickConfig.from_settings(cfg).min_coverage,
    )


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

    try:
        report.ledger_warnings = _ledger_warnings(cfg, Path(cfg["paths"]["reports_dir"]))
    except Exception as e:  # noqa: BLE001 — 純提示段；本檔契約＝永遠 exit 0，台帳檢查壞掉不得擋 make week
        report.ledger_warnings = [f"前瞻台帳檢查本身失敗（{type(e).__name__}：{e}）"]
    console.print(f"[bold]產物完整性檢查：{report.latest_week}[/bold]")
    for msg in report.ledger_warnings:
        console.print(f"[yellow]⚠️ WARNING：{msg}[/yellow]")
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
    _report_macro_risk(cfg, Path(cfg["paths"]["reports_dir"]) / report.latest_week)
    if not report.has_warnings and not report.pending_analyst:
        console.print("[green]✓ 產物齊全（機器產物＋歷週 pick 底帳）[/green]")
    return report


def _report_macro_risk(cfg: dict, week_dir: Path) -> None:
    """M8 宏觀窄橋的三態提示（可選檔，**永遠不擋流程**）。

    `macro_risk_latest.yaml` 是人工從每日掃描 project 貼進輸入包的，不是 make week 產的
    ——所以缺席是**合法狀態**（同 excluded.csv 的自願制），只印提醒讓人知道本週有沒有
    這個 gate 可用，不列進 missing_machine。
    """
    from datetime import date as _date

    from tw_screener.analysis.macro_risk import (
        DEFAULT_FILENAME,
        STATUS_OK,
        load_macro_risk,
        macro_risk_gate,
    )

    mcfg = cfg.get("macro_risk", {}) or {}
    path = week_dir / str(mcfg.get("filename", DEFAULT_FILENAME))
    risk = load_macro_risk(
        path, _date.today(), stale_trading_days=int(mcfg.get("stale_trading_days", 5))
    )
    gate = macro_risk_gate(
        risk,
        min_hits=int(mcfg.get("gate_min_hits", 3)),
        min_coverage=int(mcfg.get("min_coverage", 5)),
        cap=str(mcfg.get("new_position_cap", "1/3")),
    )
    style = "yellow" if gate.downgrade_posture else ("dim" if risk.status != STATUS_OK else "")
    prefix = "🌐 宏觀窄橋（M8）："
    console.print(f"[{style}]{prefix}{gate.note}[/{style}]" if style else f"{prefix}{gate.note}")
