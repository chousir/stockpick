"""G3 格編排（docs/31 §9 milestone；自 cli.py 薄殼呼叫）。

面板＋WS-C trend_score 重建＋net_flow_z（本檔新算，其餘全部重用既有模組）→ 逐週 G3 四維
forward 報酬格 → research/g3_grid/。CLI 只保留參數解析（比照 laggard_grid_runner.py 慣例）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def _net_flow_z(
    panel: pl.DataFrame, flow_window: int, z_window: int, z_min_periods: int
) -> pl.DataFrame:
    """(date, stock_id) → net_flow_z：三大法人合計淨額 flow_window 日滾動和，再取個股
    自身 z_window 日滾動 z（std=0 或暖機不足 → null，同 analysis/stock_panel.py::_rolling_z
    公式，此處不 import 該模組的私有函式，改地寫一份同公式的版本，避免跨模組耦合私有 API）。
    """
    total_net = (
        pl.col("foreign_net").fill_null(0)
        + pl.col("trust_net").fill_null(0)
        + pl.col("dealer_net").fill_null(0)
    )
    df = panel.select("date", "stock_id", "foreign_net", "trust_net", "dealer_net").sort(
        "stock_id", "date"
    ).with_columns(total_net.alias("_total_net"))
    df = df.with_columns(
        pl.col("_total_net")
        .rolling_sum(flow_window, min_samples=flow_window)
        .over("stock_id")
        .alias("_net_flow_w")
    )
    mean = pl.col("_net_flow_w").rolling_mean(z_window, min_samples=z_min_periods).over("stock_id")
    std = pl.col("_net_flow_w").rolling_std(z_window, min_samples=z_min_periods).over("stock_id")
    df = df.with_columns(
        pl.when(std > 0).then((pl.col("_net_flow_w") - mean) / std).otherwise(None).alias(
            "net_flow_z"
        )
    )
    return df.select("date", "stock_id", "net_flow_z")


def run_g3_grid(settings: Path, out_dir: Path | None, membership_source: str = "concepts") -> None:
    """docs/31 §9 milestone：G3（族群前5×個股貼落後×未過熱×資金非負）forward 報酬格。"""
    import yaml

    from tw_screener.analysis.rotation import compute_subindustry_baskets
    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import g3_grid as g3
    from tw_screener.backtest import laggard_grid as lg
    from tw_screener.backtest import rotation_efficacy as eff

    if membership_source not in eff.MEMBERSHIP_SOURCES:
        console.print(
            f"[red]--membership 需為 {'|'.join(eff.MEMBERSHIP_SOURCES)}，"
            f"收到 {membership_source!r}[/red]"
        )
        raise typer.Exit(1)

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    g3c = cfg.get("backtest", {}).get("g3_grid", {})
    horizons = tuple(int(h) for h in g3c.get("horizons_td", [10, 20]))
    rs_window = int(g3c.get("rs_window", 20))
    rs_lo, rs_hi = (float(v) for v in g3c.get("rs_range", [-15.0, 0.0]))
    ma60_lo, ma60_hi = (float(v) for v in g3c.get("ma60_range", [-10.0, 10.0]))
    top_n = int(g3c.get("top_n_groups", 5))
    flow_window = int(g3c.get("flow_window", 20))
    flow_z_window = int(g3c.get("flow_z_window", 60))
    flow_z_min = int(g3c.get("flow_z_min_periods", 30))
    snapshot_gap_td = int(g3c.get("snapshot_gap_td", 5))
    n_boot = int(g3c.get("n_boot", 1000))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(g3c.get("output_dir", "research/g3_grid"))

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    n_official_excluded: int | None = None
    if membership_source == "official":
        cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
        industry = load_industry_mapping(cache_dir)
        membership = eff.official_membership_frame(industry)
        n_official_excluded = industry.height - membership.height
    else:
        membership = list_subindustries()
    if membership.is_empty():
        console.print("[red]缺次產業成員[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建族群 trend_score＋個股 rs_subind＋net_flow_z（逐週）...[/bold]")
    price = panel.select("date", "stock_id", "close", "volume")
    baskets = compute_subindustry_baskets(membership, price)
    trend = eff.trend_score_series(price, membership, baskets)
    rs = lg.stock_rs_vs_group(panel, membership, window=rs_window)
    flow_z = _net_flow_z(panel, flow_window, flow_z_window, flow_z_min)

    weekly = set(eff.weekly_snapshot_dates(panel["date"].unique().to_list()))
    has_regime = "regime" in panel.columns
    stock_rows = (
        rs.filter(pl.col("date").is_in(list(weekly)))
        .join(trend, on=["sub_industry", "date"], how="left")
        .join(flow_z, on=["date", "stock_id"], how="left")
        .join(
            panel.select(
                "date", "stock_id", "ma60_dist_pct",
                *[f"alpha{h}" for h in horizons],
                *(["regime"] if has_regime else []),
            ),
            on=["date", "stock_id"],
            how="left",
        )
    )
    grid = g3.g3_cell_grid(
        stock_rows, horizons=horizons, rs_range=(rs_lo, rs_hi),
        ma60_range=(ma60_lo, ma60_hi), top_n_groups=top_n, n_boot=n_boot,
        snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    base_tag = date.today().strftime("%Y%m%d")
    is_official = membership_source == "official"
    tag = base_tag + ("_official" if is_official else "")
    out.mkdir(parents=True, exist_ok=True)
    grid.write_csv(out / f"g3_grid_{tag}.csv")

    def _c(v: object, suf: str = "", nd: int = 2) -> str:
        return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"

    n_weeks = stock_rows["date"].n_unique()
    lines = [
        "# docs/31 G3 候選 filter 驗證：族群前5×個股貼落後×位階中段×資金非負"
        + ("（membership=TWSE 官方產業別・粗分類 robustness 版）" if is_official else ""),
        "",
        "> **2026-08-22 修正版**：初版有 rank bug（排名分母是個股列非次產業，「前5」實際只"
        "框到約第1名族群）＋位階單邊門檻混入破線股＋CI對右偏原始alpha檢定近乎必過。三者皆"
        "已修正（opus複核發現），初版數字作廢，本檔為修正後重跑結果。",
        "",
        f"- 產出日：{date.today()}；逐週快照 {n_weeks} 週×個股層（週頻抽樣，快照間隔約"
        f"{snapshot_gap_td}交易日，**仍與horizon重疊**——CI已改用block bootstrap、block長度"
        "以快照步數換算，非naive iid pooled bootstrap，但重疊本身無法靠抽樣頻率消除，"
        "解讀時仍需保守）。",
        f"- 維度＝族群強弱（trend_score 當日跨次產業排名前{top_n}）×個股領先/落後"
        f"（rs_subind∈[{rs_lo:.0f},{rs_hi:.0f}]，{rs_window}日窗）×位階"
        f"（ma60_dist_pct∈[{ma60_lo:.0f},{ma60_hi:.0f}]，雙邊區間排除破線股）×資金"
        f"（net_flow_{flow_window}d_z≥0）。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}（vs全市場等權中位），供量級參考；"
        "**`ci_lo`/`ci_hi`是對delta（cell當日均值−當日全樣本均值）做CI，不是對原始alpha**"
        "——原始alpha右偏，CI幾乎必然不跨0，不是有意義的檢定。**16 cell 全列**"
        "（`is_g3=true`為四維同時命中的目標格）；h1/h2＝前後半段原始alpha平均（同向性，"
        "非完整4/5 walk-forward，只是二段近似）。",
        "",
    ]
    if is_official:
        lines.append(
            "- membership=TWSE/OTC 官方產業別（粗分類，非手標次產業）；"
            f"官方缺分類誠實排除 {n_official_excluded} 檔、不兜底。"
        )
        lines.append("")

    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"## r+{h}",
            "",
            "| 族群 | 個股 | 位階 | 資金 | G3? | n | n_dates | mean | median | win "
            "| delta_mean | CI95(delta) | 前半 | 後半 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sub.iter_rows(named=True):
            ci = (
                f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            )
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            mark = "**G3**" if r["is_g3"] else ""
            nd = r["n_dates"] if r["n_dates"] is not None else "—"
            lines.append(
                f"| {r['group_strength']} | {r['stock_pos']} | {r['tier']} | {r['flow']} "
                f"| {mark} | {r['n']} | {nd} | {_c(r['mean'], '%')} | {_c(r['median'], '%')} "
                f"| {win} | {_c(r['delta_mean'], '%')} | {ci} "
                f"| {_c(r['mean_h1'], '%')} | {_c(r['mean_h2'], '%')} |"
            )
        lines.append("")

    # ── regime 切片＋docs/23 §1c 升降級裁決（只列 G3 目標格 vs 全體，全16格見上表）────
    lines += ["## G3 格 regime 切片＋升降級裁決（docs/23 §1c；docs/31 §7.4 預先登記門檻）", ""]
    if not has_regime or stock_rows["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
    else:
        by_reg = g3.g3_cell_grid_by_regime(
            stock_rows, horizons=horizons, rs_range=(rs_lo, rs_hi),
            ma60_range=(ma60_lo, ma60_hi), top_n_groups=top_n, n_boot=n_boot,
            snapshot_gap_td=snapshot_gap_td,
        )
        by_reg.write_csv(out / f"g3_grid_by_regime_{tag}.csv")
        lines += [
            "mean＝delta（cell當日均值−當日全樣本均值）per-date序列等權平均（與bs_CI同一"
            f"統計量）；CI＝moving-block bootstrap（block長度以快照步數換算，非交易日數・"
            f"B={n_boot}・seed=42）。",
            "",
        ]
        for h in sorted(set(by_reg["horizon"].to_list())):
            subh = by_reg.filter(pl.col("horizon") == h)
            lines += [
                f"### r+{h}",
                "",
                "| regime | n | n_dates | mean | bs_CI95 | 樣本 |",
                "|---|---|---|---|---|---|",
            ]
            for r in subh.iter_rows(named=True):
                ci = (
                    f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
                )
                flag = "樣本不足" if r["thin"] else "可判"
                lines.append(
                    f"| {r['regime']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                    f"| {ci} | {flag} |"
                )
            lines.append("")

            full_cell = grid.filter((pl.col("horizon") == h) & (pl.col("is_g3")))
            full_delta = (
                full_cell.row(0, named=True)["delta_mean"] if not full_cell.is_empty() else None
            )
            if full_delta is None:
                lines.append(f"- G3格 r+{h}：全樣本該格無資料——升降級裁決跳過。")
            else:
                full_sign = 1 if full_delta >= 0 else -1
                # by_reg 的 mean 也是 delta 口徑（見上方 lines 註記），與 full_sign 同一量尺
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
                    f"- G3格 r+{h}（全樣本delta_mean {_c(full_delta, '%')}，原始alpha前半"
                    f"{_c(h1, '%')}／後半{_c(h2, '%')}＝{h1h2_same}）：跨regime同向數 "
                    f"{len(same)}/{present}（同向：{'、'.join(same) if same else '無'}）"
                    f"→ **{verdict}**"
                )
                console.print(f"  G3格 r+{h}：{len(same)}/{present} 同向・{verdict}")
            dist = "、".join(
                f"{reg} n={int(subh.filter(pl.col('regime') == reg)['n'].sum())}"
                for reg in lab.REGIME_LABELS
            )
            lines += [
                "",
                *lab.inference_footer(
                    sample_span=f"{stock_rows['date'].min()!s}~{stock_rows['date'].max()!s}",
                    regime_dist=dist,
                    method_desc=(
                        f"moving-block bootstrap（block長度以快照步數換算horizon={h}td・"
                        f"B={n_boot}・seed=42）對G3格delta（cell當日均值−當日全樣本均值）"
                        "per-date序列算CI95；週頻快照間隔仍小於horizon，重疊無法靠抽樣頻率"
                        "完全消除，解讀時仍需保守（docs/22 §7.2；本輪L6複核已示範此風險）"
                    ),
                    membership_desc=(
                        "TWSE/OTC 官方產業別（粗分類）robustness 版"
                        if is_official else "今日 concepts.yaml（非 point-in-time）"
                    ),
                ),
                "",
            ]

    lines += [
        "## 裁決（docs/31 §7.4 預先登記門檻套用）",
        "",
        "> 門檻：方向與全樣本同號的可判regime切片 ≥2（`跨regime穩健`）＋ 前後半段同向 ＋ "
        "G3格CI95不跨0。三缺一→停在揭露欄，按docs/24格式記錄，不寫成「未驗證」。"
        "以上各horizon的實際裁決見§「regime切片＋升降級裁決」段落，本節留給人工/主對話"
        "综合R+10/r+20兩窗、對照全16格是否有更強格，寫最終結論。",
        "",
    ]

    md = out / f"g3_grid_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]G3 驗證報告 → {md}（{grid.height} cells）[/green]")
