"""backtest/valuation_gap_read.py — docs/31 §20.11：`val_gap_pct_composite`
（估值回歸參考價綜合版，§20.9）效度初測 P0–P5。

**明確非正式裁決**（比照 `redesign_prelim_read`）：`valuation_ratios` 快取僅 ~12
個 ISO 週、6 腿綜合版最早存在於 ~2026-07-03、forward-return 截斷使 r+20 可判
ISO 週數 ≈ 4、r+40 = 0——全部低於 `moving_block_bootstrap_ci` 的 `T≥10` 下限。
本輪一律印「初測、無正式裁決」，正式四步裁決依 §20.11 寫死的門檻遞延（實估 ~2027）。

P0 per-次產業 gap% 基準分布（純描述）／P1 composite IC／P2 控制動能後的殘差 IC
（同義反覆守門）／P3 cheap/rich 正負號分割 cell／P4 逐腿 IC（描述性佐證）／
P5 per-次產業 IC。判準句在 docs/31 §20.11 事前寫死，跑完不論結果照該段寫。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.backtest.factor_lab import FactorReport, evaluate, spearman
from tw_screener.backtest.redesign_dimension_grid import (
    valuation_gap_by_regime,
    valuation_gap_grid,
    walk_forward_valuation_gap,
)
from tw_screener.backtest.rotation_efficacy import category_lift_table
from tw_screener.backtest.valuation_gap_panel import (
    COMPOSITE_LEG_COLS,
    HORIZONS,
    build_valuation_gap_panel,
)

_MOMENTUM_CONTROLS: tuple[str, ...] = ("ma60_dist_pct", "trail_r20")
_SIGNAL = "val_gap_pct_composite"
_MIN_SUBIND_OBS = 40  # P5 per-次產業 pooled IC 的最小 stock-week 觀察數（低於此僅列不判）


def _fmt_pct(v: float | None, plus: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%" if plus else f"{v:.2f}%"


def _fmt_ic(v: float | None) -> str:
    return "—" if v is None else f"{v:+.3f}"


def _fmt_ci(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "（n_dates<10，無 CI）"
    return f"[{lo:+.3f}, {hi:+.3f}]"


def _pooled_ic_report(panel: pl.DataFrame, factor: str, horizons: tuple[int, ...],
                      controls: tuple[str, ...] = ()) -> list[FactorReport]:
    reports: list[FactorReport] = []
    for h in horizons:
        reports.append(
            evaluate(
                panel, factor, horizon=h, target=f"alpha{h}",
                controls=list(controls), n_splits=4,
            )
        )
    return reports


def _factor_report_rows(reports: list[FactorReport], with_residual: bool) -> list[dict]:
    rows: list[dict] = []
    for rep in reports:
        p = rep.pooled
        ic = p["ic"][0] if p.height else None
        ci_lo = p["ci_lo"][0] if p.height else None
        ci_hi = p["ci_hi"][0] if p.height else None
        n = p["n"][0] if p.height else 0
        bs_lo, bs_hi = rep.bs_ci
        no = rep.nonoverlap or {}
        has_splits = rep.splits.height > 0 and "ic" in rep.splits.columns
        row = {
            "horizon": rep.horizon,
            "n": int(n) if n is not None else 0,
            "n_dates": rep.inference_meta.get("T", 0),
            "pooled_ic": ic,
            "fisher_ci": (ci_lo, ci_hi),
            "bootstrap_ci": (bs_lo, bs_hi),
            "nonoverlap_ic": no.get("ic"),
            "nonoverlap_ci": (no.get("ci_lo"), no.get("ci_hi")),
            "consistent_sign": rep.consistent_sign if has_splits else None,
        }
        if with_residual:
            r = rep.residual
            row["resid_ic"] = r["ic"][0] if r.height else None
            row["resid_ci"] = (
                (r["ci_lo"][0], r["ci_hi"][0]) if r.height else (None, None)
            )
            row["resid_n"] = int(r["n"][0]) if r.height else 0
        rows.append(row)
    return rows


def _p5_subindustry_ic(panel: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """P5：每個次產業內單獨算 `val_gap_pct_composite` vs `alpha{h}` 的 pooled
    Spearman IC（點估計；per-次產業樣本太小、CI 不算，僅供「跨產業是否一致」的
    描述性讀值）。"""
    schema = {"sub_industry": pl.Utf8, "n": pl.Int64, "ic": pl.Float64}
    tgt = f"alpha{horizon}"
    if panel.is_empty() or not {_SIGNAL, tgt, "sub_industry"}.issubset(panel.columns):
        return pl.DataFrame(schema=schema)
    base = panel.drop_nulls([_SIGNAL, tgt, "sub_industry"])
    rows: list[dict] = []
    for (sub,), g in base.group_by("sub_industry", maintain_order=True):
        if g.height < _MIN_SUBIND_OBS:
            continue
        ic, n = spearman(g, _SIGNAL, tgt)
        rows.append({"sub_industry": str(sub), "n": int(n), "ic": ic})
    return pl.DataFrame(rows, schema=schema).sort("ic", descending=True, nulls_last=True)


def run_valuation_gap_read(
    settings: Path, out_path: Path | None = None, horizons: tuple[int, ...] = HORIZONS
) -> str:
    """讀 `valuation_ratios` 歷史 + 產業別 + fresh 日線面板，重建 gap 面板，跑
    P0–P5，輸出 markdown（預設 `research/valuation_gap/read_<今日>.md`）。"""
    import yaml as _yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import (
        build_broad_industry_membership,
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.backtest.panel import build_price_panel
    from tw_screener.data.twse import create_client

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)
    vg_cfg = cfg.get("backtest", {}).get("valuation_gap", {})
    min_peers = int(vg_cfg.get("min_peers", 5))
    min_snapshots = int(vg_cfg.get("min_snapshots", 8))
    top_quantile = float(vg_cfg.get("top_quantile", 0.2))
    min_rows_per_day = int(vg_cfg.get("min_rows_per_day", 900))
    market_history_days = int(vg_cfg.get("market_history_days", 320))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    regime_path = Path(
        cfg.get("backtest", {}).get("regime_history", {}).get(
            "output_path", "research/panel/regime_labels.parquet"
        )
    )

    client = create_client(settings)
    val_history = client.load_valuation_ratios_history()
    industry_df = load_industry_mapping(cache_dir)
    membership = build_peer_membership(list_subindustries(), industry_df)
    broad_membership = build_broad_industry_membership(industry_df)
    subs = list_subindustries()
    price_panel = build_price_panel(
        load_market_history(cache_dir, n_days=market_history_days), horizons=horizons
    )
    regime = (
        pl.read_parquet(regime_path)
        if regime_path.exists()
        else pl.DataFrame(schema={"date": pl.Date, "regime_label": pl.Utf8})
    )

    panel = build_valuation_gap_panel(
        val_history, membership, broad_membership, price_panel,
        subindustry_map=subs, regime=regime,
        min_peers=min_peers, min_snapshots=min_snapshots, horizons=horizons,
        min_rows_per_day=min_rows_per_day,
    )

    report = _format_report(panel, horizons, top_quantile)
    dest = out_path or Path(f"research/valuation_gap/read_{date.today().isoformat()}.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    return report


def _coverage_table(panel: pl.DataFrame, horizons: tuple[int, ...]) -> list[str]:
    lines = [
        "| ISO 週快照 | n_stocks | 綜合版有值 | 中位線索數 | "
        + " | ".join(f"alpha{h} 可判" for h in horizons)
        + " |",
        "|---|---|---|---|" + "---|" * len(horizons),
    ]
    agg = [pl.len().alias("n"), pl.col(_SIGNAL).is_not_null().sum().alias("comp"),
           pl.col("val_composite_n_legs").median().alias("legs")]
    for h in horizons:
        agg.append(pl.col(f"alpha{h}").is_not_null().sum().alias(f"a{h}"))
    by_date = panel.group_by("date").agg(agg).sort("date")
    n_usable = {h: 0 for h in horizons}
    for r in by_date.iter_rows(named=True):
        cells = [str(r["date"]), str(r["n"]), str(r["comp"]),
                 f"{r['legs']:.0f}" if r["legs"] is not None else "—"]
        for h in horizons:
            av = r[f"a{h}"]
            cells.append(str(av))
            if av and av >= 30:
                n_usable[h] += 1
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "**可判 ISO 週數（alpha 非 null ≥30 檔的週）**："
        + "／".join(f"alpha{h}={n_usable[h]}" for h in horizons)
        + "——全部 < `moving_block_bootstrap_ci` 的 `T≥10` 下限 → 本輪一律"
        "「初測、無正式裁決」。"
    )
    # 構造性斷點：自身腿需 min_snapshots=8 → composite 在最早的 ~3 週只有 3 條
    # 同儕腿（n_legs=3），之後才 6 條。跨 horizon 讀 P1/P2 時，長 horizon 的
    # 樣本主要落在斷點前的 3 腿版本——那不是 pick.md 印的 6 腿數字。
    break_dates = [
        r["date"] for r in by_date.iter_rows(named=True)
        if r["legs"] is not None and r["legs"] < 6
    ]
    if break_dates:
        lines.append("")
        lines.append(
            f"> **構造性斷點**：{break_dates[-1]} 前 composite 只有 3 條同儕腿"
            f"（`n_legs=3`），之後才 6 條——長 horizon（尤其 r+40）的樣本多落在"
            f"斷點前，量到的是 3 腿 peer-only 版本、非 6 腿 composite；~2027 重跑時"
            f"每週皆 `n_legs=6`，此混淆屬本輪特有。母體＝全市場橫斷面（非觀察清單）。"
        )
    return lines


def _format_report(panel: pl.DataFrame, horizons: tuple[int, ...],
                   top_quantile: float) -> str:
    L: list[str] = [
        "# docs/31 §20.11：`val_gap_pct_composite` 效度初測（P0–P5）",
        "",
        "> **非正式裁決**——樣本量小、forward-return 截斷、CI 在樣本不足時空白。"
        "判準句在 docs/31 §20.11 事前寫死，正式四步裁決遞延（實估 ~2027）。"
        "`n_dates` 才是真正獨立觀察數（`n` 含同一 ISO 週橫斷面大量列）。",
        "",
    ]
    if panel.is_empty():
        L.append("（面板為空——`valuation_ratios` 快取或日線快取缺失，如實留白）")
        return "\n".join(L)

    L.append("## 覆蓋率／母體聲明（實測）")
    L.append("")
    L += _coverage_table(panel, horizons)
    L.append("")

    # P0 ------------------------------------------------------------------
    L.append("## P0　per-次產業 gap% 基準分布（純描述、無假設檢定）")
    L.append("")
    L.append("> 直接回應「不同產業具備不同標準」：各次產業 `val_gap_pct_composite` "
             "的 mean／median／正值比（win_rate＝gap>0 佔比）。**點估計（median）"
             "可引用；CI 欄未做日期叢集校正**——`category_lift_table` 把同一檔的 "
             "~12 週近重複快照當獨立抽樣，有效樣本 ≈ 檔數不是 stock-week 數"
             "（如壽險 n=36＝3 檔×12 週），CI 欄不可當推論讀。")
    L.append("")
    p0 = category_lift_table(
        panel.filter(pl.col("sub_industry").is_not_null()),
        "sub_industry", _SIGNAL,
    )
    if p0.is_empty():
        L.append("（無次產業標籤，跳過）")
    else:
        L.append("| 次產業 | n(stock-week) | mean gap% | CI95(未校正) | median gap% | gap>0 佔比 |")
        L.append("|---|---|---|---|---|---|")
        for r in p0.iter_rows(named=True):
            L.append(
                f"| {r['category']} | {r['n']} | {_fmt_pct(r['mean'])} | "
                f"{_fmt_ci(r['ci_lo'], r['ci_hi'])} | {_fmt_pct(r['median'])} | "
                f"{r['win_rate']:.0%} |" if r["win_rate"] is not None
                else f"| {r['category']} | {r['n']} | {_fmt_pct(r['mean'])} | "
                f"{_fmt_ci(r['ci_lo'], r['ci_hi'])} | {_fmt_pct(r['median'])} | — |"
            )
    L.append("")

    # P1 / P2 -----------------------------------------------------------
    p1 = _pooled_ic_report(panel, _SIGNAL, horizons)
    p2 = _pooled_ic_report(panel, _SIGNAL, horizons, controls=_MOMENTUM_CONTROLS)
    L.append("## P1　`val_gap_pct_composite` → forward alpha　pooled Spearman IC")
    L.append("")
    L.append("| horizon | n | n_dates | pooled IC | Fisher CI | bootstrap CI | "
             "非重疊 IC | non-overlap CI | walk-forward 同號 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for row in _factor_report_rows(p1, with_residual=False):
        L.append(
            f"| r+{row['horizon']} | {row['n']} | {row['n_dates']} | "
            f"{_fmt_ic(row['pooled_ic'])} | {_fmt_ci(*row['fisher_ci'])} | "
            f"{_fmt_ci(*row['bootstrap_ci'])} | {_fmt_ic(row['nonoverlap_ic'])} | "
            f"{_fmt_ci(*row['nonoverlap_ci'])} | {row['consistent_sign']} |"
        )
    L.append("")
    L.append("> Fisher CI 在週頻快照×前瞻窗重疊下偏窄（docs/22 §7.2）——以 bootstrap／"
             "非重疊 CI 為準；兩者在 `n_dates<10` 時空白。**見覆蓋率段的構造性斷點："
             "r+40 全部、r+20 多數樣本落在 3 腿 peer-only 版本，那兩列不可當 6 腿 "
             "composite 的長 horizon 證據；只有 r+10 較能代表 6 腿版本。**")
    L.append("")
    L.append("## P2　控制價格動能後的殘差 IC（同義反覆守門）")
    L.append("")
    L.append(f"> 控制欄：`{'`／`'.join(_MOMENTUM_CONTROLS)}`（`trail_r20`＝自建 trailing "
             "20 交易日報酬，**非** panel 的前瞻 `r20`）。**若 P2 殘差 IC 的 CI 跨 0 "
             "而 P1 不跨 → composite 只是動能的再表述、不是獨立訊號。**")
    L.append("")
    L.append("| horizon | resid IC | resid CI(近似) | resid n | （對照）P1 pooled IC |")
    L.append("|---|---|---|---|---|")
    p1_rows = _factor_report_rows(p1, with_residual=False)
    for row, p1row in zip(_factor_report_rows(p2, with_residual=True), p1_rows, strict=True):
        L.append(
            f"| r+{row['horizon']} | {_fmt_ic(row.get('resid_ic'))} | "
            f"{_fmt_ci(*row.get('resid_ci', (None, None)))} | {row.get('resid_n', 0)} | "
            f"{_fmt_ic(p1row['pooled_ic'])} |"
        )
    L.append("")
    L.append("> 殘差 IC 的 Fisher-z 自由度略高估（偏相關），CI 為近似——樣本再厚也"
             "只當方向參考，不當顯著性判準。")
    L.append("")

    # P3 ---------------------------------------------------------------
    L.append("## P3　cheap / rich 正負號分割（quintile cell，四步）")
    L.append("")
    L.append(f"> `cheap`＝當週 gap% 最高 {top_quantile:.0%}（最便宜）、`rich`＝最低 "
             f"{top_quantile:.0%}（現價已跑贏估值）。`delta_mean`＝相對當日全樣本。")
    L.append("")
    grid = valuation_gap_grid(panel, horizons=horizons, top_quantile=top_quantile)
    if grid.is_empty():
        L.append("（cell 表為空）")
    else:
        L.append("| horizon | cell | n | n_dates | mean | median | win | delta_mean | "
                 "CI95 | 前半 | 後半 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in grid.iter_rows(named=True):
            L.append(
                f"| r+{r['horizon']} | {r['cell']} | {r['n']} | {r['n_dates']} | "
                f"{_fmt_pct(r['mean'])} | {_fmt_pct(r['median'])} | "
                f"{r['win_rate']:.0%} | {_fmt_pct(r['delta_mean'])} | "
                f"{_fmt_ci(r['ci_lo'], r['ci_hi'])} | {_fmt_pct(r['mean_h1'])} | "
                f"{_fmt_pct(r['mean_h2'])} |"
            )
    L.append("")
    reg = valuation_gap_by_regime(panel, horizons=horizons, top_quantile=top_quantile)
    L.append("### P3 regime 切片")
    L.append("")
    if reg.is_empty() or reg["n_dates"].max() in (None, 0):
        L.append("（regime 標籤未涵蓋本窗，或每格 n_dates=0——如實跳過，"
                 "正式裁決遞延時 regime 切片才有意義）")
    else:
        L.append("| horizon | cell | regime | n | n_dates | mean | CI95 | thin |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in reg.iter_rows(named=True):
            if r["n"] == 0:
                continue
            L.append(
                f"| r+{r['horizon']} | {r['cell']} | {r['regime']} | {r['n']} | "
                f"{r['n_dates']} | {_fmt_pct(r['mean'])} | "
                f"{_fmt_ci(r['ci_lo'], r['ci_hi'])} | {r['thin']} |"
            )
    L.append("")
    wf = walk_forward_valuation_gap(panel, horizons=horizons, top_quantile=top_quantile)
    L.append("### P3 walk-forward（最後一段為保留驗證窗）")
    L.append("")
    if wf.is_empty():
        L.append("（可用 ISO 週數不足以切 walk-forward——`walk_forward_splits` 回空，"
                 "如實跳過）")
    else:
        L.append("| horizon | cell | split | test 期間 | test_n | test_n_dates | "
                 "test_delta_mean | test_CI95 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in wf.iter_rows(named=True):
            L.append(
                f"| r+{r['horizon']} | {r['cell']} | {r['split_id']} | "
                f"{r['test_start']}~{r['test_end']} | {r['test_n']} | "
                f"{r['test_n_dates']} | {_fmt_pct(r['test_delta_mean'])} | "
                f"{_fmt_ci(r['test_ci_lo'], r['test_ci_hi'])} |"
            )
    L.append("")

    # P4 --------------------------------------------------------------
    L.append("## P4　逐腿 IC（描述性佐證，非 6 個獨立假設）")
    L.append("")
    L.append("> 比照 §22.19 對 `big_holder_1000_pct` 的處置——一維度一假說，逐腿只看"
             "哪些腿帶方向、哪些是雜訊，不各自升格成裁決。")
    L.append("")
    L.append("| 腿 | " + " | ".join(f"r+{h} IC (n_dates)" for h in horizons) + " |")
    L.append("|---|" + "---|" * len(horizons))
    for leg in COMPOSITE_LEG_COLS:
        cells = [f"`{leg}`"]
        for h in horizons:
            rep = evaluate(panel, leg, horizon=h, target=f"alpha{h}", n_splits=0)
            p = rep.pooled
            ic = p["ic"][0] if p.height else None
            cells.append(f"{_fmt_ic(ic)} ({rep.inference_meta.get('T', 0)})")
        L.append("| " + " | ".join(cells) + " |")
    L.append("")

    # P5 --------------------------------------------------------------
    L.append("## P5　per-次產業 IC（跨產業是否一致）")
    L.append("")
    L.append(f"> 每個 ≥{_MIN_SUBIND_OBS} stock-week 的次產業內單獨算 composite→alpha "
             "的 pooled Spearman IC（點估計，per-產業樣本太小不算 CI）。**關係跨產業"
             "變號＝『不同產業不同標準』的直接證據；一致同號＝可用單一讀法。**")
    L.append("")
    focus_h = horizons[0]  # r+10：唯一有點深度的 horizon
    p5 = _p5_subindustry_ic(panel, focus_h)
    L.append(f"（focus horizon = r+{focus_h}；其餘 horizon 樣本更薄，略）")
    L.append("")
    if p5.is_empty():
        L.append("（無次產業達最小觀察數，跳過）")
    else:
        pos = p5.filter(pl.col("ic") > 0).height
        neg = p5.filter(pl.col("ic") < 0).height
        L.append(f"**方向分布：IC>0 的次產業 {pos} 個／IC<0 {neg} 個**"
                 f"（共 {p5.height} 個達門檻）——初測，無 CI，不得引用為結論。")
        L.append("")
        L.append(f"| 次產業 | n | r+{focus_h} IC |")
        L.append("|---|---|---|")
        for r in p5.iter_rows(named=True):
            L.append(f"| {r['sub_industry']} | {r['n']} | {_fmt_ic(r['ic'])} |")
    L.append("")

    L.append("---")
    L.append("")
    L.append("## 讀法（本輪能說與不能說）")
    L.append("")
    L.append("- **能說**：P0 的 per-次產業分布（描述性，樣本足夠）。")
    L.append("- **不能說**：P1–P5 的任何「有效／無效」定論——`n_dates` 全部 <10，"
             "CI 空白。方向讀值僅供 ~2027 累積達標後對照，現在不升級進 pick.md。")
    L.append("- **判準句**見 docs/31 §20.11（事前寫死）；正式四步裁決重跑門檻："
             "P2 焦點格 composite×r+20 的 `n_dates≥10` 且 regime 切片非全 thin。")
    return "\n".join(L)
