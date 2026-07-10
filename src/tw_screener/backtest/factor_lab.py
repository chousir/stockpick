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
) -> FactorReport:
    """統一因子評估（模組 docstring 的 1–4 全套）。

    Args:
        df: 面板形資料（需含 date 與 factor/target/controls 欄）。
        factor: 欄名或 pl.Expr（Expr 需 .alias 命名）。
        horizon: 前瞻窗（交易日）；決定預設 target 與 embargo。
        target: 預設 f"alpha{horizon}"（面板超額欄）。
        controls: 殘差 IC 的控制欄（如 ["ma60_dist_pct"]）。
        embargo_td: 預設 horizon+1（前瞻窗跨界滲漏的最小隔離）。
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
        )
    base = df.drop_nulls([factor, target])
    pooled = pl.DataFrame([_ic_row(base, factor, target)])

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
    )


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


def render_report_md(rep: FactorReport, note: str = "") -> list[str]:
    """FactorReport → markdown 段落（pooled＋walk-forward＋分桶＋殘差，全表帶 n/CI）。"""
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
    lines.append(f"- pooled IC **{_fmt(p['ic'])}** CI95 {ci}・n={p['n']}・{verdict}。")
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
    return lines
