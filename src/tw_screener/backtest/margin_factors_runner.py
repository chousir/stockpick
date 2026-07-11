"""籌碼因子（融資／TDCC 大戶）首驗編排（WS-K；自 cli.py 薄殼呼叫）。

面板 → margin_factors.build_margin_factors()（K1/K2/K3 三欄）→ 樣本前處理
（chip_coverage=True 且對應因子非 null）→ factor_lab.evaluate（K1/K2/K3 ×
r+20 主／r+5,10 輔）→ regime 切片（r+20 主窗，WS-H 標籤就緒才附）→
research/margin_factors/。docs/23-backtest-r2-w28.md §3 預註冊（首驗跑前
寫死，跑後不得改假設語彙）；裁決欄走 margin_factors.classify_verdict()。
CLI 只保留參數解析。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


@dataclass(frozen=True)
class _LabConfig:
    """backtest.factor_lab 讀出的推論參數（型別固定，避免 mixed-type dict）。"""

    inference: str
    n_boot: int
    seed: int

_HORIZONS: tuple[int, ...] = (20, 5, 10)  # 20＝主窗（regime 切片／裁決依此）、5/10＝輔
_MAIN_HORIZON = 20
_MIN_EFFECT_IC = 0.03  # docs/23 §3 效應量底線（預註冊寫死，不得事後調）
_REGIME_LABELS = ("進攻", "中性", "防禦")  # analysis/regime.py ATTACK/NEUTRAL/DEFENSE


def _fmt_ci(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "—"
    return f"[{lo:+.3f}, {hi:+.3f}]"


def _fmt_ic(v: float | None) -> str:
    return f"{v:+.3f}" if v is not None else "—"


def _wf_ratio(splits: pl.DataFrame) -> tuple[str, list[float | None]]:
    """回傳（「多數符號段數/有效段數」字串, 各段 IC list）——鏡像 docs/23 §1「4/5」記法。"""
    if splits.is_empty():
        return "0/0", []
    ics = splits["ic"].to_list()
    valid = [v for v in ics if v is not None]
    if not valid:
        return f"0/{splits.height}", ics
    pos = sum(1 for v in valid if v > 0)
    neg = sum(1 for v in valid if v < 0)
    return f"{max(pos, neg)}/{len(valid)}", ics


def _regime_alignment(
    sample: pl.DataFrame, factor: str, main_sign: int, cfg_lab: _LabConfig
) -> str:
    """r+20 主窗各 regime 子樣本 mean_daily_ic 與全樣本同號數（WS-H 標籤就緒才算）。"""
    from tw_screener.backtest import factor_lab as lab

    if "regime" not in sample.columns or sample["regime"].drop_nulls().is_empty():
        return "regime 標籤未附（WS-H 前）"
    present = [r for r in _REGIME_LABELS if sample.filter(pl.col("regime") == r).height > 0]
    if not present:
        return "regime 標籤未附（WS-H 前）"
    same = 0
    for r in present:
        sub = sample.filter(pl.col("regime") == r)
        rep = lab.evaluate(
            sub, factor, horizon=_MAIN_HORIZON, target=f"alpha{_MAIN_HORIZON}",
            n_splits=0, inference=cfg_lab.inference, n_boot=cfg_lab.n_boot,
            seed=cfg_lab.seed,
        )
        if rep.mean_daily_ic is not None and (rep.mean_daily_ic > 0) == (main_sign > 0):
            same += 1
    return f"{same}/{len(present)}"


def run_margin_factors(settings: Path, out_dir: Path | None) -> None:
    """WS-K：K1/K2/K3 籌碼因子首驗（docs/23 §3 預註冊；判準三選一自動裁決）。"""
    import yaml

    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest.margin_factors import (
        FACTOR_SPECS,
        build_margin_factors,
        classify_verdict,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    fl = cfg.get("backtest", {}).get("factor_lab", {})
    cfg_lab = _LabConfig(
        inference=str(fl.get("inference", "block_bootstrap")),
        n_boot=int(fl.get("n_boot", 1000)),
        seed=int(fl.get("seed", 42)),
    )
    n_splits = int(fl.get("n_splits", 4))
    min_train_frac = float(fl.get("min_train_frac", 0.4))
    buckets = int(fl.get("buckets", 5))
    panel_path = Path(fl.get("panel_path", "research/panel/panel.parquet"))
    out = out_dir or Path("research/margin_factors")

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    console.print("[bold]build 籌碼因子（margin_slim/big_holder_wow/margin_to_vol）...[/bold]")
    df = build_margin_factors(panel)
    if not {"margin_slim", "big_holder_wow", "margin_to_vol"}.issubset(df.columns):
        console.print("[red]面板缺 margin/TDCC 必要欄——無法建因子（先跑 make build-panel）[/red]")
        raise typer.Exit(1)

    has_chip = "chip_coverage" in df.columns
    base = df.filter(pl.col("chip_coverage")) if has_chip else df
    samples: dict[str, pl.DataFrame] = {feat: base.drop_nulls(feat) for feat in FACTOR_SPECS}

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    d0 = str(df["date"].min()) if not df.is_empty() else None
    d1 = str(df["date"].max()) if not df.is_empty() else None

    lines: list[str] = [
        "# 籌碼因子（融資／TDCC 大戶）首驗（WS-K）",
        "",
        f"- 產出日：{date.today()}；面板樣本期間：{d0 or '—'}~{d1 or '—'}；"
        "target=alpha{h}；宇宙＝上市普通股（margin 資料邊界）；樣本前處理＝"
        "chip_coverage=True 且對應因子非 null（K1/K3＝margin_balance_lots、"
        "K2＝big_holder_wow）。",
        "- 預註冊方向與判準（docs/23 §3，首驗跑前寫死，跑後不得改假設）："
        "|mean_IC|≥0.03＋bs_CI95 不含 0＋walk-forward ≥4/5 同向＝方向成立；"
        "同門檻反號＝反向顯著；其餘＝無證據；n_dates<30 或 T<10＝樣本不足不可判。",
        "",
        "| factor | 預註冊方向 | horizon | mean_IC | bs_CI95 | 非重疊 IC | "
        "WF 同向段數 | regime 同向數 | 裁決 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    summary_rows: list[dict] = []
    for feat, (desc, sign) in FACTOR_SPECS.items():
        sample = samples[feat]
        console.print(f"  {feat}（{desc}）：可用樣本 {sample.height} 列")
        main_sign = 0
        for h in _HORIZONS:
            rep = lab.evaluate(
                sample, feat, horizon=h, target=f"alpha{h}",
                buckets=buckets, n_splits=n_splits, min_train_frac=min_train_frac,
                inference=cfg_lab.inference, n_boot=cfg_lab.n_boot, seed=cfg_lab.seed,
            )
            nv = rep.nonoverlap or {}
            t_obs = (rep.inference_meta or {}).get("T", 0)
            n_dates = nv.get("n_dates", 0)
            wf_txt, split_ics = _wf_ratio(rep.splits)
            verdict = classify_verdict(
                predicted_sign=sign, mean_ic=rep.mean_daily_ic, bs_ci=rep.bs_ci,
                split_ics=split_ics, n_dates=n_dates, t=t_obs, min_effect=_MIN_EFFECT_IC,
            )
            if h == _MAIN_HORIZON:
                main_sign = 1 if (rep.mean_daily_ic or 0) >= 0 else -1
                regime_txt = _regime_alignment(sample, feat, main_sign, cfg_lab)
                if not rep.splits.is_empty():
                    rep.splits.write_csv(out / f"margin_factors_{feat}_splits_{tag}.csv")
            else:
                regime_txt = "—（僅主窗 r+20）"
            nv_txt = (
                f"{_fmt_ic(nv.get('ic'))} {_fmt_ci(nv.get('ci_lo'), nv.get('ci_hi'))}"
                if nv.get("ic") is not None
                else "—"
            )
            pred_txt = "正向" if sign > 0 else "負向"
            lines.append(
                f"| `{feat}` | {pred_txt} | r+{h} | {_fmt_ic(rep.mean_daily_ic)} "
                f"| {_fmt_ci(*rep.bs_ci)} | {nv_txt} | {wf_txt} | {regime_txt} "
                f"| **{verdict}** |"
            )
            summary_rows.append(
                {
                    "factor": feat, "predicted_sign": sign, "horizon": h,
                    "mean_ic": rep.mean_daily_ic, "bs_ci_lo": rep.bs_ci[0],
                    "bs_ci_hi": rep.bs_ci[1], "n_dates": n_dates, "t": t_obs,
                    "wf_ratio": wf_txt, "verdict": verdict,
                }
            )

    span = f"{d0}~{d1}" if d0 and d1 else "—（面板為空）"
    regime_note = (
        "regime 標籤未附（WS-H 前）＝2025-01~2026-07 多頭偏"
        if "regime" not in df.columns or df["regime"].drop_nulls().is_empty()
        else "regime 標籤已附（進攻/中性/防禦，WS-H）"
    )
    lines += [
        "",
        *lab.inference_footer(
            sample_span=span,
            regime_dist=regime_note,
            method_desc=(
                f"moving-block bootstrap（block=horizon+1td・B={cfg_lab.n_boot}・"
                f"seed={cfg_lab.seed}）為 CI 預設；Fisher-z 僅供對照連續性"
                "（重疊窗下偏窄，docs/22 §7.2）"
            ),
            membership_desc="今日 concepts.yaml（非 point-in-time）",
        ),
        "",
    ]

    md = out / f"margin_factors_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    if summary_rows:
        pl.DataFrame(summary_rows).write_csv(out / f"margin_factors_summary_{tag}.csv")
    console.print(f"[green]WS-K 報告 → {md}[/green]")
    for r in summary_rows:
        if r["horizon"] != _MAIN_HORIZON:
            continue
        console.print(
            f"  {r['factor']} r+{r['horizon']}：mean_IC {_fmt_ic(r['mean_ic'])} "
            f"{_fmt_ci(r['bs_ci_lo'], r['bs_ci_hi'])}・{r['verdict']}"
        )
