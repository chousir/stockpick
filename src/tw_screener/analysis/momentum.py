"""動能計算：N 日累計報酬率（從本地 OHLCV 快取算出）。

集中所有 N 日報酬率邏輯，供 grouping.py、leader.py、report 共用。
資料來源優先序由呼叫端決定（已合併好的 price_history DataFrame）；
此模組只做計算、不負責讀檔。
"""

from __future__ import annotations

import polars as pl


def compute_n_day_return(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    n: int = 5,
) -> dict[str, tuple[float, int]]:
    """回傳每檔股票的 N 日累計報酬率（百分比）與實際用到的交易日 gap。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        n: 目標交易日 gap（預設 5 日）。

    Returns:
        {stock_id: (cumulative_return_pct, actual_days_used)}
        - actual_days_used = min(n, 該股可用交易日數 - 1)
        - 若該股可用交易日 < 2 或 close 缺失 → 不出現在 dict 中
    """
    if price_history.is_empty():
        return {}
    required = {"stock_id", "date", "close"}
    if not required.issubset(set(price_history.columns)):
        return {}

    # 向量化：每股一次標出列數(_n)與組內序(_i)，取末列(c_now)與起點列(c_back=index
    # max(0, _n-1-n))，再 join 算報酬。等價於 per-stock 迴圈、但只掃全表一次。
    tagged = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .with_columns(
            pl.len().over("stock_id").cast(pl.Int64).alias("_n"),
            pl.int_range(pl.len()).over("stock_id").alias("_i"),
        )
        .filter(pl.col("_n") >= 2)
    )
    if tagged.is_empty():
        return {}

    back_idx = pl.max_horizontal(pl.col("_n") - 1 - n, pl.lit(0))
    c_now = tagged.filter(pl.col("_i") == pl.col("_n") - 1).select(
        "stock_id", pl.col("close").alias("_c_now"), "_n"
    )
    c_back = tagged.filter(pl.col("_i") == back_idx).select(
        "stock_id", pl.col("close").alias("_c_back")
    )
    merged = (
        c_now.join(c_back, on="stock_id")
        .filter(
            pl.col("_c_now").is_not_null()
            & pl.col("_c_back").is_not_null()
            & (pl.col("_c_back") != 0)
        )
        .with_columns(
            pl.min_horizontal(pl.lit(n), pl.col("_n") - 1).alias("_gap"),
            ((pl.col("_c_now") - pl.col("_c_back")) / pl.col("_c_back") * 100).alias("_ret"),
        )
    )
    return {
        row["stock_id"]: (float(row["_ret"]), int(row["_gap"]))
        for row in merged.iter_rows(named=True)
    }


def compute_rolling_extrema(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    windows: tuple[int, ...] = (20, 60),
) -> dict[str, dict[int, tuple[float, float]]]:
    """回傳每檔股票各視窗「最近 window 個交易日」收盤的 (最低, 最高)。

    供進場階梯的 T3 結構價（前波低）與回檔深度檢核（距區間低/高）使用。
    用原始收盤、不做除息還原——進場/停損是實際成交價，與 close/MA20/MA60 絕對價同口徑
    （近端除息另由 ex_div_cash 旗標揭露）。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        windows: 視窗交易日數集合（預設近 20 與 60 日）。

    Returns:
        {stock_id: {window: (low, high)}}；無可用收盤的股不出現。
        各視窗取最近 window 個交易日的收盤 min/max（不足則用可得全部）。
    """
    if price_history.is_empty():
        return {}
    required = {"stock_id", "date", "close"}
    if not required.issubset(set(price_history.columns)):
        return {}

    # 向量化：丟掉 null 收盤後，每股各視窗取最近 w 列(tail)的 min/max，一次 group_by 算完。
    agg_exprs = []
    for w in windows:
        agg_exprs.append(pl.col("close").tail(w).min().alias(f"_min_{w}"))
        agg_exprs.append(pl.col("close").tail(w).max().alias(f"_max_{w}"))
    grouped = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .filter(pl.col("close").is_not_null())
        .group_by("stock_id", maintain_order=True)
        .agg(agg_exprs)
    )

    result: dict[str, dict[int, tuple[float, float]]] = {}
    for row in grouped.iter_rows(named=True):
        result[row["stock_id"]] = {
            w: (float(row[f"_min_{w}"]), float(row[f"_max_{w}"])) for w in windows
        }
    return result


def compute_dividend_addback(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    dividends: pl.DataFrame,
    n: int = 5,
) -> dict[str, tuple[float, float, float]]:
    """回傳每檔在 N 日動能視窗內的「除權息還原加成」（現金股利＋配股）。

    除權息日股價缺口會讓 compute_n_day_return 的 N 日報酬假負（現金除息季 6-8 月尤甚；
    大額配股／盈餘轉增資更會造成 −50%↑ 的假崩盤，如 6669 緯穎 2026-09-02 配股 1.98
    股／股 → 原始 5 日報酬 −64%）。此函式比照 compute_n_day_return 的視窗（同 gap），
    找出 ex_date 落在視窗內的除權息，回傳該加回報酬的百分比，讓呼叫端把 momentum
    還原成總報酬（價＋息＋配股）。

    還原公式（持有人視角）：ex 日每股領到 s 股新股＋D 元現金，持股變 (1+s) 股，
    視窗末值 = (1+s)·c_now + D；相對未還原價格報酬 c_now/c_back−1，缺口 delta＝
    (s·c_now + D) / c_back。純現金（s=0）時退化為舊版 D/c_back。

    Args:
        stock_ids: 要計算的股票代號清單。
        price_history: 必須含 stock_id / date / close 欄。
        dividends: 必須含 stock_id / ex_date / cash_dividend 欄（cash 為元/股）；
            `stock_dividend_ratio`（新股／原股，如 0.08、6669 為 1.98）為選填，
            缺欄時視為 0（＝只還原現金、行為同舊版）。
        n: 動能視窗交易日數（與 compute_n_day_return 對齊，預設 5）。

    Returns:
        {stock_id: (addback_pct, total_cash, total_stock_ratio)} —— 僅含視窗內有
        除權或除息的檔。addback_pct = (Σs·c_now + Σcash) / c_back × 100；
        total_cash = 合計現金股利（元）；total_stock_ratio = 合計配股率（Σs）。
    """
    if price_history.is_empty() or not {"stock_id", "date", "close"}.issubset(
        set(price_history.columns)
    ):
        return {}
    if dividends.is_empty() or not {"stock_id", "ex_date", "cash_dividend"}.issubset(
        set(dividends.columns)
    ):
        return {}

    # 向量化：先一次算出每股視窗起點(date_back/c_back)與最新日(latest/c_now)，再與除權息
    # join、篩 ex_date 落在視窗內者合計。等價於 per-stock 迴圈、但只掃全表一次。
    tagged = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .with_columns(
            pl.len().over("stock_id").cast(pl.Int64).alias("_n"),
            pl.int_range(pl.len()).over("stock_id").alias("_i"),
        )
        .filter(pl.col("_n") >= 2)
    )
    if tagged.is_empty():
        return {}

    back_idx = pl.max_horizontal(pl.col("_n") - 1 - n, pl.lit(0))
    back = tagged.filter(pl.col("_i") == back_idx).select(
        "stock_id",
        pl.col("date").alias("_date_back"),  # 視窗起點（c_back 的日期）
        pl.col("close").alias("_c_back"),
    )
    latest = tagged.filter(pl.col("_i") == pl.col("_n") - 1).select(
        "stock_id",
        pl.col("date").alias("_latest"),
        pl.col("close").alias("_c_now"),
    )
    windows = back.join(latest, on="stock_id").filter(
        pl.col("_c_back").is_not_null() & (pl.col("_c_back") != 0)
    )
    if windows.is_empty():
        return {}

    # 現金＋配股：cash>0 或 stock_dividend_ratio>0 都要還原（stock_id 轉字串對齊呼叫端）
    divs = dividends.with_columns(pl.col("stock_id").cast(pl.Utf8))
    if "stock_dividend_ratio" not in divs.columns:
        divs = divs.with_columns(pl.lit(0.0).alias("stock_dividend_ratio"))
    divs = divs.with_columns(
        pl.col("cash_dividend").fill_null(0.0),
        pl.col("stock_dividend_ratio").fill_null(0.0),
    ).filter(
        pl.col("ex_date").is_not_null()
        & ((pl.col("cash_dividend") > 0) | (pl.col("stock_dividend_ratio") > 0))
    )
    if divs.is_empty():
        return {}

    # ex_date 嚴格晚於視窗起點收盤、且不晚於最新日 → 缺口落在視窗內，需加回
    joined = (
        windows.join(divs, on="stock_id")
        .filter(
            (pl.col("_date_back") < pl.col("ex_date"))
            & (pl.col("ex_date") <= pl.col("_latest"))
        )
        .group_by("stock_id")
        .agg(
            pl.col("cash_dividend").sum().alias("_total_cash"),
            pl.col("stock_dividend_ratio").sum().alias("_total_ratio"),
            pl.col("_c_back").first().alias("_c_back"),
            pl.col("_c_now").first().alias("_c_now"),
        )
        .filter((pl.col("_total_cash") > 0) | (pl.col("_total_ratio") > 0))
    )
    result: dict[str, tuple[float, float, float]] = {}
    for row in joined.iter_rows(named=True):
        c_back = row["_c_back"]
        c_now = row["_c_now"]
        cash = float(row["_total_cash"])
        ratio = float(row["_total_ratio"])
        # 配股價值需要視窗末收盤；c_now 缺（極罕見）時僅還原現金部分
        stock_val = ratio * float(c_now) if (c_now is not None and ratio > 0) else 0.0
        addback_pct = (stock_val + cash) / c_back * 100
        result[row["stock_id"]] = (float(addback_pct), cash, ratio)
    return result


def detect_price_discontinuity(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    lookback: int = 10,
    threshold_pct: float = 15.0,
) -> dict[str, tuple[float, str]]:
    """偵測近 `lookback` 交易日內的「價格不連續」（除權息／減資／面額分割／停牌補跳）。

    條件：單日收盤對收盤報酬 `|c[t]/c[t-1] − 1|` 超過 `threshold_pct`（漲跌停 ±10% 之外），
    **且** 當日「漲跌價差」(`change`) 無法解釋這個跳空——TWSE 在除權息／減資日的個股
    STOCK_DAY 不給 `change`（null），全市場 daily 則常填 0；兩者皆代表 `close` 序列在
    該日不連續、不可直接算報酬。此為安全網：涵蓋 TWT48U 除權息預告表收不到的減資／
    面額分割／停牌補跳。

    Args:
        stock_ids: 要檢查的股票代號清單。
        price_history: 需含 stock_id / date / close；**選填 `change`（漲跌價差）**。
            無 `change` 欄 → 無法判別跳空成因，回傳空 dict（不誤報）。
        lookback: 只看最近幾個交易日（預設 10，與 ret_10d 對齊）。
        threshold_pct: 單日報酬絕對值門檻（預設 15%）。

    Returns:
        {stock_id: (worst_1d_ret_pct, disc_date_iso)} —— 僅含命中的檔；
        worst_1d_ret_pct 帶正負號、disc_date_iso 為 "YYYY-MM-DD"。
    """
    required = {"stock_id", "date", "close"}
    if price_history.is_empty() or not required.issubset(set(price_history.columns)):
        return {}
    if "change" not in price_history.columns:
        return {}

    df = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .sort(["stock_id", "date"])
        .with_columns(
            pl.int_range(pl.len()).over("stock_id").alias("_i"),
            pl.len().over("stock_id").cast(pl.Int64).alias("_n"),
        )
        .filter(pl.col("_i") >= pl.col("_n") - lookback - 1)  # 近 lookback 日＋前一天
        .with_columns(
            pl.col("close").shift(1).over("stock_id").alias("_prev_close"),
        )
        .filter(
            pl.col("_prev_close").is_not_null()
            & (pl.col("_prev_close") != 0)
            & pl.col("close").is_not_null()
        )
        .with_columns(
            ((pl.col("close") - pl.col("_prev_close")) / pl.col("_prev_close") * 100).alias(
                "_ret1d"
            ),
            (pl.col("close") - pl.col("_prev_close")).abs().alias("_abs_move"),
        )
        .filter(
            (pl.col("_ret1d").abs() > threshold_pct)
            & (
                pl.col("change").is_null()
                | (pl.col("change").abs() < 0.5 * pl.col("_abs_move"))
            )
        )
    )
    if df.is_empty():
        return {}

    # 每檔取「最劇烈的一天」（|ret1d| 最大）
    worst = (
        df.sort(["stock_id", pl.col("_ret1d").abs()], descending=[False, True])
        .group_by("stock_id", maintain_order=True)
        .first()
    )
    return {
        row["stock_id"]: (float(row["_ret1d"]), row["date"].isoformat())
        for row in worst.iter_rows(named=True)
    }


def aggregate_group_momentum(
    momentum_map: dict[str, tuple[float, int]],
    stock_ids_per_group: dict[str, list[str]],
) -> dict[str, tuple[float, int]]:
    """把個股動能聚合到族群層級。

    Args:
        momentum_map: compute_n_day_return 的回傳。
        stock_ids_per_group: {industry_code: [stock_id, ...]}

    Returns:
        {industry_code: (avg_return_pct, min_days_used)}
        - avg_return_pct: 族群內有資料個股的平均報酬
        - min_days_used: 族群內最差的可用天數（用於顯示「N 日資料」標註）
        - 若族群內無任何個股有資料 → (0.0, 0)
    """
    result: dict[str, tuple[float, int]] = {}
    for industry_code, stock_ids in stock_ids_per_group.items():
        returns: list[float] = []
        days: list[int] = []
        for sid in stock_ids:
            entry = momentum_map.get(sid)
            if entry is None:
                continue
            returns.append(entry[0])
            days.append(entry[1])
        if returns:
            avg = sum(returns) / len(returns)
            min_d = min(days)
            result[industry_code] = (avg, min_d)
        else:
            result[industry_code] = (0.0, 0)
    return result
