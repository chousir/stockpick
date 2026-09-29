"""backtest/intra_pick_oos.py — M-Pick3a 保留樣本（2015-01～2021-12）週快照純函式（docs/33）。

M-Pick2（docs/32 §6.4）的衍生假設只能在從未被族群內研究看過的期間驗證（docs/33 §0）。本模組只補
M-Pick2 程式沒有的兩塊資料層：
- 除權息還原總報酬 target：FinMind TaiwanStockDividendResult 的 before/after 比值，現金與股票股利
  一併還原（docs/33 §2A）；entry／exit 約定同 panel.build_price_panel。
- 保留樣本快照日：全日曆先算 ISO 週末日、再依快照日裁切（邊界不造半週）。
價格因子、趨勢分、gate、主次產業一律重用 intra_pick／rotation_efficacy／shortlist 純函式
（口徑零漂移）；IO 與報表在 intra_pick_oos_runner。**本模組與 runner 都不算任何因子×target
統計**——那是 M-Pick3b 預註冊 commit 之後的事（docs/33 §5 執行順序 3）。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from tw_screener.backtest.rotation_efficacy import weekly_snapshot_dates


@dataclass(frozen=True)
class HoldoutConfig:
    """settings `backtest.intra_pick_oos` ＋ 沿用的核價門檻（backtest.panel，docs/22 同一組）。"""

    start: date = date(2015, 1, 1)
    end: date = date(2021, 12, 31)
    reconcile_years: tuple[int, int] = (2014, 2021)
    reconcile_dates_per_year: int = 3
    reconcile_seed: int = 20260928
    reconcile_tol_pct: float = 0.5
    reconcile_pass_rate: float = 0.995
    reconcile_cache_dir: str = "data/cache/twse_mi_index_check"
    adj_before_price_tol_pct: float = 0.5
    adj_before_price_min_rate: float = 0.98
    adj_event_match_min_rate: float = 0.95
    adj_cash_amount_min_rate: float = 0.99
    adj_window_diff_p99_max_pp: float = 1.0
    output_dir: str = "research/intra_pick_oos"

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> HoldoutConfig:
        """整份 settings dict → 設定（核價容差／通過線讀 backtest.panel，不在此重複定義）。"""
        d = cls()
        bt = cfg.get("backtest") or {}
        oos = bt.get("intra_pick_oos") or {}
        pn = bt.get("panel") or {}
        years = oos.get("reconcile_years") or list(d.reconcile_years)
        return cls(
            start=date.fromisoformat(str(oos.get("holdout_start", d.start))),
            end=date.fromisoformat(str(oos.get("holdout_end", d.end))),
            reconcile_years=(int(years[0]), int(years[1])),
            reconcile_dates_per_year=int(
                oos.get("reconcile_dates_per_year", d.reconcile_dates_per_year)
            ),
            reconcile_seed=int(oos.get("reconcile_seed", d.reconcile_seed)),
            reconcile_tol_pct=float(pn.get("reconcile_tol_pct", d.reconcile_tol_pct)),
            reconcile_pass_rate=float(pn.get("reconcile_pass_rate", d.reconcile_pass_rate)),
            reconcile_cache_dir=str(oos.get("reconcile_cache_dir", d.reconcile_cache_dir)),
            adj_before_price_tol_pct=float(
                oos.get("adj_before_price_tol_pct", d.adj_before_price_tol_pct)
            ),
            adj_before_price_min_rate=float(
                oos.get("adj_before_price_min_rate", d.adj_before_price_min_rate)
            ),
            adj_event_match_min_rate=float(
                oos.get("adj_event_match_min_rate", d.adj_event_match_min_rate)
            ),
            adj_cash_amount_min_rate=float(
                oos.get("adj_cash_amount_min_rate", d.adj_cash_amount_min_rate)
            ),
            adj_window_diff_p99_max_pp=float(
                oos.get("adj_window_diff_p99_max_pp", d.adj_window_diff_p99_max_pp)
            ),
            output_dir=str(oos.get("output_dir", d.output_dir)),
        )


def adjustment_events(dividends: pl.DataFrame) -> pl.DataFrame:
    """除權息事件 → (stock_id, ex_date, adj_factor)；adj_factor＝before_price／after_price。

    比值同時還原現金股利（after＝before−D）與股票股利（after＝before／(1+s)），等同「還原權值」的
    再投入假設。before 或 after 缺（parser 已把 ≤0 轉 null）→ adj_factor null＝無效事件——
    target 窗含它就作廢，不假裝事件沒發生。
    """
    schema = {"stock_id": pl.Utf8, "ex_date": pl.Date, "adj_factor": pl.Float64}
    if dividends.is_empty():
        return pl.DataFrame(schema=schema)
    before, after = pl.col("before_price"), pl.col("after_price")
    return (
        dividends.select(
            pl.col("stock_id").cast(pl.Utf8),
            pl.col("ex_date"),
            pl.when((before > 0) & (after > 0)).then(before / after).otherwise(None)
            .alias("adj_factor"),
        )
        .unique(subset=["stock_id", "ex_date"], keep="last")
        .sort("stock_id", "ex_date")
    )


def total_return_targets(
    price: pl.DataFrame,
    events: pl.DataFrame,
    horizons: Sequence[int],
    covered_ids: Collection[str] | None = None,
) -> pl.DataFrame:
    """(date, stock_id) → r{h}（除權息還原總報酬 %）＋ bad_evt_{h}（窗內有無效事件）。

    r{h}＝close[e+h]／close[e] × Π adj_factor（落在 (e, e+h] 的列）− 1，×100；e＝t 的次一交易列
    （entry＝次一交易日收盤、exit＝entry 後第 h 個交易列，同 panel.build_price_panel）。
    - 事件對齊到該股第一個 ≥ ex_date 的交易列（停牌跨日自動後移，同 panel._cumulative_dividends）；
      落在最後交易列之後的事件不進任何窗。
    - 窗內有無效事件 → r{h} null、bad_evt_{h}＝True；未到期／下市（列不足）→ 兩者皆 null。
    - covered_ids 給定時，不在其中的股票（除權息沒抓過）r{h} 全 null——「沒資料」不等於「沒配息」。
    """
    hs = sorted({int(h) for h in horizons})
    schema: dict[str, Any] = {
        "date": pl.Date, "stock_id": pl.Utf8,
        **{f"r{h}": pl.Float64 for h in hs},
        **{f"bad_evt_{h}": pl.Boolean for h in hs},
    }
    if price.is_empty():
        return pl.DataFrame(schema=schema)
    px = (
        price.select(
            pl.col("date"), pl.col("stock_id").cast(pl.Utf8), pl.col("close").cast(pl.Float64)
        )
        .filter(pl.col("close") > 0)
        .unique(subset=["stock_id", "date"], keep="first")
        .sort("stock_id", "date")
    )
    fac = pl.DataFrame(schema={"stock_id": pl.Utf8, "date": pl.Date, "_f": pl.Float64})
    bad = pl.DataFrame(schema={"stock_id": pl.Utf8, "date": pl.Date, "_bad": pl.UInt32})
    if not events.is_empty():
        aligned = (
            events.select(pl.col("stock_id").cast(pl.Utf8), "ex_date", "adj_factor")
            .sort("ex_date")
            .join_asof(
                px.select("stock_id", "date").sort("date"),
                left_on="ex_date",
                right_on="date",
                by="stock_id",
                strategy="forward",
                check_sortedness=False,  # 兩側已明確 sort（by 群組下 polars 無法自檢）
            )
            .drop_nulls("date")
        )
        fac = (
            aligned.filter(pl.col("adj_factor").is_not_null())
            .group_by("stock_id", "date")
            .agg(pl.col("adj_factor").product().alias("_f"))
        )
        bad = (
            aligned.filter(pl.col("adj_factor").is_null())
            .group_by("stock_id", "date")
            .agg(pl.len().cast(pl.UInt32).alias("_bad"))
        )
    px = (
        px.join(fac, on=["stock_id", "date"], how="left")
        .join(bad, on=["stock_id", "date"], how="left")
        .with_columns(
            pl.col("_f").fill_null(1.0).cum_prod().over("stock_id").alias("_F"),
            pl.col("_bad").fill_null(0).cast(pl.Int64).cum_sum().over("stock_id").alias("_B"),
        )
    )

    def lead(col: str, k: int) -> pl.Expr:
        return pl.col(col).shift(-k).over("stock_id")

    exprs: list[pl.Expr] = []
    for h in hs:
        n_bad = lead("_B", 1 + h) - lead("_B", 1)
        growth = lead("close", 1 + h) / lead("close", 1)
        ret = (growth * (lead("_F", 1 + h) / lead("_F", 1)) - 1) * 100
        exprs += [
            pl.when(n_bad == 0).then(ret).otherwise(None).alias(f"r{h}"),
            (n_bad > 0).alias(f"bad_evt_{h}"),
        ]
    out = px.with_columns(exprs)
    if covered_ids is not None:
        cov = sorted({str(s) for s in covered_ids})
        out = out.with_columns(
            pl.when(pl.col("stock_id").is_in(cov)).then(pl.col(f"r{h}")).otherwise(None)
            .alias(f"r{h}")
            for h in hs
        )
    return out.select(list(schema))


def _with_prev_close(events: pl.DataFrame, price: pl.DataFrame) -> pl.DataFrame:
    """events(stock_id, ex_date, …) → 加 `date`（對齊的交易列）與 `_prev_close`（其前一列收盤）。

    對齊規則同 total_return_targets：事件日對到該股第一個 ≥ ex_date 的交易列（停牌順延）；
    事件前沒有價格列（或事件在最後一列之後）→ date／_prev_close 為 null。
    """
    px = (
        price.select(pl.col("date"), pl.col("stock_id").cast(pl.Utf8), pl.col("close"))
        .filter(pl.col("close") > 0)
        .unique(subset=["stock_id", "date"], keep="first")
        .sort("stock_id", "date")
        .with_columns(pl.col("close").shift(1).over("stock_id").alias("_prev_close"))
    )
    aligned = events.with_columns(pl.col("stock_id").cast(pl.Utf8)).sort("ex_date").join_asof(
        px.select("stock_id", "date").sort("date"),
        left_on="ex_date", right_on="date", by="stock_id", strategy="forward",
        check_sortedness=False,
    )
    return aligned.join(
        px.select("stock_id", "date", "_prev_close"), on=["stock_id", "date"], how="left"
    )


def before_price_consistency(
    dividends: pl.DataFrame, price: pl.DataFrame, tol_pct: float
) -> dict[str, Any]:
    """FinMind 除權息事件的「除息前收盤價」vs 價格資料前一交易列收盤（全期自洽檢查）。

    前一列收盤應等於 before_price（TWSE／TPEX 除權息計算結果表的「除權息前收盤價」）。這條不需要
    任何官方對照期，所以能涵蓋 2015–2021 整個保留樣本；兩個 dataset 各自來自官方表，一致＝事件日期
    與價格列對得上。before_price 缺（無效事件）、或事件前無價格列 → 不檢查（分別計數）。
    """
    j = _with_prev_close(dividends.select("stock_id", "ex_date", "before_price"), price)
    checkable = j.filter(
        pl.col("before_price").is_not_null() & pl.col("_prev_close").is_not_null()
    ).with_columns(
        ((pl.col("before_price") - pl.col("_prev_close")).abs() / pl.col("_prev_close") * 100)
        .alias("diff_pct"),
        (pl.col("before_price") - pl.col("_prev_close")).abs().alias("diff_abs"),
    )
    return {
        "n_events": dividends.height,
        "n_invalid_before": dividends.filter(pl.col("before_price").is_null()).height,
        "n_no_prev_row": j.filter(
            pl.col("before_price").is_not_null() & pl.col("_prev_close").is_null()
        ).height,
        "n_checked": checkable.height,
        "n_ok": checkable.filter(pl.col("diff_pct") <= tol_pct).height,
        "n_exact": checkable.filter(pl.col("diff_abs") <= 0.005).height,
        "n_realigned": j.filter(pl.col("date") != pl.col("ex_date")).height,
        "worst": checkable.sort("diff_pct", descending=True)
        .select("stock_id", "ex_date", "before_price", pl.col("_prev_close").alias("prev_close"),
                "diff_pct")
        .head(8),
    }


def ratio_rounding_error(dividends: pl.DataFrame) -> dict[str, Any]:
    """純現金事件：FinMind 還原比值 before/after vs 精確比值 before/(before−D) 的相對誤差。

    err_pp＝((before−D)/after − 1)×100＝該事件還原因子的誤差（百分點），也是含該事件之窗 target 的
    誤差量級（再乘 1+r，影響可忽略）。after_price 是四捨五入到分的參考價，此函式量它造成的誤差。
    純現金＝event_type 不含「權」；需 D>0、before>D、before／after 皆有值。純描述，不設判準。
    """
    cash = dividends.filter(
        pl.col("event_type").is_not_null()
        & ~pl.col("event_type").fill_null("").str.contains("權")
        & (pl.col("dividend_value") > 0)
        & pl.col("before_price").is_not_null()
        & pl.col("after_price").is_not_null()
        & (pl.col("before_price") > pl.col("dividend_value"))
    ).with_columns(
        (((pl.col("before_price") - pl.col("dividend_value")) / pl.col("after_price") - 1) * 100)
        .alias("err_pp")
    )
    e = cash["err_pp"]
    return {
        "n_checked": cash.height,
        "err_median": e.median() if cash.height else None,
        "err_abs_p99": e.abs().quantile(0.99) if cash.height else None,
        "err_abs_max": e.abs().max() if cash.height else None,
        "worst": cash.sort(pl.col("err_pp").abs(), descending=True)
        .select("stock_id", "ex_date", "before_price", "after_price", "dividend_value", "err_pp")
        .head(5),
    }


def official_ratio_events(official: pl.DataFrame, price: pl.DataFrame) -> pl.DataFrame:
    """TWSE 預告表純現金事件 → (stock_id, ex_date, adj_factor)；adj_factor＝前收／(前收−現金股利)。

    前收＝價格資料事件日對齊列的前一列收盤（不用 FinMind 的 before/after）；用來造「比值版」的官方
    參考 r{h}，與 FinMind 的比值還原同一公式——差異只剩兩邊對 D 與前收的認定，沒有線性加回的二階項。
    只取 type＝「息」（純現金）；前收 ≤ 現金股利或無前收 → adj_factor null（無效事件）。
    """
    schema = {"stock_id": pl.Utf8, "ex_date": pl.Date, "adj_factor": pl.Float64}
    ev = official.filter((pl.col("type") == "息") & (pl.col("cash_dividend") > 0)).select(
        "stock_id", "ex_date", "cash_dividend"
    ).unique(subset=["stock_id", "ex_date"], keep="last")
    if ev.is_empty():
        return pl.DataFrame(schema=schema)
    j = _with_prev_close(ev, price)
    return j.select(
        "stock_id", "ex_date",
        pl.when(pl.col("_prev_close") > pl.col("cash_dividend"))
        .then(pl.col("_prev_close") / (pl.col("_prev_close") - pl.col("cash_dividend")))
        .otherwise(None)
        .alias("adj_factor"),
    ).sort("stock_id", "ex_date")


def reconcile_events(
    official: pl.DataFrame,
    finmind: pl.DataFrame,
    start: date,
    end: date,
    stock_ids: Collection[str],
    cash_tol: float = 0.01,
    date_tol_days: int = 3,
) -> dict[str, Any]:
    """FinMind 除權息事件 vs TWSE 除權息預告表（獨立官方來源）的事件層級對帳。

    official＝`load_recent_dividends` 輸出 (ex_date, stock_id, type, cash_dividend, …)；
    finmind＝`load_dividend_result_history` 輸出
    (ex_date, stock_id, event_type, dividend_value, …)。
    只比 [start, end] 內、stock_ids 內的事件。配對鍵＝(stock_id, ex_date)；純現金事件（官方 type＝
    「息」）另比金額（FinMind dividend_value＝權值＋息值，純現金時即現金股利）。官方預告表是滾動快照
    的聯集，涵蓋不保證完整——「僅 FinMind 有」無法裁決是 FinMind 多還是官方快照漏，只如實計數。
    """
    ids = sorted({str(s) for s in stock_ids})
    keys = ["stock_id", "ex_date"]

    def within(df: pl.DataFrame) -> pl.DataFrame:
        return df.filter(pl.col("ex_date").is_between(start, end) & pl.col("stock_id").is_in(ids))

    o = within(official)
    f = within(finmind)
    matched = o.join(f, on=keys, how="inner")
    official_only = o.join(f.select(keys), on=keys, how="anti")
    finmind_only = f.join(o.select(keys), on=keys, how="anti")
    near = (
        official_only.select("stock_id", "ex_date")
        .join(finmind_only.select("stock_id", pl.col("ex_date").alias("_f_date")), on="stock_id")
        .filter((pl.col("_f_date") - pl.col("ex_date")).dt.total_days().abs() <= date_tol_days)
        .select("stock_id", "ex_date")
        .unique()
    )
    cash = matched.filter(pl.col("type") == "息")
    cash_ok = cash.filter((pl.col("dividend_value") - pl.col("cash_dividend")).abs() <= cash_tol)
    cash_bad = cash.join(cash_ok.select(keys), on=keys, how="anti")
    return {
        "n_official": o.height,
        "n_finmind": f.height,
        "n_matched": matched.height,
        "official_only": official_only.sort(keys),
        "finmind_only": finmind_only.sort(keys),
        "n_official_only_near": near.height,
        "n_cash_pairs": cash.height,
        "n_cash_ok": cash_ok.height,
        "cash_bad": cash_bad.select(*keys, "cash_dividend", "dividend_value").sort(keys),
    }


def return_agreement(
    adjusted: pl.DataFrame,
    raw: pl.DataFrame,
    reference: pl.DataFrame,
    h: int,
    stock_div_ids: Collection[str],
    eps: float = 1e-9,
) -> dict[str, Any]:
    """FinMind 比值還原 r{h} vs 獨立參考 r{h}（TWSE 現金股利線性加回）逐窗一致性。

    adjusted／raw＝`total_return_targets` 有／無事件的輸出；reference＝(date, stock_id, r{h})，
    由 `build_price_panel(同一批價格列, dividends=TWSE 預告表)` 產生（TWSE 現金股利線性加回，
    呼叫端須先限定 div_coverage 範圍）。窗依「哪一方看見事件」分四組（事件＝該方 r{h} 與無事件版
    raw 不同）：兩方都沒看見／兩方都看見／僅 FinMind／僅參考。兩方都看見且該股無配股事件者，
    比值還原與線性加回只差二階項 (D/entry)·(exit/after−1)，故看差值分布；僅一方看見＝事件集合
    不一致（官方預告表是滾動快照聯集，會漏事件，故「僅 FinMind」不能單獨當 FinMind 的錯），
    逐窗計數。stock_div_ids（含配股事件的股票）另列——參考不還原配股、必然不同。
    """
    col = f"r{h}"
    j = (
        adjusted.select("date", "stock_id", pl.col(col).alias("adj"))
        .join(raw.select("date", "stock_id", pl.col(col).alias("raw")), on=["date", "stock_id"])
        .join(
            reference.select("date", "stock_id", pl.col(col).alias("ref")),
            on=["date", "stock_id"],
        )
        .drop_nulls(["adj", "raw", "ref"])
        .with_columns(
            ((pl.col("adj") - pl.col("raw")).abs() > eps).alias("fm_evt"),
            ((pl.col("ref") - pl.col("raw")).abs() > eps).alias("ref_evt"),
            pl.col("stock_id").is_in(sorted({str(s) for s in stock_div_ids})).alias("stock_div"),
        )
    )
    both = j.filter(pl.col("fm_evt") & pl.col("ref_evt"))
    both_cash = both.filter(~pl.col("stock_div")).with_columns(
        (pl.col("adj") - pl.col("ref")).alias("diff")
    )
    d = both_cash["diff"]
    return {
        "n_windows": j.height,
        "n_none": j.filter(~pl.col("fm_evt") & ~pl.col("ref_evt")).height,
        "n_both": both.height,
        "n_both_stock_div": both.filter(pl.col("stock_div")).height,
        "n_fm_only": j.filter(pl.col("fm_evt") & ~pl.col("ref_evt")).height,
        "n_ref_only": j.filter(~pl.col("fm_evt") & pl.col("ref_evt")).height,
        "n_cash": both_cash.height,
        "diff_median": d.median() if both_cash.height else None,
        "diff_abs_p99": d.abs().quantile(0.99) if both_cash.height else None,
        "diff_abs_max": d.abs().max() if both_cash.height else None,
        "worst": both_cash.sort(pl.col("diff").abs(), descending=True)
        .select("date", "stock_id", "adj", "ref", "diff")
        .head(8),
    }


def holdout_week_dates(calendar: Sequence[date], start: date, end: date) -> list[date]:
    """保留樣本快照日：完整日曆每 ISO 週最後交易日（weekly_snapshot_dates），再留 start ≤ d ≤ end。

    先算週再裁切——邊界週的快照日＝該週在完整日曆裡的最後交易日，不因裁切變成半週。
    """
    return [d for d in weekly_snapshot_dates(list(calendar)) if start <= d <= end]
