"""docs/31 §22.5 維度1（族群輪動）編排——panel-only候選排列組合研究第1個維度。

自 cli.py 薄殼呼叫。維度1的stock_rows組建方式跟`official_sector_grid_runner.py`
（group_source="hand"）完全相同——重用同一組membership/basket建構函式，唯一差異是
cell定義（本節用§22.3.2統一的「前20%」動態門檻，非official_sector_top5的固定top5）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_redesign_dimension_grid(settings: Path, out_dir: Path | None, dimension: str) -> None:
    """docs/31 §22.5：維度1(族群輪動)全樣本讀值＋regime切片＋walk-forward保留驗證窗複核。

    Args:
        dimension: 目前只支援"rotation"（維度1）——維度2-5依§22.3.4排序，各自開工前
            才寫pre-registration＋加對應dimension值，本輪不預先搭好尚未存在的分支。
    """
    if dimension != "rotation":
        console.print(
            f"[red]--dimension 目前只支援 rotation（維度1，族群輪動），收到 {dimension!r}——"
            "維度2-5尚未pre-registration，見docs/31 §22.3.4[/red]"
        )
        raise typer.Exit(1)

    import yaml

    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import official_sector_grid as osg
    from tw_screener.backtest import redesign_dimension_grid as rdg
    from tw_screener.backtest.rotation_efficacy import trend_score_series, weekly_snapshot_dates
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rc = cfg.get("backtest", {}).get("redesign_dimension_grid", {})
    horizons = tuple(int(h) for h in rc.get("horizons_td", [10, 20, 40]))
    top_quantile = float(rc.get("top_quantile", 0.2))
    min_purity = float(rc.get("hand_min_purity", 0.5))
    snapshot_gap_td = int(rc.get("snapshot_gap_td", 5))
    n_boot = int(rc.get("n_boot", 1000))
    n_splits = int(rc.get("n_splits", 4))
    min_train_frac = float(rc.get("min_train_frac", 0.4))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(rc.get("output_dir", "research/redesign_dimension_grid"))

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
    sector_index = client.load_sector_index_history()
    if sector_index.is_empty():
        console.print(
            "[red]無官方族群指數快取——先跑 "
            "`tw-screener data backfill-sector-index --start ... --end ...`[/red]"
        )
        raise typer.Exit(1)

    hand = list_subindustries()
    purity = osg.compute_subindustry_purity(hand, industry)
    membership = osg.build_hand_sector_membership(hand, purity, min_purity=min_purity)
    baskets = osg.build_hand_sector_baskets(sector_index, purity, min_purity=min_purity)
    if baskets.is_empty() or membership.is_empty():
        console.print("[red]映射後族群/籃子為空，無法計算[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建手標次產業 trend_score（逐週）...[/bold]")
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

    grid = rdg.rotation_grid(
        stock_rows, horizons=horizons, top_quantile=top_quantile, n_boot=n_boot,
        snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    wf = rdg.walk_forward_rotation(
        stock_rows, horizons=horizons, top_quantile=top_quantile, n_splits=n_splits,
        min_train_frac=min_train_frac, n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )

    cells = rdg.build_rotation_cells(stock_rows, top_quantile=top_quantile)
    all_dates = sorted(cells["date"].unique().to_list())

    base_tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    grid.write_csv(out / f"redesign_dim1_rotation_{base_tag}.csv")
    if not wf.is_empty():
        wf.write_csv(out / f"redesign_dim1_rotation_wf_{base_tag}.csv")

    def _c(v: object, suf: str = "", nd: int = 2) -> str:
        return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"

    n_weeks = stock_rows["date"].n_unique()
    n_groups = membership["sub_industry"].n_unique()
    lines = [
        "# docs/31 §22.5：維度1（族群輪動）——panel-only候選排列組合研究第1個維度",
        "",
        "> 累積測試數：1/14。門檻已於 docs/31 §22.5 執行前預先登記，本報告只呈現結果，"
        "裁決依登記門檻套用。**明確不是重跑official_sector_top5**——那是固定top5門檻的"
        "既有研究、已生產化；本節依§22.3.2統一規則改用「前20%」動態門檻，是獨立測試。",
        "",
        f"- 產出日：{date.today()}；手標次產業（purity≥{min_purity:.0%}）映射後 {n_groups} "
        f"個群組、{n_weeks} 週快照；top_quantile={top_quantile:.0%}。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}，供量級參考；`ci_lo`/`ci_hi`是對"
        "delta（cell當日均值−當日全樣本均值）做moving-block bootstrap CI。",
        "",
        "## 全樣本讀值（hit=當日trend_score排名前20%、miss=其餘）",
        "",
    ]

    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"### r+{h}",
            "",
            "| cell | n | n_dates | mean | median | win | delta_mean | CI95(delta) "
            "| 前半 | 後半 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sub.iter_rows(named=True):
            ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            nd = r["n_dates"] if r["n_dates"] is not None else "—"
            lines.append(
                f"| {r['cell']} | {r['n']} | {nd} | {_c(r['mean'], '%')} "
                f"| {_c(r['median'], '%')} | {win} | {_c(r['delta_mean'], '%')} | {ci} "
                f"| {_c(r['mean_h1'], '%')} | {_c(r['mean_h2'], '%')} |"
            )
        lines.append("")

    lines += ["## walk-forward（第4段＝§22.5保留驗證窗，不用於搜尋）", ""]
    if wf.is_empty():
        lines += ["> walk-forward資料不足（可用週數過少），該段跳過。", ""]
        console.print("[yellow]walk-forward資料不足，跳過[/yellow]")
    else:
        for h in sorted(set(wf["horizon"].to_list())):
            subh = wf.filter((pl.col("horizon") == h) & (pl.col("cell") == "hit"))
            lines += [
                f"### r+{h}（hit格）",
                "",
                "| split | test期間 | n | n_dates | delta_mean | CI95 |",
                "|---|---|---|---|---|---|",
            ]
            for r in subh.iter_rows(named=True):
                ci = (
                    f"[{_c(r['test_ci_lo'])}, {_c(r['test_ci_hi'])}]"
                    if r["test_ci_lo"] is not None else "—"
                )
                lines.append(
                    f"| {r['split_id']} | {r['test_start']}~{r['test_end']} | {r['test_n']} "
                    f"| {r['test_n_dates']} | {_c(r['test_delta_mean'], '%')} | {ci} |"
                )
            lines.append("")

    lines += ["## regime切片＋裁決（docs/31 §22.5預先登記門檻）", ""]
    if not has_regime or stock_rows["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
    else:
        for h in horizons:
            emb = h + 1
            splits = lab.walk_forward_splits(
                all_dates, n_splits=n_splits, min_train_frac=min_train_frac, embargo_td=emb
            )
            if not splits:
                lines.append(f"- r+{h}：可用週數不足以切出walk-forward段，裁決跳過。")
                continue
            confirm_split = splits[-1]
            search_cells = cells.filter(pl.col("date") < confirm_split.test_start)

            search_grid = rdg.evaluate_signal_cells(
                search_cells, rdg.ROTATION_CELLS, horizons=(h,), n_boot=n_boot, seed=42,
                snapshot_gap_td=snapshot_gap_td,
            )
            hit_rows = search_grid.filter(pl.col("cell") == "hit")
            if hit_rows.is_empty():
                lines.append(f"- r+{h}：搜尋階段hit格無資料——裁決跳過。")
                continue
            hit = hit_rows.row(0, named=True)
            ci_lo, ci_hi = hit["ci_lo"], hit["ci_hi"]

            search_by_regime = rdg.evaluate_signal_cells_by_regime(
                search_cells, rdg.ROTATION_CELLS, horizons=(h,), n_boot=n_boot,
                snapshot_gap_td=snapshot_gap_td,
            ).filter(pl.col("cell") == "hit")

            def _regime_n(reg: str, df: pl.DataFrame = search_by_regime) -> int:
                return int(df.filter(pl.col("regime") == reg)["n"].sum())

            lines += [
                f"### r+{h}",
                "",
                "| regime | n | n_dates | mean | bs_CI95 | 樣本 |",
                "|---|---|---|---|---|---|",
            ]
            for r in search_by_regime.iter_rows(named=True):
                rci = (
                    f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
                )
                flag = "樣本不足" if r["thin"] else "可判"
                lines.append(
                    f"| {r['regime']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                    f"| {rci} | {flag} |"
                )
            lines.append("")

            h1, h2 = hit["mean_h1"], hit["mean_h2"]
            fh_same = (
                isinstance(h1, (int, float)) and isinstance(h2, (int, float))
                and (h1 >= 0) == (h2 >= 0)
            )
            fh_desc = "同向" if fh_same else "不同向或缺資料"

            same: list[str] = []
            if ci_lo is None:
                verdict = "資料不足"
            elif ci_hi is not None and ci_hi < 0:
                verdict = "已否證"
            elif ci_lo is not None and ci_lo > 0:
                full_sign = 1
                regime_verdict, same, present = lab.regime_alignment_verdict(
                    search_by_regime, full_sign, value_col="mean"
                )
                if regime_verdict == "跨 regime 穩健" and fh_same:
                    confirm_row = wf.filter(
                        (pl.col("horizon") == h) & (pl.col("cell") == "hit")
                        & (pl.col("split_id") == confirm_split.split_id)
                    )
                    confirm_delta = (
                        confirm_row.row(0, named=True)["test_delta_mean"]
                        if not confirm_row.is_empty() else None
                    )
                    if isinstance(confirm_delta, (int, float)) and confirm_delta >= 0:
                        verdict = "候選"
                    else:
                        verdict = (
                            f"觀察結果（未過保留驗證窗，第{confirm_split.split_id}段"
                            f"delta_mean={_c(confirm_delta, '%')}）"
                        )
                else:
                    verdict = f"未過關（regime裁決={regime_verdict}、前後半段{fh_desc}）"
            else:
                verdict = "未過關（CI跨0）"

            lines.append(
                f"- **r+{h}裁決：{verdict}**（搜尋階段delta_mean {_c(hit['delta_mean'], '%')}、"
                f"CI[{_c(ci_lo)}, {_c(ci_hi)}]、跨regime同向 {len(same)}、"
                f"前後半段{fh_desc}；保留驗證窗＝第{confirm_split.split_id}段"
                f"{confirm_split.test_start}~{confirm_split.test_end}）"
            )
            console.print(f"  r+{h}：{verdict}")
            lines += [
                "",
                *lab.inference_footer(
                    sample_span=f"{search_cells['date'].min()!s}~{search_cells['date'].max()!s}"
                    f"（搜尋階段，保留驗證窗{confirm_split.test_start}起另計）",
                    regime_dist="、".join(
                        f"{reg} n={_regime_n(reg)}" for reg in lab.REGIME_LABELS
                    ),
                    method_desc=(
                        f"moving-block bootstrap（block長度以快照步數換算horizon={h}td・"
                        f"B={n_boot}・seed=42）對hit格delta（cell當日均值−當日全樣本均值）"
                        "per-date序列算CI95"
                    ),
                    membership_desc=f"手標46細分類（purity≥{min_purity:.0%}，同§10.9/§12/§14/§17）",
                ),
                "",
            ]

    md = out / f"redesign_dim1_rotation_{base_tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]完成，報告見 {md}[/green]")
