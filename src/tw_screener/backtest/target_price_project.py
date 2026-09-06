"""backtest/target_price_project.py — docs/31 §20.13 Phase 2：本週 production 實驗性
機械式「目標價」。

Phase 1（`target_price_read.py`）已鎖定結論：機械式歷史類比的 **per-stock 目標價
可信度低**——#1(b) r+20 pooled-null 配對差 CI [−0.10, +0.03] 跨 0（cell 的 P50 取代
全市場 P50 沒有 measurably 縮小投射誤差）、#3 中位偏誤 +2.23pp（舊 fit 窗 ⇒ 數字
系統性偏低）、#4 區間寬 ±9.86pp（r+20）。

使用者拍板（§20.13）：仍要一個帶信賴度評級的 per-stock 機械數字＋並列 search-augmented
前瞻 EPS 版——放 pick.md `<!-- picks:begin -->` **之外**的「附錄 G 實驗區塊」、可用
「目標價」字眼（鐵律 2 於此區塊豁免，非放寬）。

本 runner 只做**機械腿**：讀凍結 fit 分位查表（`config/target_price_fit_lookup.csv`，
Phase 1 fit 窗 2022-01~2024-12）＋當週薄面板算的 profile（位階×族群內相對強弱 9 格），
每檔輸出 `close×(1+cell_P50)` ＋ P25–P75 區間 ＋ `_pooled` 並列 ＋ 信賴度（**一律封頂
「低」**，settings `tier_cap`）＋一句依據（含 #3 揭露）。search-augmented 腿由 pick.md
的分析師子代理現場用 web search 前瞻 EPS 做（runner 留空欄）。

產物 `reports/<週次>/target_price_experimental.{md,yaml}`——**與 `picks sync` 完全脫鉤**
（parser 只認 picks 區塊）、不進 picks.csv、不進正式結論。

純函式 + IO wrapper；比照 `target_price_read.py` 分層。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from tw_screener.backtest.target_price_panel import (
    POOLED_CELL,
    _pos_bin_expr,
    _rs_bin_expr,
    build_analog_panel,
)
from tw_screener.backtest.target_price_read import (
    FIT_END,
    FIT_START,
    confidence_tier,
    forward_return_percentiles,
    split_fit_test,
)

# Phase 1 主裁決數字（§20.13）——寫進每檔 basis 與區塊免責語。
DRIFT_BIAS_PP = 2.23        # #3 r+20 中位偏誤（test 2025–2026 高於 fit P50）
POOLED_NULL_CI = (-0.10, 0.03)  # #1(b) r+20 配對差 CI（跨 0）
PAIRED_DIFF_MEAN = -0.04


def _expected_bin_labels(
    pos_edges: tuple[float, float], rs_edges: tuple[float, float]
) -> tuple[set[str], set[str]]:
    """依切點生成期望的 pos_bin / rs_bin label 集合（比對 fit_lookup 內嵌切點用）。"""
    dummy = pl.DataFrame({"ma60_dist_pct": [-99.0, 0.0, 99.0], "rs_subind": [-99.0, 0.0, 99.0]})
    pos = set(
        dummy.select(_pos_bin_expr(pos_edges))["pos_bin"].to_list()
    )
    rs = set(dummy.select(_rs_bin_expr(rs_edges))["rs_bin"].to_list())
    return pos, rs


def _assert_edges_match(
    fit_lookup: pl.DataFrame,
    pos_edges: tuple[float, float],
    rs_edges: tuple[float, float],
) -> None:
    """fit_lookup 的 cell label 內嵌切點（如「位階貼低≤-8｜族群內領先>5」）必須與現行
    settings 切點一致——不符 ⇒ raise（落實 §20.13「跑完不得回調」：fit 產物凍結後，
    改切點就得重跑 Phase 1）。"""
    if fit_lookup.is_empty():
        raise ValueError("fit_lookup 為空——無法投射")
    pos_ok, rs_ok = _expected_bin_labels(pos_edges, rs_edges)
    seen_cells = [c for c in fit_lookup["cell"].unique().to_list() if c != POOLED_CELL]
    bad: list[str] = []
    for cell in seen_cells:
        parts = cell.split("｜")
        if len(parts) != 2 or parts[0] not in pos_ok or parts[1] not in rs_ok:
            bad.append(cell)
    if bad:
        raise ValueError(
            f"fit_lookup 切點與 settings 不符（pos_edges={pos_edges}、rs_edges={rs_edges}）："
            f"{bad[:3]}…；改切點須重跑 Phase 1 backtest target-price-read --freeze"
        )


def _basis_line(iqr_pp: float | None) -> str:
    lo, hi = POOLED_NULL_CI
    iqr_txt = f"±{iqr_pp / 2:.0f}pp" if iqr_pp is not None else "—"
    return (
        "機械式歷史類比（位階×族群內相對強弱 9 格，r+20 fit 窗 2022–2024）；"
        f"主裁決：分桶不能縮誤差、與全市場基準統計無法區分（配對差 {PAIRED_DIFF_MEAN:+.2f}pp、"
        f"CI [{lo:+.2f}, {hi:+.2f}]）；test 窗 2025–2026 實際中位比 fit P50 高 "
        f"+{DRIFT_BIAS_PP:.2f}pp ⇒ 本數字系統性偏低；區間 P25–P75 約 {iqr_txt}。"
    )


def project_week_targets(
    latest_rows: pl.DataFrame,
    fit_lookup: pl.DataFrame,
    horizon: int = 20,
    tier_cap: str = "低",
) -> tuple[pl.DataFrame, list[str]]:
    """對薄面板「投射錨點日」那天的候選列，用凍結 fit 分位查表投射機械式目標價。

    Args:
        latest_rows: 含 stock_id / close / ma60_dist_pct / rs_subind / regime /
            pos_bin / rs_bin / cell（build_analog_panel 輸出取 date.max() 那批）。
        fit_lookup: 凍結分位查表（forward_return_percentiles 格式）；本函式只用 `horizon` 那批。
        horizon: 只用 r+`horizon`（Phase 2 固定 20，唯一裁決-eligible）。
        tier_cap: 顯示 tier 封頂（§20.13 主裁決 ⇒「低」；settings 覆寫）。

    Returns:
        (targets_df, warnings)：
        targets_df 欄＝stock_id / close / cell / regime_now / horizon_td /
            target_mechanical / p25 / p75 / target_pooled / tier / tier_raw / iqr_pp /
            n_cell / basis；cell 查無（profile null）者不出列、記入 warnings。
    """
    warn: list[str] = []
    schema = {
        "stock_id": pl.Utf8,
        "close": pl.Float64,
        "cell": pl.Utf8,
        "regime_now": pl.Utf8,
        "horizon_td": pl.Int64,
        "target_mechanical": pl.Float64,
        "p25": pl.Float64,
        "p75": pl.Float64,
        "target_pooled": pl.Float64,
        "tier": pl.Utf8,
        "tier_raw": pl.Utf8,
        "iqr_pp": pl.Float64,
        "n_cell": pl.Int64,
        "basis": pl.Utf8,
    }
    if latest_rows.is_empty() or fit_lookup.is_empty():
        return pl.DataFrame(schema=schema), ["latest_rows 或 fit_lookup 為空"]

    lk = fit_lookup.filter(pl.col("horizon") == horizon)
    if lk.is_empty():
        return pl.DataFrame(schema=schema), [f"fit_lookup 無 horizon={horizon} 的列"]
    cell_lk = {r["cell"]: r for r in lk.iter_rows(named=True)}
    pooled = cell_lk.get(POOLED_CELL)
    if pooled is None:
        return pl.DataFrame(schema=schema), ["fit_lookup 無 _pooled 列"]

    regime_thin_labels = {"thin", "薄", None}
    rows: list[dict] = []
    for r in latest_rows.iter_rows(named=True):
        sid = str(r["stock_id"])
        close = r.get("close")
        cell = r.get("cell")
        if close is None or close <= 0:
            warn.append(f"{sid}：close 缺，略過")
            continue
        if cell is None or cell not in cell_lk:
            warn.append(f"{sid}：profile 兩維任一 null（cell 查無），略過")
            continue
        c = cell_lk[cell]
        p50, p25, p75 = c.get("p50"), c.get("p25"), c.get("p75")
        n_cell, iqr = int(c.get("n") or 0), c.get("iqr")
        if p50 is None:
            warn.append(f"{sid}：cell {cell} 無 p50，略過")
            continue
        regime_now = r.get("regime")
        regime_thin = regime_now in regime_thin_labels
        tier_raw = confidence_tier(n_cell, iqr, regime_thin, horizon)
        rows.append(
            {
                "stock_id": sid,
                "close": float(close),
                "cell": cell,
                "regime_now": regime_now,
                "horizon_td": horizon,
                "target_mechanical": round(float(close) * (1 + p50 / 100), 2),
                "p25": round(float(close) * (1 + p25 / 100), 2) if p25 is not None else None,
                "p75": round(float(close) * (1 + p75 / 100), 2) if p75 is not None else None,
                "target_pooled": round(float(close) * (1 + pooled["p50"] / 100), 2),
                "tier": tier_cap,
                "tier_raw": tier_raw,
                "iqr_pp": round(float(iqr), 2) if iqr is not None else None,
                "n_cell": n_cell,
                "basis": _basis_line(iqr),
            }
        )
    return pl.DataFrame(rows, schema=schema), warn


# ── report ────────────────────────────────────────────────────────────────────
_DISCLAIMER = (
    "> **實驗性機械式/前瞻式「目標價」試算，非投資建議、非公允價、不進正式結論與 picks 底帳"
    "（`picks sync` 不消費本區塊）。** 機械腿＝位階×族群內相對強弱 9 格歷史類比分位（Phase 1"
    "主裁決：分桶不能縮小 per-stock 誤差、與全市場基準統計無法區分、且對舊 fit 窗有 regime"
    f" drift 系統性偏低 +{DRIFT_BIAS_PP:.2f}pp），信賴度一律「低」；search-augmented 腿無任何"
    "歷史驗證。數字僅供人工判讀時的量級參照。"
)


def _fmt_targets_md(targets: pl.DataFrame, anchor: str, lookup_tag: str) -> str:
    lines = [
        "# 實驗性目標價（機械腿・docs/31 §20.13 Phase 2）",
        "",
        _DISCLAIMER,
        "",
        f"- 投射錨點日：{anchor}｜fit 查表：{lookup_tag}｜時間窗：20 交易日（≈1 個月）",
        "",
    ]
    if targets.is_empty():
        lines.append("（本週無可投射的候選股——profile 兩維皆 null，或面板為空）")
        return "\n".join(lines) + "\n"
    lines += [
        "| 股號 | 現價 | cell | 機械目標價(P50) | P25–P75 | 全市場基準(P50) "
        "| 信賴度 | (原始tier) | 區間寬pp | n |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in targets.iter_rows(named=True):
        rng = (
            f"{r['p25']}–{r['p75']}"
            if (r["p25"] is not None and r["p75"] is not None)
            else "—"
        )
        lines.append(
            f"| {r['stock_id']} | {r['close']} | {r['cell']} | {r['target_mechanical']} | "
            f"{rng} | {r['target_pooled']} | {r['tier']} | {r['tier_raw']} | "
            f"{r['iqr_pp']} | {r['n_cell']} |"
        )
    lines += [
        "",
        f"> 一句依據（所有列共用）：{_basis_line(None).split('；區間')[0]}；"
        "區間見各列 P25–P75。",
        "",
        "## search-augmented 腿（前瞻 EPS × 成長調整倍數）",
        "",
        "> 由 pick.md 分析師子代理現場做（web search 取公司財測／具名券商前瞻營收/EPS，"
        "**不得抄券商目標價**）；公式與 null 路徑見 docs/11「附錄 G」。"
        "標「不可回測・無歷史驗證」。",
    ]
    return "\n".join(lines) + "\n"


def _targets_yaml(targets: pl.DataFrame, anchor: str, provenance: dict) -> str:
    import yaml as _yaml

    payload = {
        "target_price_experimental": {
            "anchor_date": anchor,
            "horizon_td": 20,
            "tier_note": "機械腿信賴度一律封頂「低」（§20.13 主裁決 pooled-null CI 跨 0）",
            "fit_lookup_provenance": provenance,
            "rows": [
                {
                    "stock_id": r["stock_id"],
                    "close": r["close"],
                    "cell": r["cell"],
                    "regime_now": r["regime_now"],
                    "target_mechanical": r["target_mechanical"],
                    "p25": r["p25"],
                    "p75": r["p75"],
                    "target_pooled": r["target_pooled"],
                    "tier": r["tier"],
                    "tier_raw": r["tier_raw"],
                    "iqr_pp": r["iqr_pp"],
                    "n_cell": r["n_cell"],
                    "basis": r["basis"],
                    # search-augmented 腿：runner 留空，分析師子代理填
                    "fwd_eps": None,
                    "target_pe": None,
                    "target_search_augmented": None,
                    "search_note": None,
                }
                for r in targets.iter_rows(named=True)
            ],
        }
    }
    return _yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


# ── IO wrapper ────────────────────────────────────────────────────────────────
def run_target_price_project(
    settings: Path,
    week: str | None = None,
    out_path: Path | None = None,
) -> str:
    """docs/31 §20.13 Phase 2：讀凍結 fit 查表 ＋ 當週薄面板 → 每檔機械式目標價
    → reports/<週次>/target_price_experimental.{md,yaml}。失敗回傳說明字串、不 raise
    （make week 以 `-` 前綴容錯掛載）。"""
    import yaml as _yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.screener.runner import derive_week_tag

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)
    tpc = cfg.get("backtest", {}).get("target_price", {})
    prj = tpc.get("project", {})
    if not prj.get("enabled", True):
        return "target_price.project.enabled=false——略過"

    _pe = tpc.get("pos_edges", [-8.0, 8.0])
    _re = tpc.get("rs_edges", [-5.0, 5.0])
    pos_edges = (float(_pe[0]), float(_pe[1]))
    rs_edges = (float(_re[0]), float(_re[1]))
    rs_window = int(tpc.get("rs_window", 20))
    min_rows_per_day = int(tpc.get("min_rows_per_day", 900))
    horizon = int(prj.get("horizon_td", 20))
    thin_days = int(prj.get("thin_history_days", 200))
    tier_cap = str(prj.get("tier_cap", "低"))
    basename = str(prj.get("output_basename", "target_price_experimental"))
    close_tol = float(prj.get("close_reconcile_tol_pct", 1.0))
    lookup_path = Path(prj.get("fit_lookup_path", "config/target_price_fit_lookup.csv"))

    week_tag = week or derive_week_tag(settings)
    reports_root = Path(cfg["paths"].get("reports_dir", "reports"))
    week_dir = reports_root / week_tag
    cand_csv = week_dir / "candidates_enriched.csv"

    dest_md = out_path or week_dir / f"{basename}.md"
    dest_yaml = week_dir / f"{basename}.yaml"
    dest_md.parent.mkdir(parents=True, exist_ok=True)

    def _write(md: str, yml: str) -> str:
        dest_md.write_text(md, encoding="utf-8")
        dest_yaml.write_text(yml, encoding="utf-8")
        return md

    if not cand_csv.exists():
        msg = f"# 實驗性目標價\n\n（{cand_csv} 不存在——先跑 make group）\n"
        return _write(msg, "target_price_experimental: {rows: []}\n")

    cand = pl.read_csv(cand_csv, infer_schema_length=None).with_columns(
        pl.col("stock_id").cast(pl.Utf8)
    )
    cand_ids = cand["stock_id"].unique().to_list()
    cand_close: dict[str, float] = {}
    if "close" in cand.columns:
        for row in cand.select("stock_id", "close").iter_rows(named=True):
            if row["close"] is not None:
                cand_close[row["stock_id"]] = float(row["close"])

    # 凍結 fit 查表；缺 → 現場重建（慢，比照 Phase 1），標「非凍結版」
    lookup_tag = f"凍結版 {lookup_path.name}"
    if lookup_path.exists():
        fit_lookup = pl.read_csv(lookup_path)
    else:
        lookup_tag = "現場重建（非凍結版！fit 查表缺）"
        cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
        regime_path = Path(
            cfg.get("backtest", {})
            .get("regime_history", {})
            .get("output_path", "research/panel/regime_labels.parquet")
        )
        full_price = load_market_history(
            cache_dir, n_days=int(tpc.get("market_history_days", 1300))
        )
        regime_df = pl.read_parquet(regime_path) if regime_path.exists() else None
        full_panel = build_analog_panel(
            full_price,
            list_subindustries(),
            regime=regime_df,
            horizons=(horizon,),
            pos_edges=pos_edges,
            rs_edges=rs_edges,
            rs_window=rs_window,
            min_rows_per_day=min_rows_per_day,
        )
        fit_part, _ = split_fit_test(full_panel, FIT_START, FIT_END)
        fit_lookup = forward_return_percentiles(fit_part, horizons=(horizon,))

    try:
        _assert_edges_match(fit_lookup, pos_edges, rs_edges)
    except ValueError as exc:
        msg = f"# 實驗性目標價\n\n（切點守衛失敗，未產出：{exc}）\n"
        return _write(msg, "target_price_experimental: {rows: []}\n")

    # 薄面板（只算當前 profile）
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    regime_path = Path(
        cfg.get("backtest", {})
        .get("regime_history", {})
        .get("output_path", "research/panel/regime_labels.parquet")
    )
    thin_price = load_market_history(cache_dir, n_days=thin_days)
    regime_df = pl.read_parquet(regime_path) if regime_path.exists() else None
    panel = build_analog_panel(
        thin_price,
        list_subindustries(),
        regime=regime_df,
        horizons=(horizon,),
        pos_edges=pos_edges,
        rs_edges=rs_edges,
        rs_window=rs_window,
        min_rows_per_day=min_rows_per_day,
    )
    if panel.is_empty():
        msg = "# 實驗性目標價\n\n（薄面板為空——日線快取不足或 membership 缺）\n"
        return _write(msg, "target_price_experimental: {rows: []}\n")

    anchor = panel.select(pl.col("date").max()).item()
    anchor_iso = anchor.isoformat() if hasattr(anchor, "isoformat") else str(anchor)
    latest = panel.filter(pl.col("date") == anchor).filter(
        pl.col("stock_id").is_in(cand_ids)
    )

    # 錨點日 vs 最新交易日
    warnings: list[str] = []
    try:
        from tw_screener.data.twse import create_client

        ltd = create_client(settings).latest_trading_date()
        if ltd is not None and anchor != ltd:
            warnings.append(
                f"投射錨點日 {anchor_iso} ≠ 最新交易日 {ltd}"
                "（今日抓取可能不完整、min_rows_per_day 整日剔除）；投射基於較舊錨點"
            )
    except Exception:  # noqa: BLE001
        pass

    targets, proj_warn = project_week_targets(
        latest, fit_lookup, horizon=horizon, tier_cap=tier_cap
    )
    warnings += proj_warn

    # close 對帳
    if not targets.is_empty() and cand_close:
        for r in targets.iter_rows(named=True):
            cc = cand_close.get(r["stock_id"])
            if cc and abs(r["close"] - cc) / cc * 100 > close_tol:
                warnings.append(
                    f"{r['stock_id']}：面板 close {r['close']} vs candidates {cc}"
                    f"（差 >{close_tol}%，本目標價由面板 close 推導）"
                )

    provenance: dict = {
        "tag": lookup_tag,
        "cell_edges": {"pos": list(pos_edges), "rs": list(rs_edges)},
    }
    prov_file = lookup_path.with_suffix(".provenance.yaml")
    if prov_file.exists():
        try:
            provenance.update(_yaml.safe_load(prov_file.read_text(encoding="utf-8")) or {})
        except Exception:  # noqa: BLE001
            pass

    md = _fmt_targets_md(targets, anchor_iso, lookup_tag)
    if warnings:
        md += "\n## 警告\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
    yml = _targets_yaml(targets, anchor_iso, provenance)
    return _write(md, yml)
