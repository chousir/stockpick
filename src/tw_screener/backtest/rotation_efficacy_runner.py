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


def _mean_slice_rows(slices: pl.DataFrame, full_sign: int) -> list[str]:
    """regime_mean_slices 表 → markdown 列（regime/n/n_週/mean/bs_CI95/同向?；缺值 — 佔位）。"""
    out = []
    for r in slices.iter_rows(named=True):
        mean = f"{r['mean']:+.2f}pp" if r["mean"] is not None else "—"
        ci = (
            f"[{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}]" if r["ci_lo"] is not None
            else "—（<10 週不可重抽）"
        )
        if r["mean"] is None:
            align = "—"
        elif r["thin"]:
            align = "樣本不足（不裁決）"
        else:
            align = "同向" if (r["mean"] > 0) == (full_sign > 0) else "反向"
        out.append(
            f"| {r['regime']} | {r['n']} | {r['n_dates']} | {mean} | {ci} | {align} |"
        )
    return out


def _regime_dist(slices: pl.DataFrame, n_col: str = "n_dates") -> str:
    """切片 n 分布字串（inference_footer 第一行用；＝表實際切片 n，不引全樣本敘述）。"""
    return "、".join(f"{r['regime']} {n_col}={r[n_col]}" for r in slices.iter_rows(named=True))


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
    from tw_screener.backtest.regime_slice import block_len_for_horizon, load_regime_labels
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
    institutional, _ = _load_institutional_all(cache_dir)  # OTC 清單僅 panel build 用
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
    # 重建深度（WS-H.4a 讀碼確認）：週快照＝面板日期 ∩ 法人快取日期，兩者皆無程式上限
    # ——面板由 panel.history_days/panel_start（settings）決定、法人走 _load_institutional_all
    # 全量讀；第一輪只有 59 週是舊 institutional retention=400 天的資料邊界，非寫死窗
    # （rotation.history_days=250 只用於生產 runner，不在此路徑）。2022 回補批次落地後
    # 重跑即自動延伸至 ~230 週，無需改參數。
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

    # ── 5. regime 切片（WS-H.4a；docs/23 §1c 晉升鐵則 (c)）─────────────────────
    # 升降級規則（docs/23 §1c，寫死）：≥2 個可判切片與全樣本同向＝「跨 regime 穩健」；
    # 僅進攻同向＝「bull-only」（只能列候補，不得晉升）；其餘＝「regime-dependent」。
    # 切片 n<30 週＝「樣本不足」照列不裁決（factor_lab.regime_alignment_verdict 共用規則）。
    lines += [f"## 5. regime 切片（WS-H.4a・r+{h_main}、target={tgt}・docs/23 §1c）", ""]
    regime_path = Path(
        cfg.get("backtest", {}).get("regime_history", {}).get(
            "output_path", "research/panel/regime_labels.parquet"
        )
    )
    labels = load_regime_labels(regime_path)
    if labels.is_empty():
        lines += [
            f"> regime 標籤未產（{regime_path} 缺席或不可讀）——本段誠實跳過"
            "（先跑 make regime-history）。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，第 5 段跳過[/yellow]")
    else:
        sig_r = sig.join(
            labels.select("date", pl.col("regime_label").alias("regime")),
            on="date", how="left",
        )
        blk_w = block_len_for_horizon(h_main, weekly=True)  # 週頻：ceil(h/5)+1 週
        span = f"{sig_r['date'].min()!s}~{sig_r['date'].max()!s}"
        footer_kw = {
            "method_desc": (
                f"moving-block bootstrap（週頻 block={blk_w} 週・B={n_boot}・seed=42）"
                "對 per-週序列算 CI95；相鄰週前瞻窗重疊，pooled CI 偏窄僅供對照"
                "（docs/22 §7.2）"
            ),
            "membership_desc": "今日 concepts.yaml（非 point-in-time）",
        }

        # 5.1 trend_score IC 切片（週訊號日 join regime 標籤）
        full_rep = lab.evaluate(
            sig_r, "trend_score", horizon=h_main, target=tgt,
            buckets=4, n_splits=0, block_td=blk_w,
        )
        lines += [f"### 5.1 `trend_score` IC 切片（r+{h_main}）", ""]
        if full_rep.mean_daily_ic is None:
            lines += ["> 全樣本 per-週 IC 不可得——切片無基準，跳過。", ""]
        else:
            full_sign = 1 if full_rep.mean_daily_ic >= 0 else -1
            slices = lab.regime_ic_slices(
                sig_r, "trend_score", target=tgt, horizon=h_main, block_td=blk_w,
            )
            verdict, same, present = lab.regime_alignment_verdict(slices, full_sign)
            lines += [
                f"全樣本 mean_IC {full_rep.mean_daily_ic:+.3f}"
                f"（T={full_rep.inference_meta.get('T', 0)} 週）",
                "",
                "| regime | n_週 | mean_IC | bs_CI95 | 與全樣本同向？ |",
                "|---|---|---|---|---|",
            ]
            for r in slices.iter_rows(named=True):
                if r["mean_ic"] is None:
                    align = "—"
                elif r["thin"]:
                    align = "樣本不足（不裁決）"
                else:
                    align = "同向" if (r["mean_ic"] > 0) == (full_sign > 0) else "反向"
                ci = (
                    f"[{r['ci_lo']:+.3f}, {r['ci_hi']:+.3f}]" if r["ci_lo"] is not None
                    else "—（<10 週不可重抽）"
                )
                mic = f"{r['mean_ic']:+.3f}" if r["mean_ic"] is not None else "—"
                lines.append(f"| {r['regime']} | {r['n_dates']} | {mic} | {ci} | {align} |")
            lines += [
                "",
                f"跨 regime 同向數：{len(same)}/{present}"
                f"（同向切片：{'、'.join(same) if same else '無'}）→ 升降級：**{verdict}**",
                "",
                *lab.inference_footer(
                    sample_span=span, regime_dist=_regime_dist(slices), **footer_kw
                ),
                "",
            ]
            console.print(f"  regime 切片 trend_score：{len(same)}/{present} 同向・{verdict}")

            # 分桶表切片版（各 regime 子樣本 4 桶；形狀對照，不下裁決）
            lines += [
                f"trend_score 分桶（regime 切片版・4 桶・target={tgt}）：",
                "",
                "| regime | 桶(低→高) | n | mean | win |",
                "|---|---|---|---|---|",
            ]
            for reg in lab.REGIME_LABELS:
                btbl = lab.bucket_table(
                    sig_r.filter(pl.col("regime") == reg), "trend_score", tgt, buckets=4
                )
                if btbl.is_empty():
                    lines.append(f"| {reg} | — | 0 | — | — |")
                    continue
                for b in btbl.iter_rows(named=True):
                    lines.append(
                        f"| {reg} | {b['bucket']} | {b['n']} | {b['mean']:+.2f}pp "
                        f"| {b['win_rate']:.0%} |"
                    )
            lines.append("")

        # 5.2 quadrant 主升續勢 lift 切片＋5.3 ★ entry lift 切片（basket mean lift）
        star_expr = pl.col("entry_triggered").fill_null(False)
        for title, subset in (
            (
                f"5.2 quadrant「{eff.Q_TREND}」lift 切片",
                sig_r.filter(pl.col("quadrant") == eff.Q_TREND),
            ),
            (f"5.3 ★ entry lift 切片（{entry_col}>{entry_thr}）", sig_r.filter(star_expr)),
        ):
            lines += [f"### {title}（r+{h_main}）", ""]
            daily_full = (
                subset.drop_nulls([tgt]).group_by("date")
                .agg(pl.col(tgt).mean().alias("_m")).sort("date")
            )
            full_vals = [float(v) for v in daily_full["_m"].to_list() if v is not None]
            if not full_vals:
                lines += ["> 全樣本無可用觸發週——切片無基準，跳過。", ""]
                continue
            full_mean = sum(full_vals) / len(full_vals)
            full_sign = 1 if full_mean >= 0 else -1
            slices = lab.regime_mean_slices(subset, target=tgt, block_len=blk_w, n_boot=n_boot)
            verdict, same, present = lab.regime_alignment_verdict(
                slices, full_sign, value_col="mean"
            )
            lines += [
                f"全樣本 per-週 mean {full_mean:+.2f}pp（{len(full_vals)} 週）",
                "",
                "| regime | n | n_週 | mean | bs_CI95 | 與全樣本同向？ |",
                "|---|---|---|---|---|---|",
                *_mean_slice_rows(slices, full_sign),
                "",
                f"跨 regime 同向數：{len(same)}/{present}"
                f"（同向切片：{'、'.join(same) if same else '無'}）→ 升降級：**{verdict}**",
                "",
                *lab.inference_footer(
                    sample_span=span, regime_dist=_regime_dist(slices), **footer_kw
                ),
                "",
            ]
            console.print(
                f"  regime 切片 {title.split(' ')[1]}：{len(same)}/{present} 同向・{verdict}"
            )

    sig.write_csv(out / f"rotation_signals_weekly_{tag}.csv")
    md = out / f"rotation_efficacy_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]WS-C 報告 → {md}[/green]")
