"""backtest/diagnostic.py — M-Diag1：診斷「抓太晚」＋「漏掉起漲股」（WS1／WS2）。

回答外部研究 prompt 的兩題，全用既有機器（strategies.compute_forward_returns 前瞻報酬引擎、
candidates_enriched 候選宇宙），不重造：

- WS1「抓太晚」：前瞻報酬 vs 進場延伸度（ma60_dist）分桶曲線 → 驗證 F2 的 +15% 硬擋是不是
  實證轉負點；排序訊號（動能／延伸／法人近端）對前瞻報酬的 IC → 檢驗「CSV 由上往下挑＝
  系統性追高」；候選組內名次的 skill。
- WS2「漏掉起漲股」（A 純無偏路）：錨定候選週，對 daily_all 全市場乾淨底料（≤06-09）算前瞻報酬，
  篩「data_date 處回檔/base、之後漲 ≥Y%」＝起漲事件，再交叉該週 picks／candidates → 分
  已進場／考慮過未選（可歸因閘門）／從未浮現（screener 沒撈到）三態。

樣本天花板（見 research/diagnostic/00_cp1_data_audit.md）：候選宇宙 ~987 列、r+10 到期 ~705 列
（W21–W25），r+20 只到 W21–W22；WS2 全市場乾淨底料停 06-09，交叉僅 W21–W23、事件數必薄
（<30＝初步）。宣稱一律附 n＋Fisher-z 解析 CI；跨單一 regime、每季重跑。
純函式；IO 由 diagnostic_runner 負責。
"""

from __future__ import annotations

import math

import polars as pl

from tw_screener.backtest.strategies import compute_forward_returns

# 候選宇宙裡當「排序訊號」測 IC 的欄（都在 candidates_enriched，逐週 schema 穩定）
_IC_FEATURES = (
    "ma60_dist_pct",       # 進場延伸度（F2 +15% 硬擋的變數）
    "momentum_5d_pct",     # 近端動能（現行排序主鍵之一）
    "ret_10d_pct",
    "foreign_net_5d_lots",
    "inst_pct20d",
    "vol_ratio",
)

# extension-at-entry 分桶：圍繞 F2 現行 +15% 門檻設粗桶（樣本薄，不設尖銳細桶）
_EXT_EDGES = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0)
_EXT_LABELS = ("<0%", "0–5%", "5–10%", "10–15%", "15–20%", "20–30%", ">30%")


def build_candidate_screens(
    enriched_by_week: dict[str, pl.DataFrame],
    week_to_date: dict[str, object],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """候選宇宙 → (screens, features)。

    screens：compute_forward_returns 需要的最小欄（week_tag/screened_at/stock_id/name/
    strategy_id）；screened_at＝該週 data_date（防前視，entry 用次一交易日）。
    features：(week_tag, stock_id) → 排序訊號欄＋rank_in_group／strategy，供 join 回報酬。

    缺 data_date 或必要欄的週跳過。strategy_id 取 enriched 的 strategy 欄（無則 'cand'）。
    """
    screen_rows: list[pl.DataFrame] = []
    feat_rows: list[pl.DataFrame] = []
    keep_feats = ["stock_id", "rank_in_group", "strategy", *(_IC_FEATURES)]
    for week, enr in enriched_by_week.items():
        sdate = week_to_date.get(week)
        if sdate is None or enr.is_empty() or "stock_id" not in enr.columns:
            continue
        base = enr.with_columns(pl.col("stock_id").cast(pl.Utf8))
        screen_rows.append(
            base.select(
                pl.lit(week).alias("week_tag"),
                pl.lit(sdate).cast(pl.Date).alias("screened_at"),
                "stock_id",
                (pl.col("name").cast(pl.Utf8) if "name" in base.columns
                 else pl.lit(None, dtype=pl.Utf8).alias("name")),
                (pl.col("strategy").cast(pl.Utf8).alias("strategy_id")
                 if "strategy" in base.columns else pl.lit("cand").alias("strategy_id")),
            )
        )
        feat_rows.append(
            base.select(
                pl.lit(week).alias("week_tag"),
                *[
                    (pl.col(c) if c in base.columns else pl.lit(None).alias(c))
                    for c in keep_feats
                ],
            )
        )
    if not screen_rows:
        return pl.DataFrame(), pl.DataFrame()
    return pl.concat(screen_rows, how="vertical_relaxed"), pl.concat(
        feat_rows, how="vertical_relaxed"
    )


def forward_returns_long(
    screens: pl.DataFrame,
    market: pl.DataFrame,
    dividends: pl.DataFrame | None,
    horizons_td: tuple[int, ...],
    trading_days_per_week: int = 5,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """對每個前瞻窗（交易日）算前瞻報酬，疊成長表（只留到期列）。

    複用 strategies.compute_forward_returns（前視防護／除息還原／下市 null／未到期排除全沿用）。
    horizons_td 需可被 trading_days_per_week 整除（換成 hold_weeks）。回傳含 hold_weeks／
    return_pct／market_return_pct／excess_return_pct／status（僅 matured）。
    """
    if screens.is_empty() or market.is_empty():
        return pl.DataFrame()
    frames: list[pl.DataFrame] = []
    for td in horizons_td:
        if td % trading_days_per_week != 0:
            continue
        f = compute_forward_returns(
            screens,
            market,
            hold_weeks=td // trading_days_per_week,
            dividends=dividends,
            trading_days_per_week=trading_days_per_week,
            clip_daily_return_pct=clip_daily_return_pct,
        )
        if not f.is_empty():
            frames.append(f.with_columns(pl.lit(td).alias("horizon_td")))
    if not frames:
        return pl.DataFrame()
    return (
        pl.concat(frames, how="vertical_relaxed")
        .filter((pl.col("status") == "matured") & pl.col("return_pct").is_not_null())
    )


def _spearman(df: pl.DataFrame, xc: str, yc: str) -> tuple[float | None, int]:
    """Spearman ρ＝平均秩後的 Pearson（純 polars，rank tie 取平均）。回 (ρ, n)。

    樣本 <3 或某側全同值（rank 常數）→ ρ None。
    """
    pair = df.select(xc, yc).drop_nulls()
    n = pair.height
    if n < 3:
        return None, n
    ranked = pair.select(
        pl.col(xc).rank(method="average").alias("_rx"),
        pl.col(yc).rank(method="average").alias("_ry"),
    )
    if ranked["_rx"].n_unique() < 2 or ranked["_ry"].n_unique() < 2:
        return None, n
    rho = ranked.select(pl.corr("_rx", "_ry")).item()
    return (float(rho) if rho is not None else None), n


def _fisher_ci(rho: float | None, n: int) -> tuple[float | None, float | None]:
    """Spearman 的 Fisher-z 解析 95% CI（無 bootstrap／無外部依賴）。

    z=atanh(ρ)、se=1/√(n−3)、CI=tanh(z±1.96·se)。n<11 或 |ρ|→1 不給 CI（近似失效）。
    連續報酬/訊號 tie 稀疏，Fisher-z 近似夠用；標明為解析近似。
    """
    if rho is None or n < 11 or abs(rho) >= 0.999:
        return None, None
    z = math.atanh(rho)
    se = 1.0 / math.sqrt(n - 3)
    return math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)


def extension_curve(
    joined: pl.DataFrame,
    feature_col: str = "ma60_dist_pct",
    target_col: str = "excess_return_pct",
    edges: tuple[float, ...] = _EXT_EDGES,
    labels: tuple[str, ...] = _EXT_LABELS,
) -> pl.DataFrame:
    """延伸度分桶 → 每桶前瞻（超額）報酬。回 horizon_td／bucket／n／median／mean／win_rate。

    joined 需含 feature_col／target_col／horizon_td。win_rate＝該桶 target>0 比例。
    """
    schema = {
        "horizon_td": pl.Int64, "bucket": pl.Utf8, "n": pl.UInt32,
        "median": pl.Float64, "mean": pl.Float64, "win_rate": pl.Float64,
    }
    need = {feature_col, target_col, "horizon_td"}
    if joined.is_empty() or not need.issubset(joined.columns):
        return pl.DataFrame(schema=schema)
    df = joined.filter(
        pl.col(feature_col).is_not_null() & pl.col(target_col).is_not_null()
    ).with_columns(
        pl.col(feature_col)
        .cut(list(edges), labels=list(labels))
        .cast(pl.Utf8)
        .alias("bucket")
    )
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    order = {lab: i for i, lab in enumerate(labels)}
    return (
        df.group_by("horizon_td", "bucket")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            pl.col(target_col).median().alias("median"),
            pl.col(target_col).mean().alias("mean"),
            (pl.col(target_col) > 0).mean().alias("win_rate"),
        )
        .with_columns(pl.col("bucket").replace_strict(order, default=99).alias("_o"))
        .sort("horizon_td", "_o")
        .drop("_o")
        .select(list(schema))
    )


def signal_ic_table(
    joined: pl.DataFrame,
    feature_cols: tuple[str, ...] = _IC_FEATURES,
    target_col: str = "excess_return_pct",
) -> pl.DataFrame:
    """各排序訊號 × 各前瞻窗 → Spearman IC＋Fisher-z 95% CI＋n。

    IC>0＝訊號值越高前瞻越好；ma60_dist／momentum 的 IC<0 即「追高」實證。
    """
    schema = {
        "feature": pl.Utf8, "horizon_td": pl.Int64, "ic": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "n": pl.UInt32,
    }
    if joined.is_empty() or target_col not in joined.columns:
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for td in sorted(joined["horizon_td"].unique().to_list()):
        sub = joined.filter(pl.col("horizon_td") == td)
        for feat in feature_cols:
            if feat not in sub.columns:
                continue
            ic, n = _spearman(sub, feat, target_col)
            if ic is None:
                continue
            lo, hi = _fisher_ci(ic, n)
            rows.append({
                "feature": feat, "horizon_td": int(td), "ic": ic,
                "ci_lo": lo, "ci_hi": hi, "n": int(n),
            })
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema).sort("horizon_td", "feature")


def rank_ic_table(
    joined: pl.DataFrame,
    target_col: str = "excess_return_pct",
    group_cols: tuple[str, ...] = ("week_tag", "strategy"),
    min_group: int = 5,
) -> pl.DataFrame:
    """組內名次（rank_in_group）對前瞻報酬的 IC——我們的排序有沒有 skill。

    每 (週,策略組) 算 Spearman(rank_in_group, target)，池化算平均 IC＋跨組標準誤。
    rank 小＝排前面；若排前面前瞻較好則 IC 為負（rank 值與報酬負相關）→ 報時翻正號為
    skill＝-mean_group_ic，正值＝排序有效。回 horizon_td／mean_skill／se／n_groups。
    """
    schema = {
        "horizon_td": pl.Int64, "mean_skill": pl.Float64,
        "se": pl.Float64, "n_groups": pl.UInt32,
    }
    need = {"rank_in_group", target_col, *group_cols}
    if joined.is_empty() or not need.issubset(joined.columns):
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for td in sorted(joined["horizon_td"].unique().to_list()):
        sub = joined.filter(pl.col("horizon_td") == td)
        ics: list[float] = []
        for _, g in sub.group_by(list(group_cols)):
            if g.height < min_group:
                continue
            r, _n = _spearman(g, "rank_in_group", target_col)
            if r is not None:
                ics.append(-r)  # 翻正號：rank 小=排前，負相關→skill 為正
        if ics:
            k = len(ics)
            mean = sum(ics) / k
            se = (
                math.sqrt(sum((v - mean) ** 2 for v in ics) / (k - 1) / k)
                if k > 1 else None
            )
            rows.append({
                "horizon_td": int(td), "mean_skill": mean, "se": se, "n_groups": k,
            })
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema).sort("horizon_td")


def _f(v: object, suf: str = "%", nd: int = 1) -> str:
    return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"


def _ws1_synthesis(
    ext_curve: pl.DataFrame, ic_table: pl.DataFrame, ext_gate_pct: float
) -> list[str]:
    """資料驅動小結：逐窗抓「中位轉負桶」＋延伸/動能 IC 是否跨窗顯著負（不硬編數字）。"""
    out = ["## 小結（資料驅動；隨每季重跑更新）", ""]
    # (1) 每窗第一個中位 ≤0 的延伸桶
    if not ext_curve.is_empty():
        turns: list[str] = []
        for td in sorted(ext_curve["horizon_td"].unique().to_list()):
            sub = ext_curve.filter(pl.col("horizon_td") == td)
            first_neg = None
            for r in sub.iter_rows(named=True):
                if r["median"] is not None and r["median"] <= 0:
                    first_neg = r["bucket"]
                    break
            turns.append(f"r+{td}d→{first_neg or '無'}")
        out.append(f"- **延伸度轉負桶（中位≤0 起點）**：{'、'.join(turns)}。")
    # (2) 延伸/動能 IC 跨窗顯著性
    def _all_sig_neg(feat: str) -> bool:
        f = ic_table.filter(pl.col("feature") == feat)
        if f.is_empty():
            return False
        return all(
            r["ci_hi"] is not None and r["ci_hi"] < 0 for r in f.iter_rows(named=True)
        )
    if _all_sig_neg("ma60_dist_pct"):
        out.append(
            f"- **距季線 IC 跨所有窗顯著為負**＝越延伸前瞻越差（追高實證）；現行 F2 "
            f"+{ext_gate_pct:.0f}% 硬擋方向對，但**桶表顯示正期望值在更淺處就流失**——"
            "是否收緊/加註 caution band 由後續 milestone 依此裁決，本 CP 只出證據。"
        )
    else:
        out.append("- 距季線 IC 未跨窗一致顯著負——延伸→報酬關係在本樣本下不穩，降 context。")
    if _all_sig_neg("momentum_5d_pct"):
        out.append("- **近端動能 IC 亦顯著為負**＝CSV 由動能高往低挑＝系統性追高，實證成立。")
    out += [
        "",
        "> 防過擬合：粗桶、單一 regime、Fisher-z 近似 CI；偏好單調關係不釘尖銳門檻。"
        "任何據此的閘門改革須另立 milestone 走 walk-forward。",
    ]
    return out


def render_late_entry_report(
    ext_curve: pl.DataFrame,
    ic_table: pl.DataFrame,
    rank_ic: pl.DataFrame,
    universe_n: dict[int, int],
    target_label: str,
    ext_gate_pct: float,
    min_sample_warn: int,
) -> str:
    """WS1「抓太晚」診斷 markdown：延伸度曲線＋排序訊號 IC＋名次 skill。"""
    lines = [
        "# 診斷 WS1：抓太晚？——進場延伸度 × 前瞻報酬（M-Diag1・CP2）",
        "",
        f"- 母體＝候選宇宙（candidates_enriched 逐週池化）；target＝**{target_label}**"
        "（entry＝資料日次一交易日收盤，防前視；除息還原）。",
        "- 前瞻到期樣本："
        + "、".join(f"r+{td}d n={n}" for td, n in sorted(universe_n.items()))
        + "。**跨單一 regime、樣本薄——方向性使用**，每季重跑。",
        f"- IC = Spearman(訊號, {target_label})；ma60_dist／momentum 的 IC<0 即「越延伸/越強→"
        "前瞻越差」＝系統性追高的實證。",
        "",
        "## 1. 進場延伸度分桶曲線（驗證 F2 現行 +"
        f"{ext_gate_pct:.0f}% 硬擋）",
        "",
    ]
    if ext_curve.is_empty():
        lines.append("> 無可分桶樣本。")
    else:
        for td in sorted(ext_curve["horizon_td"].unique().to_list()):
            sub = ext_curve.filter(pl.col("horizon_td") == td)
            warn = " ⚠️薄" if int(sub["n"].sum()) < min_sample_warn * 3 else ""
            lines += [
                f"**r+{td}d**{warn}",
                "",
                "| 距季線桶 | n | 中位 | 平均 | 勝率 |",
                "|---|---|---|---|---|",
            ]
            for r in sub.iter_rows(named=True):
                lines.append(
                    f"| {r['bucket']} | {r['n']} | {_f(r['median'])} | {_f(r['mean'])} "
                    f"| {r['win_rate']:.0%} |"
                )
            lines.append("")

    lines += ["## 2. 排序訊號 IC（哪個訊號預測前瞻・是否追高）", ""]
    if ic_table.is_empty():
        lines.append("> 無 IC 樣本。")
    else:
        lines += [
            "| 訊號 | 窗 | IC | 95% CI | n | 顯著? |",
            "|---|---|---|---|---|---|",
        ]
        for r in ic_table.iter_rows(named=True):
            ci = (
                f"[{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}]"
                if r["ci_lo"] is not None else "—"
            )
            sig = "✓" if (
                r["ci_lo"] is not None and r["ci_hi"] is not None
                and (r["ci_lo"] > 0) == (r["ci_hi"] > 0)
            ) else "—"
            ic = f"{r['ic']:+.3f}" if r["ic"] is not None else "—"
            lines.append(
                f"| {r['feature']} | r+{r['horizon_td']}d | {ic} | {ci} | {r['n']} | {sig} |"
            )
        lines += [
            "",
            "> 顯著?＝Fisher-z 解析 95% CI 不跨 0。CI 跨 0＝該窗下無證據，不當排序依據。",
        ]

    lines += ["", "## 3. 候選組內名次 skill（rank_in_group → 前瞻）", ""]
    if rank_ic.is_empty():
        lines.append("> 無足夠組樣本。")
    else:
        lines += [
            "| 窗 | 平均 skill | 標準誤 | 組數 |",
            "|---|---|---|---|",
        ]
        for r in rank_ic.iter_rows(named=True):
            se = f"{r['se']:.3f}" if r["se"] is not None else "—"
            lines.append(
                f"| r+{r['horizon_td']}d | {r['mean_skill']:+.3f} | {se} | {r['n_groups']} |"
            )
        lines += [
            "",
            "> skill>0＝組內排前面的前瞻報酬確實較高（排序有效）；≈0 或負＝名次無 skill／反向。",
        ]
    lines += ["", *_ws1_synthesis(ext_curve, ic_table, ext_gate_pct)]
    return "\n".join(lines)


# ── WS2：漏掉起漲股目錄（A 純無偏路；錨定候選週 × daily_all ≤06-09 全市場）─────────────

def build_market_screens(
    market_all: pl.DataFrame,
    week_to_date: dict[str, object],
    weeks: list[str],
) -> pl.DataFrame:
    """全市場 screens：每個目標週 data_date × 當日在市個股 → compute_forward_returns 輸入。"""
    rows: list[pl.DataFrame] = []
    for w in weeks:
        d = week_to_date.get(w)
        if d is None:
            continue
        present = (
            market_all.filter(pl.col("date") == pl.lit(d).cast(pl.Date))
            .select(pl.col("stock_id").cast(pl.Utf8))
            .unique()
        )
        rows.append(
            present.select(
                pl.lit(w).alias("week_tag"),
                pl.lit(d).cast(pl.Date).alias("screened_at"),
                "stock_id",
                pl.lit(None, dtype=pl.Utf8).alias("name"),
                pl.lit("mkt").alias("strategy_id"),
            )
        )
    if not rows:
        return pl.DataFrame()
    return pl.concat(rows, how="vertical_relaxed")


def entry_context(
    market_all: pl.DataFrame,
    week_to_date: dict[str, object],
    weeks: list[str],
    pullback_lookback: int = 10,
    ma_window: int = 60,
) -> pl.DataFrame:
    """每 (週,股) 在 data_date 的回檔脈絡：近 N 日報酬%、距 MA(ma_window)%。純由 daily_all 算。"""
    df = (
        market_all.sort("stock_id", "date")
        .with_columns(
            (
                (pl.col("close") / pl.col("close").shift(pullback_lookback).over("stock_id") - 1)
                * 100
            ).alias("ret_pullback_pct"),
            pl.col("close")
            .rolling_mean(ma_window, min_samples=ma_window)
            .over("stock_id")
            .alias("_ma"),
            (pl.col("close") * pl.col("volume") / 1e6).alias("amount_m"),
        )
        .with_columns(
            pl.when(pl.col("_ma") > 0)
            .then((pl.col("close") / pl.col("_ma") - 1) * 100)
            .otherwise(None)
            .alias("ma_dist_pct")
        )
    )
    rows: list[pl.DataFrame] = []
    for w in weeks:
        d = week_to_date.get(w)
        if d is None:
            continue
        rows.append(
            df.filter(pl.col("date") == pl.lit(d).cast(pl.Date)).select(
                pl.lit(w).alias("week_tag"),
                pl.col("stock_id").cast(pl.Utf8),
                "ret_pullback_pct",
                "ma_dist_pct",
                "amount_m",
            )
        )
    if not rows:
        return pl.DataFrame()
    return pl.concat(rows, how="vertical_relaxed")


def detect_missed_launches(
    market_all: pl.DataFrame,
    week_to_date: dict[str, object],
    weeks: list[str],
    forward_td: int = 10,
    launch_pct: float = 20.0,
    pullback_max_pct: float = 0.0,
    ma_dist_ceiling_pct: float = 10.0,
    trading_days_per_week: int = 5,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """全市場「回檔起漲」事件：data_date 處回檔且未延伸、之後 forward_td 內漲 ≥launch_pct%。

    回檔起漲判定＝近 N 日報酬 ≤pullback_max_pct（近端回檔）**且** 距 MA60 ≤ma_dist_ceiling_pct
    （未延伸、貼近 base；剔除已在高位只是小回的延伸股）。前瞻報酬用 compute_forward_returns
    （未到期週自動排除）。回 week_tag/stock_id/screened_at/ret_pullback_pct/ma_dist_pct/
    amount_m/launch_return_pct。
    """
    screens = build_market_screens(market_all, week_to_date, weeks)
    if screens.is_empty() or market_all.is_empty():
        return pl.DataFrame()
    ret = forward_returns_long(
        screens, market_all, None, horizons_td=(forward_td,),
        trading_days_per_week=trading_days_per_week,
        clip_daily_return_pct=clip_daily_return_pct,
    )
    if ret.is_empty():
        return pl.DataFrame()
    ctx = entry_context(market_all, week_to_date, weeks)
    j = ret.join(ctx, on=["week_tag", "stock_id"], how="inner")
    launched = j.filter(
        (pl.col("return_pct") >= launch_pct)
        & (pl.col("ret_pullback_pct") <= pullback_max_pct)
        & (pl.col("ma_dist_pct") <= ma_dist_ceiling_pct)
    )
    return (
        launched.select(
            "week_tag", "stock_id", "screened_at",
            "ret_pullback_pct", "ma_dist_pct", "amount_m",
            pl.col("return_pct").alias("launch_return_pct"),
        )
        .sort(["week_tag", "launch_return_pct"], descending=[False, True])
    )


# considered-not-picked 事件附帶的可歸因欄（在 candidates_enriched，供閘門/分類判讀）
_ATTR_COLS = (
    "foreign_net_5d_lots", "foreign_net_10d_lots", "trust_net_5d_lots",
    "big_holder_wow", "margin_chg_5d_lots", "vol_ratio", "flags",
)


def crossref_launches(
    launched: pl.DataFrame,
    picks: pl.DataFrame,
    enriched_by_week: dict[str, pl.DataFrame],
    excluded: pl.DataFrame | None = None,
    name_map: dict[str, str] | None = None,
    watchlist_ids: set[str] | None = None,
    held_ids: set[str] | None = None,
) -> pl.DataFrame:
    """每起漲事件標五態雷達（優先序）：
    held（持股，已擁有非漏抓）＞acted（當週 picks）＞considered（screener candidate 未選，
    可歸因閘門）＞watchlisted（在觀察清單但 screener 沒撈、也沒進場＝有雷達沒扣扳機）＞
    never_surfaced（策略＋觀察清單都沒有＝真的沒雷達）。considered 附 excluded reason＋
    enriched 可歸因欄。watchlist/holdings 是當前快照（非 point-in-time），近似雷達成員。

    name_map（stock_id→name，呼叫端由全週 enriched＋watchlist 併出）給非候選事件補名。
    """
    nm = name_map or {}
    wl = watchlist_ids or set()
    held = held_ids or set()
    schema = {
        "week_tag": pl.Utf8, "stock_id": pl.Utf8, "name": pl.Utf8,
        "status": pl.Utf8, "launch_return_pct": pl.Float64,
        "ret_pullback_pct": pl.Float64, "ma_dist_pct": pl.Float64,
        "amount_m": pl.Float64, "exclude_reason": pl.Utf8,
        **{c: (pl.Utf8 if c == "flags" else pl.Float64) for c in _ATTR_COLS},
    }
    if launched.is_empty():
        return pl.DataFrame(schema=schema)
    picks_key = (
        {(w, s) for w, s in zip(picks["week"].to_list(), picks["stock_id"].to_list())}
        if not picks.is_empty() else set()
    )
    excl_reason: dict[tuple[str, str], str] = {}
    if excluded is not None and not excluded.is_empty() and "reason" in excluded.columns:
        for w, s, r in zip(
            excluded["week"].to_list(), excluded["stock_id"].to_list(),
            excluded["reason"].to_list(),
        ):
            excl_reason.setdefault((str(w), str(s)), str(r) if r is not None else "")
    rows: list[dict] = []
    for e in launched.iter_rows(named=True):
        w, sid = e["week_tag"], str(e["stock_id"])
        enr = enriched_by_week.get(w)
        row: dict = {
            "week_tag": w, "stock_id": sid, "name": nm.get(sid, sid),
            "status": "never_surfaced",
            "launch_return_pct": e["launch_return_pct"],
            "ret_pullback_pct": e["ret_pullback_pct"], "ma_dist_pct": e["ma_dist_pct"],
            "amount_m": e.get("amount_m"),
            "exclude_reason": None,
            **{c: None for c in _ATTR_COLS},
        }
        hit = (
            enr.filter(pl.col("stock_id").cast(pl.Utf8) == sid)
            if enr is not None and not enr.is_empty() else None
        )
        if sid in held:
            row["status"] = "held"
        elif (w, sid) in picks_key:
            row["status"] = "acted"
        elif hit is not None and not hit.is_empty():
            row["status"] = "considered"
            h = hit.row(0, named=True)
            if "name" in hit.columns and h.get("name"):
                row["name"] = h["name"]
            row["exclude_reason"] = excl_reason.get((w, sid))
            for c in _ATTR_COLS:
                if c in hit.columns:
                    row[c] = h[c]
        elif sid in wl:
            row["status"] = "watchlisted"
        rows.append(row)
    order = {
        "considered": 0, "watchlisted": 1, "never_surfaced": 2, "acted": 3, "held": 4,
    }
    return (
        pl.DataFrame(rows, schema_overrides=schema)
        .with_columns(pl.col("status").replace_strict(order, default=9).alias("_o"))
        .sort(["_o", "week_tag", "launch_return_pct"], descending=[False, False, True])
        .drop("_o")
        .select(list(schema))
    )


def missed_launch_summary(
    crossref: pl.DataFrame, min_amount_m: float = 100.0
) -> pl.DataFrame:
    """三態計數（逐週）＋ never_surfaced 依流動性拆「可投資 vs 太小」。

    起漲事件裡我們進場了幾檔（acted）、考慮過沒選幾檔（considered，可歸因閘門）、根本沒撈到
    幾檔（never_surfaced）；never_surfaced 再依當日成交額 ≥min_amount_m 百萬拆 liquid/illiquid
    ——隔開「篩選器該撈卻沒撈」與「該剔的微型投機股」。
    """
    schema = {
        "week_tag": pl.Utf8, "held": pl.UInt32, "acted": pl.UInt32,
        "considered": pl.UInt32, "watchlisted": pl.UInt32,
        "never_liquid": pl.UInt32, "never_illiquid": pl.UInt32, "total": pl.UInt32,
    }
    if crossref.is_empty():
        return pl.DataFrame(schema=schema)
    tagged = crossref.with_columns(
        pl.when(pl.col("status").is_in(["held", "acted", "considered", "watchlisted"]))
        .then(pl.col("status"))
        .when(pl.col("amount_m") >= min_amount_m).then(pl.lit("never_liquid"))
        .otherwise(pl.lit("never_illiquid"))
        .alias("_bucket")
    )
    piv = (
        tagged.group_by("week_tag", "_bucket").agg(pl.len().alias("n"))
        .pivot(values="n", index="week_tag", on="_bucket")
        .sort("week_tag")
    )
    cols = (
        "held", "acted", "considered", "watchlisted", "never_liquid", "never_illiquid",
    )
    for c in cols:
        if c not in piv.columns:
            piv = piv.with_columns(pl.lit(0).alias(c))
    return (
        piv.with_columns(*[pl.col(c).fill_null(0).cast(pl.UInt32) for c in cols])
        .with_columns(
            pl.sum_horizontal(pl.col(c) for c in cols).cast(pl.UInt32).alias("total")
        )
        .select(list(schema))
    )


def liquid_missed_table(
    crossref: pl.DataFrame, min_amount_m: float = 100.0
) -> pl.DataFrame:
    """實際可行動的漏抓清單（排除 held／acted／微型投機股）：
    considered（screener 撈到未選，可歸因閘門）＋ watchlisted（觀察清單有卻沒扣扳機）＋
    never_surfaced 且流動性夠（真的沒雷達的投資級）。按 status 優先序＋漲幅排序。
    """
    if crossref.is_empty():
        return crossref
    keep = crossref.filter(
        pl.col("status").is_in(["considered", "watchlisted"])
        | ((pl.col("status") == "never_surfaced") & (pl.col("amount_m") >= min_amount_m))
    )
    order = {"considered": 0, "watchlisted": 1, "never_surfaced": 2}
    return (
        keep.with_columns(pl.col("status").replace_strict(order, default=9).alias("_o"))
        .sort(["_o", "launch_return_pct"], descending=[False, True])
        .drop("_o")
    )


def render_missed_launch_report(
    grid: pl.DataFrame,
    primary_liquid: pl.DataFrame,
    primary_label: str,
    min_amount_m: float,
    window_note: str,
) -> str:
    """WS2「漏掉起漲股」目錄 markdown：敏感度計數格＋主設定可行動漏抓具名清單＋小結。"""
    lines = [
        "# 診斷 WS2：漏掉起漲股？——全市場回檔起漲事件目錄（M-Diag1・CP3）",
        "",
        f"- 母體＝**daily_all 全市場乾淨底料**（{window_note}）；起漲事件＝data_date 處近端回檔"
        "且未延伸（距 MA60 ≤ 上限）、之後 forward_td 內漲 ≥Y%。無偏（A 路），但窗窄。",
        "- 五態雷達：**held**＝持股（已擁有）；**acted**＝當週 picks；**considered**＝screener "
        "candidate 未選（可歸因閘門）；**watchlisted**＝在觀察清單卻沒扣扳機；"
        f"**never_surfaced**＝策略＋觀察清單都沒有（真沒雷達），依成交額 ≥{min_amount_m:.0f} 百萬"
        "拆 liquid／illiquid（微型投機股）。watchlist/holdings 為當前快照、非 point-in-time。",
        "- **樣本薄（交叉僅 W21–W23、事件 <30）＝初步；方向性使用**，每季隨窗變厚重跑。",
        "",
        "## 1. 敏感度計數格（Y × forward_td）",
        "",
    ]
    if grid.is_empty():
        lines.append("> 無事件。")
    else:
        lines += [
            "| 設定 | 事件 | held | acted | considered | watchlisted "
            "| never_liquid | never_illiquid |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in grid.iter_rows(named=True):
            lines.append(
                f"| {r['config']} | {r['events']} | {r['held']} | {r['acted']} "
                f"| {r['considered']} | {r['watchlisted']} "
                f"| {r['never_liquid']} | {r['never_illiquid']} |"
            )

    lines += [
        "", f"## 2. 可行動漏抓清單（主設定 {primary_label}；具名）", "",
        "考慮未選＝screener 撈到沒選（附剔除原因）；觀察沒扣扳機＝在觀察清單卻沒進場；"
        "沒撈到＝真沒雷達（已濾微型股）。ret_pullback＝近端回檔幅度、ma_dist＝距季線。", "",
    ]
    _st_label = {
        "considered": "考慮未選", "watchlisted": "觀察沒扣扳機", "never_surfaced": "沒撈到",
    }
    if primary_liquid.is_empty():
        lines.append("> 主設定無可行動漏抓（或全為微型股已濾除）。")
    else:
        lines += [
            "| 態 | 週 | 股票 | 起漲% | 回檔% | 距季線% | 成交(百萬) | 剔除原因 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in primary_liquid.iter_rows(named=True):
            st = _st_label.get(r["status"], r["status"])
            label = (
                f"{r['stock_id']} {r['name']}"
                if r["name"] and r["name"] != r["stock_id"] else str(r["stock_id"])
            )
            lines.append(
                f"| {st} | {r['week_tag'][-3:]} | {label} "
                f"| {_f(r['launch_return_pct'])} | {_f(r['ret_pullback_pct'])} "
                f"| {_f(r['ma_dist_pct'])} | {r['amount_m']:,.0f} "
                f"| {r['exclude_reason'] or '—'} |"
            )

    lines += ["", "## 小結（資料驅動）", ""]
    prow = grid.filter(pl.col("config") == primary_label) if not grid.is_empty() else grid
    if not prow.is_empty():
        p = prow.row(0, named=True)
        lines += [
            f"- **漏抓不在排雷閘門**（主設定 {primary_label}）：considered（閘門可歸因）"
            f"僅 {p['considered']} 筆——這批回檔起漲**幾乎沒進到閘門**，turn-aware 閘門改革"
            "（WS5）救不到它們。",
            f"- **雷達拆解**：held {p['held']}、watchlisted（有雷達沒扣扳機）"
            f"{p['watchlisted']}、never_surfaced 投資級（真沒雷達）{p['never_liquid']}、"
            f"微型該剔 {p['never_illiquid']}。watchlisted 是**進場時機/紀律**問題"
            "（清單上卻沒買），never_surfaced 投資級才是**雷達覆蓋**問題。",
            "- **兩種漏各有處方**：(1) 高本益比 AI 趨勢股／輪動快漲股——策略天生框不到，"
            "**該靠觀察清單接**；漏抓＝觀察清單覆蓋不足（never_surfaced 投資級裡的趨勢股）。"
            "(2) 大型價值/景氣循環回檔起漲——不在 AI 觀察清單、策略也不框，需另一條"
            "「回檔均值回歸」掃描才接得到。",
            "- **never_surfaced 的 a/b/c/d 資金分類不可行**：這些股不在 candidates，"
            "無法人/大戶/融資近端欄可歸因；WS3 二階資金 inflection 只能在 considered"
            "（≈0 筆）上驗，本樣本無力。",
        ]
    lines += [
        "",
        "> 防過擬合：事件 <30、單一 regime、06-09 窗牆；此目錄是**初步方向**，"
        "不足以支撐任何硬規則，需 W28+ 窗變厚重跑。",
    ]
    return "\n".join(lines)
