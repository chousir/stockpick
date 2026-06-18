"""個股版起漲事件回測（CP 值補漲研究 B2，docs/13-cp-value-research.md §4 Phase B）。

把 docs/12 R2 的 event-study harness 下沉到個股層：對每檔個股偵測起漲事件
（三種 label），再拿 B1 個股特徵面板（analysis/stock_panel.py）的因子當訊號掃描，
產「每 label 一張 lift 表」供裁決哪組因子最有領先力。

方法底線（守 docs/13 §1.2 / Part 3 人設，誠實 > 樣本數）：
- **episode 純價格定義**（使用者 2026-06-14 拍板）：事件只由「前置價格情境 ＋ 前瞻
  價格報酬」界定；資金/加速度因子一律留在**訊號端**被掃描。避免「label 含資金條件
  → 資金訊號 lift 被灌水」的循環，量到的領先力才誠實。
- 三 label 的前置情境只決定「在哪看」（貼低 / 剛離低 / 深跌），前瞻報酬決定是否成事件：
    L1 埋伏  ：距 M 日低 ≤ tol% → 前瞻 N 日漲 ≥ X%（抓起漲前，價貼低）
    L2 追突破：距 M 日低落在 [lo, hi]% 帶 → 前瞻 N 日漲 ≥ X%（抓起漲初）
    L3 反轉  ：距 L 日高 ≤ −drop% → 前瞻 N 日反彈 ≥ X%（抓 V 底，選配）
- 掃描沿用 R2：向上穿越觸發、命中＝觸發後 lead_window 內有事件、報 lift/recall/領先。
  隨機基率以**全上市宇宙**一次算好（rotation_calib.compute_base_rate），對所有訊號一致，
  避免廣訊號因觸發到無事件股而灌大 lift。
- 統計力但書由呼叫端寫進報告：個股事件比族群更稀疏、L3 又更稀少，lift 1.3 與 1.5 之差
  可能只是雜訊；現實天花板大概 1.3–1.5（疊高勝率、非預測）。
"""

from __future__ import annotations

import re

import polars as pl

from tw_screener.backtest.rotation_calib import compute_base_rate, evaluate_triggers

_EPISODE_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "start_date": pl.Date,
    "base_close": pl.Float64,
    "fwd_return_pct": pl.Float64,
}

# 資金 z 欄字首 → 其對應加速度欄（unsuffixed＝短窗值，B1 stock_panel.py）
_MOM_BY_PREFIX: dict[str, str] = {
    "net_flow": "flow_momentum",
    "foreign_flow": "foreign_momentum",
    "trust_flow": "trust_momentum",
}
_MOMENTUM_SIGNALS: tuple[str, ...] = ("flow_momentum", "foreign_momentum", "trust_momentum")
# 多窗資金 z 欄名（M-MH Phase D：net_flow_3d_z / foreign_flow_10d_z…）
_FLOW_Z_RE = re.compile(r"^(net_flow|foreign_flow|trust_flow)_(\d+)d_z$")


def _flow_z_cols(cols: set[str]) -> list[tuple[str, str, int]]:
    """從面板欄推導 (z 欄, 對應加速度欄, 視窗天數)，多窗一般化（M-MH Phase 2）。

    取代舊寫死 5d/20d 清單：哪幾窗有欄就掃哪幾窗。依 (prefix, window) 穩定排序。
    """
    out: list[tuple[str, str, int]] = []
    for c in cols:
        m = _FLOW_Z_RE.match(c)
        if m:
            out.append((c, _MOM_BY_PREFIX[m.group(1)], int(m.group(2))))
    return sorted(out, key=lambda t: (t[0].split("_")[0], t[2]))


def _scan_episodes(
    priced: pl.DataFrame, x_pct: float, n_days: int, cooldown_days: int
) -> pl.DataFrame:
    """共用核心：每檔逐日找「合格情境日（_ctx）且前瞻 n_days 內漲 ≥ x_pct」的首日。

    priced 須含 stock_id / date / close / _ctx(bool)，且已去重、drop_null close、
    依 (stock_id, date) 排序。連續合格取首日，命中後 cooldown_days 內不重複計。
    """
    if priced.is_empty():
        return pl.DataFrame(schema=_EPISODE_SCHEMA)
    rows: list[dict] = []
    for sub_df in priced.partition_by("stock_id", maintain_order=True):
        sid = sub_df["stock_id"][0]
        close = sub_df["close"].to_list()
        dates = sub_df["date"].to_list()
        ctx = sub_df["_ctx"].to_list()
        n = len(close)
        i = 0
        while i < n - 1:
            if ctx[i]:
                fwd_max = max(close[i + 1 : min(i + 1 + n_days, n)])
                fwd_ret = fwd_max / close[i] - 1.0
                if fwd_ret >= x_pct / 100:
                    rows.append(
                        {
                            "stock_id": sid,
                            "start_date": dates[i],
                            "base_close": close[i],
                            "fwd_return_pct": fwd_ret * 100,
                        }
                    )
                    i += cooldown_days + 1
                    continue
            i += 1
    if not rows:
        return pl.DataFrame(schema=_EPISODE_SCHEMA)
    return pl.DataFrame(rows, schema=_EPISODE_SCHEMA).sort(["stock_id", "start_date"])


def _prep(price_history: pl.DataFrame, ctx: pl.Expr) -> pl.DataFrame:
    """價格去重、drop_null close、排序後加情境布林欄 _ctx（null → False）。"""
    if price_history.is_empty():
        return price_history  # _scan_episodes 的空判斷會回 episode schema
    return (
        price_history.select(["date", "stock_id", "close"])
        .unique(subset=["date", "stock_id"], keep="first")
        .drop_nulls("close")
        .sort(["stock_id", "date"])
        .with_columns(ctx.fill_null(False).alias("_ctx"))
    )


def detect_ambush_episodes(
    price_history: pl.DataFrame,
    m_days: int = 60,
    tol_pct: float = 5.0,
    x_pct: float = 15.0,
    n_days: int = 20,
    cooldown_days: int = 20,
) -> pl.DataFrame:
    """L1 埋伏：當日收盤 ≤ 近 m_days 低 ×(1+tol%) → 前瞻 n_days 漲 ≥ x_pct%。"""
    low = pl.col("close").rolling_min(m_days, min_samples=m_days).over("stock_id")
    ctx = pl.col("close") <= low * (1 + tol_pct / 100)
    return _scan_episodes(_prep(price_history, ctx), x_pct, n_days, cooldown_days)


def detect_breakout_episodes(
    price_history: pl.DataFrame,
    m_days: int = 60,
    lo_pct: float = 3.0,
    hi_pct: float = 8.0,
    x_pct: float = 12.0,
    n_days: int = 10,
    cooldown_days: int = 15,
) -> pl.DataFrame:
    """L2 追突破：距 m_days 低落在 [lo, hi]% 帶（剛離低）→ 前瞻 n_days 漲 ≥ x_pct%。"""
    low = pl.col("close").rolling_min(m_days, min_samples=m_days).over("stock_id")
    above = (pl.col("close") / low - 1) * 100
    ctx = (above >= lo_pct) & (above <= hi_pct)
    return _scan_episodes(_prep(price_history, ctx), x_pct, n_days, cooldown_days)


def detect_reversal_episodes(
    price_history: pl.DataFrame,
    l_days: int = 60,
    drawdown_pct: float = 20.0,
    x_pct: float = 15.0,
    n_days: int = 15,
    cooldown_days: int = 15,
) -> pl.DataFrame:
    """L3 超跌反轉（選配）：距 l_days 高 ≤ −drawdown% → 前瞻 n_days 反彈 ≥ x_pct%。"""
    high = pl.col("close").rolling_max(l_days, min_samples=l_days).over("stock_id")
    drawdown = (pl.col("close") / high - 1) * 100
    ctx = drawdown <= -drawdown_pct
    return _scan_episodes(_prep(price_history, ctx), x_pct, n_days, cooldown_days)


def _position_low_col(panel: pl.DataFrame) -> str | None:
    """panel 的「距 N 日低 %」欄名（B1 隨 position_window 命名，如 above_low_60d_pct）。"""
    cols = [c for c in panel.columns if c.startswith("above_low_") and c.endswith("d_pct")]
    return cols[0] if cols else None


def _build_signal_jobs(
    panel: pl.DataFrame,
    z_thresholds: tuple[float, ...],
    volume_thresholds: tuple[float, ...],
    position_low_pct: float,
    early_gate: dict | None = None,
) -> list[tuple[str, list[pl.Expr]]]:
    """組（訊號名, AND 條件式串）清單。只納入 panel 實際存在的欄。

    early_gate（M-MH Phase 2）：非 None 時，對「短窗（< 最長窗）」資金 z 加早偵測閘變體
    （減量＝把高觸發的短窗壓成高精度早訊號）：
      +early   ＝短窗 z 高 ＋ flow_decel ≥ floor（買盤未減速）＋ 同 prefix 20d-z < ceiling
                 （長窗尚未追上＝舊賣單稀釋態，2501 機制）
      +early+low＝上 ＋ 價貼低
      +nodiv   ＝短窗 z 高 ＋ price_flow_div_{w}d ≤ ceiling（價未先噴、資金領先價）
    """
    cols = set(panel.columns)
    low_col = _position_low_col(panel)
    flow_cols = _flow_z_cols(cols)
    long_window = max((w for _, _, w in flow_cols), default=0)
    jobs: list[tuple[str, list[pl.Expr]]] = []

    for col, mom_col, w in flow_cols:
        for t in z_thresholds:
            jobs.append((f"{col} (z>{t})", [pl.col(col) > t]))
            if mom_col in cols:  # +mom：資金 z 且加速中（A2 新鮮度濾鏡）
                jobs.append(
                    (f"{col} (z>{t}) +mom", [pl.col(col) > t, pl.col(mom_col) > 0])
                )
            if low_col is not None:  # +low：資金 z 且價貼低（CP 補漲核心假說）
                jobs.append(
                    (
                        f"{col} (z>{t}) +low≤{position_low_pct:g}",
                        [pl.col(col) > t, pl.col(low_col) <= position_low_pct],
                    )
                )
            if early_gate is not None and w < long_window:
                prefix = col[: -len(f"_{w}d_z")]
                long_z = f"{prefix}_{long_window}d_z"
                div_col = f"price_flow_div_{w}d"
                ceiling = float(early_gate.get("long_z_ceiling", 0.5))
                decel_floor = float(early_gate.get("decel_floor", 0.0))
                if long_z in cols and "flow_decel" in cols:
                    early = [
                        pl.col(col) > t,
                        pl.col("flow_decel") >= decel_floor,
                        pl.col(long_z) < ceiling,
                    ]
                    jobs.append((f"{col} (z>{t}) +early", early))
                    if low_col is not None:
                        jobs.append(
                            (
                                f"{col} (z>{t}) +early+low",
                                early + [pl.col(low_col) <= position_low_pct],
                            )
                        )
                if div_col in cols:
                    div_ceiling = float(early_gate.get("div_ceiling", 0.0))
                    jobs.append(
                        (
                            f"{col} (z>{t}) +nodiv",
                            [pl.col(col) > t, pl.col(div_col) <= div_ceiling],
                        )
                    )

    for mom_col in _MOMENTUM_SIGNALS:  # 純加速度基準（A2：單獨 ≈ 隨機，列為對照）
        if mom_col in cols:
            jobs.append((f"{mom_col} (>0)", [pl.col(mom_col) > 0]))

    if "volume_z_5d" in cols:
        for t in volume_thresholds:
            jobs.append((f"volume_z_5d (z>{t})", [pl.col("volume_z_5d") > t]))

    return jobs


def _stock_triggers(panel: pl.DataFrame, conditions: list[pl.Expr]) -> pl.DataFrame:
    """AND 條件向上穿越（昨日未達、今日達）→ (stock_id, date)。"""
    cond = conditions[0]
    for c in conditions[1:]:
        cond = cond & c
    df = (
        panel.sort(["stock_id", "date"])
        .with_columns(cond.fill_null(False).alias("_hit"))
        .with_columns(
            (pl.col("_hit") & ~pl.col("_hit").shift(1, fill_value=False).over("stock_id")).alias(
                "_trigger"
            )
        )
    )
    return df.filter(pl.col("_trigger")).select(["stock_id", "date"])


def scan_stock_signals(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    z_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0),
    volume_thresholds: tuple[float, ...] = (1.0, 1.5, 2.0),
    position_low_pct: float = 15.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
    early_gate: dict | None = None,
) -> pl.DataFrame:
    """掃描（panel 訊號 × 門檻）對 episodes 的命中統計表（docs/13 B2）。

    隨機基率以**全上市宇宙**一次算好（compute_base_rate），對所有訊號一致。
    early_gate 非 None 時加多窗早偵測閘變體（M-MH Phase 2，見 _build_signal_jobs）。
    回傳含 signal/n_triggers/hits/hit_rate/recall/base_rate/lift/median_lead_days/f1，
    依 f1 遞減排序。空輸入回空表。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    calendar = sorted(panel["date"].unique().to_list())
    warmup_pos = min(z_min_periods, len(calendar) - 1)
    base_rate = compute_base_rate(
        episodes,
        calendar,
        panel["stock_id"].unique().to_list(),
        lead_window,
        occupy_days,
        warmup_pos,
        key_col="stock_id",
    )

    rows = []
    for label, conditions in _build_signal_jobs(
        panel, z_thresholds, volume_thresholds, position_low_pct, early_gate
    ):
        triggers = _stock_triggers(panel, conditions)
        stats = evaluate_triggers(
            triggers,
            episodes,
            calendar,
            lead_window,
            occupy_days,
            warmup_pos,
            key_col="stock_id",
            base_rate=base_rate,
        )
        rows.append({"signal": label, **stats})
    out = pl.DataFrame(rows)
    return out.with_columns(
        pl.when((pl.col("hit_rate") + pl.col("recall")) > 0)
        .then(2 * pl.col("hit_rate") * pl.col("recall") / (pl.col("hit_rate") + pl.col("recall")))
        .otherwise(0.0)
        .alias("f1")
    ).sort("f1", descending=True)


def compute_cross_window_lead(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    threshold: float = 1.0,
    lookback: int = 30,
    min_lead_days: int = 2,
) -> pl.DataFrame:
    """跨窗配對領先（M-MH Phase 2 GATE 核心）——直接驗「短窗 z 早於 20d-z 達標」假說。

    對每個起漲事件、每個 prefix（外資/投信/合計），量「事件前 lookback 交易日內，短窗 z
    首次 ≥ threshold 的日」對上「同 prefix 最長窗(20d) z 首次 ≥ threshold 的日」的距離：
      領先 = 長窗首達索引 − 短窗首達索引（>0＝短窗較早偵測到同一次起漲）。
    這量的是「短窗 vs 長窗」的相對領先（2501 機制），**非** scan 的「觸發 vs 事件」領先
    （後者兩窗皆 ~7-8 日、由價貼低主導、答不出 GATE 想問的問題）。

    回傳每短窗訊號一列：n_short_fired（短窗達標的事件數）/n_paired（短窗、長窗皆達標可算
    領先）/short_only（短窗達標但 20d 始終未達＝長窗被舊賣單稀釋、最強領先）/median_lead_days
    /pct_short_leads（領先 >0 占比）/pct_lead_ge（領先 ≥ min_lead_days 占比）。空輸入回空表。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    flow_cols = _flow_z_cols(set(panel.columns))
    long_window = max((w for _, _, w in flow_cols), default=0)
    if long_window == 0:
        return pl.DataFrame()
    calendar = sorted(panel["date"].unique().to_list())
    pos = {d: i for i, d in enumerate(calendar)}
    ep_pairs = [
        (sid, pos[sd])
        for sid, sd in episodes.select(["stock_id", "start_date"]).iter_rows()
        if sd in pos
    ]

    rows: list[dict] = []
    for col, _mom, w in flow_cols:
        if w >= long_window:
            continue
        prefix = col[: -len(f"_{w}d_z")]
        long_col = f"{prefix}_{long_window}d_z"
        if long_col not in panel.columns:
            continue
        by_stock: dict[str, list[tuple[int, float | None, float | None]]] = {}
        for sid, d, sz, lz in (
            panel.select(["stock_id", "date", col, long_col]).sort(["stock_id", "date"]).iter_rows()
        ):
            by_stock.setdefault(sid, []).append((pos[d], sz, lz))

        leads: list[int] = []
        short_only = 0
        n_short_fired = 0
        for sid, ep_pos in ep_pairs:
            recs = by_stock.get(sid)
            if not recs:
                continue
            lo = ep_pos - lookback
            win = [r for r in recs if lo <= r[0] < ep_pos]
            t_short = next((p for p, sz, _ in win if sz is not None and sz >= threshold), None)
            if t_short is None:
                continue
            n_short_fired += 1
            t_long = next((p for p, _, lz in win if lz is not None and lz >= threshold), None)
            if t_long is None:
                short_only += 1
            else:
                leads.append(t_long - t_short)
        if n_short_fired == 0:
            continue
        n_paired = len(leads)
        leads.sort()
        rows.append(
            {
                "short_signal": col,
                "long_signal": long_col,
                "n_short_fired": n_short_fired,
                "n_paired": n_paired,
                "short_only": short_only,
                "median_lead_days": leads[n_paired // 2] if leads else None,
                "pct_short_leads": (sum(x > 0 for x in leads) / n_paired) if n_paired else 0.0,
                "pct_lead_ge": (
                    (sum(x >= min_lead_days for x in leads) / n_paired) if n_paired else 0.0
                ),
            }
        )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("median_lead_days", descending=True, nulls_last=True)


def render_cross_window_lead(
    lead_df: pl.DataFrame, threshold: float, min_lead_days: int
) -> list[str]:
    """跨窗配對領先表的 markdown 行（M-MH Phase 2）。空表回誠實佔位。"""
    if lead_df.is_empty():
        return [
            "",
            "## 跨窗配對領先（短窗 z 是否早於 20d-z 達標）",
            "",
            "（無可配對事件——資料不足或面板無短窗 z 欄）",
            "",
        ]
    lines = [
        "",
        f"## 跨窗配對領先（z≥{threshold:g}；領先 = 20d 首達日 − 短窗首達日，>0＝短窗較早）",
        "",
        "| 短窗訊號 | 短窗達標 | 可配對 | 僅短窗達標 | 中位領先(日) | 短窗較早% | "
        f"領先≥{min_lead_days}日% |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in lead_df.iter_rows(named=True):
        ml = str(r["median_lead_days"]) if r["median_lead_days"] is not None else "—"
        lines.append(
            f"| {r['short_signal']} | {r['n_short_fired']} | {r['n_paired']} | {r['short_only']} "
            f"| {ml} | {r['pct_short_leads']:.0%} | {r['pct_lead_ge']:.0%} |"
        )
    lines += [
        "",
        "> 「僅短窗達標」＝事件前 lookback 內短窗 z 達標但 20d-z 始終未達＝長窗被舊賣單稀釋、"
        "短窗最強領先的證據。",
        "",
    ]
    return lines


def render_cp_calibration_report(
    scan: pl.DataFrame,
    episodes: pl.DataFrame,
    label_name: str,
    label_desc: str,
    params: dict,
    coverage: dict,
    min_triggers: int = 8,
    min_lift: float = 1.3,
    top_n: int = 10,
) -> str:
    """單一 label 的校準報告 markdown（含建議因子名單與誠實統計力但書）。"""
    base_rate = scan["base_rate"][0] if not scan.is_empty() else 0.0
    lines = [
        f"# 個股起漲事件回測校準 — {label_name}",
        "",
        f"- 獵物定義：{label_desc}",
        f"- 宇宙：{coverage.get('universe', 'listed')}・"
        f"{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        f"- 法人覆蓋 {coverage.get('inst_coverage_pct', 0)}%・"
        f"次產業標記覆蓋 {coverage.get('subind_coverage_pct', 0)}%",
        f"- 前瞻 N={params['fwd_n_days']} 交易日・門檻 X={params['fwd_x_pct']}%・"
        f"冷卻 {params['cooldown_days']} 日・領先視窗 {params['lead_window']} 日",
        f"- 事件樣本：{episodes.height} 個"
        f"（{episodes['stock_id'].n_unique() if not episodes.is_empty() else 0} 檔）",
        "",
        f"## 訊號掃描（隨機基率 {base_rate:.2%}；lift = 命中率/基率）",
        "",
        "| 訊號 | 觸發 | 命中 | 誤報 | 命中率 | recall | lift | 中位領先(日) | F1 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in scan.iter_rows(named=True):
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        lead = str(r["median_lead_days"]) if r["median_lead_days"] is not None else "—"
        lines.append(
            f"| {r['signal']} | {r['n_triggers']} | {r['hits']} | {r['false_positives']} "
            f"| {r['hit_rate']:.1%} | {r['recall']:.1%} | {lift} | {lead} | {r['f1']:.3f} |"
        )

    qualified = scan.filter(
        (pl.col("n_triggers") >= min_triggers)
        & (pl.col("lift").is_not_null())
        & (pl.col("lift") >= min_lift)
    ).head(top_n)
    lines += [
        "",
        f"## 建議因子名單（觸發 ≥{min_triggers}・lift ≥{min_lift}・F1 排序前 {top_n}）",
        "",
    ]
    if qualified.is_empty():
        lines.append(
            f"（無因子同時滿足樣本數與 lift ≥{min_lift}——此 label 在現有資料下無可靠領先訊號）"
        )
    else:
        for i, r in enumerate(qualified.iter_rows(named=True), 1):
            lines.append(
                f"{i}. **{r['signal']}**：命中率 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {r['lift']:.2f}・中位領先 {r['median_lead_days']} 日"
                f"（{r['hits']}/{r['n_triggers']} 觸發命中）"
            )
    lines += [
        "",
        "---",
        "",
        "> 統計力但書：個股事件比族群更稀疏；lift 1.3 與 1.5 之差可能只是雜訊。",
        "> 現實天花板約 1.3–1.5（疊高勝率的觀察清單，非預測、非目標價）。",
        "> 量能因子＝成交量 z（非真周轉率，缺流通在外股數）；大盤基準＝等權全上市指數。",
        "> 隨資料累積每季重跑校準，不把單次回測數字當聖經。",
        "",
    ]
    return "\n".join(lines)
