"""backtest/fv_replica_read.py — docs/31 §20.17：investing.com 式多模型估值仿製版效度檢驗 Q1–Q5。

判準與裁決句全部事前寫死於 docs/31 §20.17（commit 5b7ed5d），本模組只是照表執行：
- 面板是**月頻錨點**：per-date IC 序列一個點＝一個月。塊長／非重疊步長以「月」計
  （`months_spanned(h)＝ceil(h/21)`），不沿用 factor_lab 的交易日預設。
- Q2/Q3 的殘差 IC 用 per-date 偏相關序列 ＋ moving-block bootstrap（pre-registration 指定），
  `factor_lab.residual_ic` 的 pooled Fisher CI 只列為對照（重疊窗下偏窄）。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from tw_screener.backtest.factor_lab import (
    _partial_corr,
    _rank_within_date,
    bucket_table,
    evaluate,
    moving_block_bootstrap_ci,
    regime_alignment_verdict,
    regime_ic_slices,
)
from tw_screener.backtest.fv_replica_panel import MODEL_KEYS

_EMPTY_CI: dict[str, Any] = {"mean": None, "lo": None, "hi": None, "T": 0}
FOCUS_FACTOR = "fv_upside_pct"
FOCUS_HORIZON = 120
CONTROLS: tuple[str, ...] = ("ma60_dist_pct", "trail_r250", "log_mktcap")
ELECTRONICS: frozenset[str] = frozenset(
    {
        "半導體業",
        "電腦及週邊設備業",
        "光電業",
        "通信網路業",
        "電子零組件業",
        "電子通路業",
        "資訊服務業",
        "其他電子業",
    }
)


def months_spanned(horizon_td: int) -> int:
    """前瞻 h 交易日跨幾個月錨（≈21 交易日／月）。"""
    return max(1, math.ceil(horizon_td / 21))


def _evaluate(df: pl.DataFrame, factor: str, h: int, controls: Sequence[str] = ()) -> Any:
    k = months_spanned(h)
    return evaluate(
        df,
        factor,
        horizon=k,
        target=f"alpha{h}",
        controls=controls,
        embargo_td=k,
        block_td=k + 1,
    )


def daily_partial_ic_series(
    df: pl.DataFrame, factor: str, target: str, controls: Sequence[str], min_n: int = 30
) -> list[tuple[date, float]]:
    """每錨點橫斷面 rank 偏相關（控制 controls）；當日有效 <min_n 檔跳過。"""
    cols = [factor, target, *controls]
    if df.is_empty() or not set(cols).issubset(df.columns):
        return []
    ranked = _rank_within_date(df, cols)
    out: list[tuple[date, float]] = []
    for (d,), g in ranked.group_by("date", maintain_order=True):
        if g.height < min_n:
            continue
        v = _partial_corr(g, f"_rk_{factor}", f"_rk_{target}", [f"_rk_{c}" for c in controls])
        if v is not None and not math.isnan(v):
            out.append((d, v))
    return sorted(out)


def _plain_ic(df: pl.DataFrame, factor: str, h: int) -> dict[str, Any]:
    """單因子 per-date IC（無控制）＋ block bootstrap CI（塊長＝跨月數+1）。"""
    return series_ci(daily_partial_ic_series(df, factor, f"alpha{h}", []), months_spanned(h) + 1)


def series_ci(series: list[tuple[date, float]], block: int) -> dict[str, Any]:
    vals = [v for _, v in series]
    if not vals:
        return {"mean": None, "lo": None, "hi": None, "T": 0}
    lo, hi = moving_block_bootstrap_ci(vals, block_len=block)
    return {"mean": sum(vals) / len(vals), "lo": lo, "hi": hi, "T": len(vals)}


def excludes_zero(ci: dict[str, Any]) -> bool:
    return ci["lo"] is not None and (ci["lo"] > 0 or ci["hi"] < 0)


def convergence(
    df: pl.DataFrame, factor: str, h: int, abs_min: float, n_perm: int, seed: int = 42
) -> dict[str, Any]:
    """Q4 收斂率：|upside|≥abs_min 的樣本中，h 日總報酬（與 alpha）方向和 upside 同號的比例；
    置換基準＝同錨點內打亂 upside 後同法計算 n_perm 次（mean 與 2.5/97.5 百分位）。"""
    base = df.drop_nulls([factor, f"r{h}", f"alpha{h}"]).select(
        "date", factor, f"r{h}", f"alpha{h}"
    )

    def _hit(frame: pl.DataFrame, col: str) -> tuple[float | None, float | None, int]:
        s = frame.filter(pl.col(col).abs() >= abs_min)
        if s.is_empty():
            return None, None, 0
        up = pl.col(col) > 0
        r = ((pl.col(f"r{h}") > 0) == up).mean()
        a = ((pl.col(f"alpha{h}") > 0) == up).mean()
        row = s.select(r.alias("r"), a.alias("a")).row(0)
        return row[0], row[1], s.height

    hit_r, hit_a, n = _hit(base, factor)
    rng = random.Random(seed)
    perm_r: list[float] = []
    perm_a: list[float] = []
    dates = base["date"].to_list()
    vals = base[factor].to_list()
    by_date: dict[date, list[int]] = {}
    for i, d in enumerate(dates):
        by_date.setdefault(d, []).append(i)
    for _ in range(n_perm):
        shuffled = list(vals)
        for idx in by_date.values():
            sub = [vals[i] for i in idx]
            rng.shuffle(sub)
            for i, v in zip(idx, sub, strict=True):
                shuffled[i] = v
        pr, pa, _ = _hit(base.with_columns(pl.Series("_perm", shuffled)), "_perm")
        if pr is not None and pa is not None:
            perm_r.append(pr)
            perm_a.append(pa)

    def _pct(xs: list[float], q: float) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    return {
        "n": n,
        "hit_r": hit_r,
        "hit_a": hit_a,
        "perm_r_mean": sum(perm_r) / len(perm_r) if perm_r else None,
        "perm_r_lo": _pct(perm_r, 0.025),
        "perm_r_hi": _pct(perm_r, 0.975),
        "perm_a_mean": sum(perm_a) / len(perm_a) if perm_a else None,
        "perm_a_lo": _pct(perm_a, 0.025),
        "perm_a_hi": _pct(perm_a, 0.975),
    }


def level_error(df: pl.DataFrame, fv_col: str, h: int) -> dict[str, Any]:
    """Q4 誤差：中位 |log(P_{t+h}^{TR}/FV_t)| vs naive「FV＝現價」中位 |log(P_{t+h}^{TR}/P_t)|。"""
    s = df.drop_nulls([fv_col, f"r{h}"]).filter((pl.col(fv_col) > 0) & (pl.col(f"r{h}") > -100))
    if s.is_empty():
        return {"n": 0, "err_fv": None, "err_naive": None, "share_fv_better": None}
    fut = pl.col("price") * (1 + pl.col(f"r{h}") / 100)
    out = s.select(
        (fut / pl.col(fv_col)).log().abs().alias("_e_fv"),
        (fut / pl.col("price")).log().abs().alias("_e_nv"),
    ).select(
        pl.len().alias("n"),
        pl.col("_e_fv").median().alias("err_fv"),
        pl.col("_e_nv").median().alias("err_naive"),
        (pl.col("_e_fv") < pl.col("_e_nv")).mean().alias("share_fv_better"),
    )
    return out.row(0, named=True)


def half_split(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    dates = sorted(df["date"].unique().to_list())
    mid = dates[len(dates) // 2]
    return df.filter(pl.col("date") < mid), df.filter(pl.col("date") >= mid)


def sector_group(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col("is_financial"))
        .then(pl.lit("金融"))
        .when(pl.col("industry_name").is_in(list(ELECTRONICS)))
        .then(pl.lit("電子"))
        .otherwise(pl.lit("非電子非金融"))
        .alias("sector_grp")
    )


def with_model_upsides(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col(m) > 0)
        .then((pl.col(m) / pl.col("price") - 1) * 100)
        .otherwise(None)
        .alias(f"up_{m}")
        for m in MODEL_KEYS
    )


def verdict(
    q1: dict[str, Any],
    q2: dict[str, Any],
    state_ok: bool,
    halves_ok: bool,
    q3_vs_comp6: dict[str, Any],
) -> str:
    """docs/31 §20.17 裁決句（事前寫死，照表選句）。"""
    if not excludes_zero(q1):
        return "Q1 CI 跨 0 → 對前瞻報酬無預測力；「準」只剩相對貴賤的描述。"
    if q1["mean"] is not None and q1["mean"] < 0:
        return "Q1 CI 排除 0 但方向為負（高 upside→跑輸）→ 作為多頭訊號否證。"
    if not excludes_zero(q2):
        return "Q1 顯著但 Q2（控動能／規模）CI 跨 0 → ≈動能／規模代理；只能當描述性相對貴賤參考。"
    if q2["mean"] < 0:
        return "Q2 殘差 CI 排除 0 但方向為負 → 控制後作為多頭訊號否證。"
    if not (state_ok and halves_ok):
        return (
            "Q2 殘差 CI 排除 0，但「多／空兩狀態（非 thin）同號」或「前後半同號」條件未過"
            " → 未達「帶獨立資訊」門檻（未過原因見下一行），維持描述性參考。"
        )
    if excludes_zero(q3_vs_comp6) and q3_vs_comp6["mean"] > 0:
        return (
            "仿製估值帶獨立於動能／規模的橫斷面資訊，且控制現行綜合缺口% 後仍有"
            " → 多模型平均勝過綜合缺口%。"
        )
    return (
        "仿製估值帶獨立於動能／規模的橫斷面資訊，但控制現行綜合缺口% 後不顯著"
        " → ≈現行綜合缺口%，不需另建。"
    )


def _f(v: Any, nd: int = 3, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.1f}%" if pct else f"{v:+.{nd}f}" if isinstance(v, float) else str(v)


def _ci_str(c: dict[str, Any]) -> str:
    return f"{_f(c['mean'])} [{_f(c['lo'])}, {_f(c['hi'])}] T={c['T']}"


def analyze(
    panel: pl.DataFrame, horizons: Sequence[int], fv_cfg: dict[str, Any], regime: pl.DataFrame
) -> str:
    """Q1–Q5 全格 → markdown。"""
    df = sector_group(with_model_upsides(panel))
    if not regime.is_empty():
        df = df.join(regime.select("date", "regime_label"), on="date", how="left")
    lines: list[str] = []
    w = lines.append
    w(
        "# investing.com 式多模型估值仿製版——效度檢驗（docs/31 §20.17）\n\n"
        f"產出日：{date.today()}　pre-registration：commit 5b7ed5d"
        f"（判準、焦點格 {FOCUS_FACTOR}×r+{FOCUS_HORIZON} 事前寫死）\n"
    )
    w(
        "> 「公允價值」只指 investing.com 產品；"
        "本文數字一律為**仿製估值（研究用）**，非目標價／合理價。\n"
    )

    # ── 覆蓋率 ──
    dates = sorted(df["date"].unique().to_list())
    w("## 0. 覆蓋率\n")
    w(
        f"- 錨點 {len(dates)} 個（{dates[0]} ~ {dates[-1]}，每月第一個交易日）；"
        f"列數 {df.height:,}；"
        f"每錨點中位 {df.group_by('date').len().select(pl.col('len').median()).item():.0f} 檔。"
    )
    cov = df.select(
        [
            pl.col(c).is_not_null().mean().alias(c)
            for c in [*MODEL_KEYS, "fv_replica", "fv_replica_raw", "up_composite6", "up_peer_pe"]
        ]
    )
    w("\n| 欄 | 可算率 |\n|---|---|")
    for c, v in cov.row(0, named=True).items():
        w(f"| {c} | {v * 100:.1f}% |")
    nm = df["fv_n_models"].value_counts().sort("fv_n_models")
    w(
        "\n主版保留模型數分布："
        + "、".join(f"{r['fv_n_models']}個:{r['count']}" for r in nm.iter_rows(named=True))
    )
    for h in horizons:
        k = months_spanned(h)
        n_dates = df.drop_nulls([FOCUS_FACTOR, f"alpha{h}"])["date"].n_unique()
        w(
            f"- r+{h}：可判錨點 {n_dates} 個；非重疊步長 {k} 月"
            f" → 非重疊錨點 ≈{math.ceil(n_dates / k)}"
        )
    states = df.group_by("date").agg(pl.col("mkt_state").first())["mkt_state"].value_counts()
    w(
        "- 市場狀態代理（等權指數 vs 200 日均線）錨點數："
        + "、".join(f"{r['mkt_state']}:{r['count']}" for r in states.iter_rows(named=True))
    )

    # ── Q1 / Q2 / Q3 ──
    results: dict[tuple[str, int], dict[str, Any]] = {}
    w("\n## Q1 預測力（Spearman IC vs alpha{h}）＋ Q2 控動能／規模\n")
    w(
        "| 因子 | h | pooled IC [Fisher] n | per-date mean"
        " IC [block bootstrap] T | 非重疊 IC [CI] n_dates |"
        " Q2 殘差 per-date [bootstrap] | walk-forward 各段 IC |\n|---|---|---|---|---|---|---|"
    )
    for fac in (FOCUS_FACTOR, "fv_raw_upside_pct", "up_composite6", "up_peer_pe"):
        for h in horizons:
            rep = _evaluate(df, fac, h)
            k = months_spanned(h)
            q1 = {
                "mean": rep.mean_daily_ic,
                "lo": rep.bs_ci[0],
                "hi": rep.bs_ci[1],
                "T": rep.inference_meta.get("T", 0),
            }
            q2 = series_ci(daily_partial_ic_series(df, fac, f"alpha{h}", CONTROLS), k + 1)
            results[(fac, h)] = {"q1": q1, "q2": q2, "rep": rep}
            p = rep.pooled.row(0, named=True) if not rep.pooled.is_empty() else {}
            no = rep.nonoverlap
            wf = (
                ", ".join(_f(v) for v in rep.splits["ic"].to_list())
                if not rep.splits.is_empty()
                else "—"
            )
            w(
                f"| {fac} | {h} | {_f(p.get('ic'))} [{_f(p.get('ci_lo'))}"
                f", {_f(p.get('ci_hi'))}] {p.get('n')} "
                f"| {_ci_str(q1)} | {_f(no.get('ic'))} "
                f"[{_f(no.get('ci_lo'))}, {_f(no.get('ci_hi'))}] "
                f"{no.get('n_dates')} | {_ci_str(q2)} | {wf} |"
            )

    w("\n## Q3 增益：控制簡單基準後的殘差 IC（per-date 偏相關＋bootstrap）\n")
    w(
        "| 因子 | h | 控 up_peer_pe | 控 up_composite6 | 控 up_composite6＋動能／規模 |\n"
        "|---|---|---|---|---|"
    )
    q3_focus: dict[str, Any] = {}
    for fac in (FOCUS_FACTOR, "fv_raw_upside_pct"):
        for h in horizons:
            k = months_spanned(h)
            a = series_ci(daily_partial_ic_series(df, fac, f"alpha{h}", ["up_peer_pe"]), k + 1)
            b = series_ci(daily_partial_ic_series(df, fac, f"alpha{h}", ["up_composite6"]), k + 1)
            ctl = series_ci(
                daily_partial_ic_series(df, fac, f"alpha{h}", ["up_composite6", *CONTROLS]), k + 1
            )
            if fac == FOCUS_FACTOR and h == FOCUS_HORIZON:
                q3_focus = b
            w(f"| {fac} | {h} | {_ci_str(a)} | {_ci_str(b)} | {_ci_str(ctl)} |")

    # ── Q4 ──
    w("\n## Q4 準度描述\n\n### 五分位（桶 1＝upside 最低／最貴，桶 5＝upside 最高／最便宜）\n")
    for fac in (FOCUS_FACTOR, "fv_raw_upside_pct"):
        for h in horizons:
            bt = bucket_table(df, fac, f"alpha{h}")
            w(
                f"- {fac} r+{h}："
                + "；".join(
                    f"Q{r['bucket']} 中位α {r['median']:+.1f}% 勝率 {r['win_rate'] * 100:.0f}%"
                    for r in bt.iter_rows(named=True)
                )
            )
    abs_min = float(fv_cfg.get("abs_upside_min_pct", 10.0))
    n_perm = int(fv_cfg.get("n_permutations", 200))
    w(f"\n### 收斂率（|upside|≥{abs_min:.0f}% 樣本；方向同號比例 vs 同錨點置換基準 {n_perm} 次）\n")
    w(
        "| 因子 | h | n | 總報酬同號 | 置換基準 [2.5,97.5] | alpha 同號 "
        "| 置換基準 [2.5,97.5] |\n|---|---|---|---|---|---|---|"
    )
    for fac in (FOCUS_FACTOR, "fv_raw_upside_pct"):
        for h in horizons:
            cv = convergence(df, fac, h, abs_min, n_perm, seed=int(fv_cfg.get("seed", 42)))
            w(
                f"| {fac} | {h} | {cv['n']} | {_f(cv['hit_r'], pct=True)}"
                f" | {_f(cv['perm_r_mean'], pct=True)} "
                f"[{_f(cv['perm_r_lo'], pct=True)},"
                f" {_f(cv['perm_r_hi'], pct=True)}] | {_f(cv['hit_a'], pct=True)} "
                f"| {_f(cv['perm_a_mean'], pct=True)} "
                f"[{_f(cv['perm_a_lo'], pct=True)}, {_f(cv['perm_a_hi'], pct=True)}] |"
            )
    w("\n### 價格水準誤差（中位 |log(P_{t+h}^{TR}/FV_t)| vs naive「FV＝現價」）\n")
    w("| 版本 | h | n | 誤差 FV | 誤差 naive | FV 較近的比例 |\n|---|---|---|---|---|---|")
    level_results: dict[tuple[str, int], dict[str, Any]] = {}
    for col in ("fv_replica", "fv_replica_raw"):
        for h in horizons:
            le = level_error(df, col, h)
            level_results[(col, h)] = le
            w(
                f"| {col} | {h} | {le['n']} | {_f(le['err_fv'])} | {_f(le['err_naive'])} "
                f"| {_f(le['share_fv_better'], pct=True)} |"
            )

    # ── Q5 ──
    w("\n## Q5 切片（焦點因子；per-date mean IC [block bootstrap]）\n")
    state_ok = halves_ok = False
    state_detail = "—"
    for h in horizons:
        k = months_spanned(h)
        w(f"\n### r+{h}\n")
        sl = regime_ic_slices(
            df,
            FOCUS_FACTOR,
            f"alpha{h}",
            horizon=k,
            regime_col="mkt_state",
            regime_labels=("多", "空"),
            block_td=k + 1,
        )
        full = results[(FOCUS_FACTOR, h)]["q1"]["mean"] or 0.0
        v, same, present = regime_alignment_verdict(sl, 1 if full >= 0 else -1, attack_label="多")
        w(
            "- 市場狀態："
            + "；".join(
                f"{r['regime']} {_f(r['mean_ic'])} [{_f(r['ci_lo'])}"
                f", {_f(r['ci_hi'])}] T={r['n_dates']}"
                f"{'（thin）' if r['thin'] else ''}"
                for r in sl.iter_rows(named=True)
            )
            + f" → {v}"
        )
        sl2 = (
            regime_ic_slices(
                df, FOCUS_FACTOR, f"alpha{h}", horizon=k, regime_col="regime_label", block_td=k + 1
            )
            if "regime_label" in df.columns
            else pl.DataFrame()
        )
        if not sl2.is_empty():
            w(
                "- 既有 regime_labels（僅 2022-01 起，描述性）："
                + "；".join(
                    f"{r['regime']} {_f(r['mean_ic'])} T"
                    f"={r['n_dates']}{'（thin）' if r['thin'] else ''}"
                    for r in sl2.iter_rows(named=True)
                )
            )
        h1, h2 = half_split(df)
        c1 = series_ci(daily_partial_ic_series(h1, FOCUS_FACTOR, f"alpha{h}", []), k + 1)
        c2 = series_ci(daily_partial_ic_series(h2, FOCUS_FACTOR, f"alpha{h}", []), k + 1)
        w(f"- 前半 {_ci_str(c1)}；後半 {_ci_str(c2)}")
        for grp in ("電子", "非電子非金融", "金融"):
            g = series_ci(
                daily_partial_ic_series(
                    df.filter(pl.col("sector_grp") == grp),
                    FOCUS_FACTOR,
                    f"alpha{h}",
                    [],
                    min_n=int(fv_cfg.get("ic_min_names_sector", 15)),
                ),
                k + 1,
            )
            w(f"- 產業 {grp}：{_ci_str(g)}")
        for lab, cond in (
            ("保留模型 ≥5", pl.col("fv_n_models") >= 5),
            ("保留模型 <5", pl.col("fv_n_models") < 5),
        ):
            g = series_ci(
                daily_partial_ic_series(df.filter(cond), FOCUS_FACTOR, f"alpha{h}", []), k + 1
            )
            w(f"- {lab}：{_ci_str(g)}")
        yearly = (
            pl.DataFrame(
                daily_partial_ic_series(df, FOCUS_FACTOR, f"alpha{h}", []),
                schema={"date": pl.Date, "ic": pl.Float64},
                orient="row",
            )
            .group_by(pl.col("date").dt.year().alias("y"))
            .agg(pl.col("ic").mean())
            .sort("y")
        )
        w(
            "- 逐年 mean IC（描述性）："
            + "；".join(f"{r['y']} {_f(r['ic'])}" for r in yearly.iter_rows(named=True))
        )
        if h == FOCUS_HORIZON:
            state_ok = v == "跨 regime 穩健"
            state_detail = "；".join(
                f"{r['regime']} {_f(r['mean_ic'])} [{_f(r['ci_lo'])}, {_f(r['ci_hi'])}]"
                f" T={r['n_dates']}{'（thin，不計入同號分母）' if r['thin'] else ''}"
                for r in sl.iter_rows(named=True)
            )
            state_detail = f"{v}：{state_detail}"
            m1, m2 = c1["mean"], c2["mean"]
            halves_ok = m1 is not None and m2 is not None and (m1 > 0) == (m2 > 0) == (full > 0)
        w(
            "- 各模型單腿（描述性，非獨立假設）："
            + "；".join(f"{m} {_ci_str(_plain_ic(df, f'up_{m}', h))}" for m in MODEL_KEYS)
        )

    # ── 裁決 ──
    fq = results[(FOCUS_FACTOR, FOCUS_HORIZON)]
    w("\n## 裁決（焦點格 主版 × r+120，句子照 §20.17 事前寫定）\n")
    w(
        f"- Q1 {_ci_str(fq['q1'])}；Q2 {_ci_str(fq['q2'])}；"
        f"市場狀態同號={state_ok}；前後半同號={halves_ok}；"
        f"Q3 控 composite6 {_ci_str(q3_focus) if q3_focus else '—'}"
    )
    w(f"- **{verdict(fq['q1'], fq['q2'], state_ok, halves_ok, q3_focus or _EMPTY_CI)}**")
    if not (state_ok and halves_ok):
        w(f"- 未過原因：市場狀態 {state_detail}；前後半同號={halves_ok}。")
    q4_h = max(horizons)  # §20.17 事前寫定 Q4 誤差用 P_{t+250}
    le = level_results[("fv_replica", q4_h)]
    if le["err_fv"] is not None and le["err_naive"] is not None and le["err_fv"] >= le["err_naive"]:
        w(
            f"- **Q4：預測價格水準不如直接用現價**（r+{q4_h} 仿製估值的中位誤差 "
            f"{le['err_fv']:.3f} ≥ naive「FV＝現價」{le['err_naive']:.3f}）。"
        )
    raw = results[("fv_raw_upside_pct", FOCUS_HORIZON)]["q1"]["mean"]
    main = fq["q1"]["mean"]
    if raw is not None and main is not None and (raw > 0) != (main > 0):
        w(
            "- 主版與副版方向矛盾 → 主版的 [0.4P, 2.0P] 剔除規則（以現價為錨）"
            "可能造成循環性，以副版為準檢查。"
        )
    w("\n## 已知偏差（必讀）\n")
    w("- 生存者偏差：母體＝今日次產業成員，下市股缺席（2022 起上市下市 ≥24 檔）→ 便宜股報酬高估。")
    w("- 同儕歸屬＝今日靜態分類；股數由 TTM 淨利／EPS 推（含非控制權益→少數股權大者偏高）。")
    w("- 折現率 8% 地板恆綁定＝DCF 為單一風險參數模型；無 EBITDA／分析師模型（investing.com 有）。")
    w("- 申報延遲以法定截止日近似；實際早公布者被保守延後（不造成前視）。")
    return "\n".join(lines) + "\n"


def _load_cfg(settings: Path) -> dict[str, Any]:
    import yaml as _yaml

    with open(settings, encoding="utf-8") as fh:
        return _yaml.safe_load(fh)


def _build_panel(cfg: dict[str, Any], anchors: Sequence[date] | None = None) -> pl.DataFrame:
    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.backtest.fv_replica_panel import build_fv_replica_panel
    from tw_screener.data import finmind as fm

    fv_cfg = dict(cfg.get("backtest", {}).get("fv_replica", {}))
    cd = Path(cfg["paths"]["cache_dir"]) / "finmind"
    price = fm.load_stock_price_history(cd)
    per = fm.load_finmind_per_history(cd)
    if anchors == []:  # 空 list＝「價格與 PER 皆廣泛覆蓋的最新交易日」快照
        min_names = int(fv_cfg.get("min_names_per_day", 500))
        px_days = set(price.group_by("date").len().filter(pl.col("len") >= min_names)["date"])
        px_ids = price["stock_id"].unique().to_list()
        per_days = set(  # 只數價格母體內的 PER（UNCOVERED 回補股更新較新、不代表母體）
            per.filter(pl.col("stock_id").is_in(px_ids))
            .group_by("date")
            .len()
            .filter(pl.col("len") >= min_names)["date"]
        )
        anchors = [max(px_days & per_days)]
    return build_fv_replica_panel(
        price=price,
        dividends=fm.load_dividend_result_history(cd),
        per=per,
        financials=fm.load_financials_history(cd),
        cashflow=fm.load_cashflow_history(cd),
        balancesheet=fm.load_balancesheet_history(cd),
        subind=list_subindustries(),
        industry=load_industry_mapping(Path(cfg["paths"]["cache_dir"]) / "twse"),
        dcf_cfg=cfg["cp_value"]["valuation"]["dcf"],
        fv_cfg=fv_cfg,
        horizons=tuple(int(h) for h in fv_cfg.get("horizons", [60, 120, 250])),
        anchors=anchors,
    )


#: A 輔線範本欄位：前段為本地值（預填）、後段 investing_* 由使用者從 InvestingPro 手抄。
TEMPLATE_INVESTING_COLS: tuple[str, ...] = (
    "investing_fv",
    "investing_upside_pct",
    "investing_uncertainty",
    "investing_n_models",
    "investing_date",
)


def run_investing_template(settings: Path, m4_csv: Path | None = None) -> Path:
    """A 輔線：最新交易日的仿製估值快照（settings `investing_compare_ids`）＋ 最新週
    `dcf_inputs.csv` 的 `dcf_intrinsic_est` ＋（選填）M4 區間 → 範本 CSV，investing_* 欄留白。"""
    cfg = _load_cfg(settings)
    fv_cfg = dict(cfg.get("backtest", {}).get("fv_replica", {}))
    ids = [str(x) for x in fv_cfg.get("investing_compare_ids", [])]
    out_dir = Path(fv_cfg.get("out_dir", "research/fv_replica"))
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = _build_panel(cfg, anchors=[]).filter(pl.col("stock_id").is_in(ids))
    tbl = snap.select(
        "stock_id",
        pl.col("date").alias("price_date"),
        "price",
        "fv_replica",
        "fv_upside_pct",
        "fv_n_models",
        "fv_replica_raw",
        "fv_raw_upside_pct",
        "up_composite6",
    )
    weeks = sorted(Path(cfg["paths"]["reports_dir"]).glob("2*-W*/dcf_inputs.csv"))
    if weeks:
        dcf = pl.read_csv(weeks[-1], schema_overrides={"stock_id": pl.Utf8}).select(
            "stock_id", "dcf_intrinsic_est"
        )
        tbl = tbl.join(dcf, on="stock_id", how="left")
    if m4_csv is not None and m4_csv.exists():
        m4 = pl.read_csv(m4_csv, schema_overrides={"stock_id": pl.Utf8})
        tbl = tbl.join(m4, on="stock_id", how="left")
    tbl = tbl.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c) for c in TEMPLATE_INVESTING_COLS)
    missing = sorted(set(ids) - set(tbl["stock_id"].to_list()))
    if missing:
        tbl = pl.concat([tbl, pl.DataFrame({"stock_id": missing})], how="diagonal_relaxed")
    dest = out_dir / "investing_snapshot_template.csv"
    tbl.sort("stock_id").write_csv(dest)
    return dest


def compare_investing(filled: pl.DataFrame) -> str:
    """A 輔線對照（純描述、無裁決）：investing_fv vs 仿製估值／M4 中點／dcf_intrinsic_est／
    綜合缺口%——Spearman、upside 方向一致率、中位 |log 差|，並列最大分歧。"""
    df = filled.with_columns(pl.col("investing_fv").cast(pl.Float64, strict=False)).filter(
        pl.col("investing_fv") > 0
    )
    if "m4_lo" in df.columns and "m4_hi" in df.columns:
        df = df.with_columns(((pl.col("m4_lo") + pl.col("m4_hi")) / 2).alias("m4_mid"))
    df = df.with_columns(((pl.col("investing_fv") / pl.col("price") - 1) * 100).alias("_inv_up"))
    lines = [
        "# A 輔線：investing.com（InvestingPro）公允價值 vs 本地估值——橫斷面對照\n",
        f"樣本 {df.height} 檔（純描述、樣本小、無裁決；"
        "**不能**據此判斷 investing.com 本身準不準）。\n",
        "| 本地欄 | n | Spearman(值) | upside 方向一致率 | 中位 \\|log(本地/investing)\\| |",
        "|---|---|---|---|---|",
    ]
    for col in ("fv_replica", "fv_replica_raw", "m4_mid", "dcf_intrinsic_est"):
        if col not in df.columns:
            continue
        sub = df.with_columns(pl.col(col).cast(pl.Float64, strict=False)).filter(pl.col(col) > 0)
        if sub.height < 3:
            lines.append(f"| {col} | {sub.height} | — | — | — |")
            continue
        rho = sub.select(pl.corr(col, "investing_fv", method="spearman")).item()
        agree = sub.select(
            ((pl.col(col) > pl.col("price")) == (pl.col("investing_fv") > pl.col("price"))).mean()
        ).item()
        mad = sub.select((pl.col(col) / pl.col("investing_fv")).log().abs().median()).item()
        lines.append(f"| {col} | {sub.height} | {rho:+.2f} | {agree * 100:.0f}% | {mad:.2f} |")
    if "up_composite6" in df.columns:
        sub = df.drop_nulls(["up_composite6"])
        if sub.height >= 3:
            rho = sub.select(pl.corr("up_composite6", "_inv_up", method="spearman")).item()
            lines.append(
                f"\n- 綜合缺口%（composite6 近似）vs investing upside：Spearman {rho:+.2f}"
            )
    if "fv_replica" in df.columns:
        div = (
            df.filter(pl.col("fv_replica") > 0)
            .with_columns((pl.col("fv_replica") / pl.col("investing_fv")).log().alias("_d"))
            .sort(pl.col("_d").abs(), descending=True)
            .head(5)
        )
        lines.append("\n最大分歧（仿製估值 vs investing，拆模型找原因時從這幾檔開始）：\n")
        for r in div.iter_rows(named=True):
            lines.append(
                f"- {r['stock_id']}：現價 {r['price']}、仿製 {r['fv_replica']:.1f}、"
                f"investing {r['investing_fv']:.1f}（log 差 {r['_d']:+.2f}）"
            )
    return "\n".join(lines) + "\n"


def run_fv_replica_read(settings: Path, reuse_panel: bool = False) -> str:
    """讀 FinMind 快取 → 建（或重用）面板 → Q1–Q5 → research/fv_replica/read_<今日>.md。"""
    from tw_screener.backtest.regime_slice import load_regime_labels

    cfg = _load_cfg(settings)
    fv_cfg = dict(cfg.get("backtest", {}).get("fv_replica", {}))
    horizons = tuple(int(h) for h in fv_cfg.get("horizons", [60, 120, 250]))
    out_dir = Path(fv_cfg.get("out_dir", "research/fv_replica"))
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_path = out_dir / "panel.parquet"
    if reuse_panel and panel_path.exists():
        panel = pl.read_parquet(panel_path)
    else:
        panel = _build_panel(cfg)
        panel.write_parquet(panel_path)
    regime_path = Path(
        cfg.get("backtest", {})
        .get("regime_history", {})
        .get("output_path", "research/panel/regime_labels.parquet")
    )
    report = analyze(panel, horizons, fv_cfg, load_regime_labels(regime_path))
    dest = out_dir / f"read_{date.today().isoformat()}.md"
    dest.write_text(report, encoding="utf-8")
    return report
