"""官方族群指數重測「族群前5」編排（docs/31 §10 milestone；自 cli.py 薄殼呼叫）。

面板＋官方產業分類＋官方MI_INDEX指數歷史 → 逐週官方群組trend_score → 單一維度
（族群前5 vs 非前5）forward 報酬對照 → research/official_sector_grid/。
CLI 只保留參數解析（比照 g3_grid_runner.py 慣例）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_official_sector_grid(settings: Path, out_dir: Path | None) -> None:
    """docs/31 §10 milestone：官方產業分類×MI_INDEX官方指數，重測「族群前5」。"""
    import yaml

    from tw_screener.analysis.sector_universe import load_industry_mapping
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import official_sector_grid as osg
    from tw_screener.backtest.rotation_efficacy import (
        official_membership_frame,
        trend_score_series,
        weekly_snapshot_dates,
    )
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    oc = cfg.get("backtest", {}).get("official_sector_grid", {})
    horizons = tuple(int(h) for h in oc.get("horizons_td", [10, 20]))
    top_n = int(oc.get("top_n_groups", 5))
    snapshot_gap_td = int(oc.get("snapshot_gap_td", 5))
    n_boot = int(oc.get("n_boot", 1000))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(oc.get("output_dir", "research/official_sector_grid"))

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)

    client = create_client(settings)
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    industry = load_industry_mapping(cache_dir)
    if industry.is_empty():
        console.print("[red]缺官方產業分類（industry_*.parquet/otc_industry_*.parquet）[/red]")
        raise typer.Exit(1)
    official_raw = official_membership_frame(industry)
    membership = osg.build_official_sector_membership(official_raw)
    n_excluded = official_raw["stock_id"].n_unique() - membership["stock_id"].n_unique()
    n_total = official_raw["stock_id"].n_unique()

    sector_index = client.load_sector_index_history()
    if sector_index.is_empty():
        console.print(
            "[red]無官方族群指數快取——先跑 "
            "`tw-screener data backfill-sector-index --start ... --end ...`[/red]"
        )
        raise typer.Exit(1)
    baskets = osg.build_official_sector_baskets(sector_index)
    if baskets.is_empty() or membership.is_empty():
        console.print("[red]映射後族群/籃子為空，無法計算[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建官方群組 trend_score（逐週）...[/bold]")
    price = panel.select("date", "stock_id", "close")
    trend = trend_score_series(price, membership, baskets)

    weekly = set(weekly_snapshot_dates(panel["date"].unique().to_list()))
    trend_weekly = trend.filter(pl.col("date").is_in(list(weekly)))
    has_regime = "regime" in panel.columns
    stock_rows = membership.join(trend_weekly, on="sub_industry", how="inner").join(
        panel.select(
            "date", "stock_id",
            *[f"alpha{h}" for h in horizons],
            *(["regime"] if has_regime else []),
        ),
        on=["date", "stock_id"],
        how="left",
    )

    grid = osg.official_group_rank_grid(
        stock_rows, horizons=horizons, top_n_groups=top_n, n_boot=n_boot,
        snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    base_tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    grid.write_csv(out / f"official_sector_grid_{base_tag}.csv")

    def _c(v: object, suf: str = "", nd: int = 2) -> str:
        return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"

    n_weeks = stock_rows["date"].n_unique()
    n_groups = membership["sub_industry"].n_unique()
    lines = [
        "# docs/31 §10：官方族群指數重測「族群前5」（G3 leftover finding 獨立驗證）",
        "",
        "> 全新假設，非重跑G3資料——分組單位＝TWSE官方產業分類（非手標次產業）、"
        "族群強弱量尺＝MI_INDEX官方發布市值加權指數點數（非等權籃子）。門檻／停止條件"
        "已於 docs/31 §10.3 執行前預先登記，本報告只呈現結果，裁決依登記門檻套用。",
        "",
        f"- 產出日：{date.today()}；映射後 {n_groups} 個官方群組、{n_weeks} 週快照；"
        f"官方產業分類原始 {n_total} 檔中 {n_excluded} 檔（"
        f"{n_excluded / n_total * 100:.1f}%）因查無對應MI_INDEX類指數被排除"
        "（§10.2 明確排除清單，非隨機遺漏）。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}，供量級參考；`ci_lo`/`ci_hi`是對"
        "delta（cell當日均值−當日全樣本均值）做moving-block bootstrap CI，理由同G3。"
        "**只有2個cell**（族群前5 vs 非前5）——刻意不重建G3的4維格。",
        "",
    ]

    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"## r+{h}",
            "",
            "| 族群前5? | n | n_dates | mean | median | win | delta_mean | CI95(delta) "
            "| 前半 | 後半 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sub.iter_rows(named=True):
            ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            nd = r["n_dates"] if r["n_dates"] is not None else "—"
            lines.append(
                f"| {r['group_top5']} | {r['n']} | {nd} | {_c(r['mean'], '%')} "
                f"| {_c(r['median'], '%')} | {win} | {_c(r['delta_mean'], '%')} | {ci} "
                f"| {_c(r['mean_h1'], '%')} | {_c(r['mean_h2'], '%')} |"
            )
        lines.append("")

    lines += ["## 族群前5格 regime 切片＋升降級裁決（docs/31 §10.3 門檻）", ""]
    if not has_regime or stock_rows["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
    else:
        by_reg = osg.official_group_rank_by_regime(
            stock_rows, horizons=horizons, top_n_groups=top_n, n_boot=n_boot,
            snapshot_gap_td=snapshot_gap_td,
        )
        by_reg.write_csv(out / f"official_sector_grid_by_regime_{base_tag}.csv")
        for h in sorted(set(by_reg["horizon"].to_list())):
            subh = by_reg.filter(pl.col("horizon") == h)
            lines += [
                f"### r+{h}",
                "",
                "| regime | n | n_dates | mean | bs_CI95 | 樣本 |",
                "|---|---|---|---|---|---|",
            ]
            for r in subh.iter_rows(named=True):
                ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
                flag = "樣本不足" if r["thin"] else "可判"
                lines.append(
                    f"| {r['regime']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                    f"| {ci} | {flag} |"
                )
            lines.append("")

            full_cell = grid.filter((pl.col("horizon") == h) & (pl.col("group_top5")))
            full_delta = (
                full_cell.row(0, named=True)["delta_mean"] if not full_cell.is_empty() else None
            )
            if full_delta is None:
                lines.append(f"- 族群前5格 r+{h}：全樣本該格無資料——升降級裁決跳過。")
            else:
                full_sign = 1 if full_delta >= 0 else -1
                verdict, same, present = lab.regime_alignment_verdict(
                    subh, full_sign, value_col="mean"
                )
                h1 = full_cell.row(0, named=True)["mean_h1"]
                h2 = full_cell.row(0, named=True)["mean_h2"]
                h1h2_same = (
                    "同向" if (isinstance(h1, (int, float)) and isinstance(h2, (int, float))
                              and (h1 >= 0) == (h2 >= 0))
                    else "不同向或缺資料"
                )
                lines.append(
                    f"- 族群前5格 r+{h}（全樣本delta_mean {_c(full_delta, '%')}，原始alpha前半"
                    f"{_c(h1, '%')}／後半{_c(h2, '%')}＝{h1h2_same}）：跨regime同向數 "
                    f"{len(same)}/{present}（同向：{'、'.join(same) if same else '無'}）"
                    f"→ **{verdict}**"
                )
                console.print(f"  族群前5格 r+{h}：{len(same)}/{present} 同向・{verdict}")
            lines += [
                "",
                *lab.inference_footer(
                    sample_span=f"{stock_rows['date'].min()!s}~{stock_rows['date'].max()!s}",
                    regime_dist="、".join(
                        f"{reg} n={int(subh.filter(pl.col('regime') == reg)['n'].sum())}"
                        for reg in lab.REGIME_LABELS
                    ),
                    method_desc=(
                        f"moving-block bootstrap（block長度以快照步數換算horizon={h}td・"
                        f"B={n_boot}・seed=42）對族群前5格delta（cell當日均值−當日全樣本均值）"
                        "per-date序列算CI95"
                    ),
                    membership_desc="TWSE官方產業分類（映射至MI_INDEX類指數，§10.2）",
                ),
                "",
            ]

    lines += [
        "## 裁決（docs/31 §10.3 預先登記門檻套用）",
        "",
        "> 門檻：CI95不跨0 ＋ 跨regime同向≥2（尤其須含2022空頭）＋ 前後半段同向。三缺一→"
        "「未過關」；CI排除0在下、方向為負→「已否證」；三者皆過才可討論生產化價值"
        "（生產化本身仍需另一輪）。以上各horizon的實際裁決見「regime切片＋升降級裁決」"
        "段落，本節留給人工/主對話综合r+10/r+20兩窗寫最終結論。",
        "",
    ]

    md = out / f"official_sector_grid_{base_tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]官方族群指數重測報告 → {md}（{grid.height} cells）[/green]")
