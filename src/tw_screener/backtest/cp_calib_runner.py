"""CP 值補漲股校準（cp calibrate）研究軌編排（自 cli.py 下沉）。

B2 個股版起漲事件回測：三 label 各掃因子訊號，產 research/cp_value/ 報告。
CLI 只保留參數解析＋呼叫 run_cp_calibration；純研究軌、不在主 make week 流程。
"""

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_cp_calibration(out_dir: Path, settings: Path) -> None:
    """B2 個股版起漲事件回測（研究軌）：三 label 各掃因子訊號，產 research/cp_value/ 報告。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_subindustry_baskets,
        load_market_history,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.analysis.stock_panel import build_stock_panel, compute_coverage_meta
    from tw_screener.backtest.stock_calib import (
        _laggard_lift_significance,
        _rs_subind_col,
        compute_cross_window_lead,
        detect_ambush_episodes,
        detect_breakout_episodes,
        detect_reversal_episodes,
        detect_top_episodes,
        dom_monotonicity_spearman,
        dom_monotonicity_table,
        factor_monotonicity_spearman,
        factor_monotonicity_table,
        holdout_table,
        interaction_2x2_table,
        laggard_filter_precision,
        liquidity_table,
        payoff_decay_table,
        render_cp_calibration_report,
        render_cross_window_lead,
        render_dom_monotonicity_report,
        render_interaction_report,
        render_laggard_filter_report,
        render_laggard_monotonicity_report,
        render_robustness_report,
        render_top_calibration_report,
        scan_stock_signals,
        scan_top_signals,
    )
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    # 研究軌旋鈕（windows/labels/門檻網格/robustness/monotonicity/interaction/top_calib）集中於
    # 獨立研究檔，主 make week 不載、與生產設定分離（規劃書 04 A2）；合併進 cfg["cp_value"]，
    # 既有 cp.get(...) 讀法不變。缺檔則回退函式內建預設（與舊值一致），不會壞。
    calib_path = Path(settings).parent / "research" / "cp_value_calib.yaml"
    if calib_path.exists():
        with open(calib_path) as f:
            calib = yaml.safe_load(f) or {}
        cfg.setdefault("cp_value", {}).update(calib.get("cp_value", {}))
    cp = cfg.get("cp_value", {})
    rot = cfg.get("rotation", {})
    history_days = int(cp.get("history_days", 250))
    z_window = int(cp.get("z_window", 60))
    z_min_periods = int(cp.get("z_min_periods", 30))
    lead_window = int(cp.get("lead_window", 15))
    z_thr = tuple(cp.get("z_thresholds", [0.5, 1.0, 1.5, 2.0]))
    vol_thr = tuple(cp.get("volume_thresholds", [1.0, 1.5, 2.0]))
    position_low_pct = float(cp.get("position_low_pct", 15.0))
    min_triggers = int(cp.get("min_triggers", 8))
    min_lift = float(cp.get("min_lift", 1.3))
    windows = tuple(int(x) for x in cp.get("windows", [1, 3, 5, 10, 20]))
    early_cfg = cp.get("early_gate", {})
    early_on = bool(early_cfg.get("enabled", True))
    early_z = float(early_cfg.get("z_threshold", 1.0))
    early_lookback = int(early_cfg.get("lead_lookback", 30))
    early_min_lead = int(early_cfg.get("min_lead_days", 2))
    labels_cfg = cp.get("labels", {})
    rb = cp.get("robustness", {})  # B-P1 穩健度四件套（docs/15 T3）
    rb_anchor = str(rb.get("anchor_label", "ambush"))
    rb_top_k = int(rb.get("top_k", 6))
    rb_horizons = tuple(int(x) for x in rb.get("horizons", [5, 10, 20, 40]))
    rb_holdout_frac = float(rb.get("holdout_frac", 0.7))
    rb_adv_window = int(rb.get("adv_window", 20))
    rb_adv_min = float(rb.get("adv_min_amount", 100))
    long_window = int(cp.get("long_window", 20))  # dom 錨窗（panel dom_{long_window}d）
    mono = cp.get("monotonicity", {})  # B-P2 買方主導度單調性（docs/15 T1）
    mono_buckets = int(mono.get("n_buckets", 5))
    mono_fwd = int(mono.get("fwd_window", 20))
    mono_zsig = float(mono.get("z_sig", 1.96))
    inter = cp.get("interaction", {})  # B-P3 個股×族群 2×2 交互（docs/15 T2）
    inter_s_col = str(inter.get("s_flow_col", "foreign_flow_20d_z"))
    inter_s_z = float(inter.get("s_z_threshold", 0.5))
    inter_g_thr = float(inter.get("g_threshold", 0.0))
    inter_zsig = float(inter.get("z_sig", 1.96))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    console.print(f"[bold]載入上市資料（{history_days} 交易日）...[/bold]")
    # 個股層只框上市：只讀 daily_*（otc_daily_/stock_day_ 不符此 glob），docs/13 §4 B1
    market = load_market_history(cache_dir, n_days=history_days, patterns=("daily_*.parquet",))
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    members = list_subindustries()
    if market.is_empty():
        console.print("[red]缺上市日線快取（daily_*.parquet）[/red]")
        raise typer.Exit(1)

    baskets = (
        compute_subindustry_baskets(
            members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
        )
        if not members.is_empty()
        else market.head(0)
    )
    console.print(f"[bold]建個股特徵面板（窗集合 {list(windows)}）...[/bold]")
    panel = build_stock_panel(
        market,
        institutional,
        members,
        baskets,
        windows=windows,
        z_window=z_window,
        z_min_periods=z_min_periods,
    )
    if panel.is_empty():
        console.print("[red]面板為空——檢查日線/法人快取[/red]")
        raise typer.Exit(1)
    coverage = compute_coverage_meta(panel, institutional, members, universe="listed")
    console.print(
        f"面板：{coverage['n_stocks']} 檔 × {coverage['n_trading_days']} 日"
        f"・法人覆蓋 {coverage['inst_coverage_pct']}%"
        f"・次產業覆蓋 {coverage['subind_coverage_pct']}%"
    )

    detectors = {
        "ambush": (
            "L1 埋伏",
            "距 M 日低 ≤ tol% → 前瞻續漲（抓起漲前、價貼低）",
            lambda lp: detect_ambush_episodes(
                market,
                m_days=int(lp.get("m_days", 60)),
                tol_pct=float(lp.get("tol_pct", 5.0)),
                x_pct=float(lp.get("x_pct", 15.0)),
                n_days=int(lp.get("n_days", 20)),
                cooldown_days=int(lp.get("cooldown_days", 20)),
            ),
        ),
        "breakout": (
            "L2 追突破",
            "距 M 日低落在 [lo, hi]% 帶 → 前瞻續漲（抓起漲初）",
            lambda lp: detect_breakout_episodes(
                market,
                m_days=int(lp.get("m_days", 60)),
                lo_pct=float(lp.get("lo_pct", 3.0)),
                hi_pct=float(lp.get("hi_pct", 8.0)),
                x_pct=float(lp.get("x_pct", 12.0)),
                n_days=int(lp.get("n_days", 10)),
                cooldown_days=int(lp.get("cooldown_days", 15)),
            ),
        ),
        "reversal": (
            "L3 超跌反轉（選配）",
            "距 L 日高 ≤ −drawdown% → 前瞻反彈（抓 V 底）",
            lambda lp: detect_reversal_episodes(
                market,
                l_days=int(lp.get("l_days", 60)),
                drawdown_pct=float(lp.get("drawdown_pct", 20.0)),
                x_pct=float(lp.get("x_pct", 15.0)),
                n_days=int(lp.get("n_days", 15)),
                cooldown_days=int(lp.get("cooldown_days", 15)),
            ),
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    summary = [
        f"# 個股 CP 值起漲事件校準總表（{tag}）",
        "",
        f"- 窗集合 {list(windows)}"
        + (
            f"・M-MH 早偵測閘 開（z≥{early_z:g}・領先回看 {early_lookback} 日・"
            f"過閘需中位領先 ≥{early_min_lead} 日）"
            if early_on
            else "・M-MH 早偵測閘 關"
        ),
        "",
    ]
    anchor_scan: pl.DataFrame = pl.DataFrame()  # B-P1：捕捉錨定 label 的掃描結果供穩健度剖析
    anchor_eps: pl.DataFrame = pl.DataFrame()
    anchor_occupy = 15
    for key, (name, desc, detect) in detectors.items():
        lp = labels_cfg.get(key, {})
        episodes = detect(lp)
        report_params = {
            "fwd_n_days": int(lp.get("n_days", 0)),
            "fwd_x_pct": float(lp.get("x_pct", 0.0)),
            "cooldown_days": int(lp.get("cooldown_days", 0)),
            "lead_window": lead_window,
        }
        n_ep = episodes.height
        console.print(f"\n[bold]{name}[/bold]：事件 {n_ep} 個")
        if episodes.is_empty():
            summary.append(f"- **{name}**：事件 0 個——前瞻門檻過嚴或資料不足，無法掃描。")
            continue
        scan = scan_stock_signals(
            panel,
            episodes,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=int(lp.get("cooldown_days", 15)),
            z_min_periods=z_min_periods,
            early_gate=early_cfg if early_on else None,
        )
        if key == rb_anchor:  # B-P1：留存錨定 label 的掃描＋事件供穩健度剖析
            anchor_scan = scan
            anchor_eps = episodes
            anchor_occupy = int(lp.get("cooldown_days", 15))
        report = render_cp_calibration_report(
            scan, episodes, name, desc, report_params, coverage, min_triggers, min_lift
        )
        # M-MH Phase 2：跨窗配對領先（直接驗短窗是否早於 20d-z 達標＝GATE 核心）
        lead_df = pl.DataFrame()
        if early_on:
            lead_df = compute_cross_window_lead(
                panel, episodes, early_z, early_lookback, early_min_lead
            )
            report += "\n" + "\n".join(
                render_cross_window_lead(lead_df, early_z, early_min_lead)
            )
        (out_dir / f"calibration_{tag}_{key}.md").write_text(report, encoding="utf-8")
        scan.write_csv(out_dir / f"calibration_{tag}_{key}.csv")

        qualified = scan.filter(
            (pl.col("n_triggers") >= min_triggers)
            & (pl.col("lift").is_not_null())
            & (pl.col("lift") >= min_lift)
            & (pl.col("median_lead_days") > 0)
        ).sort("lift", descending=True)
        if qualified.is_empty():
            verdict = f"無因子過門檻（lift ≥{min_lift}・觸發 ≥{min_triggers}・領先 >0 日）"
            top = "—"
        else:
            b = qualified.row(0, named=True)
            top = (
                f"{b['signal']}（lift {b['lift']:.2f}・領先中位 {b['median_lead_days']} 日"
                f"・{b['hits']}/{b['n_triggers']} 命中）"
            )
            verdict = f"{qualified.height} 個因子過門檻"
        summary.append(f"- **{name}**：事件 {n_ep} 個・{verdict}；最佳因子 {top}")

        # M-MH Phase 2 GATE：改判「早偵測力」——短窗中位領先 20d ≥ min_lead 且早閘子集 lift>基率
        if early_on:
            lead_pass = (
                lead_df.filter(pl.col("median_lead_days") >= early_min_lead)
                .sort("median_lead_days", descending=True)
                if not lead_df.is_empty()
                else pl.DataFrame()
            )
            early_lift = scan.filter(
                pl.col("signal").str.contains(r"\+early")
                & (pl.col("n_triggers") >= min_triggers)
                & (pl.col("lift") > 1.0)
            ).sort("lift", descending=True)
            passed = not lead_pass.is_empty() and not early_lift.is_empty()
            if lead_pass.is_empty():
                ld = f"無短窗中位領先 ≥{early_min_lead} 日"
            else:
                lt = lead_pass.row(0, named=True)
                ld = (
                    f"{lt['short_signal']} 領先 {lt['median_lead_days']} 日"
                    f"（vs {lt['long_signal']}）"
                )
            if early_lift.is_empty():
                lf = "無早閘因子 lift>1"
            else:
                ft = early_lift.row(0, named=True)
                lf = f"{ft['signal']} lift {ft['lift']:.2f}（{ft['hits']}/{ft['n_triggers']}）"
            summary.append(
                f"  - M-MH GATE（領先 ≥{early_min_lead} 日 ＋ 早閘 lift>1）："
                f"{'✅ 過閘' if passed else '❌ 未過閘'}；領先：{ld}；早閘：{lf}"
            )
        for r in scan.head(5).iter_rows(named=True):
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
            console.print(
                f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
            )

    # ★ L4 頂部/出貨退潮警示校準（M-MH 精修・對稱 L1；驗證 overheat_watch 啟發式是否真有頂部預測力）
    top_lp = labels_cfg.get("top", {})
    tc = cp.get("top_calib", {})
    oh = cp.get("overheat_watch", {})  # 掃描沿用生產 overheat_watch 門檻＝直接驗生產規則
    top_eps = detect_top_episodes(
        market,
        m_days=int(top_lp.get("m_days", 60)),
        tol_pct=float(top_lp.get("tol_pct", 8.0)),
        drop_pct=float(top_lp.get("drop_pct", 10.0)),
        n_days=int(top_lp.get("n_days", 10)),
        cooldown_days=int(top_lp.get("cooldown_days", 15)),
    )
    console.print(f"\n[bold]L4 頂部/出貨[/bold]：事件 {top_eps.height} 個")
    if top_eps.is_empty():
        summary.append("- **L4 頂部/出貨**：事件 0 個——前瞻跌幅門檻過嚴或資料不足，無法掃描。")
    else:
        top_scan = scan_top_signals(
            panel,
            top_eps,
            near_high_pct=float(oh.get("near_high_pct", 8.0)),
            decel_thresholds=tuple(tc.get("decel_thresholds", [0.0])),
            div_floor=float(oh.get("div_floor", 0.0)),
            vol_floor=float(oh.get("vol_contract_floor", 0.0)),
            sell_z_thresholds=tuple(tc.get("sell_z_thresholds", [1.0, 1.5])),
            sell_prefixes=tuple(tc.get("sell_prefixes", ["foreign_flow", "net_flow"])),
            lead_window=int(top_lp.get("n_days", 10)),
            occupy_days=int(top_lp.get("cooldown_days", 15)),
            z_min_periods=z_min_periods,
        )
        top_params = {
            "fwd_n_days": int(top_lp.get("n_days", 10)),
            "fwd_x_pct": float(top_lp.get("drop_pct", 10.0)),
            "tol_pct": float(top_lp.get("tol_pct", 8.0)),
            "cooldown_days": int(top_lp.get("cooldown_days", 15)),
            "lead_window": int(top_lp.get("n_days", 10)),
        }
        top_report = render_top_calibration_report(
            top_scan, top_eps, top_params, coverage, min_triggers
        )
        (out_dir / f"calibration_{tag}_top.md").write_text(top_report, encoding="utf-8")
        top_scan.write_csv(out_dir / f"calibration_{tag}_top.csv")
        oh_row = (
            top_scan.filter(
                pl.col("signal").str.starts_with("★overheat")
                & (pl.col("n_triggers") >= min_triggers)
                & pl.col("lift").is_not_null()
            )
            .sort("lift", descending=True)
            .head(1)
        )
        if oh_row.is_empty():
            summary.append(
                f"- **L4 頂部/出貨**：事件 {top_eps.height} 個・生產啟發式觸發不足或無 lift，"
                "標『資料累積後重校』。"
            )
        else:
            b = oh_row.row(0, named=True)
            summary.append(
                f"- **L4 頂部/出貨**：事件 {top_eps.height} 個・生產啟發式 ★overheat "
                f"lift {b['lift']:.2f}（{b['hits']}/{b['n_triggers']} 命中・"
                f"領先中位 {b['median_lead_days']} 日）；對照裁決見 calibration_{tag}_top.md。"
            )
        for r in top_scan.head(5).iter_rows(named=True):
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
            console.print(
                f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
            )

    # ★ B-P1 穩健度四件套（payoff/decay/holdout/流動性硬化；docs/15 T3）——錨定勝出因子、研究軌
    anchor_signals: list[str] = []
    if not anchor_scan.is_empty() and not anchor_eps.is_empty():
        anchor_signals = (
            anchor_scan.filter(
                (pl.col("n_triggers") >= min_triggers)
                & pl.col("lift").is_not_null()
                & (pl.col("lift") >= min_lift)
            )
            .sort("lift", descending=True)
            .head(rb_top_k)["signal"]
            .to_list()
        )
    if not anchor_signals:
        summary.append(
            f"- **穩健度剖析（docs/15 B-P1）**：錨定「{rb_anchor}」無因子過門檻"
            f"（lift≥{min_lift}・觸發≥{min_triggers}）或無事件，略過。"
        )
    else:
        sig_set = set(anchor_signals)
        gate = early_cfg if early_on else None
        payoff = payoff_decay_table(
            panel,
            rb_horizons,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            early_gate=gate,
        )
        holdout = holdout_table(
            panel,
            anchor_eps,
            split_frac=rb_holdout_frac,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
            early_gate=gate,
        )
        liquidity = liquidity_table(
            panel,
            anchor_eps,
            adv_window=rb_adv_window,
            adv_min_amount=rb_adv_min,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
            early_gate=gate,
        )
        rb_params = {
            "top_k": rb_top_k,
            "horizons": list(rb_horizons),
            "holdout_frac": rb_holdout_frac,
            "adv_window": rb_adv_window,
            "adv_min_amount": rb_adv_min,
        }
        rb_report = render_robustness_report(
            payoff, holdout, liquidity, rb_anchor, anchor_signals, rb_params, coverage
        )
        (out_dir / f"calibration_{tag}_robustness.md").write_text(rb_report, encoding="utf-8")
        if not payoff.is_empty():
            payoff.write_csv(out_dir / f"calibration_{tag}_robustness_payoff.csv")
        if not holdout.is_empty():
            holdout.write_csv(out_dir / f"calibration_{tag}_robustness_holdout.csv")
        if not liquidity.is_empty():
            liquidity.write_csv(out_dir / f"calibration_{tag}_robustness_liquidity.csv")
        console.print(
            f"\n[bold]穩健度剖析[/bold]（錨定 {rb_anchor}・{len(anchor_signals)} 因子）"
            f" → calibration_{tag}_robustness.md"
        )
        summary.append(
            f"- **穩健度（docs/15 B-P1）**：錨定「{rb_anchor}」前 {len(anchor_signals)} 名因子"
            f"・payoff/decay/holdout/流動性見 calibration_{tag}_robustness.md。"
        )

    # ★ B-P2 買方主導度單調性（docs/15 T1）——dom 分位 × 控制位階，錨定同 robustness label
    if anchor_eps.is_empty():
        summary.append(
            f"- **買方主導度單調性（docs/15 B-P2）**：錨定「{rb_anchor}」無事件，略過。"
        )
    else:
        dom_buckets = dom_monotonicity_table(
            panel,
            anchor_eps,
            n_buckets=mono_buckets,
            fwd_window=mono_fwd,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
        )
        dom_spear = dom_monotonicity_spearman(
            panel, fwd_window=mono_fwd, position_low_pct=position_low_pct, z_sig=mono_zsig
        )
        mono_params = {
            "n_buckets": mono_buckets,
            "fwd_window": mono_fwd,
            "dom_window": long_window,
            "position_low_pct": position_low_pct,
            "z_sig": mono_zsig,
        }
        mono_report = render_dom_monotonicity_report(
            dom_buckets, dom_spear, rb_anchor, mono_params, coverage
        )
        (out_dir / f"calibration_{tag}_monotonicity.md").write_text(mono_report, encoding="utf-8")
        if not dom_buckets.is_empty():
            dom_buckets.write_csv(out_dir / f"calibration_{tag}_monotonicity_buckets.csv")
        if not dom_spear.is_empty():
            dom_spear.write_csv(out_dir / f"calibration_{tag}_monotonicity_spearman.csv")
        sp = {r["stratum"]: r for r in dom_spear.iter_rows(named=True)}
        all_sig = bool(sp.get("全體", {}).get("significant", False))
        ctrl = bool(sp.get("貼低", {}).get("significant", False)) and bool(
            sp.get("非貼低", {}).get("significant", False)
        )
        verdict = (
            "①單調顯著＋②控制位階後仍單調 → 建議升級分級因子"
            if (all_sig and ctrl)
            else (
                "①單調顯著但②控制位階後消失（位階在做工）→ 維持 binary 旗標、記否證"
                if all_sig
                else "①單調不顯著 → 維持 binary 旗標、記否證"
            )
        )
        rho_all = sp.get("全體", {}).get("spearman_rho")
        rho_txt = f"{rho_all:+.3f}" if rho_all is not None else "—"
        console.print(
            f"\n[bold]買方主導度單調性[/bold]（錨定 {rb_anchor}・全體 ρ {rho_txt}）"
            f" → calibration_{tag}_monotonicity.md"
        )
        summary.append(
            f"- **買方主導度單調性（docs/15 B-P2）**：錨定「{rb_anchor}」・全體 ρ {rho_txt}；"
            f"{verdict}（詳 calibration_{tag}_monotonicity.md）。"
        )

    # ★ B-P3 個股×族群 2×2 交互（docs/15 T2）——資金進+貼低(S) × 個股在族群裡領先(G)，錨定同 label
    if anchor_eps.is_empty():
        summary.append(
            f"- **個股×族群交互（docs/15 B-P3）**：錨定「{rb_anchor}」無事件，略過。"
        )
    else:
        inter_tab = interaction_2x2_table(
            panel,
            anchor_eps,
            s_flow_col=inter_s_col,
            s_z_threshold=inter_s_z,
            s_low_pct=position_low_pct,
            g_threshold=inter_g_thr,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
        )
        inter_params = {
            "s_flow_col": inter_s_col,
            "s_z_threshold": inter_s_z,
            "s_low_pct": position_low_pct,
            "g_threshold": inter_g_thr,
        }
        inter_report = render_interaction_report(
            inter_tab, rb_anchor, inter_params, coverage, min_triggers, inter_zsig
        )
        (out_dir / f"calibration_{tag}_interaction.md").write_text(inter_report, encoding="utf-8")
        if not inter_tab.is_empty():
            inter_tab.write_csv(out_dir / f"calibration_{tag}_interaction.csv")
        if inter_tab.is_empty():
            summary.append(
                "- **個股×族群交互（docs/15 B-P3）**：缺 S 欄/above_low/rs_subind，無法分格。"
            )
        else:
            bc = {r["cell"]: r for r in inter_tab.iter_rows(named=True)}
            ssg, spg = bc.get("S+G+"), bc.get("S+G−")
            if ssg and spg:
                gp, gn = ssg["lift"], spg["lift"]
                gp_s = f"{gp:.2f}" if gp is not None else "—"
                gn_s = f"{gn:.2f}" if gn is not None else "—"
                if gp is not None and gn is not None and gp < gn:
                    dir_txt = "G高反降→否證交互、個股訊號自足"
                elif gp is not None and gn is not None and gp > gn:
                    dir_txt = "G高提升→可能族群確認加分"
                else:
                    dir_txt = "方向不明"
                pp_txt = f"S+ 內 G高 lift {gp_s} vs G低 {gn_s}（{dir_txt}）"
            else:
                pp_txt = "某格 lift 不可算"
            console.print(
                f"\n[bold]個股×族群 2×2 交互[/bold]（錨定 {rb_anchor}）"
                f" → calibration_{tag}_interaction.md"
            )
            summary.append(
                f"- **個股×族群交互（docs/15 B-P3）**：錨定「{rb_anchor}」・{pp_txt}；"
                f"裁決詳 calibration_{tag}_interaction.md。"
            )

    # ★ M-Part C / C-P1 個股族群內落後度單調×位階控制（docs/16 H1+H2）——rs_subind 低桶=落後、ρ<0
    if anchor_eps.is_empty():
        summary.append(f"- **族群內落後度（docs/16 C-P1）**：錨定「{rb_anchor}」無事件，略過。")
    else:
        rs_col = _rs_subind_col(panel)
        lag_buckets = factor_monotonicity_table(
            panel,
            anchor_eps,
            rs_col,
            n_buckets=mono_buckets,
            fwd_window=mono_fwd,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
        )
        lag_spear = factor_monotonicity_spearman(
            panel,
            rs_col,
            fwd_window=mono_fwd,
            position_low_pct=position_low_pct,
            z_sig=mono_zsig,
            direction="decreasing",
        )
        lag_params = {
            "rs_window": rs_col.split("_")[-1].rstrip("d") if rs_col else "?",
            "n_buckets": mono_buckets,
            "fwd_window": mono_fwd,
            "position_low_pct": position_low_pct,
        }
        lag_report = render_laggard_monotonicity_report(
            lag_buckets, lag_spear, rb_anchor, lag_params, coverage, z_sig=mono_zsig
        )
        (out_dir / f"calibration_{tag}_laggard.md").write_text(lag_report, encoding="utf-8")
        if not lag_buckets.is_empty():
            lag_buckets.write_csv(out_dir / f"calibration_{tag}_laggard_buckets.csv")
        if not lag_spear.is_empty():
            lag_spear.write_csv(out_dir / f"calibration_{tag}_laggard_spearman.csv")
        # 裁決以起漲 lift 為 on-target 量尺（非前瞻報酬 Spearman——兩者分流）
        lift_sig = _laggard_lift_significance(lag_buckets, mono_zsig)
        a = lift_sig.get("全體", {})
        h1 = bool(a.get("sig") and a.get("monotone_dec"))
        h2 = bool(
            lift_sig.get("貼低", {}).get("sig")
            and lift_sig.get("貼低", {}).get("monotone_dec")
            and lift_sig.get("非貼低", {}).get("sig")
            and lift_sig.get("非貼低", {}).get("monotone_dec")
        )
        lo_l, hi_l, z_l = a.get("lift_lo"), a.get("lift_hi"), a.get("z")
        lh_txt = (
            f"最落後桶 lift {lo_l:.2f} vs 最領先 {hi_l:.2f}"
            if lo_l is not None and hi_l is not None
            else "—"
        )
        z_txt = f"{z_l:+.1f}" if z_l is not None else "—"
        verdict = (
            "①落後起漲 lift 顯著＋②控制位階後仍在 → 進 C-P2"
            if (h1 and h2)
            else (
                "①顯著但②控制位階某層崩 → 否證(位階代理)"
                if h1
                else "①落後 lift 不顯著 → 否證"
            )
        )
        console.print(
            f"\n[bold]族群內落後度單調[/bold]（錨定 {rb_anchor}・{lh_txt}・z {z_txt}）"
            f" → calibration_{tag}_laggard.md"
        )
        summary.append(
            f"- **族群內落後度（docs/16 C-P1）**：錨定「{rb_anchor}」・{lh_txt}（z {z_txt}）；"
            f"{verdict}（詳 calibration_{tag}_laggard.md）。"
        )

    # ★ M-Part C / C-P2 冠軍 S+ 內落後濾鏡 precision 增量＋賺賠驗證（docs/16 H3）
    if anchor_eps.is_empty():
        summary.append(f"- **落後濾鏡（docs/16 C-P2）**：錨定「{rb_anchor}」無事件，略過。")
    else:
        rs_col_c2 = _rs_subind_col(panel)
        prec_tbl, prec_z = laggard_filter_precision(
            panel,
            anchor_eps,
            s_flow_col=inter_s_col,
            s_z_threshold=inter_s_z,
            s_low_pct=position_low_pct,
            lag_threshold=inter_g_thr,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
        )
        champ_name = f"{inter_s_col} (z>{inter_s_z:g}) +low≤{position_low_pct:g}"
        gate2 = early_cfg if early_on else None
        payoff_base = payoff_decay_table(
            panel,
            rb_horizons,
            signals={champ_name},
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            early_gate=gate2,
        )
        payoff_filt = (
            payoff_decay_table(
                panel,
                rb_horizons,
                signals={champ_name},
                z_thresholds=z_thr,
                volume_thresholds=vol_thr,
                position_low_pct=position_low_pct,
                early_gate=gate2,
                extra_conditions=[pl.col(rs_col_c2) < inter_g_thr],
                name_suffix="＋落後",
            )
            if rs_col_c2 is not None
            else pl.DataFrame()
        )
        c2_params = {
            "s_flow_col": inter_s_col,
            "s_z_threshold": inter_s_z,
            "lag_threshold": inter_g_thr,
            "horizons": list(rb_horizons),
        }
        c2_report = render_laggard_filter_report(
            prec_tbl, prec_z, payoff_base, payoff_filt, rb_anchor, c2_params, coverage, mono_zsig
        )
        (out_dir / f"calibration_{tag}_laggard_filter.md").write_text(c2_report, encoding="utf-8")
        if not prec_tbl.is_empty():
            prec_tbl.write_csv(out_dir / f"calibration_{tag}_laggard_filter_precision.csv")
        if prec_tbl.is_empty():
            summary.append(
                "- **落後濾鏡（docs/16 C-P2）**：缺 S 欄/above_low/rs_subind，略過。"
            )
        else:
            pcg = {r["group"]: r for r in prec_tbl.iter_rows(named=True)}
            la_l = pcg.get("S+且落後", {}).get("lift")
            al_l = pcg.get("S+全體", {}).get("lift")
            zt2 = f"{prec_z:+.1f}" if prec_z is not None else "—"
            txt = (
                f"S+且落後 lift {la_l:.2f} vs S+全體 {al_l:.2f}（落後vs領先 z {zt2}）"
                if la_l is not None and al_l is not None
                else "lift 不可算"
            )
            console.print(
                f"\n[bold]落後濾鏡 C-P2[/bold]（錨定 {rb_anchor}・{txt}）"
                f" → calibration_{tag}_laggard_filter.md"
            )
            summary.append(
                f"- **落後濾鏡（docs/16 C-P2）**：錨定「{rb_anchor}」・{txt}；"
                f"裁決詳 calibration_{tag}_laggard_filter.md。"
            )

    # L3 裁決（docs/13 §3：lift≥門檻＋領先 >0＋需確認非單日 spike，否則不上線）
    summary += [
        "",
        "## L3 超跌反轉裁決（docs/13 §3 三關）",
        "",
        "上表已套（1）lift ≥ 門檻、（2）中位領先 > 0 日兩關；",
        "（3）「需確認非單日 spike」屬訊號設計層，本掃描未強制——若 L3 過前兩關，",
        "上線前仍須在 B3 加「連 2 日續買 / breadth 同步轉正」確認，不接受單日資金 flip。",
        "未過門檻則依 §3 剔除、不放寬標準。",
        "",
    ]
    (out_dir / f"calibration_{tag}_summary.md").write_text("\n".join(summary), encoding="utf-8")
    console.print(f"\n[green]報告 → {out_dir}/calibration_{tag}_*.md（含 _summary）[/green]")

