"""FinMind PER vs TWSE 官方估值比對帳（docs/31 §20.14，1b 接線方式的前置門檻）。

在 FinMind 與 TWSE 都有資料的重疊交易日上，逐檔配對 PE/PBR，統計兩來源的系統性差異。
**事前寫死的三個通過判準**（§20.14）——過 → 路徑 (a)（FinMind 只補歷史深度、當前仍
TWSE，`compute_self_history_*` 直接吃 merged history）；不過 → 路徑 (b)（FinMind 自身腿
變獨立揭露欄、不進 6 腿綜合版）：

1. 配對 PE 中位比值（finmind_pe / twse_pe）∈ [0.97, 1.03]
2. `|ratio − 1| > 0.10` 的股票 < 5%
3. FinMind PE 對非虧損股覆蓋率 ≥ 95%（相對 TWSE 有值）

輸出 `research/finmind_reconciliation_<date>.md`。純研究、不進 pipeline。
"""

from __future__ import annotations

import statistics
from datetime import date
from pathlib import Path

import polars as pl
import yaml

# 事前寫死判準（§20.14）——改這裡等於改門檻，需回 docs/31 §20.14 同步
PE_MEDIAN_RATIO_LOW = 0.97
PE_MEDIAN_RATIO_HIGH = 1.03
OUTLIER_SHARE_MAX = 0.05
PE_COVERAGE_MIN = 0.95


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_reconciliation(finmind_hist: pl.DataFrame, twse_hist: pl.DataFrame) -> dict:
    """重疊 (stock_id, date) 上配對 FinMind / TWSE 的 PE、PBR，回傳統計 dict（純函式）。"""
    fm = finmind_hist.select(
        "stock_id", "date",
        pl.col("pe").alias("fm_pe"), pl.col("pbr").alias("fm_pbr"),
    )
    tw = twse_hist.select(
        "stock_id", "date",
        pl.col("pe").alias("tw_pe"), pl.col("pbr").alias("tw_pbr"),
    )
    joined = fm.join(tw, on=["stock_id", "date"], how="inner")
    n_pairs = joined.height
    # 重疊交易日＝兩來源資料各自出現過的日期交集（非 inner-join 後的、避免被單股缺漏縮窄）
    overlap_dates = sorted(
        set(fm["date"].drop_nulls().to_list())
        & set(tw["date"].drop_nulls().to_list())
    )

    def _ratio_stats(num: str, den: str) -> dict:
        both = joined.filter(
            pl.col(num).is_not_null() & pl.col(den).is_not_null() & (pl.col(den) > 0)
        ).with_columns((pl.col(num) / pl.col(den)).alias("ratio"))
        ratios = sorted(both["ratio"].to_list())
        if not ratios:
            return {"n": 0}
        med = statistics.median(ratios)
        q1 = ratios[len(ratios) // 4]
        q3 = ratios[(len(ratios) * 3) // 4]
        n_out = sum(1 for r in ratios if abs(r - 1.0) > 0.10)
        # per-stock 離群占比：一檔只要中位比值離 1 超過 10% 就算離群
        per_stock = (
            both.group_by("stock_id")
            .agg(pl.col("ratio").median().alias("m"))
        )
        n_stock = per_stock.height
        n_stock_out = per_stock.filter((pl.col("m") - 1.0).abs() > 0.10).height
        return {
            "n": len(ratios), "median": med, "q1": q1, "q3": q3,
            "n_pair_outlier": n_out, "pair_outlier_share": n_out / len(ratios),
            "n_stock": n_stock, "n_stock_outlier": n_stock_out,
            "stock_outlier_share": n_stock_out / n_stock if n_stock else 0.0,
        }

    pe_stats = _ratio_stats("fm_pe", "tw_pe")
    pbr_stats = _ratio_stats("fm_pbr", "tw_pbr")

    # 覆蓋率：**FinMind 有回補的股票**在重疊交易日上「TWSE 有正 PE」的每一筆 (stock, date)，
    # FinMind 也有正 PE 的比例。分母限定 FinMind universe → 初測子集也可判讀（回答「FinMind
    # 有這檔時，是否補齊它的 TWSE-PE 日」，而非「FinMind 是否涵蓋全市場」——後者由 CLI 的
    # 「有資料 X／空 Y」分開報）。全量回補時兩者等價。
    fm_universe = set(finmind_hist["stock_id"].to_list())
    overlap_set = set(overlap_dates)
    tw_has_pe = tw.filter(
        pl.col("date").is_in(list(overlap_set))
        & pl.col("stock_id").is_in(list(fm_universe))
        & pl.col("tw_pe").is_not_null()
        & (pl.col("tw_pe") > 0)
    )
    tw_with_fm = tw_has_pe.join(
        fm.select("stock_id", "date", "fm_pe"), on=["stock_id", "date"], how="left"
    )
    fm_also = tw_with_fm.filter(pl.col("fm_pe").is_not_null() & (pl.col("fm_pe") > 0))
    pe_coverage = fm_also.height / tw_has_pe.height if tw_has_pe.height else 0.0

    # 虧損股表述分歧：一邊 PE 有值、另一邊 None（含 0 已在 parser 轉 None）
    fm_none_tw_val = joined.filter(
        pl.col("fm_pe").is_null() & pl.col("tw_pe").is_not_null() & (pl.col("tw_pe") > 0)
    ).height
    tw_none_fm_val = joined.filter(
        pl.col("tw_pe").is_null() & pl.col("fm_pe").is_not_null() & (pl.col("fm_pe") > 0)
    ).height

    return {
        "n_pairs": n_pairs,
        "n_overlap_dates": len(overlap_dates),
        "overlap_first": overlap_dates[0].isoformat() if overlap_dates else None,
        "overlap_last": overlap_dates[-1].isoformat() if overlap_dates else None,
        "n_finmind_stocks": finmind_hist["stock_id"].n_unique(),
        "pe": pe_stats,
        "pbr": pbr_stats,
        "pe_coverage": pe_coverage,
        "lossmaker_fm_none_tw_val": fm_none_tw_val,
        "lossmaker_tw_none_fm_val": tw_none_fm_val,
    }


def evaluate_criteria(stats: dict) -> tuple[bool, list[tuple[str, bool, str]]]:
    """對三個事前判準，回傳 (全過?, [(判準文字, 過?, 實測值), ...])。"""
    pe = stats["pe"]
    checks: list[tuple[str, bool, str]] = []
    if pe.get("n", 0) == 0:
        return False, [("樣本不足，無法判定", False, "n=0")]

    c1 = PE_MEDIAN_RATIO_LOW <= pe["median"] <= PE_MEDIAN_RATIO_HIGH
    checks.append((
        f"配對 PE 中位比值 ∈ [{PE_MEDIAN_RATIO_LOW}, {PE_MEDIAN_RATIO_HIGH}]",
        c1, f"{pe['median']:.4f}",
    ))
    c2 = pe["stock_outlier_share"] < OUTLIER_SHARE_MAX
    checks.append((
        f"|ratio−1| > 0.10 的股票 < {_pct(OUTLIER_SHARE_MAX)}",
        c2, f"{_pct(pe['stock_outlier_share'])}（{pe['n_stock_outlier']}/{pe['n_stock']} 檔）",
    ))
    c3 = stats["pe_coverage"] >= PE_COVERAGE_MIN
    checks.append((
        f"FinMind PE 對非虧損股覆蓋率 ≥ {_pct(PE_COVERAGE_MIN)}",
        c3, _pct(stats["pe_coverage"]),
    ))
    return all(c for _, c, _ in checks), checks


def _format_report(stats: dict, is_partial: bool, n_targets: int) -> str:
    passed, checks = evaluate_criteria(stats)
    pe, pbr = stats["pe"], stats["pbr"]
    lines: list[str] = []
    lines.append(f"# FinMind PER vs TWSE 官方估值比對帳（{date.today().isoformat()}）")
    lines.append("")
    lines.append("> docs/31 §20.14。三個判準事前寫死於 `finmind_reconcile.py`。")
    lines.append("> 過 → 1b 走路徑 (a)（FinMind 補歷史深度、當前仍 TWSE）；")
    lines.append("> 不過 → 路徑 (b)（FinMind 自身腿獨立揭露欄、不進 6 腿綜合版）。")
    lines.append("")
    if is_partial:
        lines.append(
            f"⚠️ **初測：僅 {stats['n_finmind_stocks']} 檔 FinMind 子集**"
            f"（回補 {n_targets} 檔中有資料者）。三判準是對**全市場**對帳事前寫死的，"
            "本讀值不等於正式門檻裁決——待全量回補後重跑本指令。"
        )
        lines.append("")
    lines.append("## 重疊範圍")
    lines.append("")
    lines.append(f"- 配對數：{stats['n_pairs']}（stock × date）")
    lines.append(
        f"- 重疊交易日：{stats['n_overlap_dates']} 天"
        f"（{stats['overlap_first']} → {stats['overlap_last']}）"
    )
    lines.append(f"- FinMind 覆蓋股票數：{stats['n_finmind_stocks']}")
    lines.append("")
    lines.append("## PE 比值（finmind_pe / twse_pe，僅兩邊皆有正值）")
    lines.append("")
    lines.append(f"- n = {pe['n']} 配對、{pe['n_stock']} 檔")
    lines.append(f"- 中位比值：**{pe['median']:.4f}**（IQR {pe['q1']:.3f} – {pe['q3']:.3f}）")
    lines.append(
        f"- 配對離群（|ratio−1|>0.10）：{pe['n_pair_outlier']}"
        f"（{_pct(pe['pair_outlier_share'])}）"
    )
    lines.append(
        f"- 股票離群（該檔中位比值離 1 >10%）：{pe['n_stock_outlier']}/{pe['n_stock']}"
        f"（{_pct(pe['stock_outlier_share'])}）"
    )
    lines.append("")
    lines.append("## PBR 比值（finmind_pbr / twse_pbr）")
    lines.append("")
    if pbr.get("n", 0):
        lines.append(f"- n = {pbr['n']} 配對、{pbr['n_stock']} 檔")
        lines.append(
            f"- 中位比值：{pbr['median']:.4f}（IQR {pbr['q1']:.3f} – {pbr['q3']:.3f}）"
        )
        lines.append(f"- 股票離群：{_pct(pbr['stock_outlier_share'])}")
    else:
        lines.append("- 樣本不足")
    lines.append("")
    lines.append("## 覆蓋率 / 虧損股表述")
    lines.append("")
    lines.append(f"- FinMind PE 對「TWSE 有正 PE」配對的覆蓋率：**{_pct(stats['pe_coverage'])}**")
    lines.append(
        f"- FinMind 判虧損（PE None）而 TWSE 有值：{stats['lossmaker_fm_none_tw_val']} 配對"
    )
    lines.append(
        f"- TWSE 判虧損而 FinMind 有值：{stats['lossmaker_tw_none_fm_val']} 配對"
    )
    lines.append("")
    lines.append("## 判準")
    lines.append("")
    for text, ok, val in checks:
        lines.append(f"- [{'✅' if ok else '❌'}] {text} — 實測 {val}")
    lines.append("")
    verdict = "全數通過 → 路徑 (a)" if passed else "未全數通過 → 路徑 (b)"
    if is_partial:
        verdict += "（初測，非正式裁決）"
    lines.append(f"**結論：{verdict}**")
    lines.append("")
    return "\n".join(lines)


def run_finmind_reconcile(
    settings: Path, out_path: Path | None = None
) -> str:
    """讀 FinMind PER 快取 + TWSE valuation_ratios 歷史 → 對帳 → markdown。"""
    from tw_screener.data.finmind import load_finmind_per_history
    from tw_screener.data.twse import create_client

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cache_dir = Path(cfg["paths"]["cache_dir"])
    finmind_hist = load_finmind_per_history(cache_dir / "finmind")
    twse_hist = create_client(settings).load_valuation_ratios_history()

    if finmind_hist.is_empty():
        report = (
            f"# FinMind 對帳（{date.today().isoformat()}）\n\n"
            "❌ `data/cache/finmind/` 無資料——先跑 `make backfill-finmind-per`。\n"
        )
    else:
        n_targets = finmind_hist["stock_id"].n_unique()
        # 全次產業成員約 1132 檔；子集 < 900 視為初測
        is_partial = n_targets < 900
        stats = build_reconciliation(finmind_hist, twse_hist)
        report = _format_report(stats, is_partial, n_targets)

    dest = out_path or Path(
        f"research/finmind_reconciliation_{date.today().isoformat()}.md"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    return report
