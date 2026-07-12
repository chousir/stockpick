"""laggard 格編排（WS-D；自 cli.py 薄殼呼叫）。

面板＋WS-C trend_score 重建 → 逐週 2×2×位階 forward 報酬格 →
research/laggard_grid/。CLI 只保留參數解析。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_laggard_grid(settings: Path, out_dir: Path | None) -> None:
    """WS-D：族群強弱 × 個股領先落後 × 位階 forward 報酬格。"""
    import yaml

    from tw_screener.analysis.rotation import compute_subindustry_baskets
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import laggard_grid as lg
    from tw_screener.backtest import rotation_efficacy as eff

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    lgc = cfg.get("backtest", {}).get("laggard_grid", {})
    horizons = tuple(int(h) for h in lgc.get("horizons_td", [10, 20]))
    rs_window = int(lgc.get("rs_window", 20))
    n_boot = int(lgc.get("n_boot", 1000))
    base_zone = float(
        cfg.get("propicks_flags", {}).get("base_zone_ma60_max_pct", 10.0)
    )
    ext_gate = float(
        cfg.get("backtest", {}).get("diagnostic", {}).get("ext_gate_pct", 15.0)
    )
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(lgc.get("output_dir", "research/laggard_grid"))

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    membership = list_subindustries()
    if membership.is_empty():
        console.print("[red]缺次產業成員[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建族群 trend_score＋個股 rs_subind（逐週）...[/bold]")
    price = panel.select("date", "stock_id", "close", "volume")
    baskets = compute_subindustry_baskets(membership, price)
    trend = eff.trend_score_series(price, membership, baskets)
    rs = lg.stock_rs_vs_group(panel, membership, window=rs_window)

    weekly = set(eff.weekly_snapshot_dates(panel["date"].unique().to_list()))
    has_regime = "regime" in panel.columns  # WS-H.4a：面板 regime 欄（date 級 join 產物）
    stock_rows = (
        rs.filter(pl.col("date").is_in(list(weekly)))
        .join(trend, on=["sub_industry", "date"], how="left")
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
    grid = lg.laggard_cell_grid(
        stock_rows, horizons=horizons, tier_edges=(base_zone, ext_gate), n_boot=n_boot
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    grid.write_csv(out / f"laggard_grid_{tag}.csv")

    def _c(v: object, suf: str = "", nd: int = 2) -> str:
        return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"

    n_weeks = stock_rows["date"].n_unique()
    lines = [
        "# 族群內強弱 2×2×位階 forward 報酬格（WS-D・WS5-② 正式驗證）",
        "",
        f"- 產出日：{date.today()}；逐週快照 {n_weeks} 週×個股層；"
        f"維度＝族群強弱（trend_score 同日中位切）×個股領先/落後（rs_subind<0，"
        f"{rs_window} 日窗）×位階（≤{base_zone:.0f}／{base_zone:.0f}–{ext_gate:.0f}／"
        f">{ext_gate:.0f}）。",
        "- target＝個股 alpha{h}（vs 全市場等權中位）；**所有 cell 全列**；"
        "h1/h2＝前後半段平均（同向性）。單一多頭偏 regime；相鄰週窗重疊 CI 偏樂觀。",
        "",
    ]
    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"## r+{h}",
            "",
            "| 族群 | 個股 | 位階 | n | mean | CI95 | median | win | 前半 | 後半 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sub.iter_rows(named=True):
            ci = (
                f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]"
                if r["ci_lo"] is not None else "—"
            )
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            lines.append(
                f"| {r['group_strength']} | {r['stock_pos']} | {r['tier']} | {r['n']} "
                f"| {_c(r['mean'], '%')} | {ci} | {_c(r['median'], '%')} | {win} "
                f"| {_c(r['mean_h1'], '%')} | {_c(r['mean_h2'], '%')} |"
            )
        lines.append("")

    # ── regime 切片（WS-H.4a；docs/23 §1c 晉升鐵則 (c)）────────────────────────
    # 升降級規則（docs/23 §1c，寫死）：≥2 個可判 regime 切片與全樣本 cell mean 同向＝
    # 「跨 regime 穩健」；僅進攻同向＝「bull-only」（候補、不得晉升）；其餘＝
    # 「regime-dependent」。n_dates<30 切片照列標「樣本不足」，不進裁決分母。
    # cell×regime 樣本薄是預期——全 36 格照列（含 n=0），不藏格。
    lines += ["## regime 切片（cell×regime 全列・WS-H.4a・docs/23 §1c）", ""]
    if not has_regime or stock_rows["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
    else:
        by_reg = lg.laggard_cell_grid_by_regime(
            stock_rows, horizons=horizons, tier_edges=(base_zone, ext_gate), n_boot=n_boot
        )
        by_reg.write_csv(out / f"laggard_grid_by_regime_{tag}.csv")
        lines += [
            "mean＝per-date mean 序列等權平均（與 bs_CI 同一統計量，非 pooled mean）；"
            f"CI＝moving-block bootstrap（block=horizon+1・B={n_boot}・seed=42）。",
            "",
        ]
        for h in sorted(set(by_reg["horizon"].to_list())):
            subh = by_reg.filter(pl.col("horizon") == h)
            lines += [
                f"### r+{h}（cell×regime 全列）",
                "",
                "| regime | 族群 | 個股 | 位階 | n | n_dates | mean | bs_CI95 | 樣本 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for r in subh.iter_rows(named=True):
                ci = (
                    f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]"
                    if r["ci_lo"] is not None else "—"
                )
                flag = "樣本不足" if r["thin"] else "可判"
                lines.append(
                    f"| {r['regime']} | {r['group_strength']} | {r['stock_pos']} "
                    f"| {r['tier']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                    f"| {ci} | {flag} |"
                )
            lines.append("")

            # 焦點 cell 跨 regime 同向摘要：ambush（強×落後×貼低）＋強×領先×延伸
            tiers = lg._tier_labels((base_zone, ext_gate))
            for cell_name, gs, sp, tier in (
                ("ambush（強×落後×貼低）", "族群強", "落後", tiers[0]),
                ("強×領先×延伸", "族群強", "領先", tiers[2]),
            ):
                full_cell = grid.filter(
                    (pl.col("horizon") == h) & (pl.col("group_strength") == gs)
                    & (pl.col("stock_pos") == sp) & (pl.col("tier") == tier)
                )
                full_mean = (
                    full_cell.row(0, named=True)["mean"] if not full_cell.is_empty() else None
                )
                if full_mean is None:
                    lines.append(f"- {cell_name} r+{h}：全樣本該格無資料——跨 regime 摘要跳過。")
                    continue
                full_sign = 1 if full_mean >= 0 else -1
                slices = subh.filter(
                    (pl.col("group_strength") == gs) & (pl.col("stock_pos") == sp)
                    & (pl.col("tier") == tier)
                )
                verdict, same, present = lab.regime_alignment_verdict(
                    slices, full_sign, value_col="mean"
                )
                lines.append(
                    f"- {cell_name} r+{h}（全樣本 mean {_c(full_mean, '%')}）："
                    f"跨 regime 同向數 {len(same)}/{present}"
                    f"（同向切片：{'、'.join(same) if same else '無'}）→ 升降級：**{verdict}**"
                )
                console.print(f"  {cell_name} r+{h}：{len(same)}/{present} 同向・{verdict}")
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
                        f"moving-block bootstrap（block={h + 1}td・B={n_boot}・seed=42）"
                        "對 cell×regime 的 per-date mean 序列算 CI95；相鄰週快照前瞻窗"
                        "重疊，pooled CI 偏窄僅供對照（docs/22 §7.2）"
                    ),
                    membership_desc="今日 concepts.yaml（非 point-in-time）",
                ),
                "",
            ]

    md = out / f"laggard_grid_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]WS-D 報告 → {md}（{grid.height} cells）[/green]")
