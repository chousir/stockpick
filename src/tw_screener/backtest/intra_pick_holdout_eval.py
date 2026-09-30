"""backtest/intra_pick_holdout_eval.py — M-Pick3b 保留樣本驗證（純函式；docs/34 預註冊）。

在 M-Pick3a 的保留樣本週快照（2015-01～2021-12）上，對 M-Pick2 事後揭露的兩個族群內訊號
（H1 成交額、H2 偏好帶）與兩個複驗因子（F2、F4）套用 docs/32 §4 的同一組判準。
**裁決邏輯一律重用 `intra_pick.evaluate_factor`／`classify`（零改動；docs/34 §6 以 git blob id
釘版）**，本模組只補三件事：
- 完整性防線：快照 SHA-256 與判準邏輯檔 blob 與預註冊釘版比對，不符即中止（docs/34 §2.1、§6）；
- 樣本規則與鏡像因子：剔除 r+20 出場落在 2022 年的快照（§2.3）、H2 以 `band_dist`
  （＝−`neg_band_dist`）當「方向＋」因子評估（§3）；
- 分族 Bonferroni（主族 k、副族 k，§4）與跑前可行性檢查（§2.5：**只讀各欄是否非 null 的旗標**，
  結構上不讀因子值或報酬值）。
IO 與報表在 intra_pick_holdout_eval_runner。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from tw_screener.backtest import intra_pick as ip

FAMILY_PRIMARY = "primary"
FAMILY_SECONDARY = "secondary"


@dataclass(frozen=True)
class Hypothesis:
    """預註冊的一個假設（docs/34 §3）：一個定義、方向一律＋（H2 用鏡像因子達成）。"""

    key: str
    family: str
    factor: str
    label: str


#: docs/34 §3（順序＝報告列序）。H2 的 `band_dist`＝快照 `_band_dist`（距偏好帶，越遠越大）。
HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis("H1", FAMILY_PRIMARY, "log_amount", "H1 族群內成交額（ln 單日成交額）"),
    Hypothesis("H2", FAMILY_PRIMARY, "band_dist", "H2 偏好帶距離（鏡像：越遠越好＝越貼近越差）"),
    Hypothesis("F2", FAMILY_SECONDARY, "high52_near", "F2 52 週高點接近度（複驗）"),
    Hypothesis("F4", FAMILY_SECONDARY, "rev_accel", "F4 月營收 YoY 加速（複驗）"),
)


class PinMismatchError(RuntimeError):
    """快照 SHA-256 或判準邏輯檔 blob 與預註冊釘版不符（docs/34 §2.1、§6）。"""


@dataclass(frozen=True)
class HoldoutEvalConfig:
    """settings `backtest.intra_pick_holdout`（本輪新增／釘版項；判準值沿用 intra_pick 區塊）。"""

    snapshot_path: str = "research/intra_pick_oos/holdout_stockweeks_20260930.parquet"
    snapshot_sha256: str = ""
    last_snapshot: date = date(2021, 11, 30)
    n_tests_primary: int = 2
    n_tests_secondary: int = 2
    pinned_blobs: Mapping[str, str] = field(default_factory=dict)
    tdr_ids: tuple[str, ...] = ("9105", "9136")
    output_dir: str = "research/intra_pick_holdout"

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> HoldoutEvalConfig:
        d = cls()
        hv = (cfg.get("backtest") or {}).get("intra_pick_holdout") or {}
        pins = hv.get("pinned_blobs") or {}
        return cls(
            snapshot_path=str(hv.get("snapshot_path", d.snapshot_path)),
            snapshot_sha256=str(hv.get("snapshot_sha256", d.snapshot_sha256)).lower(),
            last_snapshot=date.fromisoformat(str(hv.get("last_snapshot", d.last_snapshot))),
            n_tests_primary=int(hv.get("n_tests_primary", d.n_tests_primary)),
            n_tests_secondary=int(hv.get("n_tests_secondary", d.n_tests_secondary)),
            pinned_blobs={str(k): str(v).lower() for k, v in pins.items()},
            tdr_ids=tuple(str(s) for s in hv.get("tdr_ids", d.tdr_ids)),
            output_dir=str(hv.get("output_dir", d.output_dir)),
        )

    def n_tests(self, family: str) -> int:
        return self.n_tests_primary if family == FAMILY_PRIMARY else self.n_tests_secondary


# ─── 完整性防線 ───────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_id(path: Path) -> str:
    """與 `git hash-object <path>` 相同的 blob id：sha1(b"blob <位元組數>\\0" + 內容)。"""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def verify_snapshot(path: Path, expected_sha256: str) -> str:
    """重算快照 SHA-256，與預註冊釘版比對；不符（或未釘）→ PinMismatchError。回實際值。"""
    if not expected_sha256:
        raise PinMismatchError(
            "settings 缺 backtest.intra_pick_holdout.snapshot_sha256（預註冊釘版）"
        )
    actual = sha256_file(path)
    if actual != expected_sha256.lower():
        raise PinMismatchError(
            f"快照 SHA-256 與預註冊釘版不符：{path}\n  實際 {actual}\n  釘版 {expected_sha256}"
        )
    return actual


def verify_blob_pins(pins: Mapping[str, str], root: Path = Path(".")) -> dict[str, str]:
    """判準邏輯檔的 git blob id 與釘版比對（docs/34 §6 零改動）；任一不符 → PinMismatchError。"""
    actual: dict[str, str] = {}
    bad: list[str] = []
    for rel, want in pins.items():
        path = root / rel
        if not path.exists():
            bad.append(f"{rel}：檔案不存在")
            continue
        got = git_blob_id(path)
        actual[rel] = got
        if got != want.lower():
            bad.append(f"{rel}：實際 {got}／釘版 {want}")
    if bad:
        raise PinMismatchError("判準邏輯檔與預註冊釘版不符（docs/34 §6）：\n  " + "\n  ".join(bad))
    return actual


# ─── 樣本規則、鏡像因子、分族設定 ─────────────────────────────────────────────


def apply_sample_rule(sw: pl.DataFrame, last_snapshot: date) -> tuple[pl.DataFrame, list[date]]:
    """docs/34 §2.3：只留快照日 ≤ last_snapshot（＝r+20 出場日 ≤ 2021-12-31）。

    Returns: (保留列, 被剔除的快照日)。
    """
    dropped = sorted(sw.filter(pl.col("date") > last_snapshot)["date"].unique().to_list())
    return sw.filter(pl.col("date") <= last_snapshot), dropped


def add_mirror_factor(sw: pl.DataFrame) -> pl.DataFrame:
    """H2 鏡像因子 `band_dist`＝`_band_dist`（docs/34 §3）：IC(band_dist) ≡ −IC(neg_band_dist)。"""
    return sw.with_columns(pl.col("_band_dist").alias("band_dist"))


def family_config(
    base: ip.IntraPickConfig, ecfg: HoldoutEvalConfig, family: str
) -> ip.IntraPickConfig:
    """分族 Bonferroni：n_tests 換成該族的 k（其餘判準值不動）。"""
    return replace(base, n_tests=ecfg.n_tests(family))


@dataclass(frozen=True)
class HypothesisResult:
    hypothesis: Hypothesis
    result: ip.FactorResult


def evaluate_hypotheses(
    sw: pl.DataFrame,
    base_cfg: ip.IntraPickConfig,
    ecfg: HoldoutEvalConfig,
    regimes: pl.DataFrame,
) -> list[HypothesisResult]:
    """四個預註冊假設在主宇宙（`in_main`）、r+20 的完整評估（docs/34 §4；裁決由 classify 產生）。"""
    return [
        HypothesisResult(
            h,
            ip.evaluate_factor(
                sw, h.factor, family_config(base_cfg, ecfg, h.family), "in_main", regimes=regimes
            ),
        )
        for h in HYPOTHESES
    ]


def yearly_ic(ic_weekly: pl.DataFrame) -> pl.DataFrame:
    """D2：週 IC 序列依快照日所屬自然年分組的平均（描述性）。Returns: year / n_weeks / ic_mean。"""
    schema = {"year": pl.Int32, "n_weeks": pl.UInt32, "ic_mean": pl.Float64}
    if ic_weekly.is_empty():
        return pl.DataFrame(schema=schema)
    return (
        ic_weekly.group_by(pl.col("date").dt.year().cast(pl.Int32).alias("year"))
        .agg(pl.len().cast(pl.UInt32).alias("n_weeks"), pl.col("ic").mean().alias("ic_mean"))
        .sort("year")
        .select(list(schema))
    )


# ─── 跑前可行性檢查（只讀旗標）───────────────────────────────────────────────


def _usable(col: str) -> pl.Expr:
    """可用旗標（非 null 且非 NaN）——只外露布林，不外露數值。"""
    return (pl.col(col).is_not_null() & pl.col(col).is_not_nan()).fill_null(False)


def _median(s: pl.Series) -> float | None:
    v = s.median()
    return float(v) if isinstance(v, (int, float)) else None


def _group_weeks(flags: pl.DataFrame, ok: str, cfg: ip.IntraPickConfig) -> pl.DataFrame:
    """每週檔數／組數：主宇宙且 target 與 `ok` 旗標皆可用、同（週, 次產業）≥ min_group 檔的列，
    週合格成員 ≥ min_names_week（同 ip.weekly_ic 的納入規則，不算 IC）。"""
    g = (
        flags.filter(pl.col("in_main") & pl.col("_t") & pl.col(ok))
        .with_columns(pl.len().over("date", "sub_industry").alias("_ng"))
        .filter(pl.col("_ng") >= cfg.min_group)
    )
    return (
        g.group_by("date")
        .agg(pl.len().alias("names"), pl.col("sub_industry").n_unique().alias("groups"))
        .filter(pl.col("names") >= cfg.min_names_week)
        .sort("date")
    )


def feasibility(
    sw: pl.DataFrame, cfg: ip.IntraPickConfig, tdr_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """docs/34 §2.5：宇宙規模與覆蓋。

    **只用各欄「是否可用」的布林旗標與 date／stock_id／sub_industry／in_main／regime 分組欄**，
    不讀因子值或報酬值、不算任何因子×target 統計。
    """
    target = f"r{cfg.horizon}"
    flags = sw.select(
        "date", "stock_id", "sub_industry", "in_main", "regime",
        _usable(target).alias("_t"),
        pl.col(target).is_not_null().alias("_t_nn"),
        *[_usable(h.factor).alias(f"_ok_{h.key}") for h in HYPOTHESES],
        *[pl.col(h.factor).is_not_null().alias(f"_nn_{h.key}") for h in HYPOTHESES],
    )
    regimes = flags.select("date", "regime").unique(subset=["date"])
    uni = (
        flags.filter(pl.col("in_main") & pl.col("_t"))
        .with_columns(pl.len().over("date", "sub_industry").alias("_ng"))
        .filter(pl.col("_ng") >= cfg.min_group)
    )
    uni_wk = uni.group_by("date").agg(
        pl.len().alias("names"), pl.col("sub_industry").n_unique().alias("groups")
    )
    by_hyp: dict[str, dict[str, Any]] = {}
    for h in HYPOTHESES:
        base = flags.filter(pl.col("in_main") & pl.col("_t_nn"))
        cov = base[f"_nn_{h.key}"].mean() if not base.is_empty() else None
        wk = _group_weeks(flags, f"_ok_{h.key}", cfg)
        reg = (
            wk.join(regimes, on="date", how="left")
            .group_by("regime")
            .len()
            .sort("regime")
            .to_dicts()
            if not wk.is_empty()
            else []
        )
        by_hyp[h.key] = {
            "coverage": float(cov) if isinstance(cov, (int, float)) else None,
            "computable_weeks": wk.height,
            "first": wk["date"].min() if not wk.is_empty() else None,
            "last": wk["date"].max() if not wk.is_empty() else None,
            "median_names": _median(wk["names"]) if not wk.is_empty() else None,
            "median_groups": _median(wk["groups"]) if not wk.is_empty() else None,
            "regime_weeks": {str(r["regime"]): int(r["len"]) for r in reg},
        }
    by_year = (
        uni.group_by(pl.col("date").dt.year().alias("year"))
        .agg(pl.len().alias("stock_weeks"), pl.col("date").n_unique().alias("weeks"))
        .sort("year")
        .to_dicts()
    )
    return {
        "snapshot_weeks": sw["date"].n_unique(),
        "snapshot_first": sw["date"].min(),
        "snapshot_last": sw["date"].max(),
        "universe": {
            "stock_weeks": uni.height,
            "weeks": uni_wk.height,
            "median_names": _median(uni_wk["names"]) if not uni_wk.is_empty() else None,
            "median_groups": _median(uni_wk["groups"]) if not uni_wk.is_empty() else None,
            "tdr_stock_weeks": uni.filter(pl.col("stock_id").is_in(list(tdr_ids))).height,
            "by_year": [
                {
                    "year": int(r["year"]),
                    "stock_weeks": int(r["stock_weeks"]),
                    "weeks": int(r["weeks"]),
                }
                for r in by_year
            ],
        },
        "by_hypothesis": by_hyp,
    }


def fmt_pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


def fmt_num(v: float | None, nd: int = 1) -> str:
    return f"{v:.{nd}f}" if v is not None else "—"


def render_feasibility(stats: Mapping[str, Any], cfg: ip.IntraPickConfig) -> list[str]:
    """可行性／覆蓋 markdown（docs/34 §2.5、揭露 D1）。表內只有計數與比例，沒有因子值或報酬統計。"""
    u = stats["universe"]
    lines = [
        f"- 快照 {stats['snapshot_first']}～{stats['snapshot_last']} 共 "
        f"{stats['snapshot_weeks']} 週（樣本規則之後）。",
        f"- 主宇宙（趨勢分前 {cfg.shortlist.max_bucket_for_top} 桶 × gate × r+{cfg.horizon} 可用 × "
        f"同組 ≥ {cfg.min_group} 檔）：{u['stock_weeks']:,} 股週、{u['weeks']} 週，每週中位 "
        f"{fmt_num(u['median_names'])} 檔／{fmt_num(u['median_groups'])} 組。",
        f"- TDR 股週（主宇宙內，照列不剔除）：{u['tdr_stock_weeks']:,}。",
        "",
        f"| 假設 | 覆蓋（主宇宙、r+{cfg.horizon} 非 null 列的因子非 null 比例） | "
        f"可計週數（週合格成員 ≥ {cfg.min_names_week}） | 每週中位檔數 | 每週中位組數 | "
        "regime 週數 |",
        "|---|---|---|---|---|---|",
    ]
    for h in HYPOTHESES:
        b = stats["by_hypothesis"][h.key]
        reg = "／".join(f"{k} {v}" for k, v in b["regime_weeks"].items()) or "—"
        lines.append(
            f"| {h.key} `{h.factor}` | {fmt_pct(b['coverage'])} | {b['computable_weeks']} | "
            f"{fmt_num(b['median_names'])} | {fmt_num(b['median_groups'])} | {reg} |"
        )
    lines += ["", "| 年 | 主宇宙股週 | 週數 |", "|---|---|---|"]
    lines += [f"| {r['year']} | {r['stock_weeks']:,} | {r['weeks']} |" for r in u["by_year"]]
    return lines
