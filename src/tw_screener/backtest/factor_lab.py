"""backtest/factor_lab.py — WS-B 統一因子實驗台（W28 委託核心交付）。

任何因子丟進來，回答同一組問題（防過擬合約定內建、不可繞過）：
1. **pooled Spearman IC**＋Fisher-z 95% CI＋n（與 diagnostic.signal_ic_table 同語義，
   docs/19 基準可直接對表）。
2. **walk-forward 一致性**：expanding window 切 ≥3 段，各測試段唯讀 IC＋CI；
   訓練/測試邊界帶 embargo（≥ horizon+1 交易日）防前瞻窗跨界滲漏。
3. **分桶單調性**：同日橫斷面分位桶 → 各桶 n/median/mean/win_rate（粗桶看形狀，
   不釘尖銳門檻）。
4. **殘差 IC**（controls）：rank-within-date 後遞迴偏相關——回答 docs/16 H2
   「控制 ma60_dist 後還剩多少」。無 numpy 依賴（鐵律 4）。
5. **grid_scan**：格點 ≤3 維、每維 ≤5 檔位（超出直接 raise）；訓練段選參、
   測試段唯讀；**輸出所有 cell**，禁止只呈現最好的。

量尺與假設同名（playbook/20 §6）：問 IC 用 IC 裁決；CI 跨 0＝「無證據」。
純函式；IO 由 factor_lab_runner 負責。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import product

import polars as pl

# 統計基元與 M-Diag1 同源（單一實作防漂移；diagnostic 為既有出處）
from tw_screener.backtest.diagnostic import _fisher_ci as fisher_ci
from tw_screener.backtest.diagnostic import _spearman as spearman

_GRID_MAX_DIMS = 3
_GRID_MAX_LEVELS = 5


@dataclass(frozen=True)
class Split:
    """一段 walk-forward：train ≤ train_end；test ∈ [test_start, test_end]（皆含）。"""

    split_id: int
    train_end: date
    test_start: date
    test_end: date


@dataclass
class FactorReport:
    """evaluate() 的完整輸出（各表自帶 n；空表＝該項無法計算，不臆造）。"""

    factor: str
    horizon: int
    target: str
    pooled: pl.DataFrame        # ic / ci_lo / ci_hi / n（1 列）
    splits: pl.DataFrame        # split_id / test_start / test_end / ic / ci_lo / ci_hi / n
    buckets: pl.DataFrame       # bucket / n / median / mean / win_rate
    residual: pl.DataFrame = field(default_factory=pl.DataFrame)  # controls 偏相關
    controls: tuple[str, ...] = ()
    # ── WS-I 推論硬化（moving-block bootstrap CI；docs/22 §7.2 誠實帳的修正）──
    mean_daily_ic: float | None = None                        # per-date IC 等權 mean（A 法）
    bs_ci: tuple[float | None, float | None] = (None, None)    # moving-block bootstrap CI95（B 法）
    nonoverlap: dict = field(default_factory=dict)             # {ic,ci_lo,ci_hi,n_dates}（D 法）
    inference_meta: dict = field(default_factory=dict)         # {method,B,L,seed,T,span}

    @property
    def consistent_sign(self) -> bool | None:
        """walk-forward 各段 IC 同號？（段數 <2 → None，樣本不夠談一致性）"""
        ics = [r for r in self.splits["ic"].to_list() if r is not None]
        if len(ics) < 2:
            return None
        return all(v > 0 for v in ics) or all(v < 0 for v in ics)


def walk_forward_splits(
    dates: Sequence[date],
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    embargo_td: int = 0,
) -> list[Split]:
    """expanding-window 切分：唯一交易日序列 → n_splits 段連續測試塊。

    首段訓練 ≥ min_train_frac 全長；之後每段訓練＝該測試塊之前全部（expanding）。
    embargo_td：訓練截止與測試起點間隔的交易日數（防前瞻窗跨界滲漏，
    呼叫端應傳 horizon+1）。可用日不足（每測試塊 <5 日）→ 回空 list，不硬切。
    """
    uniq = sorted(set(dates))
    n = len(uniq)
    if n_splits < 1 or n < 20:
        return []
    train0 = max(int(n * min_train_frac), 1)
    test_total = n - train0
    block = test_total // n_splits
    if block < 5:
        return []
    out: list[Split] = []
    for k in range(n_splits):
        start_i = train0 + k * block
        end_i = (start_i + block - 1) if k < n_splits - 1 else (n - 1)
        train_end_i = start_i - 1 - embargo_td
        if train_end_i < 1:
            continue
        out.append(
            Split(
                split_id=k + 1,
                train_end=uniq[train_end_i],
                test_start=uniq[start_i],
                test_end=uniq[end_i],
            )
        )
    return out


def _ic_row(df: pl.DataFrame, factor: str, target: str) -> dict:
    ic, n = spearman(df, factor, target)
    lo, hi = fisher_ci(ic, n) if ic is not None else (None, None)
    return {"ic": ic, "ci_lo": lo, "ci_hi": hi, "n": n}


def _rank_within_date(df: pl.DataFrame, cols: Sequence[str]) -> pl.DataFrame:
    """同日橫斷面百分位 rank（tie 取平均）→ `_rk_<col>` 欄；先丟任一欄 null 列。"""
    clean = df.drop_nulls(list(cols))
    return clean.with_columns(
        *[
            (pl.col(c).rank(method="average").over("date") / pl.len().over("date"))
            .alias(f"_rk_{c}")
            for c in cols
        ]
    )


def _pearson(df: pl.DataFrame, x: str, y: str) -> float | None:
    v = df.select(pl.corr(x, y)).item()
    return float(v) if v is not None and not math.isnan(v) else None


def _partial_corr(df: pl.DataFrame, x: str, y: str, controls: Sequence[str]) -> float | None:
    """遞迴偏相關 ρ_xy·C（rank 欄上的 Pearson＝Spearman 語義）。"""
    if not controls:
        return _pearson(df, x, y)
    c, rest = controls[-1], list(controls[:-1])
    rxy = _partial_corr(df, x, y, rest)
    rxc = _partial_corr(df, x, c, rest)
    ryc = _partial_corr(df, y, c, rest)
    if rxy is None or rxc is None or ryc is None:
        return None
    denom = math.sqrt((1 - rxc**2) * (1 - ryc**2))
    if denom < 1e-12:
        return None
    return (rxy - rxc * ryc) / denom


def residual_ic(
    df: pl.DataFrame, factor: str, target: str, controls: Sequence[str]
) -> pl.DataFrame:
    """控制 controls 後因子還剩多少：rank-within-date → 偏相關＋Fisher-z 近似 CI。

    docs/16 H2 精神：任何因子都要回答「控制 ma60_dist 後還剩多少」。
    CI 為近似（偏相關自由度略高估），報表標明。
    """
    schema = {"ic": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64, "n": pl.UInt32}
    cols = [factor, target, *controls]
    if df.is_empty() or not set(cols).issubset(df.columns):
        return pl.DataFrame(schema=schema)
    ranked = _rank_within_date(df, cols)
    n = ranked.height
    if n < 30:
        return pl.DataFrame(schema=schema)
    rho = _partial_corr(ranked, f"_rk_{factor}", f"_rk_{target}", [f"_rk_{c}" for c in controls])
    lo, hi = fisher_ci(rho, n) if rho is not None else (None, None)
    return pl.DataFrame([{"ic": rho, "ci_lo": lo, "ci_hi": hi, "n": n}], schema=schema)


def bucket_table(
    df: pl.DataFrame,
    factor: str,
    target: str,
    buckets: int = 5,
) -> pl.DataFrame:
    """同日橫斷面分位桶 → 各桶 n/median/mean/win_rate（桶 1＝因子最低）。

    當日有效樣本 < buckets 的日子整日跳過（分位桶無意義）。
    """
    schema = {
        "bucket": pl.Int64, "n": pl.UInt32,
        "median": pl.Float64, "mean": pl.Float64, "win_rate": pl.Float64,
    }
    if df.is_empty() or not {factor, target, "date"}.issubset(df.columns):
        return pl.DataFrame(schema=schema)
    clean = df.drop_nulls([factor, target])
    clean = clean.filter(pl.len().over("date") >= buckets)
    if clean.is_empty():
        return pl.DataFrame(schema=schema)
    pct = pl.col(factor).rank(method="average").over("date") / pl.len().over("date")
    assigned = clean.with_columns(
        (pct * buckets).ceil().clip(1, buckets).cast(pl.Int64).alias("bucket")
    )
    return (
        assigned.group_by("bucket")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            pl.col(target).median().alias("median"),
            pl.col(target).mean().alias("mean"),
            (pl.col(target) > 0).mean().alias("win_rate"),
        )
        .sort("bucket")
        .select(list(schema))
    )


def daily_ic_series(df: pl.DataFrame, factor: str, target: str) -> list[tuple[date, float]]:
    """每交易日橫斷面 Spearman IC（WS-I A 法）：該日有效樣本 <10 檔跳過該日。

    回 [(date, ic), ...] 依日期升冪；點估計（dates 等權 mean）由呼叫端算——
    這裡只給序列，供 bootstrap／未來 debug 共用同一份底料。
    """
    if df.is_empty() or not {factor, target, "date"}.issubset(df.columns):
        return []
    clean = df.drop_nulls([factor, target])
    sizes = clean.group_by("date").agg(pl.len().alias("_n"))
    keep = sizes.filter(pl.col("_n") >= 10)["date"].to_list()
    if not keep:
        return []
    thick = clean.filter(pl.col("date").is_in(keep))
    ranked = _rank_within_date(thick, [factor, target])
    ic_df = (
        ranked.group_by("date")
        .agg(pl.corr(f"_rk_{factor}", f"_rk_{target}").alias("ic"))
        .drop_nulls("ic")
        .sort("date")
    )
    return list(zip(ic_df["date"].to_list(), ic_df["ic"].to_list(), strict=True))


def _stride_dates(dates: Sequence[date], stride: int) -> list[date]:
    """非重疊 stride 抽樣（WS-I D 法）：唯一日期升冪排序，每隔 stride 天取一天（含第一天）。"""
    uniq = sorted(set(dates))
    if stride < 1:
        return uniq
    return uniq[::stride]


def nonoverlap_ic(df: pl.DataFrame, factor: str, target: str, horizon: int) -> dict:
    """非重疊子樣本（stride=horizon）pooled Spearman IC＋Fisher CI（WS-I D 法）。

    相鄰觀測間隔 ≥horizon 交易日 → 前瞻窗不重疊 → Fisher-z 常態假設近似成立，
    與 moving-block bootstrap（B 法）互為交叉檢驗。回 {ic,ci_lo,ci_hi,n_dates}
    （資料不足 → 全 None／0，如實標註不臆造）。
    """
    empty: dict = {"ic": None, "ci_lo": None, "ci_hi": None, "n_dates": 0}
    if df.is_empty() or not {factor, target, "date"}.issubset(df.columns):
        return empty
    picked = _stride_dates(df["date"].to_list(), horizon)
    if not picked:
        return empty
    sub = df.filter(pl.col("date").is_in(picked)).drop_nulls([factor, target])
    ic, n = spearman(sub, factor, target)
    lo, hi = fisher_ci(ic, n) if ic is not None else (None, None)
    return {"ic": ic, "ci_lo": lo, "ci_hi": hi, "n_dates": len(picked)}


def evaluate(
    df: pl.DataFrame,
    factor: str | pl.Expr,
    horizon: int = 20,
    target: str | None = None,
    buckets: int = 5,
    controls: Sequence[str] = (),
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    embargo_td: int | None = None,
    inference: str = "block_bootstrap",
    n_boot: int = 1000,
    seed: int = 42,
    block_td: int | None = None,
) -> FactorReport:
    """統一因子評估（模組 docstring 的 1–4 全套＋WS-I 推論硬化）。

    Args:
        df: 面板形資料（需含 date 與 factor/target/controls 欄）。
        factor: 欄名或 pl.Expr（Expr 需 .alias 命名）。
        horizon: 前瞻窗（交易日）；決定預設 target、embargo 與 bootstrap 塊長。
        target: 預設 f"alpha{horizon}"（面板超額欄）。
        controls: 殘差 IC 的控制欄（如 ["ma60_dist_pct"]）。
        embargo_td: 預設 horizon+1（前瞻窗跨界滲漏的最小隔離）。
        inference: CI 推論法，預設 "block_bootstrap"（WS-I 新預設，修正週頻/日頻
            快照×前瞻窗重疊下 Fisher-z 偏窄，見 docs/22 §7.2）。pooled 欄的
            Fisher-z 恆算（對照連續性）；傳其他值只跳過 bootstrap 計算，不崩潰。
        n_boot: moving-block bootstrap 重抽次數。
        seed: bootstrap 亂數種子（同 seed 全 deterministic）。
        block_td: bootstrap 塊長（交易日）；None → max(horizon+1, 2)。
    """
    target = target or f"alpha{horizon}"
    if isinstance(factor, pl.Expr):
        name = factor.meta.output_name()
        df = df.with_columns(factor)
        factor = name
    empty_ic = pl.DataFrame(
        schema={"ic": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64, "n": pl.UInt32}
    )
    if df.is_empty() or not {factor, target, "date"}.issubset(df.columns):
        return FactorReport(
            factor=str(factor), horizon=horizon, target=target,
            pooled=empty_ic, splits=pl.DataFrame(), buckets=pl.DataFrame(),
            controls=tuple(controls),
            inference_meta={
                "method": inference, "B": n_boot, "L": 0, "seed": seed, "T": 0, "span": None,
            },
        )
    base = df.drop_nulls([factor, target])
    pooled = pl.DataFrame([_ic_row(base, factor, target)])

    block_len = block_td if block_td is not None else max(horizon + 1, 2)
    daily = daily_ic_series(base, factor, target)
    mean_daily_ic: float | None = None
    bs_ci: tuple[float | None, float | None] = (None, None)
    if inference == "block_bootstrap" and daily:
        ic_values = [v for _, v in daily]
        mean_daily_ic = _mean(ic_values)
        bs_ci = moving_block_bootstrap_ci(
            ic_values, block_len=block_len, n_boot=n_boot, seed=seed
        )
    nonoverlap = nonoverlap_ic(base, factor, target, horizon)
    span = f"{daily[0][0]}~{daily[-1][0]}" if daily else None
    inference_meta = {
        "method": inference, "B": n_boot, "L": block_len, "seed": seed,
        "T": len(daily), "span": span,
    }

    emb = (horizon + 1) if embargo_td is None else embargo_td
    splits = walk_forward_splits(
        base["date"].to_list(), n_splits=n_splits,
        min_train_frac=min_train_frac, embargo_td=emb,
    )
    split_rows: list[dict] = []
    for s in splits:
        seg = base.filter(
            (pl.col("date") >= s.test_start) & (pl.col("date") <= s.test_end)
        )
        split_rows.append(
            {
                "split_id": s.split_id,
                "test_start": s.test_start,
                "test_end": s.test_end,
                **_ic_row(seg, factor, target),
            }
        )
    split_df = pl.DataFrame(split_rows) if split_rows else pl.DataFrame()

    resid = residual_ic(base, factor, target, controls) if controls else pl.DataFrame()
    return FactorReport(
        factor=str(factor),
        horizon=horizon,
        target=target,
        pooled=pooled,
        splits=split_df,
        buckets=bucket_table(base, factor, target, buckets=buckets),
        residual=resid,
        controls=tuple(controls),
        mean_daily_ic=mean_daily_ic,
        bs_ci=bs_ci,
        nonoverlap=nonoverlap,
        inference_meta=inference_meta,
    )


#: analysis/regime.py ATTACK/NEUTRAL/DEFENSE 中文標籤（factor_lab/rotation_efficacy/
#: laggard_grid 的 regime 切片段共用，語彙不得分歧）。
REGIME_LABELS: tuple[str, ...] = ("進攻", "中性", "防禦")
#: docs/23 §1c：切片樣本（per-date IC 序列長度 T／per-週序列長度）< 此門檻 → 表照列標
#: 「樣本不足」，但不計入跨 regime 同向裁決的分母（避免薄樣本的假方向污染晉升判定）。
REGIME_MIN_N = 30


def regime_ic_slices(
    df: pl.DataFrame,
    factor: str,
    target: str,
    horizon: int,
    regime_col: str = "regime",
    regime_labels: Sequence[str] = REGIME_LABELS,
    min_n_dates: int = REGIME_MIN_N,
    controls: Sequence[str] = (),
    buckets: int = 5,
    inference: str = "block_bootstrap",
    n_boot: int = 1000,
    seed: int = 42,
    block_td: int | None = None,
) -> pl.DataFrame:
    """regime 切片 IC 表（docs/23 §1c 晉升鐵則第三條的資料底料）：對 regime_col 各分類各跑
    evaluate()（n_splits=0，只取 pooled/per-date IC，不切 walk-forward——regime 切片本身
    已是另一種穩健性切法，避免雙重切分把樣本切到不可判）。

    block_td：bootstrap 塊長覆寫（None → evaluate 預設 horizon+1）。週頻序列（如
    rotation 週訊號）呼叫端應傳 ceil(horizon/5)+1（regime_slice.block_len_for_horizon
    weekly=True），日頻維持預設。

    regime_col 缺席或全 null（WS-H 標籤未產）→ 空表，呼叫端誠實標「regime 標籤未產」跳過
    （不臆造、不裁決）。切片 n_dates（per-date IC 序列長度 T）< min_n_dates → thin=True，
    該列照列輸出，供 regime_alignment_verdict 排除於裁決分母。

    Returns: regime/n_dates/mean_ic/ci_lo/ci_hi/thin（schema 固定；不合格輸入→空表同 schema）。
    """
    schema = {
        "regime": pl.Utf8, "n_dates": pl.Int64, "mean_ic": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "thin": pl.Boolean,
    }
    if df.is_empty() or regime_col not in df.columns or df[regime_col].drop_nulls().is_empty():
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for r in regime_labels:
        sub = df.filter(pl.col(regime_col) == r)
        rep = evaluate(
            sub, factor, horizon=horizon, target=target, controls=controls,
            buckets=buckets, n_splits=0, inference=inference, n_boot=n_boot, seed=seed,
            block_td=block_td,
        )
        t_obs = int((rep.inference_meta or {}).get("T", 0) or 0)
        rows.append(
            {
                "regime": r, "n_dates": t_obs, "mean_ic": rep.mean_daily_ic,
                "ci_lo": rep.bs_ci[0], "ci_hi": rep.bs_ci[1], "thin": t_obs < min_n_dates,
            }
        )
    return pl.DataFrame(rows, schema=schema)


def regime_mean_slices(
    df: pl.DataFrame,
    target: str,
    block_len: int,
    regime_col: str = "regime",
    regime_labels: Sequence[str] = REGIME_LABELS,
    min_n_dates: int = REGIME_MIN_N,
    n_boot: int = 1000,
    seed: int = 42,
) -> pl.DataFrame:
    """regime 切片 mean-lift 表（供 rotation quadrant/★ lift 與 laggard cell 重用）：
    各 regime 子樣本的 target 以 per-date（＝per-週，若輸入為週頻）mean 序列做
    moving_block_bootstrap_ci——mean 欄＝該序列等權平均（與 CI 同一統計量，
    非 pooled mean；相鄰快照前瞻窗重疊，pooled CI 偏窄，同 docs/22 §7.2 動機）。

    block_len：呼叫端依頻率算好傳入（週頻 ceil(horizon/5)+1、日頻 horizon+1，
    regime_slice.block_len_for_horizon）。regime_col 缺席或全 null → 空表誠實跳過。
    切片全空（n=0）照列（n/n_dates=0、統計欄 null）——「不藏格」慣例。

    Returns: regime/n/n_dates/mean/ci_lo/ci_hi/thin（thin＝n_dates < min_n_dates）。
    """
    schema = {
        "regime": pl.Utf8, "n": pl.Int64, "n_dates": pl.Int64, "mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64, "thin": pl.Boolean,
    }
    if (
        df.is_empty()
        or target not in df.columns
        or regime_col not in df.columns
        or df[regime_col].drop_nulls().is_empty()
    ):
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    for r in regime_labels:
        sub = df.filter(pl.col(regime_col) == r).drop_nulls([target])
        daily = (
            sub.group_by("date").agg(pl.col(target).mean().alias("_m")).sort("date")
            if not sub.is_empty()
            else pl.DataFrame(schema={"date": pl.Date, "_m": pl.Float64})
        )
        values = [float(v) for v in daily["_m"].to_list() if v is not None]
        lo, hi = (
            moving_block_bootstrap_ci(values, block_len=block_len, n_boot=n_boot, seed=seed)
            if values
            else (None, None)
        )
        rows.append(
            {
                "regime": r, "n": sub.height, "n_dates": len(values),
                "mean": _mean(values) if values else None,
                "ci_lo": lo, "ci_hi": hi, "thin": len(values) < min_n_dates,
            }
        )
    return pl.DataFrame(rows, schema=schema)


def regime_alignment_verdict(
    slices: pl.DataFrame,
    full_sign: int,
    value_col: str = "mean_ic",
    regime_col: str = "regime",
    thin_col: str = "thin",
    attack_label: str = "進攻",
) -> tuple[str, list[str], int]:
    """docs/23 §1c 跨 regime 升降級裁決（純函式；factor_lab/rotation_efficacy/laggard_grid
    的 regime 切片段共用，避免三處各寫一套、語彙分歧）。slices schema 需含
    regime_col/value_col/thin_col（regime_ic_slices 或同構的 lift 版切片表皆可餵）。

    規則（docs/23 §1c，寫死不得因手感調鬆）：
    - 可判切片（非 thin 且 value_col 非 null）數 <1 → 「regime 樣本不足（無可判切片）」；
    - 與 full_sign 同號的可判切片數 ≥2 → 「跨 regime 穩健」；
    - 同號數 ==1 且該切片為 attack_label（進攻）→ 「bull-only」（docs/23 §1c 原始語意是
      「WS-H 前樣本全落單一多頭偏窗」；WS-H 後泛化為「就算三個 regime 都有樣本，也只有
      多頭那片撐得住」——仍是同一件事：不能只靠多頭證據晉升）；
    - 其餘（同號數 ==1 但非進攻，或同號數 ==0）→ 「regime-dependent」（docs/23 §1(a) 同語彙）。

    full_sign 須傳 ±1（0 視為正號的反面、不建議傳 0——呼叫端應先把「≥0 記正」正規化）。

    Returns: (裁決字串, 同向 regime 標籤 list, 可判切片數)。
    """
    if slices.is_empty():
        return "regime 樣本不足（無可判切片）", [], 0
    usable = slices.filter(~pl.col(thin_col) & pl.col(value_col).is_not_null())
    present = usable.height
    if present < 1:
        return "regime 樣本不足（無可判切片）", [], 0
    same = [
        str(row[regime_col])
        for row in usable.iter_rows(named=True)
        if (row[value_col] > 0) == (full_sign > 0)
    ]
    if len(same) >= 2:
        return "跨 regime 穩健", same, present
    if len(same) == 1 and same[0] == attack_label:
        return "bull-only", same, present
    return "regime-dependent", same, present


def grid_scan(
    df: pl.DataFrame,
    make_factor: Callable[..., pl.Expr],
    grid: Mapping[str, Sequence],
    horizon: int = 20,
    target: str | None = None,
    n_splits: int = 4,
    min_train_frac: float = 0.4,
    embargo_td: int | None = None,
) -> pl.DataFrame:
    """格點掃描：訓練段算 IC 選參、測試段唯讀；**回傳所有 cell**（多重比較誠實）。

    防過擬合硬限制：維度 ≤3、每維檔位 ≤5，超出 raise ValueError。
    輸出各 cell × 各 split 的 train_ic/train_n/test_ic/test_n；`selected`＝
    「以訓練段平均 |IC| 選出」的 cell 標記（僅標記，測試段數字全部照列）。
    """
    dims = list(grid.keys())
    if len(dims) > _GRID_MAX_DIMS:
        raise ValueError(f"格點維度 {len(dims)} > {_GRID_MAX_DIMS}（防過擬合硬限制）")
    for k, vs in grid.items():
        if len(vs) > _GRID_MAX_LEVELS:
            raise ValueError(f"格點 {k} 檔位 {len(vs)} > {_GRID_MAX_LEVELS}（防過擬合硬限制）")
    target = target or f"alpha{horizon}"
    if df.is_empty() or "date" not in df.columns:
        return pl.DataFrame()
    emb = (horizon + 1) if embargo_td is None else embargo_td

    rows: list[dict] = []
    cell_train_abs: dict[tuple, list[float]] = {}
    for combo in product(*(grid[k] for k in dims)):
        params = dict(zip(dims, combo))
        expr = make_factor(**params)
        name = expr.meta.output_name()
        cell = df.with_columns(expr).drop_nulls([name, target])
        splits = walk_forward_splits(
            cell["date"].to_list(), n_splits=n_splits,
            min_train_frac=min_train_frac, embargo_td=emb,
        )
        for s in splits:
            train = cell.filter(pl.col("date") <= s.train_end)
            test = cell.filter(
                (pl.col("date") >= s.test_start) & (pl.col("date") <= s.test_end)
            )
            tr = _ic_row(train, name, target)
            te = _ic_row(test, name, target)
            rows.append(
                {
                    **{f"p_{k}": v for k, v in params.items()},
                    "split_id": s.split_id,
                    "train_ic": tr["ic"], "train_n": tr["n"],
                    "test_ic": te["ic"], "test_ci_lo": te["ci_lo"],
                    "test_ci_hi": te["ci_hi"], "test_n": te["n"],
                }
            )
            if tr["ic"] is not None:
                cell_train_abs.setdefault(combo, []).append(abs(tr["ic"]))
    if not rows:
        return pl.DataFrame()
    best = max(
        cell_train_abs, key=lambda c: sum(cell_train_abs[c]) / len(cell_train_abs[c]),
        default=None,
    ) if cell_train_abs else None
    out = pl.DataFrame(rows)
    if best is not None:
        sel = pl.all_horizontal(
            *[pl.col(f"p_{k}") == v for k, v in zip(dims, best)]
        )
        out = out.with_columns(sel.alias("selected"))
    return out


def _fmt(v: object, nd: int = 3) -> str:
    return f"{float(v):+.{nd}f}" if isinstance(v, (int, float)) else "—"


def inference_footer(
    sample_span: str, regime_dist: str, method_desc: str, membership_desc: str
) -> list[str]:
    """固定三行 footer（WS-I F 法）：樣本期間＋regime 分布／推論方法／membership 處理。

    每張結果表尾接（render_report_md 自動呼叫）；供未來其他報表沿用同一誠實揭露格式。
    """
    return [
        f"- 樣本期間：{sample_span}；regime 分布：{regime_dist}。",
        f"- 推論方法：{method_desc}。",
        f"- membership 處理：{membership_desc}。",
    ]


def render_report_md(
    rep: FactorReport,
    note: str = "",
    regime_dist: str = "regime 標籤未附（WS-H 前）＝2025-01~2026-07 多頭偏",
    membership_desc: str = "今日 concepts.yaml（非 point-in-time）",
) -> list[str]:
    """FactorReport → markdown 段落（pooled＋bootstrap CI＋walk-forward＋分桶＋殘差＋footer）。

    WS-I：pooled 列沿用 Fisher-z（對照連續性）；新增 per-date IC mean 的
    moving-block bootstrap CI95（新預設推論）與非重疊子樣本 Fisher CI（交叉檢驗）；
    表尾固定接三行誠實揭露 footer（inference_footer，見 docs/22 §7.2 動機）。
    """
    lines = [f"### `{rep.factor}` → `{rep.target}`（r+{rep.horizon}）{note}", ""]
    if rep.pooled.is_empty() or rep.pooled.row(0, named=True)["ic"] is None:
        lines.append("> 無法計算（樣本不足或欄缺）——如實標註。")
        return lines
    p = rep.pooled.row(0, named=True)
    ci = f"[{_fmt(p['ci_lo'])}, {_fmt(p['ci_hi'])}]" if p["ci_lo"] is not None else "—"
    crosses = (
        p["ci_lo"] is not None and p["ci_lo"] < 0 < p["ci_hi"]
    )
    verdict = "**無證據（CI 跨 0）**" if crosses else "CI 不跨 0"
    lines.append(
        f"- pooled IC **{_fmt(p['ic'])}** CI95 {ci}・n={p['n']}・{verdict}"
        "（Fisher-z 解析近似，對照連續性；重疊窗下偏窄，見下）。"
    )
    meta = rep.inference_meta or {}
    if rep.mean_daily_ic is not None:
        bs_lo, bs_hi = rep.bs_ci
        if bs_lo is not None and bs_hi is not None:
            bs_txt = f"[{_fmt(bs_lo)}, {_fmt(bs_hi)}]"
            bs_verdict = "**無證據（CI 跨 0）**" if bs_lo < 0 < bs_hi else "CI 不跨 0"
        else:
            bs_txt, bs_verdict = "—", "T<10，無法重抽"
        lines.append(
            f"- per-date IC mean **{_fmt(rep.mean_daily_ic)}**（T={meta.get('T', 0)} 交易日"
            f"等權）moving-block bootstrap CI95 {bs_txt}（L={meta.get('L')}td・"
            f"B={meta.get('B')}・seed={meta.get('seed')}）・{bs_verdict}。"
        )
    nv = rep.nonoverlap or {}
    if nv.get("ic") is not None:
        nv_ci = f"[{_fmt(nv['ci_lo'])}, {_fmt(nv['ci_hi'])}]" if nv["ci_lo"] is not None else "—"
        lines.append(
            f"- 非重疊 stride={rep.horizon}td 子樣本 IC **{_fmt(nv['ic'])}** Fisher CI95 {nv_ci}"
            f"・n_dates={nv['n_dates']}（觀測近似獨立，交叉檢驗）。"
        )
    if not rep.residual.is_empty():
        r = rep.residual.row(0, named=True)
        rci = f"[{_fmt(r['ci_lo'])}, {_fmt(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
        lines.append(
            f"- 殘差 IC（控制 {', '.join(rep.controls)}）**{_fmt(r['ic'])}** "
            f"CI95 {rci}・n={r['n']}（偏相關近似 CI）。"
        )
    if not rep.splits.is_empty():
        sign = rep.consistent_sign
        if sign:
            sign_note = "同號"
        elif sign is False:
            sign_note = "**變號（regime-dependent，降級）**"
        else:
            sign_note = "段數不足"
        lines += [
            f"- walk-forward {rep.splits.height} 段：{sign_note}。",
            "",
            "| 段 | 測試窗 | IC | CI95 | n |",
            "|---|---|---|---|---|",
            *[
                f"| {r['split_id']} | {r['test_start']}~{r['test_end']} | {_fmt(r['ic'])} "
                f"| [{_fmt(r['ci_lo'])}, {_fmt(r['ci_hi'])}] | {r['n']} |"
                for r in rep.splits.iter_rows(named=True)
            ],
        ]
    if not rep.buckets.is_empty():
        lines += [
            "",
            "| 桶(低→高) | n | 中位 | 平均 | 勝率 |",
            "|---|---|---|---|---|",
            *[
                f"| {r['bucket']} | {r['n']} | {_fmt(r['median'], 2)}% | {_fmt(r['mean'], 2)}% "
                f"| {r['win_rate']:.0%} |"
                for r in rep.buckets.iter_rows(named=True)
            ],
        ]
    lines.append("")
    lines += inference_footer(
        sample_span=meta.get("span") or "—（per-date IC 不可得，T<10）",
        regime_dist=regime_dist,
        method_desc=(
            f"moving-block bootstrap（block={meta.get('L')}td・B={meta.get('B')}・"
            f"seed={meta.get('seed')}）為 CI 預設；Fisher-z 僅供對照連續性"
            "（重疊窗下偏窄，docs/22 §7.2）"
        ),
        membership_desc=membership_desc,
    )
    lines.append("")
    return lines


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def moving_block_bootstrap_ci(
    values: Sequence[float],
    block_len: int,
    n_boot: int = 1000,
    seed: int = 42,
    stat: Callable[[list[float]], float] = _mean,
) -> tuple[float | None, float | None]:
    """Moving-block bootstrap 百分位 CI（WS-I C 法・通用 helper，供 basket/laggard 表重用）。

    對序列（按既定順序，通常＝日期升冪）重抽 ceil(T/L) 個長度 L 的區塊（起點
    uniform∈[0, T-L]，區塊可重疊）、串接後截斷回長度 T，計 stat（預設 mean）；
    重複 n_boot 次、CI＝percentile [2.5, 97.5]。純 python（無 numpy 依賴，鐵律 4）。

    T<10 → (None, None)（重抽不出可信分布，誠實不給，門檻同 bootstrap_mean_ci）；
    block_len ≥ T 時截斷至 T（單一區塊＝退化為對整段重抽，仍可跑不崩潰）。
    """
    import random as _random

    clean = [v for v in values if v is not None and not math.isnan(v)]
    t = len(clean)
    if t < 10:
        return None, None
    length = max(1, min(block_len, t))
    n_blocks = math.ceil(t / length)
    max_start = t - length
    rng = _random.Random(seed)
    stats: list[float] = []
    for _ in range(n_boot):
        resampled: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, max_start)
            resampled.extend(clean[start : start + length])
        stats.append(stat(resampled[:t]))
    stats.sort()
    lo_i = int(n_boot * 0.025)
    hi_i = int(n_boot * 0.975) - 1
    return stats[lo_i], stats[hi_i]


def bootstrap_mean_ci(
    values: Sequence[float],
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 20260710,
) -> tuple[float | None, float | None]:
    """平均數的 bootstrap 百分位 CI（lift 表用；純 python、無 numpy 依賴）。

    n<10 → (None, None)（重抽不出分布，誠實不給）。
    """
    import random as _random

    clean = [v for v in values if v is not None and not math.isnan(v)]
    n = len(clean)
    if n < 10:
        return None, None
    rng = _random.Random(seed)
    means = sorted(
        sum(rng.choices(clean, k=n)) / n for _ in range(n_boot)
    )
    lo_i = int(n_boot * (alpha / 2))
    hi_i = int(n_boot * (1 - alpha / 2)) - 1
    return means[lo_i], means[hi_i]
