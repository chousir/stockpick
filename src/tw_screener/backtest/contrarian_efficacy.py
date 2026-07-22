"""backtest/contrarian_efficacy.py — M-BR1 Phase 2 因子驗證（規劃書 24 §3）。

把 Phase 1 描述欄的聯合桶「flow_inflection=轉買 × base_proximity∈{在低,貼低}」，
用 WS-C/WS-D 對 rotation/laggard 做過的 **point-in-time 面板重建**＋桶 lift＋
block-bootstrap CI＋regime 切片＋walk-forward，回答唯一問題（§3 開工三行）：
兩條件同時成立的桶，前瞻 alpha 是否顯著 > 0 且跨 regime 一致？

**升 gate 唯一開關**（docs/24 §1 硬門檻，寫死不得因手感調鬆）：block-bootstrap CI
排 0 ＋ ≥4/5 walk-forward 同號 ＋ 跨 ≥2 regime（regime_alignment_verdict 非 bull-only）。
三缺一 → `contrarian_base` 維持描述欄（如 flow_turn 之否證前例，docs/22 §2）。

flow_inflection/base_proximity/dist_to_low_pct 直接**重用** analysis/contrarian.py 純函式
（避免口徑漂移，同 laggard 重用 base_zone 切法）。純函式；IO 由 runner 負責。
"""

from __future__ import annotations

import polars as pl

from tw_screener.analysis.contrarian import (
    BASE_AT_LOW,
    BASE_NEAR_LOW,
    FLOW_TURN_BUY,
    base_proximity,
    dist_to_low_pct,
    flow_inflection,
)
from tw_screener.backtest.factor_lab import (
    REGIME_MIN_N,
    Split,
    bootstrap_mean_ci,
    moving_block_bootstrap_ci,
    walk_forward_splits,
)

# 焦點桶的貼低集合（§4.1；重用常數避免漂移）
_CONTRARIAN_PROX = (BASE_AT_LOW, BASE_NEAR_LOW)
# 焦點桶標籤（報表與裁決共用同一字串，防兩處分歧）
FOCAL_LABEL = "contrarian_base"
ALL_LABEL = "（全體）"
# §1「含 2022 空頭」的 regime 代號（V2 引擎；防禦≈2022 空頭年，升 gate 必含此片非 thin 且同向）
DEFENSIVE_LABEL = "防禦"


def add_universe_lift(
    signal: pl.DataFrame, horizons: tuple[int, ...] = (5, 10, 20, 40)
) -> pl.DataFrame:
    """加 `lift{h}` ＝ alpha{h} − 當日全宇宙 alpha{h} 均值——**正確的零假設基準**。

    為何必要：面板 alpha{h}=r−當日**中位**，報酬右偏 → 全宇宙 alpha **均值 > 0**
    （非 0）。故「桶 alpha 均值 > 0」不是「打敗市場」的檢定（隨機桶均值就 >0）。
    lift＝桶 − 同日宇宙均值，零假設才是 0；桶要有 edge，lift 才須顯著為正。
    宇宙＝signal 全體列（週快照抽樣後的可比宇宙）。
    """
    if signal.is_empty():
        return signal
    exprs = [
        (pl.col(f"alpha{h}") - pl.col(f"alpha{h}").mean().over("date")).alias(f"lift{h}")
        for h in horizons
        if f"alpha{h}" in signal.columns
    ]
    return signal.with_columns(exprs) if exprs else signal


def contrarian_signal_panel(
    panel: pl.DataFrame,
    *,
    low_window: int = 60,
    flow_long: int = 20,
    flow_short: int = 5,
    min_shares: float = 1_000_000.0,
    stall_share_pct: float = 5.0,
    accel_ratio: float = 1.0,
    at_low_pct: float = 2.0,
    near_low_pct: float = 5.0,
    mid_pct: float = 20.0,
) -> pl.DataFrame:
    """每列附 point-in-time `flow_inflection` / `base_proximity` / `contrarian_base`。

    重建口徑對齊生產（group_report §2）：
    - net_20d/net_5d ＝日 `foreign_net` 的滾動和（生產列的 foreign_net 已是 20 日累計，
      面板為日淨額，故此處 rolling_sum 還原）；
    - dist_low_60d ＝ close 對自身 `low_window` 日滾動低（含當日）的距離%。

    誠實 null：滾動窗未滿（min_periods=window）→ 該衍生欄 null，`flow_inflection`
    / `base_proximity` 對 null 各自回 None／空字串（不臆造位階/流向）。前視安全：
    滾動窗僅含當日及過去，entry＝訊號日，前瞻報酬 alpha{h} 已在 panel 算好（無跨界）。

    需要欄：date / stock_id / close / foreign_net。其餘（alpha{h}/regime/ma60_dist_pct）
    原樣 carry，缺則呼叫端自負。
    """
    if panel.is_empty():
        return panel
    need = {"date", "stock_id", "close", "foreign_net"}
    missing = need - set(panel.columns)
    if missing:
        raise ValueError(f"contrarian_signal_panel 缺必要欄：{sorted(missing)}")

    df = panel.sort(["stock_id", "date"]).with_columns(
        pl.col("close").rolling_min(window_size=low_window).over("stock_id").alias("_low"),
        pl.col("foreign_net").rolling_sum(window_size=flow_long).over("stock_id").alias("_f20"),
        pl.col("foreign_net").rolling_sum(window_size=flow_short).over("stock_id").alias("_f5"),
    )
    # 重用純函式（含 dist_to_low_pct 的四捨五入到 1 位——影響 ≤2.0 貼低邊界，故不可另寫）
    df = df.with_columns(
        pl.struct(["close", "_low"])
        .map_elements(
            lambda s: dist_to_low_pct(s["close"], s["_low"]),
            return_dtype=pl.Float64,
        )
        .alias("dist_low_60d_pct")
    )
    df = df.with_columns(
        pl.col("dist_low_60d_pct")
        .map_elements(
            lambda v: base_proximity(
                v, at_low_pct=at_low_pct, near_low_pct=near_low_pct, mid_pct=mid_pct
            ),
            return_dtype=pl.Utf8,
            skip_nulls=False,  # dist null → base_proximity(None)="" 與生產逐值一致（非 skip→null）
        )
        .alias("base_proximity"),
        pl.struct(["_f20", "_f5"])
        .map_elements(
            lambda s: flow_inflection(
                s["_f20"],
                s["_f5"],
                min_shares=min_shares,
                stall_share_pct=stall_share_pct,
                accel_ratio=accel_ratio,
            ),
            return_dtype=pl.Utf8,
        )
        .alias("flow_inflection"),
    )
    df = df.with_columns(
        (
            (pl.col("flow_inflection") == FLOW_TURN_BUY)
            & pl.col("base_proximity").is_in(_CONTRARIAN_PROX)
        ).alias(FOCAL_LABEL)
    )
    return df.drop(["_low", "_f20", "_f5"])


def _lift_row(
    label: str, g: pl.DataFrame, tgt: str, mid_date: object, n_boot: int
) -> dict:
    """單桶 pooled lift 列：mean/bootstrap CI/median/win_rate/前後半同向。"""
    vals = [float(v) for v in g[tgt].to_list() if v is not None]
    ci_lo, ci_hi = bootstrap_mean_ci(vals, n_boot=n_boot) if vals else (None, None)
    h1 = [float(v) for v in g.filter(pl.col("date") <= mid_date)[tgt].to_list() if v is not None]
    h2 = [float(v) for v in g.filter(pl.col("date") > mid_date)[tgt].to_list() if v is not None]
    med = g[tgt].median()
    return {
        "bucket": label,
        "n": len(vals),
        "mean": (sum(vals) / len(vals)) if vals else None,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "median": float(med) if isinstance(med, (int, float)) else None,
        "win_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
        "mean_h1": (sum(h1) / len(h1)) if h1 else None,
        "mean_h2": (sum(h2) / len(h2)) if h2 else None,
    }


def contrarian_lift_table(
    signal: pl.DataFrame,
    horizons: tuple[int, ...] = (5, 10, 20, 40),
    n_boot: int = 1000,
) -> pl.DataFrame:
    """焦點桶（contrarian_base=True）vs 全體：各前瞻窗 alpha pooled mean lift。

    pooled CI＝bootstrap_mean_ci（與 laggard_cell_grid 同法）；前後半 mean_h1/mean_h2
    供同向性目視。**pooled 只作方向與效應量參考**，升 gate 的 CI 判準走 walk-forward
    ＋regime 切片（moving-block，處理前瞻窗重疊；docs/22 §7.2）。
    """
    schema = {
        "horizon": pl.Int64, "bucket": pl.Utf8, "n": pl.UInt32, "mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "median": pl.Float64,
        "win_rate": pl.Float64, "mean_h1": pl.Float64, "mean_h2": pl.Float64,
    }
    if signal.is_empty() or FOCAL_LABEL not in signal.columns:
        return pl.DataFrame(schema=schema)
    mid_date = signal["date"].median()
    rows: list[dict] = []
    for h in horizons:
        tgt = f"alpha{h}"
        if tgt not in signal.columns:
            continue
        sub = signal.drop_nulls([tgt])
        focal = sub.filter(pl.col(FOCAL_LABEL))
        rows.append({"horizon": h, **_lift_row(FOCAL_LABEL, focal, tgt, mid_date, n_boot)})
        rows.append({"horizon": h, **_lift_row(ALL_LABEL, sub, tgt, mid_date, n_boot)})
    return pl.DataFrame(rows, schema=schema).sort(["horizon", "bucket"])


def contrarian_decomp_grid(
    signal: pl.DataFrame,
    horizon: int = 20,
    n_boot: int = 1000,
) -> pl.DataFrame:
    """flow_inflection × base_proximity 全 cell 分解格（單一窗）——lift 來自哪一臂？

    照 laggard「全 cell 全列」慣例，不藏格；讓讀者看見焦點桶（轉買×貼近低）的 lift
    是否真來自交互作用，而非單臂（例如 base 貼低本身就有 lift、與 flow 無關）。
    """
    schema = {
        "flow": pl.Utf8, "base": pl.Utf8, "n": pl.UInt32, "mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "win_rate": pl.Float64,
    }
    tgt = f"alpha{horizon}"
    need = {"flow_inflection", "base_proximity", tgt}
    if signal.is_empty() or not need.issubset(signal.columns):
        return pl.DataFrame(schema=schema)
    sub = signal.drop_nulls([tgt, "flow_inflection", "base_proximity"]).filter(
        pl.col("flow_inflection").is_not_null() & (pl.col("base_proximity") != "")
    )
    rows: list[dict] = []
    for key, g in sub.group_by(["flow_inflection", "base_proximity"], maintain_order=True):
        vals = [float(v) for v in g[tgt].to_list() if v is not None]
        ci_lo, ci_hi = bootstrap_mean_ci(vals, n_boot=n_boot) if vals else (None, None)
        rows.append(
            {
                "flow": str(key[0]), "base": str(key[1]), "n": len(vals),
                "mean": (sum(vals) / len(vals)) if vals else None,
                "ci_lo": ci_lo, "ci_hi": ci_hi,
                "win_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
            }
        )
    return pl.DataFrame(rows, schema=schema).sort(["flow", "base"])


def contrarian_walk_forward(
    signal: pl.DataFrame,
    horizon: int = 20,
    n_splits: int = 5,
    min_train_frac: float = 0.4,
    embargo_td: int | None = None,
    metric: str = "lift",
) -> tuple[pl.DataFrame, int, int]:
    """walk-forward 各測試段焦點桶 lift 均值＋**為正**段數（§1「≥4/5 walk-forward 同號」）。

    切分沿用 factor_lab.walk_forward_splits（expanding + embargo 防前瞻窗跨界）。各段
    測試窗內取焦點桶 per-date mean(metric) 的等權平均。**升機會層 BUY gate 要求一致
    「打敗宇宙」＝ lift 為正**，故計「為正段數」而非「與整體同號」（整體恆負時同號
    只代表一致落後、非 edge）。metric 預設 lift{h}（零假設 0）；傳 alpha 則測原始桶。

    Returns: (各段表, 為正段數, 有效段數)。
    """
    schema = {
        "split_id": pl.Int64, "test_start": pl.Date, "test_end": pl.Date,
        "n": pl.Int64, "n_dates": pl.Int64, "mean": pl.Float64,
    }
    tgt = f"{metric}{horizon}"
    if signal.is_empty() or FOCAL_LABEL not in signal.columns or tgt not in signal.columns:
        return pl.DataFrame(schema=schema), 0, 0
    focal = signal.filter(pl.col(FOCAL_LABEL)).drop_nulls([tgt])
    if focal.is_empty():
        return pl.DataFrame(schema=schema), 0, 0

    emb = (horizon + 1) if embargo_td is None else embargo_td
    dates = signal["date"].unique().to_list()
    splits: list[Split] = walk_forward_splits(
        dates, n_splits=n_splits, min_train_frac=min_train_frac, embargo_td=emb
    )
    rows: list[dict] = []
    positive = valid = 0
    for sp in splits:
        g = focal.filter(
            (pl.col("date") >= sp.test_start) & (pl.col("date") <= sp.test_end)
        )
        m = _daily_mean(g, tgt)
        rows.append(
            {
                "split_id": sp.split_id, "test_start": sp.test_start,
                "test_end": sp.test_end, "n": g.height,
                "n_dates": g["date"].n_unique(), "mean": m,
            }
        )
        if m is not None:
            valid += 1
            if m > 0:
                positive += 1
    return pl.DataFrame(rows, schema=schema), positive, valid


def _daily_mean(df: pl.DataFrame, tgt: str) -> float | None:
    """焦點桶 per-date mean 的等權平均（與 regime_mean_slices/walk-forward 同一統計量）。"""
    daily = df.group_by("date").agg(pl.col(tgt).mean().alias("_m"))
    vals = [float(v) for v in daily["_m"].to_list() if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def contrarian_full_ci(
    signal: pl.DataFrame,
    horizon: int = 20,
    block_len: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
    metric: str = "lift",
) -> tuple[float | None, float | None, float | None, int]:
    """焦點桶全樣本 per-date mean(metric) 序列的 **moving-block bootstrap** CI（§1 CI 判準）。

    §1「block-bootstrap CI 排 0」的正解——per-date lift 序列做 moving-block（處理相鄰
    週前瞻窗重疊，pooled iid 偏窄不可用於裁決，docs/22 §7.2）。metric 預設 lift{h}。

    Returns: (mean, ci_lo, ci_hi, n_dates)。
    """
    tgt = f"{metric}{horizon}"
    if signal.is_empty() or FOCAL_LABEL not in signal.columns or tgt not in signal.columns:
        return None, None, None, 0
    focal = signal.filter(pl.col(FOCAL_LABEL)).drop_nulls([tgt])
    daily = focal.group_by("date").agg(pl.col(tgt).mean().alias("_m")).sort("date")
    vals = [float(v) for v in daily["_m"].to_list() if v is not None]
    if not vals:
        return None, None, None, 0
    lo, hi = moving_block_bootstrap_ci(vals, block_len=block_len, n_boot=n_boot, seed=seed)
    return sum(vals) / len(vals), lo, hi, len(vals)


def contrarian_gate_verdict(
    full_ci: tuple[float | None, float | None, float | None, int],
    wf_pos: int,
    wf_valid: int,
    regime_slices: pl.DataFrame,
    wf_min_frac: float = 0.8,
    defensive_label: str = DEFENSIVE_LABEL,
) -> tuple[bool, str]:
    """§1 三叉硬門檻合議（全部以 **lift**＝桶−宇宙均值為量尺，零假設 0）。

    三缺一 → 不升。門檻（寫死，不因手感調鬆）：
    1. **block-bootstrap CI 排 0 且為正**：全樣本 lift 序列 moving-block CI 下界 > 0。
    2. **walk-forward ≥wf_min_frac 段 lift 為正**：一致打敗宇宙（非一致落後）。
    3. **跨 ≥2 regime 且含 2022 空頭**：≥2 個可判（非 thin）regime 切片 lift 為正，
       **且防禦（≈2022 空頭）該片可判且為正**——§1「尤須含 2022 空頭」的硬化，
       防 bull-only 升級（regime_alignment_verdict 只數 ≥2 同號、不強制含空頭，故此處自判）。

    Returns: (是否升 Phase 3 候選, 裁決字串含各門檻通過與否＋不過原因)。
    """
    _mean, lo, hi, _n = full_ci
    ci_ok = lo is not None and lo > 0  # lift 下界 > 0：顯著且正向打敗宇宙
    wf_ok = wf_valid > 0 and wf_pos >= (wf_valid * wf_min_frac)

    has_slices = not regime_slices.is_empty() and {"thin", "mean", "regime"}.issubset(
        regime_slices.columns
    )
    usable = (
        regime_slices.filter(~pl.col("thin") & pl.col("mean").is_not_null())
        if has_slices
        else pl.DataFrame()
    )
    pos_regimes = (
        usable.filter(pl.col("mean") > 0)["regime"].to_list() if not usable.is_empty() else []
    )
    defensive_ok = defensive_label in pos_regimes
    regime_ok = len(pos_regimes) >= 2 and defensive_ok

    passed = bool(ci_ok and wf_ok and regime_ok)
    ci_txt = "—" if lo is None else f"lift95%下界{lo:+.2f}%"
    reg_detail = (
        f"可判且正切片 {len(pos_regimes)}（{'、'.join(pos_regimes) if pos_regimes else '無'}）"
        f"、含防禦={'是' if defensive_ok else '否'}"
    )
    parts = [
        f"CI排0且正={'✓' if ci_ok else '✗'}（{ci_txt}）",
        f"walk-forward為正={wf_pos}/{wf_valid}{'✓' if wf_ok else '✗'}",
        f"跨regime含空頭={'✓' if regime_ok else '✗'}（{reg_detail}）",
    ]
    head = "升 Phase 3 機會層 gate 候選（三門檻齊過）" if passed else "**否證——維持描述欄**（未過）"
    return passed, f"{head}｜" + "、".join(parts)


# thin 門檻沿用 factor_lab.REGIME_MIN_N（30），供 runner 標註樣本薄切片
REGIME_THIN_N = REGIME_MIN_N
