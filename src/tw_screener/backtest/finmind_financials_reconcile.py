"""FinMind 財報 vs 本地 fundamentals 對帳（docs/31 §20.15，機械式 DCF 接線前置門檻）。

在 FinMind Financials（單季）與本地 `fundamentals_*.parquet`（TWSE MOPS，單季）都有的
(stock_id, year, quarter) 上，逐檔配對 Revenue / EPS，統計系統性差異。**事前寫死的判準**：

1. 配對 Revenue 中位比值（finmind / local）∈ [0.97, 1.03]
2. `|ratio − 1| > 0.10` 的股票 < 10%（比 PER 的 5% 鬆——FinMind 對財報 de-cumulate 有誤差）
3. EPS 符號一致率 ≥ 98%
4. FinMind Revenue 對非金融股的覆蓋率 ≥ 95%

過 → `dcf_intrinsic_est` 進 `candidates_enriched.csv`；不過 → 只留 `reports/<週>/dcf_inputs.csv`
研究檔、docs/11 M3 改讀研究檔。輸出 `research/finmind_financials_reconciliation_<date>.md`。
純研究、不進 pipeline。
"""

from __future__ import annotations

import glob
import statistics
from datetime import date
from pathlib import Path

import polars as pl
import yaml

# 事前寫死判準（§20.15）——改這裡等於改門檻，需回 docs/31 §20.15 同步
REVENUE_MEDIAN_RATIO_LOW = 0.97
REVENUE_MEDIAN_RATIO_HIGH = 1.03
STOCK_OUTLIER_SHARE_MAX = 0.10
EPS_SIGN_CONSISTENCY_MIN = 0.98
REVENUE_COVERAGE_MIN = 0.95

_FINANCIAL_INDUSTRY = "金融保險"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_reconciliation(
    finmind_financials: pl.DataFrame,
    local_fundamentals: pl.DataFrame,
    financial_sids: set[str],
) -> dict:
    """配對 FinMind vs 本地的 Revenue / EPS（純函式）。

    finmind_financials：(stock_id, year, quarter, revenue[原始 TWD], eps, …)
    local_fundamentals：(stock_id, year, quarter, revenue_m[百萬], eps, …)
    """
    fm = finmind_financials.select(
        "stock_id", "year", "quarter",
        (pl.col("revenue") / 1e6).alias("fm_rev_m"),
        pl.col("eps").alias("fm_eps"),
    ).filter(pl.col("stock_id").is_in(list(local_fundamentals["stock_id"].unique())))
    loc = local_fundamentals.select(
        "stock_id", "year", "quarter",
        pl.col("revenue_m").alias("loc_rev_m"),
        pl.col("eps").alias("loc_eps"),
    )
    joined = fm.join(loc, on=["stock_id", "year", "quarter"], how="inner")
    n_pairs = joined.height

    both_rev = joined.filter(
        pl.col("fm_rev_m").is_not_null()
        & pl.col("loc_rev_m").is_not_null()
        & (pl.col("loc_rev_m") > 0)
    ).with_columns((pl.col("fm_rev_m") / pl.col("loc_rev_m")).alias("ratio"))
    ratios = sorted(both_rev["ratio"].to_list())
    rev_stats: dict = {"n": len(ratios)}
    if ratios:
        rev_stats["median"] = statistics.median(ratios)
        rev_stats["q1"] = ratios[len(ratios) // 4]
        rev_stats["q3"] = ratios[(len(ratios) * 3) // 4]
        per_stock = both_rev.group_by("stock_id").agg(pl.col("ratio").median().alias("m"))
        n_stock = per_stock.height
        n_stock_out = per_stock.filter((pl.col("m") - 1.0).abs() > 0.10).height
        rev_stats["n_stock"] = n_stock
        rev_stats["n_stock_outlier"] = n_stock_out
        rev_stats["stock_outlier_share"] = n_stock_out / n_stock if n_stock else 0.0

    # EPS 符號一致率（兩邊皆非 null）
    both_eps = joined.filter(
        pl.col("fm_eps").is_not_null() & pl.col("loc_eps").is_not_null()
    )
    n_eps = both_eps.height
    n_eps_agree = both_eps.filter(
        (pl.col("fm_eps") >= 0) == (pl.col("loc_eps") >= 0)
    ).height
    eps_sign_consistency = n_eps_agree / n_eps if n_eps else 0.0

    # 覆蓋率：本地有正 revenue_m 的非金融 (stock, y, q)，FinMind 也有 revenue
    loc_nonfin = loc.filter(
        ~pl.col("stock_id").is_in(list(financial_sids))
        & pl.col("loc_rev_m").is_not_null()
        & (pl.col("loc_rev_m") > 0)
    )
    loc_with_fm = loc_nonfin.join(
        fm.select("stock_id", "year", "quarter", "fm_rev_m"),
        on=["stock_id", "year", "quarter"], how="left",
    )
    fm_present = loc_with_fm.filter(pl.col("fm_rev_m").is_not_null())
    coverage = fm_present.height / loc_nonfin.height if loc_nonfin.height else 0.0

    return {
        "n_pairs": n_pairs,
        "n_finmind_stocks": finmind_financials["stock_id"].n_unique(),
        "n_local_stocks": local_fundamentals["stock_id"].n_unique(),
        "periods": sorted(
            {
                f"{r['year']}Q{r['quarter']}"
                for r in joined.select("year", "quarter").iter_rows(named=True)
            }
        ),
        "revenue": rev_stats,
        "eps_sign_consistency": eps_sign_consistency,
        "n_eps_pairs": n_eps,
        "coverage": coverage,
        "n_coverage_denom": loc_nonfin.height,
    }


def evaluate_criteria(stats: dict) -> tuple[bool, list[tuple[str, bool, str]]]:
    rev = stats["revenue"]
    checks: list[tuple[str, bool, str]] = []
    if rev.get("n", 0) == 0:
        return False, [("樣本不足，無法判定", False, "n=0")]

    c1 = REVENUE_MEDIAN_RATIO_LOW <= rev["median"] <= REVENUE_MEDIAN_RATIO_HIGH
    checks.append((
        f"配對 Revenue 中位比值 ∈ [{REVENUE_MEDIAN_RATIO_LOW}, {REVENUE_MEDIAN_RATIO_HIGH}]",
        c1, f"{rev['median']:.4f}",
    ))
    c2 = rev["stock_outlier_share"] < STOCK_OUTLIER_SHARE_MAX
    checks.append((
        f"|ratio−1| > 0.10 的股票 < {_pct(STOCK_OUTLIER_SHARE_MAX)}",
        c2, f"{_pct(rev['stock_outlier_share'])}（{rev['n_stock_outlier']}/{rev['n_stock']} 檔）",
    ))
    c3 = stats["eps_sign_consistency"] >= EPS_SIGN_CONSISTENCY_MIN
    checks.append((
        f"EPS 符號一致率 ≥ {_pct(EPS_SIGN_CONSISTENCY_MIN)}",
        c3, f"{_pct(stats['eps_sign_consistency'])}（n={stats['n_eps_pairs']}）",
    ))
    c4 = stats["coverage"] >= REVENUE_COVERAGE_MIN
    checks.append((
        f"FinMind Revenue 對非金融股覆蓋率 ≥ {_pct(REVENUE_COVERAGE_MIN)}",
        c4, f"{_pct(stats['coverage'])}（分母 {stats['n_coverage_denom']}）",
    ))
    return all(c for _, c, _ in checks), checks


def _format_report(stats: dict, is_partial: bool) -> list[str]:
    passed, checks = evaluate_criteria(stats)
    rev = stats["revenue"]
    lines: list[str] = []
    lines.append(f"# FinMind 財報 vs 本地 fundamentals 對帳（{date.today().isoformat()}）")
    lines.append("")
    lines.append("> docs/31 §20.15。四個判準事前寫死於 `finmind_financials_reconcile.py`。")
    lines.append("> 過 → `dcf_intrinsic_est` 進 candidates_enriched.csv；")
    lines.append("> 不過 → 只留 reports/<週>/dcf_inputs.csv 研究檔、docs/11 M3 改讀研究檔。")
    lines.append("")
    if is_partial:
        lines.append(
            f"⚠️ **初測：僅 {stats['n_finmind_stocks']} 檔 FinMind 子集**。判準對全市場事前寫死，"
            "本讀值非正式裁決——待全量回補後重跑。"
        )
        lines.append("")
    lines.append("## 配對範圍")
    lines.append("")
    lines.append(f"- 配對數：{stats['n_pairs']}（stock × 季）")
    lines.append(f"- 季別：{', '.join(stats['periods'])}")
    lines.append(
        f"- FinMind 覆蓋 {stats['n_finmind_stocks']} 檔、本地 {stats['n_local_stocks']} 檔"
    )
    lines.append("")
    lines.append("## Revenue 比值（finmind / local，皆正值）")
    lines.append("")
    if rev.get("n", 0):
        lines.append(f"- n = {rev['n']} 配對、{rev['n_stock']} 檔")
        lines.append(
            f"- 中位比值：**{rev['median']:.4f}**（IQR {rev['q1']:.3f} – {rev['q3']:.3f}）"
        )
        lines.append(
            f"- 股票離群（中位比值離 1 >10%）：{rev['n_stock_outlier']}/{rev['n_stock']}"
            f"（{_pct(rev['stock_outlier_share'])}）"
        )
    else:
        lines.append("- 樣本不足")
    lines.append("")
    lines.append("## EPS 符號一致 / 覆蓋率")
    lines.append("")
    lines.append(
        f"- EPS 符號一致率：**{_pct(stats['eps_sign_consistency'])}**（n={stats['n_eps_pairs']}）"
    )
    lines.append(
        f"- FinMind Revenue 對非金融股覆蓋率：**{_pct(stats['coverage'])}**"
        f"（分母 {stats['n_coverage_denom']}）"
    )
    lines.append("")
    lines.append("## 判準")
    lines.append("")
    for text, ok, val in checks:
        lines.append(f"- [{'✅' if ok else '❌'}] {text} — 實測 {val}")
    lines.append("")
    verdict = (
        "全數通過 → 接進 candidates_enriched.csv"
        if passed
        else "未全數通過 → 只留 dcf_inputs.csv 研究檔"
    )
    if is_partial:
        verdict += "（初測，非正式裁決）"
    lines.append(f"**結論：{verdict}**")
    lines.append("")
    return lines


def _load_local_fundamentals(cache_dir: Path) -> pl.DataFrame:
    files = sorted(glob.glob(str(cache_dir / "twse" / "fundamentals_*.parquet")))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def run_finmind_financials_reconcile(
    settings: Path, out_path: Path | None = None
) -> str:
    """讀 FinMind Financials 快取 + 本地 fundamentals → 對帳 → markdown。"""
    from tw_screener.analysis.sector_universe import load_industry_mapping
    from tw_screener.data.finmind import load_financials_history

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cache_dir = Path(cfg["paths"]["cache_dir"])
    fm_fin = load_financials_history(cache_dir / "finmind")
    local = _load_local_fundamentals(cache_dir)

    if fm_fin.is_empty() or local.is_empty():
        report = (
            f"# FinMind 財報對帳（{date.today().isoformat()}）\n\n"
            "❌ 缺 FinMind 財報快取或本地 fundamentals——先跑 "
            "`make backfill-finmind-financials` 與 `make fetch-twse`。\n"
        )
    else:
        industry = load_industry_mapping(cache_dir / "twse")
        financial_sids = set(
            industry.filter(pl.col("industry_name") == _FINANCIAL_INDUSTRY)["stock_id"].to_list()
        )
        n_targets = fm_fin["stock_id"].n_unique()
        is_partial = n_targets < 900
        stats = build_reconciliation(fm_fin, local, financial_sids)
        report = "\n".join(_format_report(stats, is_partial))

    dest = out_path or Path(
        f"research/finmind_financials_reconciliation_{date.today().isoformat()}.md"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    return report
