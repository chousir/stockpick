"""M-Pick3b 保留樣本驗證編排（docs/34；自 cli.py 薄殼呼叫）。

驗快照 SHA-256 與判準邏輯檔 blob 釘版 → 樣本規則（剔除 2021-12 快照）→ 鏡像因子 →
跑前可行性檢查（`--feasibility` 只做到這步）→ 四假設評估（重用 intra_pick.evaluate_factor，
裁決規則零改動）→ 揭露 D1–D4 → research/intra_pick_holdout/。

單次正式執行紀律（docs/34 §6）：輸出目錄已有正式結果（`holdout_eval_verdicts_*.csv`）時
拒絕重跑；所有檔案寫完才在 console 顯示結果數字。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import typer
from rich.console import Console

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_holdout_eval as ev
from tw_screener.backtest.intra_pick_runner import _ci, _fmt, _verdict_rows, _weekly_rows

console = Console()

_FAMILY_TITLES = {
    ev.FAMILY_PRIMARY: "主族（H1、H2）",
    ev.FAMILY_SECONDARY: "副族（F2、F4 複驗）",
}


def _head_commit() -> str:
    """報表溯源用的 git HEAD；取不到就寫「未取得」（不影響評估）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "未取得"
    return out.stdout.strip() or "未取得"


def _verdict_table(
    results: Sequence[ev.HypothesisResult], k_by_family: dict[str, int]
) -> list[str]:
    lines: list[str] = []
    for family, title in _FAMILY_TITLES.items():
        rows = [r for r in results if r.hypothesis.family == family]
        if not rows:
            continue
        lines += [
            f"### {title}（Bonferroni k＝{k_by_family[family]}）",
            "",
            "| 假設 | 覆蓋 | 週數 | 週 IC 平均 | CI95 | Bonferroni CI | 分段>0 | regime | "
            "M2 首選−組均 | M3 首選−現行 | 同選率 | 裁決 | 未過 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for hr in rows:
            r = hr.result
            seg_pos = sum(1 for s in r.segments if s is not None and s > 0)
            cov = f"{r.coverage:.0%}" if r.coverage is not None else "—"
            same = f"{r.same_pick_rate:.0%}" if r.same_pick_rate is not None else "—"
            lines.append(
                f"| {hr.hypothesis.label} | {cov} | {r.ic.n} | **{_fmt(r.ic.mean)}** | "
                f"{_ci(r.ic.ci)} | {_ci(r.ic.ci_bonf)} | {seg_pos}/{len(r.segments)} | "
                f"{r.regime_label} | {_fmt(r.m2.mean, 2, '%')} {_ci(r.m2.ci, 2)} | "
                f"{_fmt(r.m3.mean, 2, '%')} {_ci(r.m3.ci, 2)} | {same} | "
                f"**{r.verdict}** | {','.join(r.failed) or '—'} |"
            )
        lines.append("")
    return lines


def _header_lines(
    stats: dict[str, Any],
    meta: dict[str, Any],
    cfg: ip.IntraPickConfig,
    ecfg: ev.HoldoutEvalConfig,
) -> list[str]:
    u = stats["universe"]
    dropped = "、".join(str(d) for d in meta["dropped"]) or "無"
    pins = "；".join(f"{Path(k).name} `{v[:12]}…`" for k, v in meta["blobs"].items())
    return [
        "# M-Pick3b 保留樣本驗證（docs/34 預註冊裁決）",
        "",
        f"- 產出日：{meta['today']}；程式 git HEAD `{meta['head']}`；"
        "預註冊 docs/34（§0–§6 於任何結果計算前 commit）。",
        f"- 快照：`{ecfg.snapshot_path}`，SHA-256 `{meta['sha256']}`（與預註冊釘版**相符**）。",
        f"- 判準邏輯檔 blob 釘版（docs/34 §6）**相符**：{pins or '（未設釘版）'}。",
        f"- 樣本規則（docs/34 §2.3）：快照日 ≤ {ecfg.last_snapshot}；剔除 "
        f"{len(meta['dropped'])} 個快照（{dropped}）；保留 {stats['snapshot_weeks']} 週"
        f"（{stats['snapshot_first']}～{stats['snapshot_last']}）。",
        f"- 主宇宙：趨勢分前 {cfg.shortlist.max_bucket_for_top} 桶（共 "
        f"{cfg.shortlist.trend_buckets} 桶）× gate × 同組 ≥ {cfg.min_group} 檔；"
        f"{u['stock_weeks']:,} 股週、{u['weeks']} 週，每週中位 "
        f"{ev.fmt_num(u['median_names'])} 檔／{ev.fmt_num(u['median_groups'])} 組。",
        f"- 判準（沿用 docs/32 §4）：C1 週 IC 平均 ≥ {cfg.min_effect_ic}、C2 CI95 下界 > 0、"
        f"C3 {cfg.n_segments} 段 ≥ {cfg.min_same_segments} 段 > 0、C4 跨 regime 穩健、"
        "C5 M2 與 M3 點估計 > 0；成立＝C1–C5 全過且 Bonferroni 下界 > 0；"
        f"覆蓋 < {cfg.min_coverage:.0%} 或週數 < {cfg.min_weeks} → 不可判。"
        "方向皆＋（H2 為鏡像因子）。",
        "",
    ]


def _detail_lines(results: Sequence[ev.HypothesisResult]) -> list[str]:
    lines: list[str] = ["## 2. 逐假設明細", ""]
    for hr in results:
        r = hr.result
        lines += [
            f"### {hr.hypothesis.label}（`{hr.hypothesis.factor}`）",
            "",
            "- 分段（時間序 5 等分）週 IC 平均：" + "、".join(_fmt(s) for s in r.segments) + "。",
            f"- M2 首選−組均 {_fmt(r.m2.mean, 2, '%')} CI95 {_ci(r.m2.ci, 2)}（{r.m2.n} 週）；"
            f"M3 首選−現行 {_fmt(r.m3.mean, 2, '%')} CI95 {_ci(r.m3.ci, 2)}（{r.m3.n} 週）。",
            "",
            "| regime | 週數 | 週 IC 平均 | bs_CI95 | 樣本 |",
            "|---|---|---|---|---|",
        ]
        if r.regime_slices.is_empty():
            lines.append("| — | 0 | — | — | regime 標籤缺 |")
        for s in r.regime_slices.iter_rows(named=True):
            lines.append(
                f"| {s['regime']} | {s['n_dates']} | {_fmt(s['mean'])} | "
                f"{_ci((s['ci_lo'], s['ci_hi']))} | {'樣本不足' if s['thin'] else '可判'} |"
            )
        lines.append("")
    return lines


def _disclosure_lines(
    results: Sequence[ev.HypothesisResult],
    stats: dict[str, Any],
    corr: pl.DataFrame,
    cfg: ip.IntraPickConfig,
) -> list[str]:
    lines = [
        "## 3. 揭露（docs/34 §5；不當門檻、不得事後升格）",
        "",
        "### D1 覆蓋與宇宙規模",
        "",
        *ev.render_feasibility(stats, cfg),
    ]
    yearly = {hr.hypothesis.key: ev.yearly_ic(hr.result.ic_weekly) for hr in results}
    years = sorted({int(y) for t in yearly.values() for y in t["year"].to_list()})
    lines += [
        "",
        "### D2 分年週 IC 平均（描述性；以快照日所屬年歸屬）",
        "",
        "| 年 | " + " | ".join(f"{hr.hypothesis.key}（週數）" for hr in results) + " |",
        "|---|" + "---|" * len(results),
    ]
    for y in years:
        cells = []
        for hr in results:
            row = yearly[hr.hypothesis.key].filter(pl.col("year") == y)
            cells.append(
                f"{_fmt(row['ic_mean'][0])}（{row['n_weeks'][0]}）" if not row.is_empty() else "—"
            )
        lines.append(f"| {y} | " + " | ".join(cells) + " |")
    h1 = next((r for r in results if r.hypothesis.key == "H1"), None)
    if h1 is not None:
        lines += [
            "",
            "### D3 規則對規則（成交額首選 − 現行首選＝H1 的 M3）",
            "",
            f"- r+{cfg.horizon}：{_fmt(h1.result.m3.mean, 2, '%')} CI95 "
            f"{_ci(h1.result.m3.ci, 2)}（{h1.result.m3.n} 週；照列、不當門檻）。",
        ]
    cols = [c for c in corr.columns if c != "factor"]
    lines += [
        "",
        "### D4 組內置中排名相關（主宇宙）",
        "",
        "| | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
        *[
            f"| {row['factor']} | " + " | ".join(_fmt(row[c], 2) for c in cols) + " |"
            for row in corr.iter_rows(named=True)
        ],
        "",
    ]
    return lines


def render_report(
    results: Sequence[ev.HypothesisResult],
    stats: dict[str, Any],
    corr: pl.DataFrame,
    meta: dict[str, Any],
    cfg: ip.IntraPickConfig,
    ecfg: ev.HoldoutEvalConfig,
) -> list[str]:
    """報表 markdown：釘版與樣本規則→裁決總表→逐假設明細→揭露 D1–D4→推論口徑。"""
    from tw_screener.backtest.factor_lab import inference_footer

    k_by_family = {f: ecfg.n_tests(f) for f in _FAMILY_TITLES}
    lines = [
        *_header_lines(stats, meta, cfg, ecfg),
        f"## 1. 裁決總表（主宇宙、r+{cfg.horizon}、除權息還原總報酬）",
        "",
        *_verdict_table(results, k_by_family),
    ]
    h2 = next((r for r in results if r.hypothesis.key == "H2"), None)
    if h2 is not None and h2.result.ic.mean is not None:
        lo, hi = h2.result.ic.ci
        neg_ci = _ci((-hi if hi is not None else None, -lo if lo is not None else None))
        lines += [
            "> H2 換算：表中為鏡像因子 `band_dist`；對應 `−偏好帶距離`（`neg_band_dist`）的週 IC "
            f"平均 **{_fmt(-h2.result.ic.mean)}**、CI95 {neg_ci}"
            "（IC 對取負嚴格反號，docs/34 §3）。",
            "",
        ]
    lines += _detail_lines(results)
    lines += _disclosure_lines(results, stats, corr, cfg)
    regime_txt = (
        "／".join(f"{k} {v}" for k, v in stats["by_hypothesis"]["H1"]["regime_weeks"].items())
        or "—"
    )
    lines += ["## 4. 推論與口徑", ""]
    span = (
        f"{stats['snapshot_first']}~{stats['snapshot_last']}"
        f"（週頻 {stats['snapshot_weeks']} 週）"
    )
    lines += inference_footer(
        sample_span=span,
        regime_dist=f"H1 可計週 {regime_txt}（降級 regime 定義：成員等權趨勢＋廣度、無法人分項）",
        method_desc=(
            f"週序列 moving-block bootstrap（block={cfg.block_len()} 週・B={cfg.n_boot}・"
            f"seed={cfg.seed}）；Bonferroni 層＝同一重抽分布取 α/k 分位（主族、副族各自的 k）"
        ),
        membership_desc=(
            "今日 concepts.yaml（非 point-in-time，2015–2021 比 2022+ 更重）；"
            "每檔主次產業＝手標第一個"
        ),
    )
    lines += ["- 已知偏誤全列：docs/34 §5（引用 docs/33 §6.7）。", ""]
    return lines


def run_intra_pick_holdout_eval(
    settings: Path, out_dir: Path | None, feasibility_only: bool = False
) -> None:
    """M-Pick3b：驗釘版 → 樣本規則 → 可行性檢查（或）四假設正式評估（docs/34）。"""
    import yaml

    with open(settings, encoding="utf-8") as f:
        cfg_all = yaml.safe_load(f)
    cfg = ip.IntraPickConfig.from_settings(cfg_all)
    ecfg = ev.HoldoutEvalConfig.from_settings(cfg_all)
    out = out_dir or Path(ecfg.output_dir)

    if not feasibility_only:
        prior = sorted(out.glob("holdout_eval_verdicts_*.csv"))
        if prior:
            console.print(
                f"[red]{out} 已有正式執行結果 {prior[0].name}——docs/34 §6：單次正式執行，"
                "不得重跑。僅當上次因程式錯誤中止、且確定沒有產出任何結果時，"
                "才可手動刪除該檔後重跑並記錄原因。[/red]"
            )
            raise typer.Exit(1)

    snap = Path(ecfg.snapshot_path)
    if not snap.exists():
        console.print(f"[red]找不到快照 {snap}——先跑 make intra-pick-holdout[/red]")
        raise typer.Exit(1)
    try:
        sha = ev.verify_snapshot(snap, ecfg.snapshot_sha256)
        blobs = ev.verify_blob_pins(ecfg.pinned_blobs)
    except ev.PinMismatchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    sw, dropped = ev.apply_sample_rule(pl.read_parquet(snap), ecfg.last_snapshot)
    sw = ev.add_mirror_factor(sw)
    stats = ev.feasibility(sw, cfg, ecfg.tdr_ids)
    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)

    if feasibility_only:
        path = out / f"holdout_feasibility_{tag}.md"
        header = [
            "# M-Pick3b 跑前可行性檢查（docs/34 §2.5；只數宇宙規模，不含任何因子值或報酬統計）",
            "",
            f"- 快照 SHA-256 `{sha}` 相符；樣本規則剔除 {len(dropped)} 個快照"
            f"（{'、'.join(str(d) for d in dropped)}）。",
            "",
        ]
        lines = ev.render_feasibility(stats, cfg)
        path.write_text("\n".join([*header, *lines, ""]), encoding="utf-8")
        console.print(f"[green]可行性檢查：{path}[/green]")
        for line in lines:
            console.print(line, markup=False)
        return

    console.print("[bold]評估四個預註冊假設（主宇宙、r+20、預註冊判準）...[/bold]")
    regimes = sw.select("date", "regime").unique(subset=["date"])
    results = ev.evaluate_hypotheses(sw, cfg, ecfg, regimes)
    corr = ip.centered_rank_corr(
        sw.filter(pl.col("in_main")),
        ["high52_near", "rev_accel", "log_amount", "neg_band_dist"],
        cfg.min_group,
    )
    meta = {
        "today": date.today(), "head": _head_commit(), "sha256": sha, "blobs": blobs,
        "dropped": dropped,
    }
    report_lines = render_report(results, stats, corr, meta, cfg, ecfg)

    weekly = _weekly_rows(sw, [hr.result for hr in results], cfg)
    by_factor = {hr.hypothesis.factor: hr.hypothesis for hr in results}
    verdicts = pl.DataFrame(
        [
            {
                "hypothesis": by_factor[row["factor"]].key,
                "family": by_factor[row["factor"]].family,
                **row,
            }
            for row in _verdict_rows([hr.result for hr in results])
        ]
    )
    report = out / f"holdout_eval_{tag}.md"
    weekly.write_csv(out / f"holdout_eval_weekly_{tag}.csv")
    report.write_text("\n".join(report_lines), encoding="utf-8")
    verdicts.write_csv(out / f"holdout_eval_verdicts_{tag}.csv")  # 最後寫：存在＝已產出正式結果

    console.print(f"[green]報告：{report}[/green]")
    for hr in results:
        r = hr.result
        console.print(
            f"  {hr.hypothesis.key} {r.factor:<12} IC {_fmt(r.ic.mean)} {_ci(r.ic.ci)} "
            f"M2 {_fmt(r.m2.mean, 2, '%')} M3 {_fmt(r.m3.mean, 2, '%')} → {r.verdict}"
            + (f"（未過 {','.join(r.failed)}）" if r.failed else "")
        )
