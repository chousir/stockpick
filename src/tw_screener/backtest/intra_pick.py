"""backtest/intra_pick.py — M-Pick2 族群內個股因子錦標賽（純函式；docs/32 預註冊）。

問題：shortlist（M-Pick1）同次產業上限 1 檔，族群內「挑哪一檔」目前只靠偏好帶距離＋成交額
（非訊號）。本模組在重建的 shortlist 可入選池（趨勢分前 2 桶 × gate）內，對四個預先登記的
個股因子量三把尺——M1 族群內 IC（組內置中排名、跨組合算的週序列）、M2 首選超額（vs 組平均）、
M3 首選 vs 現行規則——再依 docs/32 §4 寫死的五條件＋Bonferroni 層裁決。

口徑（docs/32 §2–§3）：
- 交易日位移以全市場交易日曆計數；窗內缺日或價格不連續（相鄰收盤 |報酬| > disc_pct）→ 因子 null。
- EPS／月營收 point-in-time：期限次日起才可用；取「當日應可得的最新一期」，該期缺 → null
  （不回退更舊的期，免得把缺資料偷換成舊資料）。
- 門檻值一律沿用生產設定（ShortlistConfig、rotation.min_members、price_discontinuity_pct），
  由 IntraPickConfig.from_settings 組裝；IO（載入快取、落檔）由 intra_pick_runner 負責。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import polars as pl

from tw_screener.backtest.factor_lab import (
    REGIME_MIN_N,
    moving_block_bootstrap_ci,
    regime_alignment_verdict,
    regime_mean_slices,
)
from tw_screener.report.shortlist import ShortlistConfig, _band_dist, sector_trend_table

#: 預註冊四因子（docs/32 §3；方向皆＋）。順序＝報告列序。
FACTORS: tuple[str, ...] = ("mom_6_1", "high52_near", "eps_accel", "rev_accel")
#: R4 揭露：現行決勝規則的兩個鍵（取「越大越優先」方向，便於與因子同號比較）。
BASELINE_KEYS: tuple[str, ...] = ("neg_band_dist", "log_amount")

VERDICT_PASS = "成立"
VERDICT_MARGINAL = "候補（多重比較邊緣）"
VERDICT_FAIL = "未過關"
VERDICT_NONE = "無證據"
VERDICT_REVERSE = "反向顯著"
VERDICT_COVERAGE = "不可判（覆蓋不足）"
VERDICT_THIN = "不可判（週數不足）"
REGIME_ROBUST = "跨 regime 穩健"   # factor_lab.regime_alignment_verdict 的通過字串


@dataclass(frozen=True)
class IntraPickConfig:
    """settings `backtest.intra_pick` ＋ 沿用的生產門檻（gate 值不在此重複定義）。"""

    horizon: int = 20
    disclose_horizons: tuple[int, ...] = (10, 40)
    history_days: int = 1400
    calendar_min_names: int = 300
    disc_pct: float = 15.0                       # group_analysis.price_discontinuity_pct
    gate_disc_lookback: int = 10
    mom_skip: int = 21
    mom_lookback: int = 126
    high_window: int = 250
    # 季別 → (年位移, 月, 日)：保守期限次日起可用（docs/32 §3 F3）
    eps_available_from: Mapping[int, tuple[int, int, int]] = field(
        default_factory=lambda: {1: (0, 5, 31), 2: (0, 9, 1), 3: (0, 11, 30), 4: (1, 4, 1)}
    )
    revenue_available_day: int = 11
    min_sector_members: int = 4                  # rotation.min_members
    min_group: int = 2
    min_names_week: int = 10
    min_coverage: float = 0.70
    min_weeks: int = 100
    min_effect_ic: float = 0.03
    n_segments: int = 5
    min_same_segments: int = 4
    n_tests: int = 4                             # Bonferroni k（預註冊主檢定數）
    alpha: float = 0.05
    n_boot: int = 1000
    seed: int = 42
    shortlist: ShortlistConfig = field(default_factory=ShortlistConfig)

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> IntraPickConfig:
        """整份 settings dict → 設定（backtest.intra_pick 為主；gate 值讀生產出處）。"""
        d = cls()
        ip = (cfg.get("backtest") or {}).get("intra_pick") or {}
        avail_raw = ip.get("eps_available_from") or {}
        avail = dict(d.eps_available_from)
        for q in (1, 2, 3, 4):
            v = avail_raw.get(f"q{q}")
            if v is not None:
                avail[q] = (int(v[0]), int(v[1]), int(v[2]))
        return cls(
            horizon=int(ip.get("horizon_td", d.horizon)),
            disclose_horizons=tuple(
                int(h) for h in ip.get("disclose_horizons_td", d.disclose_horizons)
            ),
            history_days=int(ip.get("history_days", d.history_days)),
            calendar_min_names=int(ip.get("calendar_min_names", d.calendar_min_names)),
            disc_pct=float(
                (cfg.get("group_analysis") or {}).get("price_discontinuity_pct", d.disc_pct)
            ),
            gate_disc_lookback=int(ip.get("gate_disc_lookback_td", d.gate_disc_lookback)),
            mom_skip=int(ip.get("mom_skip_td", d.mom_skip)),
            mom_lookback=int(ip.get("mom_lookback_td", d.mom_lookback)),
            high_window=int(ip.get("high_window_td", d.high_window)),
            eps_available_from=avail,
            revenue_available_day=int(ip.get("revenue_available_day", d.revenue_available_day)),
            min_sector_members=int(
                (cfg.get("rotation") or {}).get("min_members", d.min_sector_members)
            ),
            min_group=int(ip.get("min_group", d.min_group)),
            min_names_week=int(ip.get("min_names_week", d.min_names_week)),
            min_coverage=float(ip.get("min_coverage", d.min_coverage)),
            min_weeks=int(ip.get("min_weeks", d.min_weeks)),
            min_effect_ic=float(ip.get("min_effect_ic", d.min_effect_ic)),
            n_segments=int(ip.get("n_segments", d.n_segments)),
            min_same_segments=int(ip.get("min_same_segments", d.min_same_segments)),
            n_tests=int(ip.get("n_tests", d.n_tests)),
            alpha=float(ip.get("alpha", d.alpha)),
            n_boot=int(ip.get("n_boot", d.n_boot)),
            seed=int(ip.get("seed", d.seed)),
            shortlist=ShortlistConfig.from_settings(cfg),
        )

    def block_len(self, horizon: int | None = None) -> int:
        """週頻序列 bootstrap 塊長＝ceil(h/5)+1（同 regime_slice.block_len_for_horizon weekly）。"""
        h = self.horizon if horizon is None else horizon
        return math.ceil(max(1, h) / 5) + 1


# ─── 交易日曆與價格衍生欄 ─────────────────────────────────────────────────────


def trading_calendar(price: pl.DataFrame, min_names: int) -> list[date]:
    """全市場交易日曆：當日有價檔數 ≥ min_names 的日期（剔除只有零星快取的日子）。"""
    if price.is_empty():
        return []
    counts = price.group_by("date").agg(pl.col("stock_id").n_unique().alias("_n"))
    return sorted(counts.filter(pl.col("_n") >= min_names)["date"].to_list())


def price_features(
    price: pl.DataFrame, calendar: Sequence[date], cfg: IntraPickConfig
) -> pl.DataFrame:
    """(date, stock_id) → amount_million／gate_disc／mom_6_1／high52_near／fwd_disc_{h}。

    - 只留日曆內的日子；`_disc`＝與前一筆收盤 |報酬| > disc_pct（首筆 False）。
    - 「無缺日」＝往回 k 筆的日曆索引差恰為 k（停牌缺日 → 因子 null，不補值）。
    - mom_6_1＝close[t−skip]/close[t−lookback]−1，要求 (t−lookback, t−skip] 無不連續。
    - high52_near＝close[t]/max(close[t−w+1..t])，要求窗內無缺日、無不連續。
    - gate_disc＝近 gate_disc_lookback 筆（含當日）有不連續（生產安全網同窗）。
    - fwd_disc_{h}＝entry(t+1) 之後至 exit(t+1+h) 有不連續（該窗報酬失真 → target 作廢）。
    """
    horizons = sorted({cfg.horizon, *cfg.disclose_horizons})
    out_schema: dict[str, Any] = {
        "date": pl.Date, "stock_id": pl.Utf8, "close": pl.Float64,
        "amount_million": pl.Float64, "gate_disc": pl.Boolean,
        "mom_6_1": pl.Float64, "high52_near": pl.Float64,
        **{f"fwd_disc_{h}": pl.Boolean for h in horizons},
    }
    if price.is_empty() or not calendar:
        return pl.DataFrame(schema=out_schema)
    cal = pl.DataFrame({"date": list(calendar)}).with_row_index("cidx")
    px = (
        price.filter(pl.col("close") > 0)
        .join(cal, on="date", how="inner")
        .with_columns(pl.col("cidx").cast(pl.Int64))
        .sort("stock_id", "date")
    )
    prev = pl.col("close").shift(1).over("stock_id")
    px = px.with_columns(
        ((pl.col("close") / prev - 1).abs() * 100 > cfg.disc_pct)
        .fill_null(False)
        .cast(pl.Int64)
        .alias("_disc")
    ).with_columns(pl.col("_disc").cum_sum().over("stock_id").alias("_cum"))

    def lag(col: str, k: int) -> pl.Expr:
        return pl.col(col).shift(k).over("stock_id")

    def lead(col: str, k: int) -> pl.Expr:
        return pl.col(col).shift(-k).over("stock_id")

    def contiguous(k: int) -> pl.Expr:
        return (pl.col("cidx") - lag("cidx", k)) == k

    w = cfg.high_window
    mom = (
        pl.when(
            contiguous(cfg.mom_lookback)
            & ((lag("_cum", cfg.mom_skip) - lag("_cum", cfg.mom_lookback)) == 0)
        )
        .then(lag("close", cfg.mom_skip) / lag("close", cfg.mom_lookback) - 1)
        .otherwise(None)
    )
    high = (
        pl.when(contiguous(w - 1) & ((pl.col("_cum") - lag("_cum", w - 1)) == 0))
        .then(pl.col("close") / pl.col("close").rolling_max(w).over("stock_id"))
        .otherwise(None)
    )
    gate = (pl.col("_cum") - lag("_cum", cfg.gate_disc_lookback).fill_null(0)) > 0
    fwd = [
        ((lead("_cum", h + 1) - lead("_cum", 1)) > 0).fill_null(False).alias(f"fwd_disc_{h}")
        for h in horizons
    ]
    return px.select(
        "date",
        "stock_id",
        "close",
        (pl.col("close") * pl.col("volume") / 1e6).alias("amount_million"),
        gate.alias("gate_disc"),
        mom.alias("mom_6_1"),
        high.alias("high52_near"),
        *fwd,
    )


# ─── 基本面因子（point-in-time）─────────────────────────────────────────────


def quarter_end_close(price_features_df: pl.DataFrame) -> pl.DataFrame:
    """(stock_id, year, quarter, p_q)：每檔每日曆季最後一筆收盤（季末停牌則取季內最後一筆）。"""
    if price_features_df.is_empty():
        return pl.DataFrame(
            schema={"stock_id": pl.Utf8, "year": pl.Int64, "quarter": pl.Int64, "p_q": pl.Float64}
        )
    return (
        price_features_df.sort("stock_id", "date")
        .with_columns(
            pl.col("date").dt.year().cast(pl.Int64).alias("year"),
            ((pl.col("date").dt.month().cast(pl.Int64) - 1) // 3 + 1).alias("quarter"),
        )
        .group_by("stock_id", "year", "quarter")
        .agg(pl.col("close").sort_by("date").last().alias("p_q"))
    )


def eps_accel_table(financials: pl.DataFrame, qclose: pl.DataFrame) -> pl.DataFrame:
    """(stock_id, qi, eps_accel)：[(E_q−E_{q−4}) − (E_{q−1}−E_{q−5})] / P_q。

    qi＝year·4+quarter−1。

    E＝單季 EPS（FinMind financials，已 de-cumulate）；四期任一缺或 P_q ≤ 0 → null。
    保留 null 列——as-of 取「應可得的最新季」時，該季缺值要回 null、不得回退舊季。
    """
    schema = {"stock_id": pl.Utf8, "qi": pl.Int64, "eps_accel": pl.Float64}
    if financials.is_empty():
        return pl.DataFrame(schema=schema)
    base = (
        financials.select(
            pl.col("stock_id").cast(pl.Utf8),
            (pl.col("year").cast(pl.Int64) * 4 + pl.col("quarter").cast(pl.Int64) - 1).alias("qi"),
            pl.col("eps").cast(pl.Float64),
        )
        .unique(subset=["stock_id", "qi"], keep="last")
    )
    out = base
    for k in (1, 4, 5):
        out = out.join(
            base.select(
                "stock_id", (pl.col("qi") + k).alias("qi"), pl.col("eps").alias(f"_e{k}")
            ),
            on=["stock_id", "qi"],
            how="left",
        )
    q = qclose.select(
        pl.col("stock_id").cast(pl.Utf8),
        (pl.col("year").cast(pl.Int64) * 4 + pl.col("quarter").cast(pl.Int64) - 1).alias("qi"),
        "p_q",
    )
    accel = (pl.col("eps") - pl.col("_e4")) - (pl.col("_e1") - pl.col("_e5"))
    return (
        out.join(q, on=["stock_id", "qi"], how="left")
        .select(
            "stock_id",
            "qi",
            pl.when(pl.col("p_q") > 0).then(accel / pl.col("p_q")).otherwise(None)
            .alias("eps_accel"),
        )
    )


def latest_available_quarter(d: date, avail: Mapping[int, tuple[int, int, int]]) -> int:
    """d 當日「已過保守期限」的最新季 qi（year·4+quarter−1）；avail＝季別→(年位移, 月, 日)。"""
    for year in (d.year, d.year - 1, d.year - 2):
        for quarter in (4, 3, 2, 1):
            off, m, day = avail[quarter]
            if date(year + off, m, day) <= d:
                return year * 4 + quarter - 1
    raise ValueError(f"{d} 前兩年內找不到可得季別——avail 設定異常")


def eps_asof(keys: pl.DataFrame, accel: pl.DataFrame, cfg: IntraPickConfig) -> pl.DataFrame:
    """keys(date, stock_id) → (date, stock_id, eps_accel)：取當日應可得的最新季（缺 → null）。"""
    dates = keys["date"].unique().to_list()
    qmap = pl.DataFrame(
        {
            "date": dates,
            "qi": [latest_available_quarter(d, cfg.eps_available_from) for d in dates],
        },
        schema={"date": pl.Date, "qi": pl.Int64},
    )
    return (
        keys.select("date", "stock_id")
        .join(qmap, on="date", how="left")
        .join(accel, on=["stock_id", "qi"], how="left")
        .select("date", "stock_id", "eps_accel")
    )


def revenue_accel_table(revenue: pl.DataFrame) -> pl.DataFrame:
    """(stock_id, mi, rev_accel)：YoY3(m) − YoY3(m−3)；YoY3＝近 3 月營收和／前一年同 3 月 − 1。

    mi＝year·12+month−1；需 m−17…m 連續 18 個月（join 精確月份，缺月 → null）；
    前一年分母 ≤ 0 → null。保留 null 列（理由同 eps_accel_table）。
    """
    schema = {"stock_id": pl.Utf8, "mi": pl.Int64, "rev_accel": pl.Float64}
    if revenue.is_empty():
        return pl.DataFrame(schema=schema)
    base = (
        revenue.select(
            pl.col("stock_id").cast(pl.Utf8),
            (pl.col("year").cast(pl.Int64) * 12 + pl.col("month").cast(pl.Int64) - 1).alias("mi"),
            pl.col("revenue").cast(pl.Float64),
        )
        .unique(subset=["stock_id", "mi"], keep="last")
    )

    def shifted(df: pl.DataFrame, col: str, k: int, name: str) -> pl.DataFrame:
        return df.select("stock_id", (pl.col("mi") + k).alias("mi"), pl.col(col).alias(name))

    r3 = (
        base.join(shifted(base, "revenue", 1, "_r1"), on=["stock_id", "mi"], how="left")
        .join(shifted(base, "revenue", 2, "_r2"), on=["stock_id", "mi"], how="left")
        .select("stock_id", "mi", (pl.col("revenue") + pl.col("_r1") + pl.col("_r2")).alias("r3"))
    )
    yoy = r3.join(shifted(r3, "r3", 12, "_r3_ly"), on=["stock_id", "mi"], how="left").select(
        "stock_id",
        "mi",
        pl.when(pl.col("_r3_ly") > 0).then(pl.col("r3") / pl.col("_r3_ly") - 1).otherwise(None)
        .alias("yoy3"),
    )
    prev = shifted(yoy, "yoy3", 3, "_yoy3_prev")
    return yoy.join(prev, on=["stock_id", "mi"], how="left").select(
        "stock_id", "mi", (pl.col("yoy3") - pl.col("_yoy3_prev")).alias("rev_accel")
    )


def revenue_asof(keys: pl.DataFrame, accel: pl.DataFrame, avail_day: int) -> pl.DataFrame:
    """keys(date, stock_id) → (date, stock_id, rev_accel)：m*＝已過公告期限（次月 avail_day 日起）
    之最新營收月——t 日 ≥ avail_day → 上個月，否則上上個月（缺 → null）。"""
    year, month = pl.col("date").dt.year().cast(pl.Int64), pl.col("date").dt.month().cast(pl.Int64)
    mi_t = year * 12 + month - 1
    return (
        keys.select("date", "stock_id")
        .with_columns(
            pl.when(pl.col("date").dt.day() >= avail_day)
            .then(mi_t - 1)
            .otherwise(mi_t - 2)
            .alias("mi")
        )
        .join(accel, on=["stock_id", "mi"], how="left")
        .select("date", "stock_id", "rev_accel")
    )


# ─── 族群強度與宇宙 ───────────────────────────────────────────────────────────


def primary_sector(members_ordered: pl.DataFrame) -> pl.DataFrame:
    """(stock_id, sub_industry)：列序即優先序、每檔取第一個（同 shortlist.attach_sector_trend）。"""
    return (
        members_ordered.select(
            pl.col("stock_id").cast(pl.Utf8), pl.col("sub_industry").cast(pl.Utf8)
        )
        .drop_nulls()
        .unique(subset=["stock_id"], keep="first", maintain_order=True)
    )


def sector_bucket_table(
    trend: pl.DataFrame,
    baskets: pl.DataFrame,
    dates: Sequence[date],
    min_members: int,
    trend_buckets: int,
) -> pl.DataFrame:
    """(date, sub_industry, trend_score, trend_rank, trend_n, trend_bucket)：逐快照日重算桶。

    當日 members_priced ≥ min_members（生產 rotation.min_members）且有 trend_score 的次產業才排名；
    排名／切桶直接呼叫 shortlist.sector_trend_table（生產同一函式，口徑零漂移）。
    """
    schema = {
        "date": pl.Date, "sub_industry": pl.Utf8, "trend_score": pl.Float64,
        "trend_rank": pl.Int64, "trend_n": pl.Int64, "trend_bucket": pl.Int64,
    }
    if trend.is_empty() or not dates:
        return pl.DataFrame(schema=schema)
    rows = (
        trend.filter(pl.col("date").is_in(list(dates)))
        .join(
            baskets.select("sub_industry", "date", "members_priced"),
            on=["sub_industry", "date"],
            how="left",
        )
        .filter(pl.col("members_priced") >= min_members)
    )
    frames: list[pl.DataFrame] = []
    for (d,), g in rows.group_by(["date"], maintain_order=True):
        tbl = sector_trend_table(g.select("sub_industry", "trend_score"), trend_buckets)
        if not tbl.is_empty():
            frames.append(tbl.with_columns(pl.lit(d, dtype=pl.Date).alias("date")))
    if not frames:
        return pl.DataFrame(schema=schema)
    return pl.concat(frames, how="vertical").select(list(schema)).sort("date", "trend_rank")


def mark_universe(df: pl.DataFrame, cfg: IntraPickConfig) -> pl.DataFrame:
    """加 gate 旗標與現行規則欄：`in_all`（任一桶、過 gate）、`in_main`（另限前 max_bucket 桶）、
    `_band_dist`（生產同式）、neg_band_dist／log_amount（R4 揭露鍵），以及各窗作廢後的 target。

    gate（值同生產）：ext_min ≤ ma60_dist ≤ F2 上限、成交額 ≥ low_liquidity_amount、
    近 10 日無不連續。
    target r{h} 在 fwd_disc_{h} 為真時設 null（該窗報酬被減資／分割等事件污染）。
    """
    sl = cfg.shortlist
    ext = pl.col("ma60_dist_pct")
    gate = (
        pl.col("trend_bucket").is_not_null()
        & ext.is_not_null()
        & (ext >= sl.ext_min_pct)
        & (ext <= sl.ext_max_pct)
        & (pl.col("amount_million") >= sl.low_liquidity_amount)
        & ~pl.col("gate_disc").fill_null(True)
    ).fill_null(False)
    horizons = sorted({cfg.horizon, *cfg.disclose_horizons})
    return df.with_columns(
        gate.alias("in_all"),
        (gate & (pl.col("trend_bucket") <= sl.max_bucket_for_top))
        .fill_null(False)
        .alias("in_main"),
        _band_dist(sl).alias("_band_dist"),
        *[
            pl.when(pl.col(f"fwd_disc_{h}").fill_null(False))
            .then(None)
            .otherwise(pl.col(f"r{h}"))
            .alias(f"r{h}")
            for h in horizons
        ],
    ).with_columns(
        (-pl.col("_band_dist")).alias("neg_band_dist"),
        pl.when(pl.col("amount_million") > 0)
        .then(pl.col("amount_million").log())
        .otherwise(None)
        .alias("log_amount"),
    )


# ─── 量尺 ─────────────────────────────────────────────────────────────────────


def _groups(df: pl.DataFrame, factor: str, target: str, min_group: int) -> pl.DataFrame:
    """因子與 target 皆非 null、且同（週, 次產業）≥ min_group 檔的列，附 `_ng`。"""
    return (
        df.drop_nulls([factor, target])
        .filter(pl.col(factor).is_not_nan() & pl.col(target).is_not_nan())
        .with_columns(pl.len().over("date", "sub_industry").alias("_ng"))
        .filter(pl.col("_ng") >= min_group)
    )


def weekly_ic(
    df: pl.DataFrame, factor: str, target: str, min_group: int, min_names: int
) -> pl.DataFrame:
    """M1：每週產業中性 rank IC＝Pearson(組內置中排名_因子, 組內置中排名_target)，跨組合算。

    置中排名 c＝(rank − (n_g+1)/2)/n_g（tie 取平均）→ 每組和恰為 0；合格成員 < min_names 的週不計。
    Returns: date / ic / n_names / n_groups（依日期升冪）。
    """
    schema = {"date": pl.Date, "ic": pl.Float64, "n_names": pl.UInt32, "n_groups": pl.UInt32}
    g = _groups(df, factor, target, min_group)
    if g.is_empty():
        return pl.DataFrame(schema=schema)
    ng = pl.col("_ng").cast(pl.Float64)

    def centered(col: str) -> pl.Expr:
        return (pl.col(col).rank(method="average").over("date", "sub_industry") - (ng + 1) / 2) / ng

    return (
        g.with_columns(centered(factor).alias("_cx"), centered(target).alias("_cy"))
        .group_by("date")
        .agg(
            (
                (pl.col("_cx") * pl.col("_cy")).sum()
                / ((pl.col("_cx") ** 2).sum() * (pl.col("_cy") ** 2).sum()).sqrt()
            ).alias("ic"),
            pl.len().cast(pl.UInt32).alias("n_names"),
            pl.col("sub_industry").n_unique().cast(pl.UInt32).alias("n_groups"),
        )
        .filter((pl.col("n_names") >= min_names) & pl.col("ic").is_not_null())
        .filter(pl.col("ic").is_finite())
        .sort("date")
        .select(list(schema))
    )


def pick_table(df: pl.DataFrame, factor: str, target: str, min_group: int) -> pl.DataFrame:
    """M2/M3 逐組：因子首選（因子大→成交額大→stock_id 小）vs 組平均、vs 現行首選
    （偏好帶距離小→成交額大→stock_id 小；同組同一可比集合）。

    Returns: date / sub_industry / pick / base_pick / m2 / m3。
    """
    schema = {
        "date": pl.Date, "sub_industry": pl.Utf8, "pick": pl.Utf8, "base_pick": pl.Utf8,
        "m2": pl.Float64, "m3": pl.Float64,
    }
    g = _groups(df, factor, target, min_group)
    if g.is_empty():
        return pl.DataFrame(schema=schema)
    keys = ["date", "sub_industry"]
    fpick = (
        g.sort([factor, "amount_million", "stock_id"], descending=[True, True, False],
               nulls_last=True)
        .group_by(keys, maintain_order=True)
        .first()
        .select(*keys, pl.col("stock_id").alias("pick"), pl.col(target).alias("_yf"))
    )
    bpick = (
        g.sort(["_band_dist", "amount_million", "stock_id"], descending=[False, True, False],
               nulls_last=True)
        .group_by(keys, maintain_order=True)
        .first()
        .select(*keys, pl.col("stock_id").alias("base_pick"), pl.col(target).alias("_yb"))
    )
    gmean = g.group_by(keys).agg(pl.col(target).mean().alias("_ym"))
    return (
        fpick.join(bpick, on=keys, how="inner")
        .join(gmean, on=keys, how="inner")
        .select(
            *keys, "pick", "base_pick",
            (pl.col("_yf") - pl.col("_ym")).alias("m2"),
            (pl.col("_yf") - pl.col("_yb")).alias("m3"),
        )
        .sort(keys)
        .select(list(schema))
    )


def weekly_pick(picks: pl.DataFrame) -> pl.DataFrame:
    """逐組 M2/M3 → 週平均（date / m2 / m3 / n_groups / same_pick_rate）。"""
    if picks.is_empty():
        return pl.DataFrame(
            schema={"date": pl.Date, "m2": pl.Float64, "m3": pl.Float64,
                    "n_groups": pl.UInt32, "same_pick_rate": pl.Float64}
        )
    return (
        picks.group_by("date")
        .agg(
            pl.col("m2").mean(),
            pl.col("m3").mean(),
            pl.len().cast(pl.UInt32).alias("n_groups"),
            (pl.col("pick") == pl.col("base_pick")).mean().alias("same_pick_rate"),
        )
        .sort("date")
    )


# ─── 推論與裁決 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeriesStat:
    """週序列摘要：平均＋moving-block bootstrap CI（α 與 Bonferroni α/k 兩層，同一重抽分布）。"""

    n: int
    mean: float | None
    ci: tuple[float | None, float | None]
    ci_bonf: tuple[float | None, float | None]


def summarize(values: Sequence[float], block_len: int, cfg: IntraPickConfig) -> SeriesStat:
    clean = [float(v) for v in values if v is not None and math.isfinite(v)]
    if not clean:
        return SeriesStat(0, None, (None, None), (None, None))
    mean = sum(clean) / len(clean)
    ci = moving_block_bootstrap_ci(
        clean, block_len=block_len, n_boot=cfg.n_boot, seed=cfg.seed, alpha=cfg.alpha
    )
    ci_b = moving_block_bootstrap_ci(
        clean, block_len=block_len, n_boot=cfg.n_boot, seed=cfg.seed,
        alpha=cfg.alpha / max(1, cfg.n_tests),
    )
    return SeriesStat(len(clean), mean, ci, ci_b)


def segment_means(values: Sequence[float], n_segments: int) -> list[float | None]:
    """依時間序切 n 段等長連續子期間（邊界 round(i·T/n)），回各段平均（空段 → None）。"""
    t = len(values)
    out: list[float | None] = []
    for i in range(n_segments):
        lo, hi = round(i * t / n_segments), round((i + 1) * t / n_segments)
        seg = [float(v) for v in values[lo:hi]]
        out.append(sum(seg) / len(seg) if seg else None)
    return out


def regime_check(
    ic_weekly: pl.DataFrame, regimes: pl.DataFrame, block_len: int, full_mean: float | None,
    cfg: IntraPickConfig,
) -> tuple[pl.DataFrame, str]:
    """C4：週 IC 依快照日 regime 切片（factor_lab.regime_mean_slices；週數 < REGIME_MIN_N 標 thin）
    → factor_lab.regime_alignment_verdict（docs/23 §1(c) 同一裁決函式）。"""
    df = ic_weekly.join(regimes, on="date", how="left")
    slices = regime_mean_slices(
        df, target="ic", block_len=block_len, min_n_dates=REGIME_MIN_N,
        n_boot=cfg.n_boot, seed=cfg.seed,
    )
    if full_mean is None:
        return slices, "regime 樣本不足（無可判切片）"
    label, _, _ = regime_alignment_verdict(slices, 1 if full_mean > 0 else -1, value_col="mean")
    return slices, label


@dataclass(frozen=True)
class FactorResult:
    """單一因子在單一宇宙／窗的完整結果（報告與裁決共用）。"""

    factor: str
    horizon: int
    coverage: float | None
    ic: SeriesStat
    segments: list[float | None]
    regime_slices: pl.DataFrame
    regime_label: str
    m2: SeriesStat
    m3: SeriesStat
    same_pick_rate: float | None
    ic_weekly: pl.DataFrame
    verdict: str = ""
    failed: tuple[str, ...] = ()


def coverage(df: pl.DataFrame, factor: str, target: str, universe_col: str) -> float | None:
    """宇宙內（target 非 null 的股週）因子非 null 比例。"""
    base = df.filter(pl.col(universe_col) & pl.col(target).is_not_null())
    if base.is_empty():
        return None
    return float(base[factor].is_not_null().mean() or 0.0)


def classify(r: FactorResult, cfg: IntraPickConfig) -> tuple[str, tuple[str, ...]]:
    """docs/32 §4.3 五選一（＋不可判）；回 (裁決, 未過條件)。規則寫死、方向預註冊為＋。"""
    if r.coverage is None or r.coverage < cfg.min_coverage:
        return VERDICT_COVERAGE, ()
    if r.ic.n < cfg.min_weeks or r.ic.mean is None:
        return VERDICT_THIN, ()
    lo, hi = r.ic.ci
    failed: list[str] = []
    if r.ic.mean < cfg.min_effect_ic:
        failed.append("C1")
    if lo is None or lo <= 0:
        failed.append("C2")
    if sum(1 for s in r.segments if s is not None and s > 0) < cfg.min_same_segments:
        failed.append("C3")
    if r.regime_label != REGIME_ROBUST or r.ic.mean <= 0:
        failed.append("C4")
    if not (r.m2.mean is not None and r.m2.mean > 0 and r.m3.mean is not None and r.m3.mean > 0):
        failed.append("C5")
    if not failed:
        blo = r.ic.ci_bonf[0]
        return (VERDICT_PASS if blo is not None and blo > 0 else VERDICT_MARGINAL), ()
    if r.ic.mean <= -cfg.min_effect_ic and hi is not None and hi < 0:
        return VERDICT_REVERSE, tuple(failed)
    if r.ic.mean > 0:
        return VERDICT_FAIL, tuple(failed)
    return VERDICT_NONE, tuple(failed)


def evaluate_factor(
    df: pl.DataFrame,
    factor: str,
    cfg: IntraPickConfig,
    universe_col: str = "in_main",
    horizon: int | None = None,
    regimes: pl.DataFrame | None = None,
) -> FactorResult:
    """單因子全套：coverage → M1 週 IC（＋分段、regime）→ M2/M3 → classify。"""
    h = cfg.horizon if horizon is None else horizon
    target = f"r{h}"
    block = cfg.block_len(h)
    uni = df.filter(pl.col(universe_col))
    ic_w = weekly_ic(uni, factor, target, cfg.min_group, cfg.min_names_week)
    ic_stat = summarize(ic_w["ic"].to_list(), block, cfg)
    regs = regimes if regimes is not None else pl.DataFrame(
        schema={"date": pl.Date, "regime": pl.Utf8}
    )
    slices, label = regime_check(ic_w.select("date", "ic"), regs, block, ic_stat.mean, cfg)
    wp = weekly_pick(pick_table(uni, factor, target, cfg.min_group))
    same = wp["same_pick_rate"].mean() if not wp.is_empty() else None
    res = FactorResult(
        factor=factor,
        horizon=h,
        coverage=coverage(df, factor, target, universe_col),
        ic=ic_stat,
        segments=segment_means(ic_w["ic"].to_list(), cfg.n_segments),
        regime_slices=slices,
        regime_label=label,
        m2=summarize(wp["m2"].to_list(), block, cfg),
        m3=summarize(wp["m3"].to_list(), block, cfg),
        same_pick_rate=float(same) if isinstance(same, (int, float)) else None,
        ic_weekly=ic_w,
    )
    verdict, failed = classify(res, cfg)
    return replace(res, verdict=verdict, failed=failed)


def centered_rank_corr(df: pl.DataFrame, cols: Sequence[str], min_group: int) -> pl.DataFrame:
    """R5：宇宙內各欄組內置中排名的 Pearson 相關矩陣（兩兩只用皆非 null、組 ≥ min_group 的列）。"""
    rows: list[dict[str, Any]] = []
    for a in cols:
        row: dict[str, Any] = {"factor": a}
        for b in cols:
            if a == b:
                row[b] = 1.0
                continue
            g = _groups(df, a, b, min_group)
            if g.is_empty():
                row[b] = None
                continue
            ng = pl.col("_ng").cast(pl.Float64)
            cx = (pl.col(a).rank(method="average").over("date", "sub_industry") - (ng + 1) / 2) / ng
            cy = (pl.col(b).rank(method="average").over("date", "sub_industry") - (ng + 1) / 2) / ng
            v = g.select(
                ((cx * cy).sum() / ((cx**2).sum() * (cy**2).sum()).sqrt()).alias("r")
            )["r"].item()
            row[b] = float(v) if v is not None and math.isfinite(v) else None
        rows.append(row)
    return pl.DataFrame(rows)
