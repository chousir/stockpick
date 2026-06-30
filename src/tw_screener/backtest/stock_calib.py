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

區塊導覽（全檔純研究軌，由 backtest/cp_calib_runner.py 編排；門檻/網格旋鈕在
config/research/cp_value_calib.yaml，規劃書 04 A2）：
  1. 事件偵測         detect_*_episodes（L1–L4 純價格 label）
  2. 因子訊號掃描      scan_stock_signals / scan_top_signals（z/量/+low/+early 網格 → lift）
  3. 跨窗配對領先      compute_cross_window_lead（M-MH Phase 2 GATE 核心）
  4. 校準報告輸出      render_cp_calibration_report / render_top_calibration_report
  5. B-P1 穩健度       payoff/decay/holdout/流動性硬化（docs/15 T3）
  6. B-P2 主導度單調    dom 分位 × 控制位階（docs/15 T1）
  7. B-P3 個股×族群交互  S×G 2×2（docs/15 T2）
  8. Part C 落後度      族群內落後度單調 + 冠軍 S+ 落後濾鏡（docs/16 C-P1/C-P2）
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


# ── 事件偵測：L1–L4 起漲/出貨純價格 label（前置情境 ＋ 前瞻報酬；docs/13 §4）──────────
# episode 只由價格界定，資金/加速度因子全留訊號端被掃描（避免循環、量到的領先才誠實）。
def _scan_episodes(
    priced: pl.DataFrame, x_pct: float, n_days: int, cooldown_days: int, direction: str = "up"
) -> pl.DataFrame:
    """共用核心：每檔逐日找「合格情境日（_ctx）且前瞻 n_days 內達報酬門檻」的首日。

    priced 須含 stock_id / date / close / _ctx(bool)，且已去重、drop_null close、
    依 (stock_id, date) 排序。連續合格取首日，命中後 cooldown_days 內不重複計。
    direction="up"（起漲，預設）＝前瞻最大漲幅 ≥ x_pct；"down"（頂部/出貨，M-MH 退潮校準）
    ＝前瞻最大跌幅 ≥ x_pct（fwd_return_pct 記負值，即谷底相對基準的報酬）。
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
                window = close[i + 1 : min(i + 1 + n_days, n)]
                if direction == "up":
                    fwd_ret = max(window) / close[i] - 1.0
                    hit = fwd_ret >= x_pct / 100
                else:  # down：前瞻谷底相對基準的跌幅
                    fwd_ret = min(window) / close[i] - 1.0
                    hit = fwd_ret <= -x_pct / 100
                if hit:
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
    """價格去重、drop_null close、剔非正收盤、排序後加情境布林欄 _ctx（null → False）。

    剔 close ≤ 0（停牌/髒資料）須在算 ctx 前：否則 0 會毒化 rolling_min（low=0→ctx 恆 False），
    且 _scan_episodes 以 close[i] 當前瞻報酬基準、close[i]=0 會 ZeroDivisionError。
    """
    if price_history.is_empty():
        return price_history  # _scan_episodes 的空判斷會回 episode schema
    return (
        price_history.select(["date", "stock_id", "close"])
        .unique(subset=["date", "stock_id"], keep="first")
        .drop_nulls("close")
        .filter(pl.col("close") > 0)
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


def detect_top_episodes(
    price_history: pl.DataFrame,
    m_days: int = 60,
    tol_pct: float = 8.0,
    drop_pct: float = 10.0,
    n_days: int = 10,
    cooldown_days: int = 15,
) -> pl.DataFrame:
    """L4 頂部/出貨（M-MH 退潮警示校準）：當日收盤 ≥ 近 m_days 高 ×(1−tol%)（已在區間
    高位）→ 前瞻 n_days 內谷底跌 ≥ drop%。

    與 L1 埋伏對稱（情境換成貼高、前瞻換成下跌），純價格定義——退潮/背離因子一律留在
    訊號端被 scan_top_signals 掃描，避免「label 含資金條件 → 資金訊號 lift 灌水」的循環。
    tol_pct 對齊 overheat_watch.near_high_pct（生產啟發式的「已在高位」定義）。
    """
    high = pl.col("close").rolling_max(m_days, min_samples=m_days).over("stock_id")
    ctx = pl.col("close") >= high * (1 - tol_pct / 100)
    return _scan_episodes(_prep(price_history, ctx), drop_pct, n_days, cooldown_days, "down")


# ── 因子訊號掃描：資金 z／量／+low／+early 變體網格 → lift/recall/領先（沿用 R2 穿越觸發）──
# 隨機基率以全上市宇宙一次算好（rotation_calib.compute_base_rate），對所有訊號一致。
def _position_low_col(panel: pl.DataFrame) -> str | None:
    """panel 的「距 N 日低 %」欄名（B1 隨 position_window 命名，如 above_low_60d_pct）。"""
    cols = [c for c in panel.columns if c.startswith("above_low_") and c.endswith("d_pct")]
    return cols[0] if cols else None


def _position_high_col(panel: pl.DataFrame) -> str | None:
    """panel 的「距 N 日高 %」欄名（如 above_high_60d_pct；貼高時 ≈ 0、低於高為負）。"""
    cols = [c for c in panel.columns if c.startswith("above_high_") and c.endswith("d_pct")]
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


def _build_top_signal_jobs(
    panel: pl.DataFrame,
    near_high_pct: float = 8.0,
    decel_thresholds: tuple[float, ...] = (0.0,),
    div_floor: float = 0.0,
    vol_floor: float = 0.0,
    sell_z_thresholds: tuple[float, ...] = (1.0, 1.5),
    sell_prefixes: tuple[str, ...] = ("foreign_flow", "net_flow"),
) -> list[tuple[str, list[pl.Expr]]]:
    """組頂部/出貨退潮訊號（名, AND 條件串）。只納 panel 實有欄。

    對照組讓裁決誠實：(1) 純「在區間高位」基準（貼高本身就會跌？）；(2) 退潮三因子各自
    單獨（無位階）的原始預測力；(3) 法人短窗賣超基準（高位×賣超）；(4) 生產啟發式
    （compute_overheat_warning：高位＋短窗減速＋量價背離/量縮）及其組件。啟發式要 lift>1
    才對「前瞻下跌」有預測力，且須贏過 (1)(3) 才算背離因子本身加值（非只是貼高/賣超在做工）。
    """
    cols = set(panel.columns)
    high_col = _position_high_col(panel)
    jobs: list[tuple[str, list[pl.Expr]]] = []
    nh: pl.Expr | None = None
    if high_col is not None:
        nh = pl.col(high_col) >= -near_high_pct
        jobs.append((f"near_high (≤{near_high_pct:g}%)", [nh]))

    has_decel = "flow_decel" in cols
    has_div = "price_flow_div_5d" in cols
    has_vol = "volume_z_5d" in cols
    div_expr = pl.col("price_flow_div_5d") > div_floor
    vol_expr = pl.col("volume_z_5d") < vol_floor

    # 退潮三因子各自單獨（無位階）＝原始預測力
    if has_div:
        jobs.append((f"price_flow_div_5d (>{div_floor:g})", [div_expr]))
    if has_vol:
        jobs.append((f"volume_z_5d (<{vol_floor:g})", [vol_expr]))
    if has_decel:
        for dt in decel_thresholds:
            jobs.append((f"flow_decel (<{dt:g})", [pl.col("flow_decel") < dt]))

    # 法人短窗賣超基準（賣超、賣超×高位）
    for prefix in sell_prefixes:
        zc = f"{prefix}_5d_z"
        if zc in cols:
            for st in sell_z_thresholds:
                jobs.append((f"{zc} (z<−{st:g})", [pl.col(zc) < -st]))
                if nh is not None:
                    jobs.append((f"{zc} (z<−{st:g}) +high", [pl.col(zc) < -st, nh]))

    # 生產啟發式（compute_overheat_warning 的確切規則）＋其組件，全條件於高位
    if nh is not None and has_decel:
        for dt in decel_thresholds:
            base = [nh, pl.col("flow_decel") < dt]
            tag = f"decel<{dt:g}"
            jobs.append((f"high+{tag}", base))
            if has_div:
                jobs.append((f"high+{tag}+div>{div_floor:g}", [*base, div_expr]))
            if has_vol:
                jobs.append((f"high+{tag}+vol<{vol_floor:g}", [*base, vol_expr]))
            if has_div and has_vol:  # ★ 生產 overheat_warning 規則：減速 ＋（背離｜量縮）
                jobs.append((f"★overheat high+{tag}+(div|vol)", [*base, div_expr | vol_expr]))
    return jobs


def scan_top_signals(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    near_high_pct: float = 8.0,
    decel_thresholds: tuple[float, ...] = (0.0,),
    div_floor: float = 0.0,
    vol_floor: float = 0.0,
    sell_z_thresholds: tuple[float, ...] = (1.0, 1.5),
    sell_prefixes: tuple[str, ...] = ("foreign_flow", "net_flow"),
    lead_window: int = 10,
    occupy_days: int = 15,
    z_min_periods: int = 30,
) -> pl.DataFrame:
    """掃描頂部/出貨退潮訊號對 episodes 的命中統計（M-MH 退潮校準；對稱 scan_stock_signals）。

    隨機基率以**全上市宇宙**一次算好（compute_base_rate），對所有訊號一致。回傳欄同
    scan_stock_signals（signal/n_triggers/hits/hit_rate/recall/base_rate/lift/median_lead_days
    /f1），依 lift 遞減排序（頂部警示重精度/lift > recall）。空輸入回空表。
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
    for label, conditions in _build_top_signal_jobs(
        panel,
        near_high_pct,
        decel_thresholds,
        div_floor,
        vol_floor,
        sell_z_thresholds,
        sell_prefixes,
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
    ).sort("lift", descending=True, nulls_last=True)


# ── M-MH Phase 2：跨窗配對領先（直接驗短窗是否早於 20d-z 達標＝GATE 核心；docs/13 Phase D）──
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


# ── 校準報告輸出：每 label 一張 lift 表 ＋ L4 退潮對照（render_*；供裁決哪組因子最領先）──
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


def render_top_calibration_report(
    scan: pl.DataFrame,
    episodes: pl.DataFrame,
    params: dict,
    coverage: dict,
    min_triggers: int = 8,
) -> str:
    """頂部/出貨退潮警示校準報告 markdown（M-MH 精修・退潮警示）。

    含對照裁決：把生產啟發式（★overheat 列）的 lift 對上「純貼高」與「法人賣超」基準，
    答「這套背離因子是否真有頂部預測力、且贏過只看位階/賣超」。誠實標：絕對下跌含大盤
    系統性回檔；這是停利風險標註的事後檢驗，非賣訊、非目標價。
    """
    base_rate = scan["base_rate"][0] if not scan.is_empty() else 0.0
    lines = [
        "# 個股頂部/出貨事件回測校準 — L4 退潮警示（M-MH 精修）",
        "",
        "- 獵物定義：距 M 日高 ≤ tol%（已在區間高位）→ 前瞻 N 日谷底跌 ≥ drop%（絕對下跌）",
        f"- 宇宙：{coverage.get('universe', 'listed')}・"
        f"{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        f"- 前瞻 N={params['fwd_n_days']} 交易日・跌幅門檻 drop={params['fwd_x_pct']}%・"
        f"貼高 tol={params.get('tol_pct', '?')}%・冷卻 {params['cooldown_days']} 日・"
        f"領先視窗 {params['lead_window']} 日",
        f"- 事件樣本：{episodes.height} 個"
        f"（{episodes['stock_id'].n_unique() if not episodes.is_empty() else 0} 檔）",
        "",
        f"## 退潮訊號掃描（基率 {base_rate:.2%}＝高位股前瞻下跌無條件機率；lift=命中率/基率）",
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

    def _lift_of(pred: pl.Expr) -> tuple[str, float] | None:
        sub = scan.filter(
            pred & (pl.col("n_triggers") >= min_triggers) & pl.col("lift").is_not_null()
        )
        if sub.is_empty():
            return None
        r = sub.sort("lift", descending=True).row(0, named=True)
        return r["signal"], float(r["lift"])

    overheat = _lift_of(pl.col("signal").str.starts_with("★overheat"))
    near_high = _lift_of(pl.col("signal").str.starts_with("near_high"))
    sell = _lift_of(pl.col("signal").str.contains("z<") & pl.col("signal").str.contains(r"\+high"))
    lines += [
        "",
        f"## 對照裁決（觸發 ≥{min_triggers}・lift = 高位股下跌機率的倍數）",
        "",
    ]
    if overheat is None:
        lines.append("- 生產啟發式（★overheat）觸發不足，無法裁決——標「資料累積後重校」。")
    else:
        nh_txt = f"{near_high[1]:.2f}（{near_high[0]}）" if near_high else "—"
        sl_txt = f"{sell[1]:.2f}（{sell[0]}）" if sell else "—"
        beat_nh = near_high is None or overheat[1] > near_high[1]
        beat_sl = sell is None or overheat[1] > sell[1]
        lines += [
            f"- **生產啟發式 ★overheat**：lift {overheat[1]:.2f}",
            f"- 純貼高基準 near_high：lift {nh_txt}",
            f"- 法人短窗賣超×高位基準：lift {sl_txt}",
            "",
            f"→ 啟發式 {'>' if beat_nh else '≤'} 純貼高、{'>' if beat_sl else '≤'} 賣超基準；"
            + (
                "背離因子在頂部有加值、可考慮升級。"
                if (overheat[1] > 1.0 and beat_nh and beat_sl)
                else "未明顯贏過位階/賣超基準——**維持低信心啟發式註記、不升級為訊號**。"
            ),
        ]
    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) 絕對下跌含大盤系統性回檔，高 lift 不等於『個股獨走弱』；",
        "> (2) 這是停利**風險標註**的事後檢驗，非賣訊、非目標價、非進場反向操作依據；",
        "> (3) 個股事件稀疏、1 年單一樣本，lift 差距可能是雜訊；每季重跑校準。",
        "",
    ]
    return "\n".join(lines)


# ── B-P1：穩健度四件套（payoff／decay／holdout／流動性硬化；docs/15 T3）──────────
# 全研究軌、純加法：度量「既有勝出因子」的賺賠/衰減/樣本外/可交易性，不改任何生產判讀。


def _scalar(x: object) -> float | None:
    """Polars Series 聚合純量 → float|None（polars stub 回寬 union，集中轉型供 Python 端運算）。"""
    return None if x is None else float(x)  # type: ignore[arg-type]


def _select_jobs(
    panel: pl.DataFrame,
    signals: set[str] | None,
    z_thresholds: tuple[float, ...],
    volume_thresholds: tuple[float, ...],
    position_low_pct: float,
    early_gate: dict | None,
) -> list[tuple[str, list[pl.Expr]]]:
    """_build_signal_jobs 的結果，可選依 signal 名子集過濾（保序）；signals=None 取全部。

    讓四件套沿用主掃描的條件定義（同一 +low/+mom/+early 串），不重寫條件、保證一致。
    """
    jobs = _build_signal_jobs(panel, z_thresholds, volume_thresholds, position_low_pct, early_gate)
    if signals is None:
        return jobs
    return [(name, cond) for name, cond in jobs if name in signals]


def _forward_returns(panel: pl.DataFrame, horizons: tuple[int, ...]) -> pl.DataFrame:
    """每 (stock_id, date) 各 horizon 前瞻報酬%＝close[t+H]/close[t]−1（原始價、不除息還原，
    與 episode 偵測同口徑）。panel 須含 close。尾端不足 H 日的列該窗為 null。"""
    return (
        panel.select(["stock_id", "date", "close"])
        .sort(["stock_id", "date"])
        .with_columns(
            [
                (
                    (pl.col("close").shift(-h).over("stock_id") / pl.col("close") - 1.0) * 100
                ).alias(f"fwd_ret_{h}d")
                for h in horizons
            ]
        )
        .drop("close")
    )


def payoff_decay_table(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = (5, 10, 20, 40),
    *,
    signals: set[str] | None = None,
    z_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0),
    volume_thresholds: tuple[float, ...] = (1.0, 1.5, 2.0),
    position_low_pct: float = 15.0,
    early_gate: dict | None = None,
    extra_conditions: list[pl.Expr] | None = None,
    name_suffix: str = "",
) -> pl.DataFrame:
    """T3 payoff＋decay（docs/15）：對選定訊號的觸發日，算各前瞻窗的報酬分布。

    payoff（賠率/期望）＝win_rate、avg_win/avg_loss、payoff_ratio（均盈/均虧）；
    decay＝同訊號跨 horizon 的 median_ret 衰減曲線；excess_median＝訊號中位 − 全宇宙同窗
    中位（扣掉大盤/持有期自然漂移，誠實量訊號超額）。label 無關（純前瞻報酬），不需 episodes。
    extra_conditions（M-Part C C-P2）：額外 AND 到每個 job 的條件（如落後濾鏡 rs_subind<0），
    name_suffix 標進 signal 名以區分。回每 (signal, horizon) 一列。空輸入或缺 close 回空表。
    """
    if panel.is_empty() or "close" not in panel.columns:
        return pl.DataFrame()
    fwd = _forward_returns(panel, horizons)
    base_med = {h: _scalar(fwd[f"fwd_ret_{h}d"].median()) for h in horizons}
    jobs = _select_jobs(
        panel, signals, z_thresholds, volume_thresholds, position_low_pct, early_gate
    )
    rows: list[dict] = []
    for name, conditions in jobs:
        trig = _stock_triggers(panel, conditions + list(extra_conditions or []))
        if trig.is_empty():
            continue
        joined = trig.join(fwd, on=["stock_id", "date"], how="left")
        for h in horizons:
            vals = joined[f"fwd_ret_{h}d"].drop_nulls()
            n = vals.len()
            if n == 0:
                continue
            wins = vals.filter(vals > 0)
            losses = vals.filter(vals < 0)
            avg_win = _scalar(wins.mean())
            avg_loss = _scalar(losses.mean())
            median_ret = _scalar(vals.median())
            bm = base_med[h]
            rows.append(
                {
                    "signal": name + name_suffix,
                    "horizon_d": h,
                    "n": n,
                    "median_ret_pct": median_ret,
                    "mean_ret_pct": _scalar(vals.mean()),
                    "win_rate": wins.len() / n,
                    "avg_win_pct": avg_win,
                    "avg_loss_pct": avg_loss,
                    "payoff_ratio": (avg_win / abs(avg_loss))
                    if avg_win is not None and avg_loss is not None and avg_loss != 0.0
                    else None,
                    "base_median_pct": bm,
                    "excess_median_pct": (median_ret - bm)
                    if median_ret is not None and bm is not None
                    else None,
                }
            )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort(["signal", "horizon_d"])


def holdout_table(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    *,
    split_frac: float = 0.7,
    signals: set[str] | None = None,
    z_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0),
    volume_thresholds: tuple[float, ...] = (1.0, 1.5, 2.0),
    position_low_pct: float = 15.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
    early_gate: dict | None = None,
) -> pl.DataFrame:
    """T3 holdout（docs/15）：日曆按 split_frac 切「前段(train)/後段(test)」各自重掃，比對 lift
    是否撐住樣本外（呼應 §D「1 年單一樣本」過擬合疑慮）。

    回 signal/lift_train/n_triggers_train/lift_test/n_triggers_test，依 lift_test 遞減。
    空輸入或日曆過短（<4 日）回空表；某側無事件 → 該側 lift 為 null。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    calendar = sorted(panel["date"].unique().to_list())
    if len(calendar) < 4:
        return pl.DataFrame()
    cut = calendar[max(1, min(len(calendar) - 1, int(len(calendar) * split_frac)))]
    train = scan_stock_signals(
        panel.filter(pl.col("date") < cut),
        episodes.filter(pl.col("start_date") < cut),
        z_thresholds=z_thresholds,
        volume_thresholds=volume_thresholds,
        position_low_pct=position_low_pct,
        lead_window=lead_window,
        occupy_days=occupy_days,
        z_min_periods=z_min_periods,
        early_gate=early_gate,
    )
    test = scan_stock_signals(
        panel.filter(pl.col("date") >= cut),
        episodes.filter(pl.col("start_date") >= cut),
        z_thresholds=z_thresholds,
        volume_thresholds=volume_thresholds,
        position_low_pct=position_low_pct,
        lead_window=lead_window,
        occupy_days=occupy_days,
        z_min_periods=z_min_periods,
        early_gate=early_gate,
    )
    if train.is_empty() and test.is_empty():
        return pl.DataFrame()
    train_map = {r["signal"]: r for r in train.iter_rows(named=True)}
    test_map = {r["signal"]: r for r in test.iter_rows(named=True)}
    rows: list[dict] = []
    for s in dict.fromkeys([*train_map, *test_map]):
        if signals is not None and s not in signals:
            continue
        tr = train_map.get(s)
        te = test_map.get(s)
        rows.append(
            {
                "signal": s,
                "lift_train": tr["lift"] if tr else None,
                "n_triggers_train": tr["n_triggers"] if tr else 0,
                "lift_test": te["lift"] if te else None,
                "n_triggers_test": te["n_triggers"] if te else 0,
            }
        )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("lift_test", descending=True, nulls_last=True)


def liquidity_table(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    *,
    adv_window: int = 20,
    adv_min_amount: float = 100.0,
    signals: set[str] | None = None,
    z_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0),
    volume_thresholds: tuple[float, ...] = (1.0, 1.5, 2.0),
    position_low_pct: float = 15.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
    early_gate: dict | None = None,
) -> pl.DataFrame:
    """T3 流動性硬化（docs/15）：只保留觸發日 ADV（近 adv_window 日均成交額）≥ adv_min_amount
    百萬元 的觸發，比對 lift 硬化前/後（剔除大資金進不去的小量股、看訊號在可交易宇宙是否成立）。

    ADV＝close×volume（元；真周轉率不可得＝缺流通股數，誠實標）。回 signal/n_raw/lift_raw/
    n_hardened/lift_hardened，依 lift_hardened 遞減。空輸入或缺 volume 回空表。
    """
    if panel.is_empty() or episodes.is_empty() or "volume" not in panel.columns:
        return pl.DataFrame()
    thr = adv_min_amount * 1_000_000
    adv = (
        panel.select(["stock_id", "date", "close", "volume"])
        .sort(["stock_id", "date"])
        .with_columns(
            (pl.col("close") * pl.col("volume"))
            .rolling_mean(adv_window, min_samples=1)
            .over("stock_id")
            .alias("_adv")
        )
        .select(["stock_id", "date", "_adv"])
    )
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
    jobs = _select_jobs(
        panel, signals, z_thresholds, volume_thresholds, position_low_pct, early_gate
    )
    rows: list[dict] = []
    for name, conditions in jobs:
        trig = _stock_triggers(panel, conditions)
        trig_hard = (
            trig.join(adv, on=["stock_id", "date"], how="left")
            .filter(pl.col("_adv") >= thr)
            .select(["stock_id", "date"])
        )
        raw = evaluate_triggers(
            trig, episodes, calendar, lead_window, occupy_days, warmup_pos,
            key_col="stock_id", base_rate=base_rate,
        )
        hard = evaluate_triggers(
            trig_hard, episodes, calendar, lead_window, occupy_days, warmup_pos,
            key_col="stock_id", base_rate=base_rate,
        )
        rows.append(
            {
                "signal": name,
                "n_raw": raw["n_triggers"],
                "lift_raw": raw["lift"],
                "n_hardened": hard["n_triggers"],
                "lift_hardened": hard["lift"],
            }
        )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("lift_hardened", descending=True, nulls_last=True)


def render_robustness_report(
    payoff: pl.DataFrame,
    holdout: pl.DataFrame,
    liquidity: pl.DataFrame,
    anchor_label: str,
    anchor_signals: list[str],
    params: dict,
    coverage: dict,
) -> str:
    """穩健度四件套 markdown（docs/15 B-P1）。錨定 anchor_label 的勝出因子，並陳 payoff/decay
    /holdout/流動性硬化，誠實標所有限制。空輸入回誠實佔位。"""
    lines = [
        "# 個股起漲因子穩健度剖析 — payoff／decay／holdout／流動性（docs/15 B-P1）",
        "",
        f"- 錨定 label：{anchor_label}（CP 補漲主假說、現任冠軍之家）",
        f"- 剖析因子（錨定主掃描合格前 {params.get('top_k', '?')} 名）："
        + ("、".join(anchor_signals) if anchor_signals else "（無合格因子）"),
        f"- 宇宙：{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        f"- decay 前瞻窗 {params.get('horizons', [])} 交易日・holdout 前段占比 "
        f"{params.get('holdout_frac', '?')}・流動性門檻 ADV ≥ {params.get('adv_min_amount', '?')} "
        f"百萬元（近 {params.get('adv_window', '?')} 日均成交額）",
        "",
        "## 1. payoff × decay（觸發日前瞻報酬分布；excess＝訊號中位 − 全宇宙同窗中位）",
        "",
    ]
    if payoff.is_empty():
        lines.append("（無合格因子或觸發不足，無法剖析賺賠/衰減）")
    else:
        lines += [
            "| 訊號 | 前瞻(日) | n | 中位% | 超額中位% | 勝率 | 均盈% | 均虧% | 賠率 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in payoff.iter_rows(named=True):
            pr = f"{r['payoff_ratio']:.2f}" if r["payoff_ratio"] is not None else "—"
            aw = f"{r['avg_win_pct']:.1f}" if r["avg_win_pct"] is not None else "—"
            al = f"{r['avg_loss_pct']:.1f}" if r["avg_loss_pct"] is not None else "—"
            ex = f"{r['excess_median_pct']:+.1f}" if r["excess_median_pct"] is not None else "—"
            md = f"{r['median_ret_pct']:.1f}" if r["median_ret_pct"] is not None else "—"
            lines.append(
                f"| {r['signal']} | {r['horizon_d']} | {r['n']} | {md} | {ex} "
                f"| {r['win_rate']:.0%} | {aw} | {al} | {pr} |"
            )

    lines += [
        "",
        "## 2. holdout（時間樣本外：前段選因子、後段驗 lift 是否撐住）",
        "",
    ]
    if holdout.is_empty():
        lines.append("（日曆過短或某側無事件，無法做樣本外切分）")
    else:
        lines += [
            "| 訊號 | lift(前段) | 觸發(前段) | lift(後段) | 觸發(後段) |",
            "|---|---|---|---|---|",
        ]
        for r in holdout.iter_rows(named=True):
            lt = f"{r['lift_train']:.2f}" if r["lift_train"] is not None else "—"
            le = f"{r['lift_test']:.2f}" if r["lift_test"] is not None else "—"
            lines.append(
                f"| {r['signal']} | {lt} | {r['n_triggers_train']} | {le} "
                f"| {r['n_triggers_test']} |"
            )

    lines += [
        "",
        "## 3. 流動性硬化（只算可交易量級觸發；lift 硬化前/後）",
        "",
    ]
    if liquidity.is_empty():
        lines.append("（無合格因子或缺成交量，無法硬化）")
    else:
        lines += [
            "| 訊號 | 觸發(原始) | lift(原始) | 觸發(硬化) | lift(硬化) |",
            "|---|---|---|---|---|",
        ]
        for r in liquidity.iter_rows(named=True):
            lr = f"{r['lift_raw']:.2f}" if r["lift_raw"] is not None else "—"
            lh = f"{r['lift_hardened']:.2f}" if r["lift_hardened"] is not None else "—"
            lines.append(
                f"| {r['signal']} | {r['n_raw']} | {lr} | {r['n_hardened']} | {lh} |"
            )

    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) 前瞻報酬用原始收盤、不除息還原（與 episode 同口徑）；",
        "> (2) ADV＝成交額代理，**真周轉率不可得（缺流通在外股數）**；",
        "> (3) 1 年單一樣本，holdout 後段樣本更稀疏、lift 差距可能是雜訊；",
        "> (4) 研究軌產出，非買賣訊、非目標價；每季資料累積後重跑校準。",
        "",
    ]
    return "\n".join(lines)


# ── B-P2：買方主導度單調性（T1；docs/15）──────────────────────────────────────────
# 把修法4 的 binary 土洋對作旗標連續化（dom∈[−1,1]），測「主導度是否與起漲單調」，
# 並控制位階（above_low 分層）檢核是否「位階在做工」（守 §D 教訓）。全研究軌、不改生產。
_DOM_RE = re.compile(r"^dom_(\d+)d$")


def _dom_col(panel: pl.DataFrame) -> str | None:
    """panel 的買方主導度欄名（B1 隨 long_window 命名，如 dom_20d）。"""
    cols = [c for c in panel.columns if _DOM_RE.match(c)]
    return cols[0] if cols else None


def _spearman(df: pl.DataFrame, x_col: str, y_col: str) -> tuple[float | None, int, float | None]:
    """Spearman 秩相關 ρ（＝秩的 Pearson）與有效樣本 n、大樣本常態近似 z＝ρ·√(n−1)。

    兩欄皆非 null 才納；n<3 或某欄零變異（rank 相關未定義）→ (None, n, None)。
    z 供顯著判讀（|z|>1.96 ≈ 雙尾 5%；ρ>0 且 z>界＝主導度越高、前瞻越強）。
    """
    sub = df.select([x_col, y_col]).drop_nulls()
    n = sub.height
    if n < 3:
        return None, n, None
    ranked = sub.select(pl.col(x_col).rank().alias("_rx"), pl.col(y_col).rank().alias("_ry"))
    rho = _scalar(ranked.select(pl.corr("_rx", "_ry")).to_series()[0])
    if rho is None:
        return None, n, None
    return rho, n, rho * ((n - 1) ** 0.5)


def _factor_strata(
    panel: pl.DataFrame, factor_col: str | None, fwd_window: int, position_low_pct: float
) -> tuple[str | None, list[tuple[str, pl.DataFrame]]]:
    """組單調性分析的股日子集（任一 factor_col）：全體＋（有位階欄時）貼低/非貼低（控制位階）。

    回 (前瞻報酬欄, [(層名, 含 factor/位階/前瞻報酬的 df)…])。缺 factor_col 或 close → 空清單。
    供 B-P2 dom（docs/15 T1）與 M-Part C rs_subind 落後度（docs/16）共用，不重造分位/控制邏輯。
    """
    if not factor_col or factor_col not in panel.columns or "close" not in panel.columns:
        return None, []
    low_col = _position_low_col(panel)
    fwd_col = f"fwd_ret_{fwd_window}d"
    cols = ["stock_id", "date", factor_col] + ([low_col] if low_col else [])
    base = (
        panel.select(cols)
        .join(_forward_returns(panel, (fwd_window,)), on=["stock_id", "date"], how="left")
        .filter(pl.col(factor_col).is_not_null())
    )
    strata: list[tuple[str, pl.DataFrame]] = [("全體", base)]
    if low_col is not None:
        strata.append(("貼低", base.filter(pl.col(low_col) <= position_low_pct)))
        strata.append(("非貼低", base.filter(pl.col(low_col) > position_low_pct)))
    return fwd_col, strata


def factor_monotonicity_table(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    factor_col: str | None,
    *,
    n_buckets: int = 5,
    fwd_window: int = 20,
    position_low_pct: float = 15.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
) -> pl.DataFrame:
    """因子分位單調性表（docs/15 B-P2 機制；M-Part C docs/16 共用）：factor_col 分 n_buckets 分位，
    各桶算前瞻起漲 lift 與前瞻報酬中位，分「全體／貼低／非貼低」三層（控制位階）。

    桶以 ordinal rank 切（避 ±1/0 等重邊；rank 保證桶均衡）。lift 以全宇宙基率為分母（與
    scan_stock_signals 一致）：把每桶所有股日當「觸發」丟 evaluate_triggers——答「處在此桶的隨機
    一天，前瞻 lead_window 內起漲機率是基率的幾倍」。每層各自重分位（控制位階＝同位階內比因子）。
    回每 (stratum, bucket) 一列（含 factor_min/median/max）。空輸入／缺 factor_col/close 回空表。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    fwd_col, strata = _factor_strata(panel, factor_col, fwd_window, position_low_pct)
    if not strata or fwd_col is None or factor_col is None:
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
    rows: list[dict] = []
    for stratum, df in strata:
        n = df.height
        if n < n_buckets:
            continue
        rank = pl.col(factor_col).rank(method="ordinal").cast(pl.Int64)
        bucketed = df.with_columns((((rank - 1) * n_buckets) // n + 1).alias("_b"))
        for b in range(1, n_buckets + 1):
            cell = bucketed.filter(pl.col("_b") == b)
            if cell.is_empty():
                continue
            stats = evaluate_triggers(
                cell.select(["stock_id", "date"]),
                episodes,
                calendar,
                lead_window,
                occupy_days,
                warmup_pos,
                key_col="stock_id",
                base_rate=base_rate,
            )
            rows.append(
                {
                    "stratum": stratum,
                    "bucket": b,
                    "n_stock_days": cell.height,
                    "factor_min": _scalar(cell[factor_col].min()),
                    "factor_median": _scalar(cell[factor_col].median()),
                    "factor_max": _scalar(cell[factor_col].max()),
                    "n_eval": stats["n_triggers"],
                    "hits": stats["hits"],
                    "hit_rate": stats["hit_rate"],
                    "lift": stats["lift"],
                    "median_fwd_ret_pct": _scalar(cell[fwd_col].median()),
                }
            )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows)


def dom_monotonicity_table(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    *,
    n_buckets: int = 5,
    fwd_window: int = 20,
    position_low_pct: float = 15.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
) -> pl.DataFrame:
    """T1 買方主導度單調性（docs/15）＝factor_monotonicity_table 套 dom 欄（薄包、向後相容）。"""
    return factor_monotonicity_table(
        panel,
        episodes,
        _dom_col(panel),
        n_buckets=n_buckets,
        fwd_window=fwd_window,
        position_low_pct=position_low_pct,
        lead_window=lead_window,
        occupy_days=occupy_days,
        z_min_periods=z_min_periods,
    )


def factor_monotonicity_spearman(
    panel: pl.DataFrame,
    factor_col: str | None,
    *,
    fwd_window: int = 20,
    position_low_pct: float = 15.0,
    z_sig: float = 1.96,
    direction: str = "increasing",
) -> pl.DataFrame:
    """因子 vs 前瞻報酬的 Spearman ρ，分三層（docs/15 B-P2 機制；M-Part C docs/16 共用）。

    桶表給可讀視覺；本表給大樣本顯著（z=ρ·√(n−1) 近似）。direction="increasing"＝因子越高前瞻越強
    （significant＝ρ>0 且 z>z_sig）；"decreasing"＝越低越強（significant＝ρ<0 且 z<−z_sig，供
    rs_subind 落後度：rs_subind 越低=越落後其族群=起漲越強）。回每層 stratum/n/spearman_rho/z
    /significant。缺 factor_col／close 回空表。
    """
    fwd_col, strata = _factor_strata(panel, factor_col, fwd_window, position_low_pct)
    if not strata or fwd_col is None or factor_col is None:
        return pl.DataFrame()
    rows: list[dict] = []
    for name, df in strata:
        rho, n, z = _spearman(df, factor_col, fwd_col)
        if direction == "decreasing":
            sig = rho is not None and z is not None and rho < 0 and z < -z_sig
        else:
            sig = rho is not None and z is not None and rho > 0 and z > z_sig
        rows.append({"stratum": name, "n": n, "spearman_rho": rho, "z": z, "significant": sig})
    return pl.DataFrame(rows)


def dom_monotonicity_spearman(
    panel: pl.DataFrame,
    *,
    fwd_window: int = 20,
    position_low_pct: float = 15.0,
    z_sig: float = 1.96,
) -> pl.DataFrame:
    """T1 連續單調性顯著檢定（docs/15）＝factor_monotonicity_spearman 套 dom、遞增方向（薄包）。"""
    return factor_monotonicity_spearman(
        panel,
        _dom_col(panel),
        fwd_window=fwd_window,
        position_low_pct=position_low_pct,
        z_sig=z_sig,
        direction="increasing",
    )


def render_dom_monotonicity_report(
    buckets: pl.DataFrame,
    spearman: pl.DataFrame,
    anchor_label: str,
    params: dict,
    coverage: dict,
) -> str:
    """買方主導度單調性報告 markdown（docs/15 B-P2）。並陳分位桶 lift／報酬與三層 Spearman，
    依裁決門檻①（全體單調顯著）②（控制位階後兩層仍單調顯著）給「升級分級因子 / 維持 binary 旗標、
    記否證」的誠實裁決。空輸入回誠實佔位。"""
    lines = [
        "# 買方主導度單調性 — dom 分位 × 控制位階（docs/15 B-P2 / T1）",
        "",
        f"- 因子：dom_{params.get('dom_window', '?')}d＝(外資+投信長窗淨買)/(|外資|+|投信|)∈[−1,1]"
        "（+1 雙邊同向買到底、−1 完全土洋對作、0 勢均）",
        f"- 錨定起漲 label：{anchor_label}（CP 補漲主假說；lift 以全宇宙基率為分母）",
        f"- 分位桶數 {params.get('n_buckets', '?')}（ordinal rank 切）・前瞻報酬窗 "
        f"{params.get('fwd_window', '?')} 交易日・位階分層界 above_low ≤ "
        f"{params.get('position_low_pct', '?')}%（貼低）",
        f"- 宇宙：{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        "",
        "## 1. 分位桶（各桶＝同層內 dom 由低到高；lift＝桶內前瞻起漲機率 / 全宇宙基率）",
        "",
    ]
    if buckets.is_empty():
        lines.append("（缺 dom 欄、缺 close 或樣本不足分位，無法分桶）")
    else:
        for stratum in ("全體", "貼低", "非貼低"):
            sub = buckets.filter(pl.col("stratum") == stratum).sort("bucket")
            if sub.is_empty():
                continue
            lines += [
                f"### {stratum}",
                "",
                "| 桶 | dom 中位 | 股日數 | 評估數 | 命中率 | lift | 前瞻報酬中位% |",
                "|---|---|---|---|---|---|---|",
            ]
            for r in sub.iter_rows(named=True):
                lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
                dm = f"{r['factor_median']:+.2f}" if r["factor_median"] is not None else "—"
                fr = (
                    f"{r['median_fwd_ret_pct']:+.1f}"
                    if r["median_fwd_ret_pct"] is not None
                    else "—"
                )
                lines.append(
                    f"| {r['bucket']} | {dm} | {r['n_stock_days']} | {r['n_eval']} "
                    f"| {r['hit_rate']:.1%} | {lift} | {fr} |"
                )
            lines.append("")

    lines += [
        "## 2. 連續單調性（Spearman ρ：dom vs 前瞻報酬；z＝ρ·√(n−1) 大樣本近似）",
        "",
    ]
    sig: dict[str, dict] = {}
    if spearman.is_empty():
        lines.append("（缺 dom 欄或樣本不足，無法檢定）")
    else:
        sig = {r["stratum"]: r for r in spearman.iter_rows(named=True)}
        lines += ["| 層 | n | Spearman ρ | z | 顯著(ρ>0 且 z>界) |", "|---|---|---|---|---|"]
        for stratum in ("全體", "貼低", "非貼低"):
            sr = sig.get(stratum)
            if sr is None:
                continue
            rho = f"{sr['spearman_rho']:+.3f}" if sr["spearman_rho"] is not None else "—"
            z = f"{sr['z']:+.2f}" if sr["z"] is not None else "—"
            lines.append(
                f"| {stratum} | {sr['n']} | {rho} | {z} | {'✅' if sr['significant'] else '❌'} |"
            )
        lines.append("")

    all_sig = bool(sig.get("全體", {}).get("significant", False))
    low_sig = bool(sig.get("貼低", {}).get("significant", False))
    high_sig = bool(sig.get("非貼低", {}).get("significant", False))
    controlled = low_sig and high_sig
    lines += ["## 3. 裁決（docs/15 T1 門檻①單調顯著 ②控制位階後仍單調）", ""]
    if not sig:
        lines.append("- 無法檢定——標『資料累積後重校』。")
    elif all_sig and controlled:
        lines.append(
            "- **①全體單調顯著 ✅ 且 ②貼低/非貼低兩層皆仍單調顯著 ✅** → 主導度非僅『位階在做工』，"
            "**建議升級為連續分級因子**（B3 進場可按 dom 分位加分；上線前另開生產 milestone）。"
        )
    elif all_sig and not controlled:
        lines.append(
            "- **①全體單調顯著 ✅ 但 ②控制位階後消失 ❌**（貼低或非貼低層不顯著）→ 全體單調多由"
            "位階驅動（守 §D 反例）。**維持修法4 binary 土洋對作旗標、記否證**，不升級分級。"
        )
    else:
        lines.append(
            "- **①全體單調不顯著 ❌** → 主導度與前瞻起漲無系統性單調關係。"
            "**維持修法4 binary 土洋對作旗標、記否證**（誠實的『沒贏』＝省下未來做白工，守 §D）。"
        )
    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) lift 以全宇宙基率為分母、前瞻報酬用原始收盤不還原（與 episode 同口徑）；",
        "> (2) 1 年單一樣本、個股事件稀疏，桶內 lift 差距可能是雜訊——顯著建立在連續秩相關非桶數；",
        "> (3) 研究軌裁決，非買賣訊、非目標價；每季資料累積後重跑校準。",
        "",
    ]
    return "\n".join(lines)


# ── B-P3：個股×族群 2×2 交互（T2；docs/15）────────────────────────────────────────
# 測「資金進+貼低(S) × 個股在族群裡領先(G)」是否超加（S高G高 lift > 邊際相加），G＝面板
# rs_subind（個股相對其次產業，非族群絕對強度，D-E4 拍板）。全研究軌、不改生產。
_RS_SUBIND_RE = re.compile(r"^rs_subind_(\d+)d$")


def _rs_subind_col(panel: pl.DataFrame) -> str | None:
    """panel 的個股相對次產業強度欄名（B1 隨 rs_window 命名，如 rs_subind_20d）。"""
    cols = [c for c in panel.columns if _RS_SUBIND_RE.match(c)]
    return cols[0] if cols else None


def _two_prop_z(h1: int, n1: int, h2: int, n2: int) -> float | None:
    """兩比例差 z 檢定（pooled）：(p1−p2)/SE。任一 n=0 或零變異 → None。

    用於「S+ 內 G高 vs G低 的起漲命中率差」是否顯著（族群確認加分的可測判準）。
    """
    if n1 == 0 or n2 == 0:
        return None
    p1, p2 = h1 / n1, h2 / n2
    pool = (h1 + h2) / (n1 + n2)
    se = (pool * (1 - pool) * (1 / n1 + 1 / n2)) ** 0.5
    return (p1 - p2) / se if se > 0 else None


def interaction_2x2_table(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    *,
    s_flow_col: str = "foreign_flow_20d_z",
    s_z_threshold: float = 0.5,
    s_low_pct: float = 15.0,
    g_threshold: float = 0.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
) -> pl.DataFrame:
    """T2 個股×族群 2×2 交互（docs/15，D-E3 拍板＝2×2 列聯）：把股日依 S 高/低 × G 高/低
    分四格，各算前瞻起漲 lift（同 dom 桶法：格內全股日當觸發 × 全宇宙基率），看是否超加。

    S 高＝冠軍個股訊號（`s_flow_col` z > s_z_threshold 且 above_low ≤ s_low_pct＝資金進+貼低）；
    G 高＝rs_subind > g_threshold（個股相對其次產業領先，D-E4；非族群絕對強度）。只取 S/G 輸入
    皆非 null 的股日（缺次產業標記者排除，誠實）。回每格一列：cell/s_high/g_high/n_eval/hits
    /hit_rate/lift。空輸入／缺 S 流向欄／缺 above_low／缺 rs_subind 回空表。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    low_col = _position_low_col(panel)
    g_col = _rs_subind_col(panel)
    if s_flow_col not in panel.columns or low_col is None or g_col is None:
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
    base = (
        panel.select(["stock_id", "date", s_flow_col, low_col, g_col])
        .drop_nulls()
        .with_columns(
            ((pl.col(s_flow_col) > s_z_threshold) & (pl.col(low_col) <= s_low_pct)).alias("_s"),
            (pl.col(g_col) > g_threshold).alias("_g"),
        )
    )
    rows: list[dict] = []
    for s_high, g_high in ((True, True), (True, False), (False, True), (False, False)):
        cell = base.filter((pl.col("_s") == s_high) & (pl.col("_g") == g_high))
        stats = evaluate_triggers(
            cell.select(["stock_id", "date"]),
            episodes,
            calendar,
            lead_window,
            occupy_days,
            warmup_pos,
            key_col="stock_id",
            base_rate=base_rate,
        )
        rows.append(
            {
                "cell": f"S{'+' if s_high else '−'}G{'+' if g_high else '−'}",
                "s_high": s_high,
                "g_high": g_high,
                "n_eval": stats["n_triggers"],
                "hits": stats["hits"],
                "hit_rate": stats["hit_rate"],
                "lift": stats["lift"],
            }
        )
    return pl.DataFrame(rows)


def render_interaction_report(
    table: pl.DataFrame,
    anchor_label: str,
    params: dict,
    coverage: dict,
    min_triggers: int = 8,
    z_sig: float = 1.96,
) -> str:
    """個股×族群 2×2 交互報告 markdown（docs/15 B-P3 / T2）。並陳四格 lift、加法基準與超加性
    差、S+ 內 G高 vs G低 兩比例 z 檢定，依裁決門檻給「族群確認進場加分 / 個股訊號已自足」。"""
    lines = [
        "# 個股×族群 2×2 交互 — 資金進+貼低(S) × 個股在族群裡領先(G)（docs/15 B-P3 / T2）",
        "",
        f"- 個股訊號 S 高：{params.get('s_flow_col', '?')} z > {params.get('s_z_threshold', '?')} "
        f"且 above_low ≤ {params.get('s_low_pct', '?')}%（冠軍資金進+貼低）",
        f"- 族群因子 G 高：rs_subind > {params.get('g_threshold', '?')}"
        "（個股相對其次產業領先＝D-E4；**非族群絕對強度**）",
        f"- 錨定起漲 label：{anchor_label}（lift 以全宇宙基率為分母；只取有次產業標記的股日）",
        f"- 宇宙：{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        "",
        "## 1. 2×2 格（lift＝格內前瞻起漲機率 / 全宇宙基率）",
        "",
    ]
    if table.is_empty():
        lines += ["（缺 S 流向欄、above_low 或 rs_subind，無法分格）", ""]
        return "\n".join(lines)

    by_cell = {r["cell"]: r for r in table.iter_rows(named=True)}
    lines += ["| 格 | 評估數 | 命中 | 命中率 | lift |", "|---|---|---|---|---|"]
    for cell in ("S+G+", "S+G−", "S−G+", "S−G−"):
        r = by_cell.get(cell)
        if r is None:
            continue
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        lines.append(
            f"| {cell} | {r['n_eval']} | {r['hits']} | {r['hit_rate']:.1%} | {lift} |"
        )

    def _lift(cell: str) -> float | None:
        r = by_cell.get(cell)
        return r["lift"] if r else None

    pp, pn, np_, nn = _lift("S+G+"), _lift("S+G−"), _lift("S−G+"), _lift("S−G−")
    lines += ["", "## 2. 超加性（S+G+ lift vs 邊際相加基準）", ""]
    if None in (pp, pn, np_, nn):
        lines.append("- 某格 lift 不可算（基率 0 或無觸發），無法評超加性。")
        additive = interaction = None
    else:
        additive = pn + np_ - nn  # type: ignore[operator]
        interaction = pp - additive  # type: ignore[operator]
        lines += [
            f"- 加法基準（S+G− + S−G+ − S−G−）＝{pn:.2f} + {np_:.2f} − {nn:.2f} = {additive:.2f}",
            f"- 觀測 S+G+ lift＝{pp:.2f} → **交互差 {interaction:+.2f}**"
            f"（>0＝超加；但 lift 非線性可加、邊際格高 lift 會汙染此基準，**以 §3 方向為準**）",
        ]

    # 族群確認加分的可測判準：S+ 內 G高 vs G低 的起漲命中率差是否顯著
    sp, snp = by_cell.get("S+G+"), by_cell.get("S+G−")
    lines += ["", "## 3. 族群確認加分（S+ 內 G高 vs G低 兩比例 z 檢定）", ""]
    z = None
    enough = bool(sp and snp and sp["n_eval"] >= min_triggers and snp["n_eval"] >= min_triggers)
    if sp and snp:
        z = _two_prop_z(sp["hits"], sp["n_eval"], snp["hits"], snp["n_eval"])
        zt = f"{z:+.2f}" if z is not None else "—"
        lines.append(
            f"- S+G+ 命中率 {sp['hit_rate']:.1%}（{sp['hits']}/{sp['n_eval']}）"
            f" vs S+G− 命中率 {snp['hit_rate']:.1%}（{snp['hits']}/{snp['n_eval']}）；z = {zt}"
            f"・樣本{'足' if enough else '不足（tiny-N，守 §6）'}"
        )

    superadd = interaction is not None and interaction > 0
    z_pos = z is not None and z > z_sig and enough  # G高顯著提升命中
    z_neg = z is not None and z < -z_sig and enough  # G高顯著降低命中
    lines += [
        "",
        "## 4. 裁決（§3 方向為主——§2 加法基準易受邊際格高 lift 算術汙染、非真綜效）",
        "",
    ]
    if z_pos and superadd:
        lines.append(
            "- **超加性 ✅ 且 S+ 內族群領先顯著提升命中率 ✅（樣本足）** → "
            "**建議設計「族群確認」進場加分**（資金進+貼低且個股在族群裡領先＝更高起漲機率；"
            "上線前另開生產 milestone）。"
        )
    elif z_neg:
        lines.append(
            "- **S+ 內族群領先(G高)顯著「降低」起漲命中率（z<−界、樣本足）** → 否證強族群強個股；"
            "**反向發現：個股『落後』其次產業(G低)才是補漲訊號**（重申 CP 補漲＝買未動的）。"
            "→ 個股訊號已自足，**不加族群領先確認**（§2 超加是邊際格高 lift 的算術假象）。"
        )
    elif superadd:
        lines.append(
            "- **超加性 ✅ 但 S+ 內族群確認未達顯著（tiny-N 或方向不明）** → 交互方向對但力不足，"
            "**記『暫不升級、資料累積後重校』**，不據單次差距加規則（守 §6）。"
        )
    else:
        lines.append(
            "- **無超加性且族群確認未顯著提升** → 族群領先未在資金訊號上額外加分，"
            "**記個股訊號已自足、否證交互**（誠實的『沒贏』＝守 §D）。"
        )
    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) G＝rs_subind 是個股相對其次產業、非族群絕對強度；只含有次產業標記的股日；",
        "> (2) lift 以全宇宙基率為分母、前瞻起漲純價格定義（資金/族群因子全在訊號端，避免循環）；",
        "> (3) 1 年單一樣本、交互格稀疏，tiny-N lift 差可能是雜訊；研究軌裁決非賣訊；每季重校。",
        "",
    ]
    return "\n".join(lines)


# ── M-Part C / C-P1：個股族群內落後度補漲因子（rs_subind 落後度單調 × 位階控制；docs/16）──
# 承 B-P3 反向發現，複用 factor_monotonicity_table 把因子換成 rs_subind（低桶＝落後）。
# **裁決以「起漲 lift」為 on-target 量尺**（最落後桶 vs 最領先桶 hit-rate 兩比例 z）——
# 實跑發現 factor vs 前瞻「報酬」的 Spearman 測到不同結果（落後↑起漲機率但領先↑中位報酬、兩者分流，
# ρ≈0 誤判否證），故 Spearman 退為診斷、不當裁決閘。關鍵關＝控制位階後 lift 是否仍遞減（守 §D）。


def _laggard_lift_significance(buckets: pl.DataFrame, z_sig: float) -> dict[str, dict]:
    """C-P1 的 on-target 顯著性：每層最落後桶(桶1) vs 最領先桶(最大桶) 起漲 hit-rate 兩比例 z，
    並檢桶 lift 是否單調遞減。回 {stratum: {z, monotone_dec, sig, lift_lo, lift_hi}}。

    取代 factor_monotonicity_spearman 的「factor vs 前瞻報酬」當裁決——後者測不同結果（落後↑起漲
    機率但領先↑中位報酬，兩者分流）。sig＝桶1 hit-rate 顯著 > 桶N（z>z_sig＝落後起漲較多）。
    """
    out: dict[str, dict] = {}
    for stratum in ("全體", "貼低", "非貼低"):
        sub = buckets.filter(pl.col("stratum") == stratum).sort("bucket")
        if sub.height < 2:
            continue
        recs = sub.to_dicts()
        lo, hi = recs[0], recs[-1]  # 桶1＝最落後、最大桶＝最領先
        z = _two_prop_z(lo["hits"], lo["n_eval"], hi["hits"], hi["n_eval"])
        lifts = [r["lift"] for r in recs if r["lift"] is not None]
        monotone_dec = (
            len(lifts) >= 2
            and all(lifts[i] >= lifts[i + 1] for i in range(len(lifts) - 1))
            and lifts[0] > lifts[-1]
        )
        out[stratum] = {
            "z": z,
            "monotone_dec": monotone_dec,
            "sig": z is not None and z > z_sig,
            "lift_lo": lo["lift"],
            "lift_hi": hi["lift"],
        }
    return out


def render_laggard_monotonicity_report(
    buckets: pl.DataFrame,
    spearman: pl.DataFrame,
    anchor_label: str,
    params: dict,
    coverage: dict,
    z_sig: float = 1.96,
) -> str:
    """族群內落後度單調性報告 markdown（docs/16 C-P1 / H1+H2）。factor＝rs_subind（低桶＝落後）。

    裁決以**起漲 lift** 為 on-target 量尺：H1＝全體最落後桶 hit-rate 顯著 > 最領先桶且桶 lift 單調
    遞減；H2＝貼低/非貼低兩層皆然（控制位階後仍在）。Spearman（vs 前瞻報酬）退為診斷——落後↑起漲
    機率但領先↑中位報酬、兩者分流，不當裁決閘。空輸入回誠實佔位。"""
    lines = [
        "# 族群內落後度單調性 — rs_subind 分位 × 控制位階（docs/16 C-P1 / H1+H2）",
        "",
        f"- 因子：rs_subind_{params.get('rs_window', '?')}d＝個股報酬 − 次產業籃報酬"
        "（**低桶＝越落後其族群**；去族群 beta、非族群絕對強度）",
        f"- 假說：rs_subind 越低（越落後）前瞻起漲越強（ρ<0）；錨定 label：{anchor_label}",
        f"- 分位桶數 {params.get('n_buckets', '?')}（ordinal rank 切）・前瞻報酬窗 "
        f"{params.get('fwd_window', '?')} 交易日・位階分層界 above_low ≤ "
        f"{params.get('position_low_pct', '?')}%（貼低）",
        f"- 宇宙：{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日"
        f"（{coverage.get('date_min', '?')} ~ {coverage.get('date_max', '?')}）",
        "",
        "## 1. 分位桶（桶 1＝最落後其族群；lift＝桶內前瞻起漲機率 / 全宇宙基率）",
        "",
    ]
    if buckets.is_empty():
        lines.append("（缺 rs_subind 欄、缺 close 或樣本不足分位，無法分桶）")
    else:
        for stratum in ("全體", "貼低", "非貼低"):
            sub = buckets.filter(pl.col("stratum") == stratum).sort("bucket")
            if sub.is_empty():
                continue
            lines += [
                f"### {stratum}",
                "",
                "| 桶 | rs_subind 中位 | 股日數 | 評估數 | 命中率 | lift | 前瞻報酬中位% |",
                "|---|---|---|---|---|---|---|",
            ]
            for r in sub.iter_rows(named=True):
                lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
                fm = f"{r['factor_median']:+.2f}" if r["factor_median"] is not None else "—"
                fr = (
                    f"{r['median_fwd_ret_pct']:+.1f}"
                    if r["median_fwd_ret_pct"] is not None
                    else "—"
                )
                lines.append(
                    f"| {r['bucket']} | {fm} | {r['n_stock_days']} | {r['n_eval']} "
                    f"| {r['hit_rate']:.1%} | {lift} | {fr} |"
                )
            lines.append("")

    lift_sig = _laggard_lift_significance(buckets, z_sig) if not buckets.is_empty() else {}
    lines += [
        "## 2. 起漲 lift 顯著性（on-target：最落後桶 vs 最領先桶 hit-rate 兩比例 z）",
        "",
    ]
    if not lift_sig:
        lines.append("（無分位桶，無法檢定）")
    else:
        lines += ["| 層 | 桶 lift 單調遞減 | 最落後桶 lift | 最領先桶 lift | z | 落後顯著(z>界) |",
                  "|---|---|---|---|---|---|"]
        for stratum in ("全體", "貼低", "非貼低"):
            lr = lift_sig.get(stratum)
            if lr is None:
                continue
            zt = f"{lr['z']:+.2f}" if lr["z"] is not None else "—"
            ll = f"{lr['lift_lo']:.2f}" if lr["lift_lo"] is not None else "—"
            lh = f"{lr['lift_hi']:.2f}" if lr["lift_hi"] is not None else "—"
            lines.append(
                f"| {stratum} | {'✅' if lr['monotone_dec'] else '❌'} | {ll} | {lh} | {zt} "
                f"| {'✅' if lr['sig'] else '❌'} |"
            )
        lines.append("")

    lines += [
        "## 3. 連續報酬診斷（Spearman ρ：rs_subind vs 前瞻報酬；非裁決閘、僅揭分流）",
        "",
    ]
    if spearman.is_empty():
        lines.append("（缺 rs_subind 欄或樣本不足）")
    else:
        sp_map = {r["stratum"]: r for r in spearman.iter_rows(named=True)}
        lines += ["| 層 | Spearman ρ | z |", "|---|---|---|"]
        for stratum in ("全體", "貼低", "非貼低"):
            sr = sp_map.get(stratum)
            if sr is None:
                continue
            rho = f"{sr['spearman_rho']:+.3f}" if sr["spearman_rho"] is not None else "—"
            z = f"{sr['z']:+.2f}" if sr["z"] is not None else "—"
            lines.append(f"| {stratum} | {rho} | {z} |")
        lines += [
            "",
            "> ρ≈0／正：落後**不**預測較高中位報酬（領先股反而高）——與 §2『落後↑起漲機率』分流。"
            "故 C-P2 須用 payoff/decay 四件套驗賺賠，不能只看 lift。",
        ]

    a = lift_sig.get("全體", {})
    low = lift_sig.get("貼低", {})
    high = lift_sig.get("非貼低", {})
    h1 = bool(a.get("sig") and a.get("monotone_dec"))
    h2 = bool(
        low.get("sig")
        and low.get("monotone_dec")
        and high.get("sig")
        and high.get("monotone_dec")
    )
    lines += ["", "## 4. 裁決（docs/16 H1 落後度起漲 lift 單調顯著・H2 控制位階後仍在）", ""]
    if not lift_sig:
        lines.append("- 無法檢定——標『資料累積後重校』。")
    elif h1 and h2:
        lines.append(
            "- **①全體落後桶起漲 lift 顯著高且單調遞減 ✅ 且 ②貼低/非貼低兩層皆然 ✅** → "
            "落後度**非僅位階代理**、控制位階後仍在＝獨立起漲-機率加分。**進 C-P2 測 S+ 內落後濾鏡"
            " precision 增量＋穩健度（須驗賺賠，因領先股中位報酬反而高）**。"
        )
    elif h1 and not h2:
        lines.append(
            "- **①全體落後顯著 ✅ 但 ②控制位階後某層崩 ❌** → 落後多由位階驅動（§D『位階做工』）。"
            "**否證 H2＝落後只是貼低代理**，個股訊號自足、不另立落後因子。"
        )
    else:
        lines.append(
            "- **①落後度起漲 lift 未單調顯著 ❌** → 落後與前瞻起漲無系統。**否證**（守 §D/B-P2）。"
        )
    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) rs_subind 只在有次產業標記股日有值（無標排除）；lift 以全宇宙基率為分母；",
        "> (2) 裁決用起漲 lift（§2）非前瞻報酬 Spearman（§3）——兩者分流，落後↑起漲機率≠↑中位報酬；",
        "> (3) 1 年單一樣本、個股事件稀疏；研究軌裁決非買賣訊；每季資料累積後重校。",
        "",
    ]
    return "\n".join(lines)


# ── M-Part C / C-P2：冠軍 S+ 內落後濾鏡 precision 增量 ＋ 賺賠驗證（docs/16 H3）─────────
# D-F4 拍板＝S+全體 vs S+且落後。precision 用冠軍 S+ 觸發；賺賠用 payoff_decay_table（冠軍 vs
# 冠軍+落後濾鏡）——因 C-P1 揭領先股中位報酬反高，落後濾鏡須驗賺賠不惡化才升級。全研究軌。


def laggard_filter_precision(
    panel: pl.DataFrame,
    episodes: pl.DataFrame,
    *,
    s_flow_col: str = "foreign_flow_20d_z",
    s_z_threshold: float = 0.5,
    s_low_pct: float = 15.0,
    lag_threshold: float = 0.0,
    lead_window: int = 15,
    occupy_days: int = 15,
    z_min_periods: int = 30,
) -> tuple[pl.DataFrame, float | None]:
    """H3（docs/16）：冠軍 S+（資金進+貼低）觸發內，加『落後其族群(rs_subind<lag_threshold)』濾鏡
    是否提升 precision。回 (precision_table, z)；table 三列 S+全體/S+且落後/S+且領先（n_eval/hits
    /hit_rate/lift），z＝落後 vs 領先 兩比例（獨立組乾淨檢定；S+全體＝兩者聯集當生產基線參考）。
    空輸入／缺 S 流向欄／缺 above_low／缺 rs_subind 回 (空表, None)。
    """
    if panel.is_empty() or episodes.is_empty():
        return pl.DataFrame(), None
    low_col = _position_low_col(panel)
    g_col = _rs_subind_col(panel)
    if s_flow_col not in panel.columns or low_col is None or g_col is None:
        return pl.DataFrame(), None
    calendar = sorted(panel["date"].unique().to_list())
    warmup_pos = min(z_min_periods, len(calendar) - 1)
    base_rate = compute_base_rate(
        episodes, calendar, panel["stock_id"].unique().to_list(),
        lead_window, occupy_days, warmup_pos, key_col="stock_id",
    )
    s_cond = [pl.col(s_flow_col) > s_z_threshold, pl.col(low_col) <= s_low_pct]
    s_trig = _stock_triggers(panel, s_cond).join(
        panel.select(["stock_id", "date", g_col]), on=["stock_id", "date"], how="left"
    )
    groups = [
        ("S+全體", s_trig),
        ("S+且落後", s_trig.filter(pl.col(g_col) < lag_threshold)),
        ("S+且領先", s_trig.filter(pl.col(g_col) >= lag_threshold)),
    ]
    rows: list[dict] = []
    for name, t in groups:
        stats = evaluate_triggers(
            t.select(["stock_id", "date"]), episodes, calendar,
            lead_window, occupy_days, warmup_pos, key_col="stock_id", base_rate=base_rate,
        )
        rows.append({
            "group": name, "n_eval": stats["n_triggers"], "hits": stats["hits"],
            "hit_rate": stats["hit_rate"], "lift": stats["lift"],
        })
    by = {r["group"]: r for r in rows}
    lag, lead = by["S+且落後"], by["S+且領先"]
    z = _two_prop_z(lag["hits"], lag["n_eval"], lead["hits"], lead["n_eval"])
    return pl.DataFrame(rows), z


def render_laggard_filter_report(
    precision: pl.DataFrame,
    z: float | None,
    payoff_base: pl.DataFrame,
    payoff_filt: pl.DataFrame,
    anchor_label: str,
    params: dict,
    coverage: dict,
    z_sig: float = 1.96,
) -> str:
    """C-P2 報告 markdown（docs/16 H3）：H3 precision 增量（S+全體/落後/領先＋z）＋ payoff/decay
    對照（冠軍 vs 冠軍+落後濾鏡），依『precision 顯著增量 且 賺賠不惡化』裁決升級/否證。"""
    key_h = max(params.get("horizons", [20]))
    lines = [
        "# 冠軍 S+ 內落後濾鏡 — precision 增量 × 賺賠驗證（docs/16 C-P2 / H3）",
        "",
        f"- 冠軍 S+：{params.get('s_flow_col', '?')} z>{params.get('s_z_threshold', '?')} 且貼低"
        f"・落後濾鏡：rs_subind < {params.get('lag_threshold', 0)}（落後其族群）",
        f"- 錨定 label：{anchor_label}・賺賠前瞻窗 {params.get('horizons', [])}（裁決看 {key_h}d）",
        f"- 宇宙：{coverage.get('n_stocks', 0)} 檔・{coverage.get('n_trading_days', 0)} 交易日",
        "",
        "## 1. H3 precision（冠軍 S+ 觸發內，落後 vs 領先；lift＝命中率/全宇宙基率）",
        "",
    ]
    if precision.is_empty():
        lines.append("（缺 S 流向欄/above_low/rs_subind，無法分組）")
        return "\n".join(lines)
    by = {r["group"]: r for r in precision.iter_rows(named=True)}
    lines += ["| 組 | 觸發 | 命中 | 命中率 | lift |", "|---|---|---|---|---|"]
    for g in ("S+全體", "S+且落後", "S+且領先"):
        r = by.get(g)
        if r is None:
            continue
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        lines.append(f"| {g} | {r['n_eval']} | {r['hits']} | {r['hit_rate']:.1%} | {lift} |")
    zt = f"{z:+.2f}" if z is not None else "—"
    lines += ["", f"- 落後 vs 領先 兩比例 z = {zt}（>界＝落後濾鏡顯著提升命中）", ""]

    lines += [
        "## 2. 賺賠 payoff/decay（冠軍 vs 冠軍+落後濾鏡；excess＝訊號中位 − 全宇宙同窗中位）",
        "",
        "| 前瞻(日) | 冠軍中位% | 冠軍超額% | +落後中位% | +落後超額% | +落後勝率 | +落後賠率 |",
        "|---|---|---|---|---|---|---|",
    ]
    pb = {r["horizon_d"]: r for r in payoff_base.iter_rows(named=True)}
    pf = {r["horizon_d"]: r for r in payoff_filt.iter_rows(named=True)}

    def _f(v: float | None, p: str = "+.1f") -> str:
        return format(v, p) if v is not None else "—"

    for h in params.get("horizons", []):
        b, f = pb.get(h), pf.get(h)
        if b is None and f is None:
            continue
        b = b or {}
        f = f or {}
        lines.append(
            f"| {h} | {_f(b.get('median_ret_pct'))} | {_f(b.get('excess_median_pct'))} "
            f"| {_f(f.get('median_ret_pct'))} | {_f(f.get('excess_median_pct'))} "
            f"| {_f(f.get('win_rate'), '.0%')} | {_f(f.get('payoff_ratio'), '.2f')} |"
        )

    lift_all = by.get("S+全體", {}).get("lift")
    lift_lag = by.get("S+且落後", {}).get("lift")
    prec_gain = (
        lift_lag is not None and lift_all is not None and lift_lag > lift_all
        and z is not None and z > z_sig
    )
    b_key, f_key = pb.get(key_h, {}), pf.get(key_h, {})
    bm, fm = b_key.get("median_ret_pct"), f_key.get("median_ret_pct")
    payoff_ok = bm is not None and fm is not None and fm >= bm
    lines += ["", f"## 3. 裁決（precision 顯著增量 且 {key_h}d 中位報酬不惡化）", ""]
    if prec_gain and payoff_ok:
        lines.append(
            "- **precision 顯著增量 ✅ 且賺賠不惡化 ✅** → **落後濾鏡升級為冠軍 S+ 的進場加分**"
            "（上線前另開生產 milestone：S+ 且 rs_subind<0 提高權重/分批）。"
        )
    elif prec_gain and not payoff_ok:
        lines.append(
            f"- **precision 顯著增量 ✅ 但 {key_h}d 中位報酬較冠軍低 ❌**（C-P1 警示成真：落後↑起漲"
            "機率卻↓報酬）→ **當『觀察/分批』濾鏡、不升為主加分**；命中多但賺賠未改善。"
        )
    else:
        lines.append(
            "- **precision 未顯著增量 ❌** → 落後濾鏡在冠軍 S+ 上不加值。**否證 H3、訊號自足**。"
        )
    lines += [
        "",
        "---",
        "",
        "> 誠實但書：(1) z＝落後 vs 領先 獨立組（S+全體＝聯集生產基線）；前瞻報酬原始收盤不還原；",
        "> (2) 雙重濾鏡樣本更稀疏，holdout/流動性硬化留待資料累積（冠軍版已在 B-P1 驗）；",
        "> (3) 研究軌裁決非買賣訊、非目標價；每季資料累積後重校。",
        "",
    ]
    return "\n".join(lines)
