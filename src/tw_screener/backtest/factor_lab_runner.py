"""factor_lab 編排（WS-B；自 cli.py 薄殼呼叫）。

三段驗收（缺一不宣稱實驗台可用）：
1. **機器等價**：候選宇宙同一份 joined 表，factor_lab pooled IC 必須＝
   diagnostic.signal_ic_table 的 IC（同 _spearman，容差 1e-9）——證明實驗台沒改語義。
2. **docs/19 基準對表**：限定 docs/19 樣本週（W21–W25），ma60_dist r+20 ≈ −0.25、
   vol_ratio ≈ +0.14；樣本成熟度已增（當時 r+20 只到 W22），差異如實回報不硬湊。
3. **面板全宇宙首驗**：ma60_dist_pct／vol_ratio 上全面板（alpha20 target、
   walk-forward、殘差 IC）＝實驗台第一份正式輸出。

產 research/factor_lab/。CLI 只保留參數解析。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import typer
from rich.console import Console

if TYPE_CHECKING:
    from tw_screener.backtest.factor_lab import FactorReport

console = Console()

_DOCS19_WEEKS = ("2026-W21", "2026-W22", "2026-W23", "2026-W24", "2026-W25")
_DOCS19_BENCH = {"ma60_dist_pct": -0.25, "vol_ratio": 0.14}  # r+20、target=excess


def _fmt_ic(v: float | None) -> str:
    return f"{v:+.3f}" if v is not None else "—"


def _fmt_ci(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "—"
    return f"[{lo:+.3f}, {hi:+.3f}]"


def _candidate_joined(cfg: dict, horizons: tuple[int, ...]) -> pl.DataFrame:
    """重建 M-Diag1 的候選宇宙 joined 表（features × 前瞻報酬長表）。"""
    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.backtest import diagnostic as diag
    from tw_screener.data.twse import load_recent_dividends
    from tw_screener.report.pick_store import load_all_picks

    dg = cfg.get("backtest", {}).get("diagnostic", {})
    reports_dir = Path(cfg["paths"]["reports_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    picks = load_all_picks(reports_dir)
    if picks.is_empty():
        return pl.DataFrame()
    week_to_date: dict[str, object] = {}
    for w, d in zip(picks["week"].to_list(), picks["data_date"].to_list()):
        week_to_date.setdefault(w, d)
    enriched: dict[str, pl.DataFrame] = {}
    for w in sorted(week_to_date):
        p = reports_dir / w / "candidates_enriched.csv"
        if p.exists():
            try:
                enriched[w] = pl.read_csv(
                    p, schema_overrides={"stock_id": pl.Utf8}, infer_schema_length=2000
                )
            except Exception as e:  # noqa: BLE001 — 單週 enriched 壞掉不擋
                console.print(f"[yellow]讀 {p} 失敗（{e}），跳過該週[/yellow]")
    if not enriched:
        return pl.DataFrame()
    market = load_market_history(cache_dir, n_days=int(dg.get("history_days", 250)))
    if market.is_empty():
        return pl.DataFrame()
    since = picks["data_date"].min()
    dividends = (
        load_recent_dividends(cache_dir, since) if isinstance(since, date) else pl.DataFrame()
    )
    screens, feats = diag.build_candidate_screens(enriched, week_to_date)
    returns = diag.forward_returns_long(
        screens, market, dividends, horizons_td=horizons,
        trading_days_per_week=int(dg.get("trading_days_per_week", 5)),
        clip_daily_return_pct=float(dg.get("clip_daily_return_pct", 10.0)),
    )
    if returns.is_empty():
        return pl.DataFrame()
    joined = feats.join(
        returns.select(
            "week_tag", "stock_id", "horizon_td",
            "return_pct", "market_return_pct", "excess_return_pct",
        ),
        on=["week_tag", "stock_id"], how="inner",
    )
    # factor_lab 需要 date 欄（用該週 data_date 當橫斷面日）
    wk = pl.DataFrame(
        {"week_tag": list(week_to_date), "date": list(week_to_date.values())}
    ).with_columns(pl.col("date").cast(pl.Date))
    return joined.join(wk, on="week_tag", how="left")


def run_factor_lab(settings: Path, out_dir: Path | None) -> None:
    """WS-B 驗收：機器等價＋docs/19 對表＋面板首驗 → research/factor_lab/。"""
    import yaml

    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest.diagnostic import signal_ic_table

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    fl = cfg.get("backtest", {}).get("factor_lab", {})
    n_splits = int(fl.get("n_splits", 4))
    min_train_frac = float(fl.get("min_train_frac", 0.4))
    buckets = int(fl.get("buckets", 5))
    inference = str(fl.get("inference", "block_bootstrap"))
    n_boot = int(fl.get("n_boot", 1000))
    seed = int(fl.get("seed", 42))
    panel_path = Path(fl.get("panel_path", "research/panel/panel.parquet"))
    out = out_dir or Path(fl.get("output_dir", "research/factor_lab"))
    horizons = tuple(
        int(h) for h in cfg.get("backtest", {}).get("diagnostic", {}).get(
            "horizons_td", [5, 10, 20]
        )
    )

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# factor_lab 驗收（WS-B）",
        "",
        f"- 產出日：{date.today()}；n_splits={n_splits}、min_train_frac={min_train_frac}、"
        f"buckets={buckets}、inference={inference}（n_boot={n_boot}・seed={seed}，WS-I）。",
        "",
    ]

    # ── 1. 機器等價：同 joined 表，factor_lab pooled IC == diagnostic IC ────────
    lines += ["## 1. 機器等價（factor_lab vs diagnostic.signal_ic_table）", ""]
    joined = _candidate_joined(cfg, horizons)
    equiv_ok = None
    if joined.is_empty():
        lines.append("> 候選宇宙 joined 表無資料——等價驗證未執行（如實標註）。")
    else:
        ref = signal_ic_table(joined, target_col="excess_return_pct")
        rows = ["| feature | r+td | diagnostic IC | factor_lab IC | Δ |", "|---|---|---|---|---|"]
        equiv_ok = True
        for r in ref.iter_rows(named=True):
            sub = joined.filter(pl.col("horizon_td") == r["horizon_td"])
            rep = lab.evaluate(
                sub, r["feature"], horizon=int(r["horizon_td"]),
                target="excess_return_pct", n_splits=0,
                inference=inference, n_boot=n_boot, seed=seed,
            )
            mine = rep.pooled.row(0, named=True)["ic"]
            delta = abs((mine or 0.0) - r["ic"]) if mine is not None else None
            if delta is None or delta > 1e-9:
                equiv_ok = False
            rows.append(
                f"| {r['feature']} | {r['horizon_td']} | {r['ic']:+.6f} "
                f"| {mine:+.6f} | {delta:.2e} |"
                if mine is not None
                else f"| {r['feature']} | {r['horizon_td']} | {r['ic']:+.6f} | — | — |"
            )
        lines += rows
        lines += ["", f"**等價判定：{'PASS（全數 Δ<1e-9）' if equiv_ok else 'FAIL'}**", ""]
        console.print(f"  機器等價：{'PASS' if equiv_ok else 'FAIL'}")

    # ── 2. docs/19 基準對表（限 W21–W25 樣本週；樣本成熟度差異如實回報）─────────
    lines += ["## 2. docs/19 基準對表（樣本限 W21–W25、r+20、target=excess）", ""]
    bench_ok = None
    if not joined.is_empty():
        sub19 = joined.filter(
            pl.col("week_tag").is_in(list(_DOCS19_WEEKS)) & (pl.col("horizon_td") == 20)
        )
        if sub19.is_empty():
            lines.append("> W21–W25 r+20 無到期樣本——對表未執行。")
        else:
            bench_ok = True
            lines += [
                "| feature | docs/19 | 本次 | Fisher CI95 | bs_CI95(T)（WS-I） | n | 判定 |",
                "|---|---|---|---|---|---|---|",
            ]
            for feat, bench in _DOCS19_BENCH.items():
                rep = lab.evaluate(
                    sub19, feat, horizon=20, target="excess_return_pct", n_splits=0,
                    inference=inference, n_boot=n_boot, seed=seed,
                )
                p = rep.pooled.row(0, named=True)
                if p["ic"] is None:
                    lines.append(f"| {feat} | {bench:+.2f} | — | — | — | — | 無法計算 |")
                    bench_ok = False
                    continue
                ok = abs(p["ic"] - bench) <= 0.10 and (p["ic"] * bench > 0)
                bench_ok = bench_ok and ok
                ci = (
                    f"[{p['ci_lo']:+.2f}, {p['ci_hi']:+.2f}]"
                    if p["ci_lo"] is not None else "—"
                )
                bs_lo, bs_hi = rep.bs_ci
                t_obs = rep.inference_meta.get("T", 0) if rep.inference_meta else 0
                bs_col = (
                    f"[{bs_lo:+.2f}, {bs_hi:+.2f}](T={t_obs})"
                    if bs_lo is not None else f"—（T={t_obs}<10，無法重抽）"
                )
                lines.append(
                    f"| {feat} | {bench:+.2f} | {p['ic']:+.3f} | {ci} | {bs_col} | {p['n']} "
                    f"| {'✓ 同號±0.10' if ok else '✗ 超出'} |"
                )
            lines += [
                "",
                "> 註：docs/19 撰寫時 r+20 僅 W21–W22 到期；本次樣本已多出後續到期週，"
                "數值非逐位複現、同號且量級一致即判複現。判定欄仍依 Fisher CI（既有邏輯不動）；"
                "bs_CI95 為 WS-I 新增對照欄，候選宇宙週頻樣本通常 T<10"
                "（如實標「無法重抽」，不臆造）。",
                "",
            ]
            console.print(f"  docs/19 對表：{'PASS' if bench_ok else 'FAIL'}")

    # ── 3. 面板全宇宙首驗（走 walk-forward＋殘差 IC）────────────────────────────
    lines += ["## 3. 面板全宇宙首驗（target=alpha20）", ""]
    panel: pl.DataFrame | None = None
    panel_reps: dict[str, FactorReport] = {}  # feat → 全樣本 rep（第 4 段基準沿用，不重算）
    if not panel_path.exists():
        lines.append(f"> 無面板 `{panel_path}`——先跑 make build-panel。")
        console.print(f"[yellow]無面板 {panel_path}，跳過第 3 段[/yellow]")
    else:
        panel = pl.read_parquet(panel_path)
        d0, d1 = str(panel["date"].min()), str(panel["date"].max())
        lines.append(
            f"- 樣本期間 {d0} ~ {d1}（{panel['date'].n_unique()} 交易日、"
            f"{panel['stock_id'].n_unique()} 檔）；單一多頭偏 regime，跨窗不穩者降級。"
        )
        lines.append("")
        for feat, ctrls in (
            ("ma60_dist_pct", ()),
            ("vol_ratio", ("ma60_dist_pct",)),
        ):
            rep = lab.evaluate(
                panel, feat, horizon=20, controls=ctrls,
                buckets=buckets, n_splits=n_splits, min_train_frac=min_train_frac,
                inference=inference, n_boot=n_boot, seed=seed,
            )
            panel_reps[feat] = rep
            lines += lab.render_report_md(rep)
            if not rep.splits.is_empty():
                rep.splits.write_csv(out / f"panel_{feat}_splits_{tag}.csv")

    # ── 4. regime 切片（WS-H.4a；docs/23 §1c 晉升鐵則 (c)）─────────────────────
    # 升降級規則（docs/23 §1c，寫死不得因手感調鬆）：
    #   - ≥2 個可判 regime 切片與全樣本 per-date IC mean 同向 → 「跨 regime 穩健」；
    #   - 僅 1 個同向且該切片＝進攻 → 「bull-only」（分不清因子有效 vs 多頭裡強者恆強，
    #     只能列候補、不得晉升）；
    #   - 其餘 → 「regime-dependent」。切片 n_dates<30 → 照列標「樣本不足」，不進裁決分母
    #     （regime_alignment_verdict 的 thin 排除）。
    lines += ["## 4. regime 切片（r+20、target=alpha20；docs/23 §1c）", ""]
    if panel is None:
        lines.append(f"> 無面板 `{panel_path}`——本段隨第 3 段一併跳過。")
    elif "regime" not in panel.columns or panel["regime"].drop_nulls().is_empty():
        lines.append(
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。"
        )
        console.print("[yellow]regime 標籤未產，第 4 段跳過[/yellow]")
    else:
        for feat in ("ma60_dist_pct", "vol_ratio"):
            full_rep = panel_reps[feat]
            full_ic = full_rep.mean_daily_ic
            if full_ic is None:
                lines += [
                    f"### `{feat}`", "", "> 全樣本 per-date IC 不可得——切片無基準，跳過。", "",
                ]
                continue
            full_sign = 1 if full_ic >= 0 else -1
            slices = lab.regime_ic_slices(
                panel, feat, target="alpha20", horizon=20,
                inference=inference, n_boot=n_boot, seed=seed,
            )
            verdict, same, present = lab.regime_alignment_verdict(slices, full_sign)
            lines += [
                f"### `{feat}`（全樣本 mean_IC {full_ic:+.3f}）",
                "",
                "| regime | n_dates | mean_IC | bs_CI95 | 與全樣本同向？ |",
                "|---|---|---|---|---|",
            ]
            for r in slices.iter_rows(named=True):
                if r["mean_ic"] is None:
                    align = "—"
                elif r["thin"]:
                    align = "樣本不足（不裁決）"
                else:
                    align = "同向" if (r["mean_ic"] > 0) == (full_sign > 0) else "反向"
                lines.append(
                    f"| {r['regime']} | {r['n_dates']} | {_fmt_ic(r['mean_ic'])} "
                    f"| {_fmt_ci(r['ci_lo'], r['ci_hi'])} | {align} |"
                )
            dist = "、".join(
                f"{r['regime']} n_dates={r['n_dates']}" for r in slices.iter_rows(named=True)
            )
            lines += [
                "",
                f"跨 regime 同向數：{len(same)}/{present}"
                f"（同向切片：{'、'.join(same) if same else '無'}）→ 升降級：**{verdict}**",
                "",
                *lab.inference_footer(
                    sample_span=(full_rep.inference_meta or {}).get("span") or "—",
                    regime_dist=dist,
                    method_desc=(
                        f"moving-block bootstrap（block=21td・B={n_boot}・seed={seed}）"
                        "為 CI 預設；Fisher-z 僅供對照連續性（重疊窗下偏窄，docs/22 §7.2）"
                    ),
                    membership_desc="今日 concepts.yaml（非 point-in-time）",
                ),
                "",
            ]
            console.print(f"  regime 切片 {feat}：{len(same)}/{present} 同向・{verdict}")

    md = out / f"factor_lab_{tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]factor_lab 驗收報告 → {md}[/green]")
    if equiv_ok is False or bench_ok is False:
        raise typer.Exit(1)
