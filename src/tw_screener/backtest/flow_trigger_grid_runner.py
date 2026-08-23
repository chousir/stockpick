"""flow_trigger_grid 編排（docs/31 §17 milestone；自 cli.py 薄殼呼叫）。

面板（foreign_net/trust_net/dealer_net 全歷史）→ 重建★投信流校準觸發序列（沿用
production `rotation.entry_signal`校準值，不重新調參）→ 手標46細分類（purity=0.5，
跟§12/§13.3/§14上線設定一致）逐週群組trend_score/排名 → 3-cell（top5／未進前5且
近期★觸發／未進前5無觸發）forward 報酬對照＋regime切片＋多段walk-forward →
research/flow_trigger_grid/。CLI 只保留參數解析（比照 rank_velocity_grid_runner.py 慣例）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_flow_trigger_grid(settings: Path, out_dir: Path | None) -> None:
    """docs/31 §17 milestone：flow_trigger 3-cell對照，全部重用既有函式。"""
    import yaml

    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import official_sector_grid as osg
    from tw_screener.backtest.rotation_efficacy import trend_score_series, weekly_snapshot_dates
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    fc = cfg.get("backtest", {}).get("flow_trigger_grid", {})
    horizons = tuple(int(h) for h in fc.get("horizons_td", [10, 20, 40]))
    top_n = int(fc.get("top_n_groups", 5))
    min_purity = float(fc.get("min_purity", 0.5))
    lookback_window = int(fc.get("lookback_window", 15))
    short_window = int(fc.get("short_window", 5))
    long_window = int(fc.get("long_window", 20))
    z_window = int(fc.get("z_window", 60))
    z_min_periods = int(fc.get("z_min_periods", 30))
    signal_col = str(fc.get("signal_col", "trust_flow_20d"))
    threshold = float(fc.get("threshold", 1.0))
    require_momentum = bool(fc.get("require_momentum", True))
    snapshot_gap_td = int(fc.get("snapshot_gap_td", 5))
    n_boot = int(fc.get("n_boot", 1000))
    n_splits = int(fc.get("n_splits", 4))
    min_train_frac = float(fc.get("min_train_frac", 0.4))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(fc.get("output_dir", "research/flow_trigger_grid"))

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    need_flow_cols = {"foreign_net", "trust_net", "dealer_net"}
    if not need_flow_cols.issubset(panel.columns):
        console.print(f"[red]面板缺法人欄位 {need_flow_cols - set(panel.columns)}[/red]")
        raise typer.Exit(1)

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
    if membership.is_empty() or baskets.is_empty():
        console.print("[red]映射後族群/籃子為空，無法計算[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建★投信流校準歷史觸發序列（2022-2026全期，沿用production校準值）...[/bold]")
    institutional_daily = panel.select("date", "stock_id", "foreign_net", "trust_net", "dealer_net")
    triggers = osg.build_flow_triggers(
        institutional_daily, membership, short_window=short_window, long_window=long_window,
        z_window=z_window, z_min_periods=z_min_periods, signal_col=signal_col,
        threshold=threshold, require_momentum=require_momentum,
    )
    if triggers.is_empty():
        console.print("[red]觸發序列為空——輸入資料異常或校準值在本樣本從未觸發[/red]")
        raise typer.Exit(1)
    daily_dates = sorted(panel["date"].unique().to_list())
    n_trig_groups = triggers["sub_industry"].n_unique()
    console.print(f"  觸發事件數：{triggers.height}（{n_trig_groups} 個群組曾觸發）")

    console.print("[bold]重建手標群組 trend_score（逐週，purity=0.5生產設定）...[/bold]")
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

    grid = osg.flow_trigger_grid(
        stock_rows, triggers, daily_dates, horizons=horizons, top_n_groups=top_n,
        lookback_window=lookback_window, n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    out.mkdir(parents=True, exist_ok=True)
    base_tag = date.today().strftime("%Y%m%d")
    grid.write_csv(out / f"flow_trigger_grid_{base_tag}.csv")

    def _c(v: object, suf: str = "", nd: int = 2) -> str:
        return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"

    n_weeks = stock_rows["date"].n_unique()
    n_groups = membership["sub_industry"].n_unique()
    lines = [
        "# docs/31 §17：flow_trigger——★投信流校準訊號能否預測未進前5族群的forward報酬",
        "",
        "> 焦點格＝`not_top5_triggered`——這裡測的是「還沒進前5、但近期有★投信流校準"
        "觸發」是否優於「還沒進前5、無觸發」，換一個資金流向（非價格衍生）的因果通道"
        "重試§14未過關的「提早卡位」問題。門檻/停止條件已於 docs/31 §17.1/§17.2 執行前"
        "預先登記，本報告只呈現結果，裁決依登記門檻套用。",
        "",
        f"- 產出日：{date.today()}；手標46細分類，purity≥{min_purity:.0%}"
        f"（跟§12/§13.3/§14上線設定一致，未重新調參數）；映射後 {n_groups} 個群組、"
        f"{n_weeks} 週快照；trigger規則沿用production `rotation.entry_signal`校準值"
        f"（{signal_col}_z>{threshold}∧flow_momentum>0，lookback_window={lookback_window}"
        "交易日，皆非本節新調參）。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}；`ci_lo`/`ci_hi`是對delta"
        "（cell當日均值−當日全樣本均值）做moving-block bootstrap CI，理由同§10/§14。",
        "",
    ]

    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"## r+{h}",
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

    lines += ["## `not_top5_triggered` regime 切片＋升降級裁決（docs/31 §17.2 門檻）", ""]
    if not has_regime or stock_rows["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
    else:
        by_reg = osg.flow_trigger_by_regime(
            stock_rows, triggers, daily_dates, horizons=horizons, top_n_groups=top_n,
            lookback_window=lookback_window, n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
        )
        by_reg.write_csv(out / f"flow_trigger_grid_by_regime_{base_tag}.csv")
        focus = by_reg.filter(pl.col("cell") == "not_top5_triggered")
        for h in sorted(set(focus["horizon"].to_list())):
            subh = focus.filter(pl.col("horizon") == h)
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

            full_cell = grid.filter(
                (pl.col("horizon") == h) & (pl.col("cell") == "not_top5_triggered")
            )
            full_delta = (
                full_cell.row(0, named=True)["delta_mean"] if not full_cell.is_empty() else None
            )
            if full_delta is None:
                lines.append(f"- `not_top5_triggered` r+{h}：全樣本該格無資料——升降級裁決跳過。")
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
                untrig_cell = grid.filter(
                    (pl.col("horizon") == h) & (pl.col("cell") == "not_top5_untriggered")
                )
                untrig_delta = (
                    untrig_cell.row(0, named=True)["delta_mean"]
                    if not untrig_cell.is_empty() else None
                )
                beats_untrig = (
                    "是" if (
                        isinstance(untrig_delta, (int, float)) and full_delta > untrig_delta
                    ) else "否"
                )
                lines.append(
                    f"- `not_top5_triggered` r+{h}（全樣本delta_mean {_c(full_delta, '%')}，"
                    f"原始alpha前半{_c(h1, '%')}／後半{_c(h2, '%')}＝{h1h2_same}；"
                    f"delta_mean是否優於`not_top5_untriggered`（{_c(untrig_delta, '%')}）："
                    f"{beats_untrig}）：跨regime同向數 {len(same)}/{present}"
                    f"（同向：{'、'.join(same) if same else '無'}）→ **{verdict}**"
                )
                console.print(f"  not_top5_triggered r+{h}：{len(same)}/{present} 同向・{verdict}")
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
                        f"B={n_boot}・seed=42）對`not_top5_triggered`格delta"
                        "（cell當日均值−當日全樣本均值）per-date序列算CI95"
                    ),
                    membership_desc="手標46細分類（§10.6映射），purity≥0.5",
                ),
                "",
            ]

    console.print("[bold]多段walk-forward（不選threshold，逐段獨立算delta/CI）...[/bold]")
    wf = osg.walk_forward_flow_trigger(
        stock_rows, triggers, daily_dates, horizons=horizons, top_n_groups=top_n,
        lookback_window=lookback_window, n_splits=n_splits, min_train_frac=min_train_frac,
        n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )
    if not wf.is_empty():
        wf.write_csv(out / f"flow_trigger_walk_forward_{base_tag}.csv")
        lines += ["## 多段walk-forward（docs/31 §17.2，不選threshold）", ""]
        focus_wf = wf.filter(pl.col("cell") == "not_top5_triggered")
        for h in sorted(set(focus_wf["horizon"].to_list())):
            subh = focus_wf.filter(pl.col("horizon") == h).sort("split_id")
            lines += [
                f"### r+{h}｜`not_top5_triggered`",
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
    else:
        lines += [
            "## 多段walk-forward", "",
            "> 週數不足以切出任何有效split——本段誠實跳過。", "",
        ]

    lines += [
        "## 裁決（docs/31 §17.2 預先登記門檻套用）", "",
        "> 焦點格`not_top5_triggered`門檻：CI95不跨0 ＋ 跨regime同向≥2（尤其須含防禦或"
        "中性至少一項）＋ 前後半段同向＋delta_mean優於`not_top5_untriggered`，缺一→"
        "「未過關」；CI排除0在下、方向為負→「已否證」；皆過才可討論新增揭露欄位。以上"
        "實際裁決見「regime切片＋升降級裁決」與「多段walk-forward」段落，本節留給人工/"
        "主對話综合各horizon寫最終結論。", "",
    ]

    md = out / f"flow_trigger_grid_{base_tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]flow_trigger報告 → {md}（{grid.height} cells）[/green]")
