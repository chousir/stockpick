"""rotation 效度編排（WS-C；自 cli.py 薄殼呼叫）。

面板＋法人快取 → 歷史重建逐週輪動訊號 → 生產快照對表驗證 → forward basket
IC/lift 檢驗＋榜外機會成本 → research/rotation_efficacy/。CLI 只保留參數解析。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def _lift_rows(tbl: pl.DataFrame) -> list[str]:
    """lift 表 → markdown 列（值缺 → 全 — 佔位）。"""
    out = []
    for r in tbl.iter_rows(named=True):
        if r["ci_lo"] is not None and r["mean"] is not None:
            out.append(
                f"| {r['category']} | {r['n']} | {r['mean']:+.2f}% "
                f"| [{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}] | {r['median']:+.2f}% "
                f"| {r['win_rate']:.0%} |"
            )
        else:
            out.append(f"| {r['category']} | {r['n']} | — | — | — | — |")
    return out


def _load_production_snapshots(reports_dir: Path) -> pl.DataFrame:
    """各週 sector_rotation.csv concat（舊週缺欄補 null；供重建對表）。"""
    frames: list[pl.DataFrame] = []
    for csv in sorted(reports_dir.glob("*/sector_rotation.csv")):
        try:
            df = pl.read_csv(csv, infer_schema_length=2000)
        except Exception as e:  # noqa: BLE001 — 單週快照壞掉不擋對表
            console.print(f"[yellow]讀 {csv} 失敗（{e}），跳過[/yellow]")
            continue
        if "sub_industry" not in df.columns or "date" not in df.columns:
            continue
        casts = {
            "trend_score": pl.Float64,
            "quadrant": pl.Utf8, "freshness": pl.Utf8, "flow_turn": pl.Utf8,
            "sub_industry": pl.Utf8,
        }
        frames.append(
            df.with_columns(pl.col("date").cast(pl.Utf8).str.to_date(strict=False))
            .select(
                [
                    pl.col("date"),
                    *[
                        (
                            pl.col(c).cast(dt, strict=False)
                            if c in df.columns
                            else pl.lit(None, dtype=dt).alias(c)
                        )
                        for c, dt in casts.items()
                    ],
                ]
            )
        )
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed").unique(
        subset=["date", "sub_industry"], keep="first"
    )


def run_rotation_efficacy(settings: Path, out_dir: Path | None) -> None:
    """WS-C：重建→驗證→IC/lift→榜外機會成本。"""
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        compute_subindustry_baskets,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import rotation_efficacy as eff
    from tw_screener.backtest.panel_runner import _load_institutional_all
    from tw_screener.backtest.rotation_calib import standardize_signals

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    re_cfg = cfg.get("backtest", {}).get("rotation_efficacy", {})
    horizons = tuple(int(h) for h in re_cfg.get("horizons_td", [5, 10, 20]))
    n_splits = int(re_cfg.get("n_splits", 3))
    min_train_frac = float(re_cfg.get("min_train_frac", 0.4))
    n_boot = int(re_cfg.get("n_boot", 1000))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(re_cfg.get("output_dir", "research/rotation_efficacy"))
    rot = cfg.get("rotation", {})
    s_win = int(rot.get("short_window", 5))
    l_win = int(rot.get("long_window", 20))
    min_members = int(rot.get("min_members", 5))
    quad = rot.get("quadrant", {})
    pos_window = int(quad.get("position_window", 60))
    pos_low = float(quad.get("position_low_pct", 10.0))
    entry = rot.get("entry_signal", {})
    entry_name = str(entry.get("signal", "trust_flow_20d"))
    entry_col = f"{entry_name}_z" if entry.get("mode", "z") == "z" else entry_name
    entry_thr = float(entry.get("threshold", 1.0))
    z_window = int(rot.get("calibration", {}).get("z_window", 60))
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    price = panel.select("date", "stock_id", "close", "volume")
    membership = list_subindustries()
    institutional = _load_institutional_all(cache_dir)
    if membership.is_empty() or institutional.is_empty():
        console.print("[red]缺次產業成員或法人快取[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建逐週輪動訊號（flows z／位階／趨勢分）...[/bold]")
    flows = compute_fund_flows(
        membership, institutional,
        volume_history=price.select("date", "stock_id", "volume"),
        short_window=s_win, long_window=l_win,
    )
    flows_z = standardize_signals(flows, z_window=z_window)
    baskets = compute_subindustry_baskets(membership, price)
    position = eff.basket_position_series(baskets, position_window=pos_window)
    trend = eff.trend_score_series(price, membership, baskets)
    snap_dates = [
        d for d in eff.weekly_snapshot_dates(panel["date"].unique().to_list())
        if d in set(flows_z["date"].unique().to_list())
    ]
    signals = eff.rotation_signal_panel(
        flows_z, position, trend, snap_dates,
        short_window=s_win, long_window=l_win, position_low_pct=pos_low,
        entry_col=entry_col, entry_threshold=entry_thr, min_members=min_members,
    )
    fwd = eff.basket_forward_returns(panel, membership, horizons=horizons)
    sig = signals.join(fwd, on=["sub_industry", "date"], how="left")
    console.print(
        f"  重建 {sig['date'].n_unique()} 週 × {sig['sub_industry'].n_unique()} 族群"
        f"＝{sig.height} 列"
    )

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# rotation 欄位效度（WS-C・歷史重建 forward basket 檢驗）",
        "",
        f"- 產出日：{date.today()}；週快照 {sig['date'].n_unique()} 週"
        f"（{sig['date'].min()!s} ~ {sig['date'].max()!s}）"
        f"×{sig['sub_industry'].n_unique()} 族群；單一多頭偏 regime。",
        f"- 訊號重建＝生產表達式鏡像（z rolling {z_window} 日、位階 {pos_window} 日、"
        f"趨勢分權重同生產）；forward＝面板 basket 等權 r/alpha（entry 次一交易日）。",
        "- ⚠️ r+20 相鄰週窗重疊（自相關），CI 偏樂觀；裁決以方向與跨段一致性為主。",
        "",
    ]

    # ── 0. 重建 vs 生產快照對表 ────────────────────────────────────────────────
    prod = _load_production_snapshots(reports_dir)
    lines += ["## 0. 重建正確性（vs 生產 sector_rotation.csv 同日對表）", ""]
    cmp_df = eff.compare_with_production(sig, prod)
    if cmp_df.is_empty():
        lines.append("> 無可對表的生產快照日（週五重建日 vs 快照 data_date 未交集）——降信心使用。")
    else:
        def _f1(v: object) -> str:
            return f"{float(v):.1f}" if isinstance(v, (int, float)) else "—"

        def _p0(v: object) -> str:
            return f"{float(v):.0%}" if isinstance(v, (int, float)) else "—"

        lines += [
            "| date | n | trend 平均絕對差 | quadrant 一致 | freshness 一致 | flow_turn 一致 |",
            "|---|---|---|---|---|---|",
            *[
                f"| {r['date']} | {r['n']} | {_f1(r['trend_mad'])} | {_p0(r['quadrant_match'])} "
                f"| {_p0(r['freshness_match'])} | {_p0(r['flow_turn_match'])} |"
                for r in cmp_df.iter_rows(named=True)
            ],
            "",
        ]
        for r in cmp_df.iter_rows(named=True):
            console.print(
                f"  對表 {r['date']}：quadrant {_p0(r['quadrant_match'])}・"
                f"trend MAD {_f1(r['trend_mad'])}"
            )

    # ── 1. 連續欄 IC（trend_score／rank_delta）─────────────────────────────────
    lines += ["## 1. 連續欄 IC（factor_lab 全套）", ""]
    for feat in ("trend_score", "rank_delta"):
        for h in horizons:
            rep = lab.evaluate(
                sig, feat, horizon=h, target=f"basket_alpha{h}",
                buckets=4, n_splits=n_splits, min_train_frac=min_train_frac,
            )
            lines += lab.render_report_md(rep)

    # ── 2. 類別欄 lift（quadrant／flow_turn／freshness／★entry）────────────────
    h_main = 20 if 20 in horizons else horizons[-1]
    tgt = f"basket_alpha{h_main}"
    lines += [f"## 2. 類別欄 lift（target={tgt}・bootstrap CI95）", ""]
    cat_specs = [
        ("quadrant", None),
        ("flow_turn", "（同號）"),
        ("freshness", None),
    ]
    for cat, null_as in cat_specs:
        tbl = eff.category_lift_table(sig, cat, tgt, n_boot=n_boot, include_null_as=null_as)
        tbl.write_csv(out / f"lift_{cat}_{tag}.csv")
        lines += [
            f"### {cat}",
            "",
            "| 類別 | n | mean | CI95 | median | win |",
            "|---|---|---|---|---|---|",
            *_lift_rows(tbl),
            "",
        ]
    star = sig.with_columns(
        pl.when(pl.col("entry_triggered")).then(pl.lit("★觸發")).otherwise(pl.lit("未觸發"))
        .alias("_star")
    )
    tbl = eff.category_lift_table(star, "_star", tgt, n_boot=n_boot)
    lines += [
        f"### ★ entry_triggered（{entry_col}>{entry_thr}）",
        "",
        "| 類別 | n | mean | CI95 | median | win |",
        "|---|---|---|---|---|---|",
        *_lift_rows(tbl),
        "",
    ]

    # ── 3. 退潮轉折假設（W27→W28 敗因）：主升續勢內 flow_turn=退潮 是否領先轉弱 ──
    lines += ["## 3. 退潮轉折假設（主升續勢族群內，flow_turn＝退潮 vs 同號）", ""]
    trending = sig.filter(pl.col("quadrant") == eff.Q_TREND)
    lines += ["| 組 | n | mean | CI95 | median | win |", "|---|---|---|---|---|---|"]
    for h in horizons:
        tbl = eff.category_lift_table(
            trending, "flow_turn", f"basket_alpha{h}", n_boot=n_boot, include_null_as="（同號）"
        )
        tbl = tbl.with_columns(("r+" + str(h) + " " + pl.col("category")).alias("category"))
        lines += _lift_rows(tbl)
    lines.append("")

    # ── 4. 榜外機會成本（members < min_members 不列輪動排名）───────────────────
    lines += [f"## 4. 榜外機會成本（members < {min_members}）", ""]
    ob = eff.category_lift_table(
        sig.with_columns(
            pl.when(pl.col("on_board")).then(pl.lit("上榜")).otherwise(pl.lit("榜外"))
            .alias("_board")
        ),
        "_board", tgt, n_boot=n_boot,
    )
    lines += [
        "| 組 | n | mean | CI95 | median | win |",
        "|---|---|---|---|---|---|",
        *_lift_rows(ob),
        "",
    ]
    missed = (
        sig.filter(~pl.col("on_board") & (pl.col(tgt) > 5.0))
        .sort(tgt, descending=True)
        .head(12)
    )
    if not missed.is_empty():
        lines += [
            f"榜外且 {tgt} > +5pp 的具名案例（機會成本清單）：",
            "",
            "| date | 族群 | members | basket_alpha20 |",
            "|---|---|---|---|",
            *[
                f"| {r['date']} | {r['sub_industry']} | {r['members']} | {r[tgt]:+.1f}pp |"
                for r in missed.iter_rows(named=True)
            ],
            "",
        ]

    sig.write_csv(out / f"rotation_signals_weekly_{tag}.csv")
    md = out / f"rotation_efficacy_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]WS-C 報告 → {md}[/green]")
