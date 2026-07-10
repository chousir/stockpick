"""資金流 inflection 編排（WS-E；自 cli.py 薄殼呼叫）。

面板 → 因子族 build → 週抽樣 → factor_lab 全套（IC/CI/walk-forward/殘差/分桶）
→ research/flow_inflection/。融資減肥/大戶 WoW 快取深度不足＝如實標註不回測。
CLI 只保留參數解析。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_flow_inflection(settings: Path, out_dir: Path | None) -> None:
    """WS-E：inflection 因子族首驗。"""
    import yaml

    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest.flow_inflection import FACTOR_SPECS, build_flow_factors
    from tw_screener.backtest.rotation_efficacy import weekly_snapshot_dates

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    fi = cfg.get("backtest", {}).get("flow_inflection", {})
    horizons = tuple(int(h) for h in fi.get("horizons_td", [10, 20]))
    n_splits = int(fi.get("n_splits", 4))
    min_train_frac = float(fi.get("min_train_frac", 0.4))
    buckets = int(fi.get("buckets", 5))
    z_window = int(fi.get("z_window", 60))
    min_effect = float(fi.get("min_effect_ic", 0.03))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(fi.get("output_dir", "research/flow_inflection"))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    console.print("[bold]build 因子族（日頻 rolling）→ 週抽樣...[/bold]")
    df = build_flow_factors(panel, z_window=z_window)
    weekly = weekly_snapshot_dates(df["date"].unique().to_list())
    wk = df.filter(pl.col("date").is_in(weekly))
    n_margin = len(list(cache_dir.glob("margin_*.parquet")))
    n_tdcc = len(list(cache_dir.glob("tdcc_distribution_*.parquet")))

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# 資金流 level→inflection 因子族首驗（WS-E）",
        "",
        f"- 產出日：{date.today()}；週抽樣 {wk['date'].n_unique()} 週×個股層；"
        f"target=alpha{{h}}；殘差 IC 控制 ma60_dist_pct（docs/16 H2）。",
        "- 已否證項不重測：20d level 排序（docs/19）只留 `ref_level20` 對照列；"
        "近端佔比單獨判別（docs/20 結案）不在本因子族。",
        f"- **無法回測（快取深度不足，如實標註）**：融資減肥×價漲（margin 快取 "
        f"{n_margin} 日）、大戶 WoW（TDCC 快取 {n_tdcc} 週）——待每週累積 ≥26 週後"
        "另案首驗。",
        f"- **效應量底線 |IC| ≥ {min_effect}**（未校準啟發式，取自 docs/19 有用訊號量級"
        "0.09–0.25 的保守下緣）：n 數萬時 CI 不跨 0 但效應≈0 是假象"
        "（playbook/20 §6.5 大樣本顯著性陷阱），未達底線一律判「效應量≈0」。",
        "- 單一多頭偏 regime；相鄰週窗重疊 CI 偏樂觀。",
        "",
    ]
    summary_rows: list[dict] = []
    for feat, (desc, ref_only) in FACTOR_SPECS.items():
        tag_note = "（僅對照・已否證 level）" if ref_only else ""
        lines.append(f"## {feat}{tag_note} — {desc}")
        lines.append("")
        for h in horizons:
            rep = lab.evaluate(
                wk, feat, horizon=h, controls=("ma60_dist_pct",),
                buckets=buckets, n_splits=n_splits, min_train_frac=min_train_frac,
            )
            lines += lab.render_report_md(rep, note=tag_note)
            p = rep.pooled.row(0, named=True) if not rep.pooled.is_empty() else None
            r = rep.residual.row(0, named=True) if not rep.residual.is_empty() else None
            summary_rows.append(
                {
                    "factor": feat, "horizon": h,
                    "ic": p["ic"] if p else None,
                    "ci_lo": p["ci_lo"] if p else None,
                    "ci_hi": p["ci_hi"] if p else None,
                    "n": p["n"] if p else None,
                    "resid_ic": r["ic"] if r else None,
                    "resid_ci_lo": r["ci_lo"] if r else None,
                    "resid_ci_hi": r["ci_hi"] if r else None,
                    "consistent_sign": rep.consistent_sign,
                    "ref_only": ref_only,
                }
            )
    summary = pl.DataFrame(summary_rows)
    summary.write_csv(out / f"flow_inflection_summary_{tag}.csv")
    md = out / f"flow_inflection_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]WS-E 報告 → {md}[/green]")
    for r in summary.filter(~pl.col("ref_only")).iter_rows(named=True):
        if r["ic"] is None:
            continue
        ci_clear = (r["ci_lo"] or 0) * (r["ci_hi"] or 0) > 0
        if abs(r["ic"]) < min_effect:
            sig = "效應量≈0（大樣本假顯著）" if ci_clear else "無證據"
        elif ci_clear and r["consistent_sign"]:
            sig = "存活候選"
        elif ci_clear:
            sig = "CI不跨0但跨段變號"
        else:
            sig = "無證據"
        console.print(
            f"  {r['factor']} r+{r['horizon']}：IC {r['ic']:+.3f} "
            f"[{r['ci_lo']:+.3f},{r['ci_hi']:+.3f}]・{sig}"
        )
