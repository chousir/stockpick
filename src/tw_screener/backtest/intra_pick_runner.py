"""M-Pick2 族群內個股因子錦標賽編排（docs/32；自 cli.py 薄殼呼叫）。

日線快取（2021-06 起）→ 交易日曆／價格因子／趨勢分重建 → EPS 與月營收 as-of → 面板週快照
→ 重建 shortlist 可入選池 → intra_pick 純函式評估 → research/intra_pick/。
裁決規則全在 intra_pick.classify（docs/32 §4 寫死）；本檔只做 IO 與報表排版。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import typer
from rich.console import Console

from tw_screener.backtest import intra_pick as ip

console = Console()

_FACTOR_LABELS = {
    "mom_6_1": "F1 個股 RS（6-1 月動能）",
    "high52_near": "F2 52 週高點接近度",
    "eps_accel": "F3 EPS 加速",
    "rev_accel": "F4 月營收 YoY 加速",
    "neg_band_dist": "現行鍵：−偏好帶距離",
    "log_amount": "現行鍵：log 成交額",
}


def _fmt(v: object, nd: int = 3, suffix: str = "") -> str:
    return f"{float(v):+.{nd}f}{suffix}" if isinstance(v, (int, float)) else "—"


def _ci(ci: tuple[float | None, float | None], nd: int = 3) -> str:
    lo, hi = ci
    return f"[{_fmt(lo, nd)}, {_fmt(hi, nd)}]" if lo is not None and hi is not None else "—"


def build_stock_weeks(
    cfg_all: dict[str, Any], settings: Path, cfg: ip.IntraPickConfig
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """IO：組出 (date, stock_id) 週快照寬表（因子＋gate 欄＋target＋regime）與 meta。"""
    from tw_screener.analysis.concepts import load_themes
    from tw_screener.analysis.rotation import compute_subindustry_baskets, load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest import rotation_efficacy as eff
    from tw_screener.data.finmind import load_financials_history, load_month_revenue_history
    from tw_screener.report.shortlist import order_members_by_concepts

    cache_root = Path(cfg_all["paths"]["cache_dir"])
    panel_path = Path(
        (cfg_all.get("backtest") or {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]載入日線快取（近 {cfg.history_days} 交易日、面板同來源優先序）...[/bold]")
    price = load_market_history(
        cache_root / "twse",
        n_days=cfg.history_days,
        patterns=("stock_day_*.parquet", "daily_*.parquet", "otc_daily_*.parquet"),
    )
    calendar = ip.trading_calendar(price, cfg.calendar_min_names)
    if not calendar:
        console.print("[red]日線快取無可用交易日[/red]")
        raise typer.Exit(1)
    price = price.filter(pl.col("date").is_in(calendar))
    feats = ip.price_features(price, calendar, cfg)

    concepts_path = settings.parent / "concepts.yaml"
    members = list_subindustries(concepts_path=concepts_path)
    if members.is_empty():
        console.print("[red]缺 concepts.yaml 次產業成員[/red]")
        raise typer.Exit(1)
    primary = ip.primary_sector(order_members_by_concepts(members, load_themes(concepts_path)))

    console.print("[bold]重建次產業籃子與趨勢分（逐日）...[/bold]")
    px = price.select("date", "stock_id", "close", "volume")
    baskets = compute_subindustry_baskets(members, px)
    trend = eff.trend_score_series(px, members, baskets)

    panel = pl.read_parquet(
        panel_path, columns=["date", "stock_id", "ma60_dist_pct", "r10", "r20", "r40", "regime"]
    )
    cal_set = set(calendar)
    weeks = eff.weekly_snapshot_dates([d for d in panel["date"].unique().to_list() if d in cal_set])
    buckets = ip.sector_bucket_table(
        trend, baskets, weeks, cfg.min_sector_members, cfg.shortlist.trend_buckets
    )

    sw = (
        panel.filter(pl.col("date").is_in(weeks))
        .join(feats.drop("close"), on=["date", "stock_id"], how="left")
        .join(primary, on="stock_id", how="inner")
        .join(
            buckets.select("date", "sub_industry", "trend_score", "trend_bucket"),
            on=["date", "sub_industry"],
            how="left",
        )
    )
    keys = sw.select("date", "stock_id")
    finmind_dir = cache_root / "finmind"
    fin = load_financials_history(finmind_dir)
    rev = load_month_revenue_history(finmind_dir)
    eps = ip.eps_asof(keys, ip.eps_accel_table(fin, ip.quarter_end_close(feats)), cfg)
    revf = ip.revenue_asof(keys, ip.revenue_accel_table(rev), cfg.revenue_available_day)
    sw = ip.mark_universe(
        sw.join(eps, on=["date", "stock_id"], how="left").join(
            revf, on=["date", "stock_id"], how="left"
        ),
        cfg,
    ).with_columns(
        (pl.col("in_main") & ~pl.col("date").dt.month().is_in([7, 8, 9])).alias("in_main_exdiv")
    )
    meta = {
        "calendar": (calendar[0], calendar[-1], len(calendar)),
        "panel_path": str(panel_path),
        "n_fin_stocks": fin["stock_id"].n_unique() if not fin.is_empty() else 0,
        "n_rev_stocks": rev["stock_id"].n_unique() if not rev.is_empty() else 0,
        "n_members": primary.height,
    }
    return sw, meta


def _universe_stats(sw: pl.DataFrame, col: str, target: str, min_group: int) -> dict[str, Any]:
    """宇宙規模（不依因子）：週數、每週中位檔數／組數、regime 週數。"""
    u = (
        sw.filter(pl.col(col) & pl.col(target).is_not_null())
        .with_columns(pl.len().over("date", "sub_industry").alias("_ng"))
        .filter(pl.col("_ng") >= min_group)
    )
    wk = u.group_by("date").agg(
        pl.len().alias("names"), pl.col("sub_industry").n_unique().alias("groups")
    )
    reg = (
        u.select("date", "regime").unique().group_by("regime").len().sort("regime").to_dicts()
        if not u.is_empty()
        else []
    )
    return {
        "weeks": wk.height,
        "first": u["date"].min() if not u.is_empty() else None,
        "last": u["date"].max() if not u.is_empty() else None,
        "median_names": wk["names"].median() if not wk.is_empty() else None,
        "median_groups": wk["groups"].median() if not wk.is_empty() else None,
        "regimes": {str(r["regime"]): int(r["len"]) for r in reg},
    }


def _verdict_rows(results: Sequence[ip.FactorResult]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        rows.append(
            {
                "factor": r.factor, "horizon": r.horizon, "coverage": r.coverage,
                "n_weeks": r.ic.n, "ic_mean": r.ic.mean, "ci_lo": r.ic.ci[0],
                "ci_hi": r.ic.ci[1], "bonf_lo": r.ic.ci_bonf[0], "bonf_hi": r.ic.ci_bonf[1],
                "seg_pos": sum(1 for s in r.segments if s is not None and s > 0),
                "regime_label": r.regime_label, "m2_mean": r.m2.mean, "m2_lo": r.m2.ci[0],
                "m2_hi": r.m2.ci[1], "m3_mean": r.m3.mean, "m3_lo": r.m3.ci[0],
                "m3_hi": r.m3.ci[1], "same_pick_rate": r.same_pick_rate,
                "verdict": r.verdict, "failed": ",".join(r.failed),
            }
        )
    return rows


def _weekly_rows(
    sw: pl.DataFrame, results: Sequence[ip.FactorResult], cfg: ip.IntraPickConfig
) -> pl.DataFrame:
    """主結果逐週序列（ic＋M2/M3＋regime）供稽核。"""
    frames = []
    regimes = sw.select("date", "regime").unique(subset=["date"])
    uni = sw.filter(pl.col("in_main"))
    for r in results:
        target = f"r{r.horizon}"
        wp = ip.weekly_pick(ip.pick_table(uni, r.factor, target, cfg.min_group))
        frames.append(
            r.ic_weekly.join(wp.drop("n_groups"), on="date", how="full", coalesce=True)
            .join(regimes, on="date", how="left")
            .with_columns(pl.lit(r.factor).alias("factor"))
        )
    return pl.concat(frames, how="diagonal_relaxed").sort("factor", "date")


def render_report(
    sw: pl.DataFrame,
    meta: dict[str, Any],
    cfg: ip.IntraPickConfig,
    main: Sequence[ip.FactorResult],
    disclosures: dict[str, list[ip.FactorResult]],
    corr: pl.DataFrame,
) -> list[str]:
    """報表 markdown（裁決總表→逐因子明細→揭露 R1–R6→footer）。"""
    from tw_screener.backtest.factor_lab import inference_footer

    target = f"r{cfg.horizon}"
    sl = cfg.shortlist
    us = _universe_stats(sw, "in_main", target, cfg.min_group)
    ua = _universe_stats(sw, "in_all", target, cfg.min_group)
    reg_txt = "／".join(f"{k} {v}" for k, v in us["regimes"].items())
    cal0, cal1, ncal = meta["calendar"]
    lines = [
        "# M-Pick2 族群內個股因子錦標賽（docs/32 預註冊裁決）",
        "",
        f"- 產出日：{date.today()}；主宇宙快照 {us['first']}～{us['last']} 共 {us['weeks']} 週"
        f"（r+{cfg.horizon} 到期者）；每週中位 {us['median_names']} 檔／{us['median_groups']} 組；"
        f"regime 週數 {reg_txt}。",
        f"- 主宇宙＝趨勢分前 {sl.max_bucket_for_top} 桶（共 {sl.trend_buckets} 桶）"
        f"× gate（{sl.ext_min_pct:.0f} ≤ 距季線 ≤ {sl.ext_max_pct:.0f}%、"
        f"成交額 ≥ {sl.low_liquidity_amount:.0f} 百萬、近 {cfg.gate_disc_lookback} 日無價格"
        f"不連續）× 同組 ≥ {cfg.min_group} 檔；全次產業宇宙（R1）每週中位 "
        f"{ua['median_names']} 檔／{ua['median_groups']} 組。",
        f"- 日線交易日曆 {cal0}～{cal1}（{ncal} 日）；次產業成員 {meta['n_members']} 檔；"
        f"FinMind 財報 {meta['n_fin_stocks']} 檔、月營收 {meta['n_rev_stocks']} 檔。",
        f"- 裁決規則：docs/32 §4（C1 效應量 ≥ {cfg.min_effect_ic}、C2 CI95 下界 > 0、"
        f"C3 {cfg.n_segments} 段 ≥ {cfg.min_same_segments} 段 > 0、C4 跨 regime 穩健、"
        f"C5 M2 與 M3 點估計 > 0；全過且 Bonferroni（k={cfg.n_tests}）CI 下界 > 0＝成立）。"
        "方向預註冊皆＋。",
        "",
        f"## 1. 裁決總表（主宇宙、r+{cfg.horizon}）",
        "",
        "| 因子 | 覆蓋 | 週數 | 週 IC 平均 | CI95 | Bonferroni CI | 分段>0 | regime | "
        "M2 首選−組均 | M3 首選−現行 | 同選率 | 裁決 | 未過 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in main:
        seg_pos = sum(1 for s in r.segments if s is not None and s > 0)
        cov = f"{r.coverage:.0%}" if r.coverage is not None else "—"
        same = f"{r.same_pick_rate:.0%}" if r.same_pick_rate is not None else "—"
        lines.append(
            f"| {_FACTOR_LABELS.get(r.factor, r.factor)} | {cov} | {r.ic.n} | "
            f"**{_fmt(r.ic.mean)}** | {_ci(r.ic.ci)} | {_ci(r.ic.ci_bonf)} | "
            f"{seg_pos}/{len(r.segments)} | "
            f"{r.regime_label} | {_fmt(r.m2.mean, 2, '%')} {_ci(r.m2.ci, 2)} | "
            f"{_fmt(r.m3.mean, 2, '%')} {_ci(r.m3.ci, 2)} | {same} | **{r.verdict}** | "
            f"{','.join(r.failed) or '—'} |"
        )
    lines += ["", "## 2. 逐因子明細", ""]
    for r in main:
        lines += [
            f"### {_FACTOR_LABELS.get(r.factor, r.factor)}（`{r.factor}`）",
            "",
            "- 分段（時間序 5 等分）週 IC 平均：" + "、".join(_fmt(s) for s in r.segments) + "。",
            f"- M2 首選−組均 {_fmt(r.m2.mean, 2, '%')} CI95 {_ci(r.m2.ci, 2)}（{r.m2.n} 週）；"
            f"M3 首選−現行 {_fmt(r.m3.mean, 2, '%')} CI95 {_ci(r.m3.ci, 2)}（{r.m3.n} 週）。",
            "",
            "| regime | 週數 | 週 IC 平均 | bs_CI95 | 樣本 |",
            "|---|---|---|---|---|",
        ]
        if r.regime_slices.is_empty():
            lines.append("| — | 0 | — | — | regime 標籤缺 |")
        for s in r.regime_slices.iter_rows(named=True):
            lines.append(
                f"| {s['regime']} | {s['n_dates']} | {_fmt(s['mean'])} | "
                f"{_ci((s['ci_lo'], s['ci_hi']))} | {'樣本不足' if s['thin'] else '可判'} |"
            )
        lines.append("")

    titles = {
        "R1": "R1 宇宙依賴：全次產業（不限桶）同 gate",
        "R2": "R2 窗長：主宇宙 r+10／r+40",
        "R3": "R3 除息季：主宇宙排除 7–9 月快照",
        "R4": "R4 現行決勝規則兩鍵自身的族群內 IC（主宇宙、r+20）",
    }
    lines += ["## 3. 揭露（不當門檻、不得事後升格）", ""]
    for key, title in titles.items():
        lines += [
            f"### {title}",
            "",
            "| 項目 | 窗 | 週數 | 週 IC 平均 | CI95 | 分段>0 | regime |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in disclosures.get(key, []):
            seg_pos = sum(1 for s in r.segments if s is not None and s > 0)
            lines.append(
                f"| {_FACTOR_LABELS.get(r.factor, r.factor)} | r+{r.horizon} | {r.ic.n} | "
                f"{_fmt(r.ic.mean)} | {_ci(r.ic.ci)} | {seg_pos}/{len(r.segments)} | "
                f"{r.regime_label} |"
            )
        lines.append("")
    cols = [c for c in corr.columns if c != "factor"]
    lines += [
        "### R5 組內置中排名相關（主宇宙）",
        "",
        "| | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
        *[
            f"| {row['factor']} | " + " | ".join(_fmt(row[c], 2) for c in cols) + " |"
            for row in corr.iter_rows(named=True)
        ],
        "",
        "### R6 覆蓋（主宇宙、target 非 null 的股週）",
        "",
        "| 因子 | 非 null 比例 |",
        "|---|---|",
        *[
            f"| {_FACTOR_LABELS.get(r.factor, r.factor)} | "
            f"{(f'{r.coverage:.1%}' if r.coverage is not None else '—')} |"
            for r in main
        ],
        "",
        "## 4. 推論與口徑",
        "",
    ]
    lines += inference_footer(
        sample_span=f"{us['first']}~{us['last']}（週頻 {us['weeks']} 週）",
        regime_dist=f"主宇宙可計週 {reg_txt}",
        method_desc=(
            f"週序列 moving-block bootstrap（block={cfg.block_len()} 週・B={cfg.n_boot}・"
            f"seed={cfg.seed}）；Bonferroni 層＝同一重抽分布取 α/{cfg.n_tests} 分位"
        ),
        membership_desc="今日 concepts.yaml（非 point-in-time）；每檔主次產業＝手標第一個",
    )
    lines.append("")
    return lines


def run_intra_pick(settings: Path, out_dir: Path | None) -> None:
    """M-Pick2：四因子（docs/32 §3）在重建 shortlist 可入選池的族群內挑檔力＋R1–R6 揭露。"""
    import yaml

    with open(settings, encoding="utf-8") as f:
        cfg_all = yaml.safe_load(f)
    cfg = ip.IntraPickConfig.from_settings(cfg_all)
    out = out_dir or Path(
        (cfg_all.get("backtest") or {}).get("intra_pick", {}).get(
            "output_dir", "research/intra_pick"
        )
    )
    sw, meta = build_stock_weeks(cfg_all, settings, cfg)
    regimes = sw.select("date", "regime").unique(subset=["date"])

    console.print("[bold]評估四因子（主宇宙、預註冊判準）...[/bold]")
    main = [ip.evaluate_factor(sw, f, cfg, regimes=regimes) for f in ip.FACTORS]
    disclosures: dict[str, list[ip.FactorResult]] = {
        "R1": [ip.evaluate_factor(sw, f, cfg, "in_all", regimes=regimes) for f in ip.FACTORS],
        "R2": [
            ip.evaluate_factor(sw, f, cfg, horizon=h, regimes=regimes)
            for h in cfg.disclose_horizons
            for f in ip.FACTORS
        ],
        "R3": [
            ip.evaluate_factor(sw, f, cfg, "in_main_exdiv", regimes=regimes) for f in ip.FACTORS
        ],
        "R4": [ip.evaluate_factor(sw, k, cfg, regimes=regimes) for k in ip.BASELINE_KEYS],
    }
    corr = ip.centered_rank_corr(
        sw.filter(pl.col("in_main")), [*ip.FACTORS, *ip.BASELINE_KEYS], cfg.min_group
    )

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(_verdict_rows(main)).write_csv(out / f"intra_pick_verdicts_{tag}.csv")
    _weekly_rows(sw, main, cfg).write_csv(out / f"intra_pick_weekly_{tag}.csv")
    sw.write_parquet(out / f"intra_pick_stockweeks_{tag}.parquet")
    report = out / f"intra_pick_{tag}.md"
    report.write_text(
        "\n".join(render_report(sw, meta, cfg, main, disclosures, corr)), encoding="utf-8"
    )

    console.print(f"[green]報告：{report}[/green]")
    for r in main:
        console.print(
            f"  {r.factor:<12} IC {_fmt(r.ic.mean)} {_ci(r.ic.ci)} "
            f"M2 {_fmt(r.m2.mean, 2, '%')} M3 {_fmt(r.m3.mean, 2, '%')} → {r.verdict}"
            + (f"（未過 {','.join(r.failed)}）" if r.failed else "")
        )
