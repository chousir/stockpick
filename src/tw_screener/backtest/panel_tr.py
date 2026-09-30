"""backtest/panel_tr.py — D6 成員稠密總報酬面板（純函式；docs/33 §7）。

研究面板 `research/panel/panel.parquet` 有兩個 target 污染（docs/33 §6.6）：自 2026-06 起列數稀疏
（快取密度，docs/29 ③；r{h} 對多數股票不是 h 個交易日）、2022–2025 現金股利未還原。M-Pick3a 已備妥
FinMind 稠密日線＋除權息（今日次產業成員、2013 起），本模組用同一套函式重建 2022 起的前瞻報酬：
- 不覆蓋原面板：產出獨立的「成員稠密總報酬面板」（僅今日次產業成員；全市場基準、法人、
  融資等欄不在內）。
- 欄位＝M-Pick2 從面板取用的 ma60_dist_pct、r10／r20／r40，換成稠密＋總報酬版；口徑同 M-Pick3a
  （build_price_panel 的位階與 ETF 過濾、oos.total_return_targets 的除權息還原總報酬）。
- 另附三種「新舊面板差異」量尺（列吻合率、窗跨度、target 差），把 docs/33 §6.6 未量化的影響量化。
**本模組不計任何因子×target 統計。**
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_oos as oos
from tw_screener.backtest.panel import build_price_panel


@dataclass(frozen=True)
class PanelTrConfig:
    """settings `backtest.panel_tr`。驗收門檻（span_*、before_price_*）於看到重建結果前寫定。"""

    start: date = date(2022, 1, 3)
    horizons: tuple[int, ...] = (10, 20, 40)
    main_horizon: int = 20
    old_panel_path: str = "research/panel/panel.parquet"
    old_panel_end: date = date(2026, 8, 28)
    mpick2_stockweeks_glob: str = "research/intra_pick/intra_pick_stockweeks_*.parquet"
    calendar_min_names: int = 300
    span_baseline_year: int = 2025
    span_floor_margin_pp: float = 1.0
    span_check_since: date = date(2026, 6, 1)
    # 報告顯示切點（非門檻）：diff_month_from 起逐月列、之前併成逐年；
    # mpick2_recent_from＝M-Pick2 主宇宙 target 差的分界（docs/33 §6.6-1）
    diff_month_from: str = "2025-07"
    mpick2_recent_from: date = date(2026, 5, 1)
    before_price_tol_pct: float = 0.5
    before_price_min_rate: float = 0.98
    spot_checks: tuple[tuple[str, date], ...] = ()
    output_dir: str = "research/panel"
    output_name: str = "panel_tr_members.parquet"

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> PanelTrConfig:
        d = cls()
        pc = (cfg.get("backtest") or {}).get("panel_tr") or {}
        return cls(
            start=date.fromisoformat(str(pc.get("start_date", d.start))),
            horizons=tuple(int(h) for h in pc.get("horizons_td", d.horizons)),
            main_horizon=int(pc.get("main_horizon_td", d.main_horizon)),
            old_panel_path=str(pc.get("old_panel_path", d.old_panel_path)),
            old_panel_end=date.fromisoformat(str(pc.get("old_panel_end", d.old_panel_end))),
            mpick2_stockweeks_glob=str(pc.get("mpick2_stockweeks_glob", d.mpick2_stockweeks_glob)),
            calendar_min_names=int(pc.get("calendar_min_names", d.calendar_min_names)),
            span_baseline_year=int(pc.get("span_baseline_year", d.span_baseline_year)),
            span_floor_margin_pp=float(pc.get("span_floor_margin_pp", d.span_floor_margin_pp)),
            span_check_since=date.fromisoformat(
                str(pc.get("span_check_since", d.span_check_since))
            ),
            diff_month_from=str(pc.get("diff_month_from", d.diff_month_from)),
            mpick2_recent_from=date.fromisoformat(
                str(pc.get("mpick2_recent_from", d.mpick2_recent_from))
            ),
            before_price_tol_pct=float(pc.get("before_price_tol_pct", d.before_price_tol_pct)),
            before_price_min_rate=float(pc.get("before_price_min_rate", d.before_price_min_rate)),
            spot_checks=tuple(
                (str(sid), date.fromisoformat(str(dt))) for sid, dt in pc.get("spot_checks", [])
            ),
            output_dir=str(pc.get("output_dir", d.output_dir)),
            output_name=str(pc.get("output_name", d.output_name)),
        )


def build_member_tr_panel(
    price: pl.DataFrame,
    events: pl.DataFrame,
    horizons: Sequence[int],
    covered_ids: Collection[str],
    start: date,
) -> pl.DataFrame:
    """成員稠密總報酬面板：(date, stock_id) → close、ma60_dist_pct、r{h}、bad_evt_{h}、div_covered。

    price：今日成員且有成交的日線長表（date/stock_id/close/volume；含 start 之前的暖身段，供 ma60 與
    前瞻窗用）。宇宙過濾（4 位數字且非 00 開頭）與位階口徑取自 build_price_panel；r{h}＝除權息還原
    總報酬（entry＝次一交易列收盤、exit＝其後第 h 列；窗內無效除權息事件或除權息沒抓過 → null）。
    輸出只留 date ≥ start，指標已在完整載入窗算完，不受裁切影響。
    """
    hs = sorted({int(h) for h in horizons})
    base = build_price_panel(price, horizons=(hs[0],), ma_windows=(60,)).select(
        "date", "stock_id", "close", "ma60_dist_pct"
    )
    targets = oos.total_return_targets(price, events, hs, covered_ids=covered_ids)
    return (
        base.join(targets, on=["date", "stock_id"], how="left")
        .with_columns(pl.col("stock_id").is_in(sorted(covered_ids)).alias("div_covered"))
        .filter(pl.col("date") >= start)
        .sort("stock_id", "date")
    )


def market_calendar(price: pl.DataFrame, min_names: int) -> pl.DataFrame:
    """市場交易日曆 (date, ci)：當日有價檔數 ≥ min_names 的日期及其序號（升冪從 0）。"""
    days = ip.trading_calendar(price, min_names)
    return pl.DataFrame(
        {"date": days, "ci": list(range(len(days)))}, schema={"date": pl.Date, "ci": pl.Int64}
    )


def window_spans(rows: pl.DataFrame, cal: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """每列的 r{h} 窗實際跨了幾個「市場交易日」：Returns date / stock_id / span。

    entry＝該股次一列、exit＝entry 後第 h 列（與面板 r{h} 同一列序約定），span＝兩端在市場交易日曆的
    序號差。窗內該股沒有漏列 → span＝h；漏列（該股無成交日，或快取稀疏）→ span > h（不會 < h）。
    窗未到期、或任一端日期不在日曆 → null。輸入以 (date, stock_id) 去重。
    """
    r = (
        rows.select("date", pl.col("stock_id").cast(pl.Utf8))
        .unique(subset=["date", "stock_id"], keep="first")
        .sort("stock_id", "date")
        .with_columns(
            pl.col("date").shift(-1).over("stock_id").alias("_entry"),
            pl.col("date").shift(-(1 + horizon)).over("stock_id").alias("_exit"),
        )
        .join(cal.rename({"date": "_entry", "ci": "_ie"}), on="_entry", how="left")
        .join(cal.rename({"date": "_exit", "ci": "_ix"}), on="_exit", how="left")
    )
    return r.select("date", "stock_id", (pl.col("_ix") - pl.col("_ie")).alias("span"))


def span_share_by_month(spans: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """月 × 窗跨度分布。Returns: ym / n_windows / share_eq（恰 h 日）/ share_gt（多於 h 日）。"""
    return (
        spans.filter(pl.col("span").is_not_null())
        .group_by(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
        .agg(
            pl.len().alias("n_windows"),
            (pl.col("span") == horizon).mean().alias("share_eq"),
            (pl.col("span") > horizon).mean().alias("share_gt"),
        )
        .sort("ym")
    )


def span_share_by_old_coverage(
    new_rows: pl.DataFrame, old_rows: pl.DataFrame, cal: pl.DataFrame, horizon: int, before: date
) -> pl.DataFrame:
    """新面板的 r{h} 窗，依「舊面板是否收錄該列」拆開看窗跨度（年 × 組）。

    舊面板的「恰跨 h 日」比例常高於新面板，但舊面板是新面板的列子集：它漏掉的偏是流動性差的列
    （無成交日多），使它看起來更稠密——這是組成效應，不是窗品質。本函式以新面板的列序算窗，
    分三組：have_row＝舊面板有同鍵列；missing_row＝舊面板有收錄該股、缺這一列；absent_stock＝
    舊面板整檔缺席該股。只計 date < before 且窗已到期者。
    Returns: year / grp / n_windows / share_eq（恰 h 日）/ share_rows（該年各組窗數占比）。
    """
    spans = window_spans(new_rows, cal, horizon).filter(
        pl.col("span").is_not_null() & (pl.col("date") < before)
    )
    old_keys = old_rows.select("date", pl.col("stock_id").cast(pl.Utf8)).unique().with_columns(
        pl.lit(True).alias("_in_old")
    )
    old_ids = sorted(old_rows["stock_id"].cast(pl.Utf8).unique().to_list())
    grp = (
        pl.when(pl.col("_in_old").is_not_null())
        .then(pl.lit("have_row"))
        .when(pl.col("stock_id").is_in(old_ids))
        .then(pl.lit("missing_row"))
        .otherwise(pl.lit("absent_stock"))
    )
    return (
        spans.join(old_keys, on=["date", "stock_id"], how="left")
        .group_by(pl.col("date").dt.year().alias("year"), grp.alias("grp"))
        .agg(pl.len().alias("n_windows"), (pl.col("span") == horizon).mean().alias("share_eq"))
        .with_columns(
            (pl.col("n_windows") / pl.col("n_windows").sum().over("year")).alias("share_rows")
        )
        .sort("year", "grp")
    )


def row_match_by_month(new_rows: pl.DataFrame, old_rows: pl.DataFrame) -> pl.DataFrame:
    """新面板每列在舊面板是否有同鍵列。Returns: ym / n_new / n_matched / match_rate。"""
    old_keys = old_rows.select("date", "stock_id").unique().with_columns(pl.lit(True).alias("_hit"))
    return (
        new_rows.select("date", "stock_id")
        .join(old_keys, on=["date", "stock_id"], how="left")
        .group_by(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
        .agg(
            pl.len().alias("n_new"),
            pl.col("_hit").is_not_null().sum().alias("n_matched"),
        )
        .with_columns((pl.col("n_matched") / pl.col("n_new")).alias("match_rate"))
        .sort("ym")
    )


def target_diff_by_period(
    new: pl.DataFrame,
    old: pl.DataFrame,
    horizon: int,
    period: pl.Expr | None = None,
    gt_pp: Sequence[float] = (1.0, 5.0),
) -> pl.DataFrame:
    """同鍵列上新舊 r{h} 的差（新−舊，百分點）依期間分布；期間預設為月（`%Y-%m`）。

    n_both＝兩邊皆非 null；n_new_only＝舊 null／缺列而新非 null（重建補回的窗）；n_old_only＝新 null
    而舊非 null。差只在 n_both 上算：中位、|差| 的 p50／p90／p99／max，以及 |差| > gt_pp 的筆數。
    Returns: period / n_both / n_new_only / n_old_only / diff_median / abs_p50 / abs_p90 /
    abs_p99 / abs_max / n_gt_<g>pp…
    """
    col = f"r{horizon}"
    key = (period if period is not None else pl.col("date").dt.strftime("%Y-%m")).alias("period")
    j = new.select("date", "stock_id", pl.col(col).alias("_new")).join(
        old.select("date", "stock_id", pl.col(col).alias("_old")).unique(
            subset=["date", "stock_id"]
        ),
        on=["date", "stock_id"],
        how="left",
    )
    both = pl.col("_new").is_not_null() & pl.col("_old").is_not_null()
    diff = (pl.col("_new") - pl.col("_old")).filter(both)
    return (
        j.group_by(key)
        .agg(
            both.sum().alias("n_both"),
            (pl.col("_new").is_not_null() & pl.col("_old").is_null()).sum().alias("n_new_only"),
            (pl.col("_new").is_null() & pl.col("_old").is_not_null()).sum().alias("n_old_only"),
            diff.median().alias("diff_median"),
            diff.abs().quantile(0.5).alias("abs_p50"),
            diff.abs().quantile(0.9).alias("abs_p90"),
            diff.abs().quantile(0.99).alias("abs_p99"),
            diff.abs().max().alias("abs_max"),
            *[(diff.abs() > g).sum().alias(f"n_gt_{g:g}pp") for g in gt_pp],
        )
        .sort("period")
    )


def span_floor(share_by_month: pl.DataFrame, baseline_year: int, margin_pp: float) -> float | None:
    """A1 門檻＝基線年各月「窗跨度恰 h」比例的最小值 − margin_pp/100（防退化，非正確性證明）。"""
    base = share_by_month.filter(pl.col("ym").str.starts_with(f"{baseline_year}-"))
    if base.is_empty():
        return None
    return float(base["share_eq"].min()) - margin_pp / 100.0  # type: ignore[arg-type]


def span_check(
    share_by_month: pl.DataFrame, floor: float | None, since: date
) -> tuple[bool | None, list[str]]:
    """A1：`since` 所在月起各月 share_eq 皆 ≥ floor → (True, [])；否則 (False, 未過月份)。
    門檻缺（基線年無資料）或 since 之後無月份 → (None, [])＝無法判定。"""
    if floor is None:
        return None, []
    after = share_by_month.filter(pl.col("ym") >= since.strftime("%Y-%m"))
    if after.is_empty():
        return None, []
    bad = after.filter(pl.col("share_eq") < floor)["ym"].to_list()
    return (not bad), bad


def spot_check(
    price: pl.DataFrame, events: pl.DataFrame, stock_id: str, d: date, horizon: int
) -> dict[str, Any] | None:
    """單一窗的可手算拆解：entry／exit 日與收盤、窗內除權息（對齊列與還原比值）、純價差與總報酬。

    entry＝d 的次一列、exit＝entry 後第 h 列（該股列序）；事件對齊到該股第一個 ≥ ex_date 的列，
    落在 (entry, exit] 者才入窗——與 oos.total_return_targets 同一約定。
    d 不在該股列內或窗未到期 → None。
    """
    rows = (
        price.filter(pl.col("stock_id") == stock_id)
        .select("date", "close")
        .unique(subset=["date"])
        .sort("date")
    )
    dates = rows["date"].to_list()
    if d not in dates:
        return None
    i = dates.index(d)
    if i + 1 + horizon >= len(dates):
        return None
    entry_d, exit_d = dates[i + 1], dates[i + 1 + horizon]
    entry_c, exit_c = float(rows["close"][i + 1]), float(rows["close"][i + 1 + horizon])
    evs: list[dict[str, Any]] = []
    factor = 1.0
    for ev in events.filter(pl.col("stock_id") == stock_id).sort("ex_date").iter_rows(named=True):
        aligned = next((x for x in dates if x >= ev["ex_date"]), None)
        if aligned is None or not (entry_d < aligned <= exit_d):
            continue
        evs.append({"ex_date": ev["ex_date"], "aligned": aligned, "adj_factor": ev["adj_factor"]})
        factor *= ev["adj_factor"] if ev["adj_factor"] is not None else float("nan")
    return {
        "stock_id": stock_id, "date": d, "entry_date": entry_d, "entry_close": entry_c,
        "exit_date": exit_d, "exit_close": exit_c, "events": evs,
        "price_ret_pct": (exit_c / entry_c - 1) * 100,
        "total_ret_pct": (exit_c / entry_c * factor - 1) * 100,
    }
