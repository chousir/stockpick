"""族群分析：把篩選結果按 TWSE 產業別分組，計算族群強度分數。

強度公式（2026-W21 起改為動能主導）：
  score = 50 * sigmoid(momentum_5d / 5)   # 動能主導（momentum_5d = 族群中位數，抗單檔灌水）
        + 25 * entry_rate                 # 入選率（仍保留訊號）
        + 15 * inst_score                 # 法人買超家數比（inst_net > 0 家數 / 成員數）
        + 10 * (log1p(members) / log1p(max))   # 規模

另回傳 up_count（族群內 5 日漲幅 > 0 的家數），供報告計算上漲家數比（breadth），
一眼看穿「整族群轉強」是真廣度還是單檔小型股獨拉。
"""

from __future__ import annotations

import math

import polars as pl
from loguru import logger

from tw_screener.analysis.momentum import (
    compute_dividend_addback,
    compute_n_day_return,
    compute_rolling_extrema,
)

_DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.50,
    "entry_rate": 0.25,
    "institutional": 0.15,
    "size": 0.10,
}

_MOMENTUM_DAYS = 5
# 修法6 趨勢窗：近 10 日報酬，供報表區分「健康回踩」vs「下跌反彈」（比照 _MOMENTUM_DAYS）。
_TREND_DAYS = 10
# 法人近端窗：三大法人/外資/投信近 5/10 日累計，揭露 20 日累計蓋住的近端轉向（純揭露、非 gate）。
# 修法6 起於外資；分析層補窗擴及投信(trust)/三大法人(inst=total_net)，比照外資同口徑。
_INST_NEAR_WINDOWS = (5, 10)
# 近端窗法人別：institutional 來源欄 → 輸出欄字首（total_net 對外稱 inst，比照 20 日累計命名）。
_NEAR_SRC_TO_PREFIX = {
    "foreign_net": "foreign_net",
    "trust_net": "trust_net",
    "total_net": "inst_net",
}
# M-修法7（7a）進場區間視窗：近 20/60 日收盤 min/max（絕對價），供進場階梯 T3 結構價（前波低）
# 與回檔深度檢核（距區間低/高）；比照 _MOMENTUM_DAYS/_TREND_DAYS 為模組常數。
_RANGE_WINDOWS = (20, 60)

# 策略 G（成長拉回）的「拉回 setup」判定門檻；可由 settings.yaml 的 g_pullback 覆蓋。
# 三者皆硬門檻：季線上揚 + 乖離帶內 + 量縮。
_DEFAULT_G_PULLBACK: dict[str, float] = {
    "ma60_band_low": -5.0,      # 乖離帶下界（% 距季線；允許略破季線）
    "ma60_band_high": 10.0,     # 乖離帶上界（超過＝仍延伸，不算回踩）
    "ma60_slope_window": 10,    # 季線斜率比較視窗（交易日）
    "ma60_slope_min_pct": 0.0,  # 季線上揚最低斜率（%）；> 此值才算上揚
    "vol_ratio_max": 1.0,       # 量縮門檻（今日量 / 20 日均量 ≤ 此值）
}


def is_etf_or_warrant(stock_id: str) -> bool:
    """Return True if stock_id is an ETF, structured product, or warrant (should skip analysis).

    Filters:
    - starts with "00": ETF codes (0050, 006208, 00400A, 00631L ...)
    - contains non-digit: warrant/bond codes (2330Y, 6592A ...)
    """
    if stock_id.startswith("00"):
        return True
    if not stock_id.isdigit():
        return True
    return False


def _compute_rs_from_history(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,  # noqa: ARG001 — kept for backward compatibility
    n: int = _MOMENTUM_DAYS,
) -> dict[str, float]:
    """回傳 {stock_id: n_day_return_pct}（純絕對動能，不再減大盤）。

    用 momentum.compute_n_day_return；若該股可用天數不足 n，仍會回傳實際可算的報酬。
    無資料的股票不在回傳 dict 中。
    """
    momentum_map = compute_n_day_return(stock_ids, price_history, n=n)
    return {sid: ret for sid, (ret, _days) in momentum_map.items()}


def _compute_ma_dist(
    price_history: pl.DataFrame,
    stock_df: pl.DataFrame,
    windows: tuple[int, ...] = (20, 60),
    slope_window: int = 10,
    slope_for: int = 60,
) -> pl.DataFrame:
    """Compute MA distance % from latest close per stock, plus MA{slope_for} slope %.

    Returns DataFrame with stock_id + ma{w}_dist_pct columns, and (when slope_for is
    in windows) `ma{slope_for}_slope_pct` = % change of MA{slope_for} vs slope_window
    trading days ago（季線上揚與否）. Null = insufficient data（< w，或斜率需 w+slope_window 日）.
    """
    has_slope = slope_for in windows
    out_cols = [f"ma{w}_dist_pct" for w in windows]
    if has_slope:
        out_cols.append(f"ma{slope_for}_slope_pct")

    null_cols = [pl.lit(None, dtype=pl.Float64).alias(c) for c in out_cols]
    if (
        price_history.is_empty()
        or "close" not in price_history.columns
        or "stock_id" not in price_history.columns
    ):
        return stock_df.select("stock_id").with_columns(null_cols)

    # Pre-sort so tail(w) inside each group = most-recent w closes
    sorted_hist = price_history.drop_nulls("close").sort(["stock_id", "date"])

    agg_exprs: list[pl.Expr] = []
    for w in windows:
        agg_exprs += [
            pl.col("close").tail(w).mean().alias(f"_ma{w}"),
            pl.col("close").tail(w).count().alias(f"_cnt{w}"),
        ]
    if has_slope:
        # MA{slope_for} as of slope_window days ago = mean of the window ending then.
        # tail(slope_for+slope_window).head(slope_for) selects exactly that older window.
        agg_exprs += [
            pl.col("close")
            .tail(slope_for + slope_window)
            .head(slope_for)
            .mean()
            .alias("_ma_prev"),
            pl.col("close").count().alias("_cnt_all"),
        ]
    ma_df = sorted_hist.group_by("stock_id").agg(agg_exprs)

    ma_df = ma_df.join(stock_df.select(["stock_id", "close"]), on="stock_id", how="left")
    for w in windows:
        ma_df = ma_df.with_columns(
            pl.when(
                (pl.col(f"_cnt{w}") >= w)
                & (pl.col(f"_ma{w}") > 0)
                & pl.col("close").is_not_null()
            )
            .then((pl.col("close") - pl.col(f"_ma{w}")) / pl.col(f"_ma{w}") * 100.0)
            .otherwise(None)
            .alias(f"ma{w}_dist_pct")
        )

    if has_slope:
        ma_df = ma_df.with_columns(
            pl.when(
                (pl.col("_cnt_all") >= slope_for + slope_window)
                & (pl.col("_ma_prev") > 0)
            )
            .then(
                (pl.col(f"_ma{slope_for}") - pl.col("_ma_prev"))
                / pl.col("_ma_prev")
                * 100.0
            )
            .otherwise(None)
            .alias(f"ma{slope_for}_slope_pct")
        )

    return stock_df.select("stock_id").join(
        ma_df.select(["stock_id"] + out_cols),
        on="stock_id",
        how="left",
    )


def _sigmoid(x: float) -> float:
    """logistic sigmoid; saturates to [0, 1]."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def near_flow_state(
    foreign_20d: float | None,
    foreign_5d: float | None,
    trust_20d: float | None,
    trust_5d: float | None,
    min_shares: float = 1_000_000.0,
    stall_share_pct: float = 5.0,
    accel_share_pct: float = 40.0,
) -> tuple[str | None, float | None]:
    """F5（沿舊 06 NF1）：近端籌碼狀態旗標——把「拆窗」自動化，純揭露非 gate。

    對外資/投信各評「20 日大幅買超邊」的近 5 日佔比（share＝5d/20d×100；均勻≈25%）：
    5d 轉負＝轉賣、share < stall＝熄火（台新新光金 2% 型）、≥ accel＝加速、其餘平穩。
    兩邊都夠大時取較警示者（轉賣＞熄火＞加速＞平穩）。20 日非大幅正（含賣超主導、
    量小）＝無「前段買很兇」可揭露 → (None, None)。

    §1.4 實證：近端佔比**單獨無判別力**（聯詠近端在賣仍 +13.7%、華碩近端仍熱也賠）
    ——本欄只免除人工拆窗、標出「20 日累計蓋住的近端狀態」，不得當排序主判或 gate。

    Returns:
        (state, share_pct)：state ∈ {"轉賣(外資)", "熄火(投信)", ...} 帶主體標示。
    """
    order = {"轉賣": 0, "熄火": 1, "加速": 2, "平穩": 3}
    sides: list[tuple[str, float, str]] = []
    for s20, s5, label in ((foreign_20d, foreign_5d, "外資"), (trust_20d, trust_5d, "投信")):
        if s20 is None or s5 is None or s20 < min_shares:
            continue
        share = s5 / s20 * 100
        if s5 < 0:
            state = "轉賣"
        elif share < stall_share_pct:
            state = "熄火"
        elif share >= accel_share_pct:
            state = "加速"
        else:
            state = "平穩"
        sides.append((state, share, label))
    if not sides:
        return None, None
    sides.sort(key=lambda x: order[x[0]])
    state, share, label = sides[0]
    return f"{state}({label})", round(share, 1)


def classify_risk_kind(
    flow_state: str | None,
    ma60_dist_pct: float | None,
    ma20_dist_pct: float | None,
    momentum_5d_pct: float | None,
    ext_ma60_pct: float = 15.0,
    down_5d_pct: float = -5.0,
) -> str:
    """F5（沿舊 06 NF1）：三風險分類——同是「近端有狀況」但要三種動作，先替人分類。

    價格已跌（5 日 < 門檻且破月線＝直接剔除級）＞ 籌碼熄火（轉賣/熄火＝降級等訊號）
    ＞ 價格延伸（距季線 > 門檻＝買強勢階梯控小；門檻預設對齊 F2 核心位階紀律 +15%）。
    皆非 → 空字串。純分類揭露、不改變入選（判斷權在人）。
    """
    if (
        momentum_5d_pct is not None
        and momentum_5d_pct < down_5d_pct
        and ma20_dist_pct is not None
        and ma20_dist_pct < 0
    ):
        return "價格已跌"
    if flow_state is not None and ("熄火" in flow_state or "轉賣" in flow_state):
        return "籌碼熄火"
    if ma60_dist_pct is not None and ma60_dist_pct > ext_ma60_pct:
        return "價格延伸"
    return ""


def group_stocks(
    screener_results: dict[str, pl.DataFrame],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,  # noqa: ARG001 — kept for backward compatibility
    industry_df: pl.DataFrame | None = None,
    weights: dict[str, float] | None = None,
    min_group_size: int = 2,
    institutional: pl.DataFrame | None = None,
    volume_history: pl.DataFrame | None = None,
    g_pullback: dict[str, float] | None = None,
    vol_lookback: int = 20,
    dividends: pl.DataFrame | None = None,
    trajectory_cfg: dict | None = None,
    skip_etf: bool = True,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Group screened stocks by industry and compute group strength scores (動能主導).

    skip_etf: True（預設）＝跳過 ETF/權證（選股宇宙現行為）；False＝保留 ETF 列
        （僅 holdings/watchlist enrich 用，讓持股 ETF 產出價格/報酬輕量列，docs/21）。

    institutional: 近 N 日三大法人（含 stock_id / total_net）；提供時計入族群法人強度。
        個股層聚合為 inst_net（近 N 日合計淨買超股數），族群層 inst_score =
        法人買超家數比（inst_net > 0 的家數 / 成員數）。未提供 → inst_net = 0、inst_score = 0。
    volume_history: 含 stock_id / date / trade_volume；提供時計算量比
        （今日量 / 近 vol_lookback 日均量）。未提供 → vol_ratio = 0。
    vol_lookback: 量比均量視窗（交易日）。每檔只取今日之前最近 vol_lookback 天平均，
        與輸入的 volume_history 列數無關 → 不論餵幾天都是一致的 N 日均量。
    g_pullback: 策略 G 的拉回 setup 門檻（見 _DEFAULT_G_PULLBACK）；若 screener 含
        in_g_growth_pullback，G 的有效命中會被收斂為「基本面命中 ∧ 季線上揚 ∧ 乖離帶內 ∧ 量縮」。
    dividends: 含 stock_id / ex_date / cash_dividend（load_recent_dividends 的回傳）；提供時把
        5 日視窗內現金股利加回 momentum_5d/rs（除息還原），並輸出 div_addback_pct / ex_div_cash。
        未提供 → 兩欄為 0，momentum 維持原始價格報酬。

    Returns (groups_df, enriched_stocks_df):
    - groups_df: industry-level stats sorted by score desc (filtered by min_group_size)
        欄位：industry_code / industry_name / members_count / total_in_industry /
              entry_rate / momentum_5d（族群中位數）/ up_count / momentum_5d_days_used /
              rs_avg(alias) / inst_buy_count / inst_score / count_{sid} / score
    - enriched_stocks_df: per-stock data with industry, rs (= n_day_return), strategy flags,
        momentum_5d, momentum_days_used, inst_net（三大法人總和）,
        foreign_net（外資）, trust_net（投信）
    """
    if weights is None:
        weights = _DEFAULT_WEIGHTS

    strategy_ids = sorted(screener_results.keys())

    # Build per-stock dict from all strategies (skip ETFs and warrants)
    stock_rows: dict[str, dict] = {}
    skipped_etf = 0
    for sid in strategy_ids:
        df = screener_results.get(sid, pl.DataFrame())
        if df.is_empty():
            continue
        for row in df.iter_rows(named=True):
            stock_id = str(row["stock_id"])
            if skip_etf and is_etf_or_warrant(stock_id):
                skipped_etf += 1
                continue
            if stock_id not in stock_rows:
                stock_rows[stock_id] = {
                    "stock_id": stock_id,
                    "name": str(row.get("name") or ""),
                    "close": float(row.get("close") or 0),
                    "change_pct": float(row.get("change_pct") or 0),
                    "amount_million": float(row.get("amount_million") or 0),
                    "goodinfo_url": str(row.get("goodinfo_url") or ""),
                    **{f"in_{s}": False for s in strategy_ids},
                }
            stock_rows[stock_id][f"in_{sid}"] = True

    if skipped_etf:
        logger.info("group_stocks: 排除 {} 檔 ETF/權證/結構型商品", skipped_etf)

    if not stock_rows:
        logger.warning("group_stocks: 三組 CSV 均為空（或全為 ETF），無法進行族群分析")
        _empty_g: dict[str, type[pl.DataType]] = {
            "industry_code": pl.Utf8,
            "industry_name": pl.Utf8,
            "members_count": pl.Int32,
            "total_in_industry": pl.Int32,
            "entry_rate": pl.Float64,
            "momentum_5d": pl.Float64,
            "up_count": pl.Int32,
            "momentum_5d_days_used": pl.Int32,
            "rs_avg": pl.Float64,
            "inst_buy_count": pl.Int32,
            "inst_score": pl.Float64,
            "score": pl.Float64,
        }
        return pl.DataFrame(schema=_empty_g), pl.DataFrame()

    stock_df = pl.DataFrame(list(stock_rows.values()))

    # 5-day momentum: prefer price_history; fallback to change_pct
    stock_ids = stock_df["stock_id"].to_list()
    momentum_map = compute_n_day_return(stock_ids, price_history, n=_MOMENTUM_DAYS)

    rs_values: list[float] = []
    days_values: list[int] = []
    for sid, row in zip(stock_ids, stock_df.iter_rows(named=True)):
        entry = momentum_map.get(sid)
        if entry is not None:
            rs_values.append(entry[0])
            days_values.append(entry[1])
        else:
            rs_values.append(float(row.get("change_pct") or 0))
            days_values.append(1)
    stock_df = stock_df.with_columns(
        [
            pl.Series("rs", rs_values, dtype=pl.Float64),  # 5-day return (alias kept)
            pl.Series("momentum_5d", rs_values, dtype=pl.Float64),
            pl.Series("momentum_days_used", days_values, dtype=pl.Int32),
        ]
    )

    # 除息還原：6-8 月除息季，除息日股價缺口會讓 momentum_5d 假負並把除息股壓到排名後段。
    # 把視窗內現金股利加回報酬（價＋息），momentum_5d/rs 一併還原；div_addback_pct/ex_div_cash
    # 供報表標註「已還原除息」。此處 stock_df 仍與 stock_ids 同序，可用 positional Series 對齊。
    if dividends is not None and not dividends.is_empty():
        addback = compute_dividend_addback(
            stock_ids, price_history, dividends, n=_MOMENTUM_DAYS
        )
    else:
        addback = {}
    add_vals = [addback.get(sid, (0.0, 0.0))[0] for sid in stock_ids]
    cash_vals = [addback.get(sid, (0.0, 0.0))[1] for sid in stock_ids]
    add_series = pl.Series("_div_add", add_vals, dtype=pl.Float64)
    stock_df = stock_df.with_columns(
        [
            (pl.col("momentum_5d") + add_series).alias("momentum_5d"),
            (pl.col("rs") + add_series).alias("rs"),
            pl.Series("div_addback_pct", add_vals, dtype=pl.Float64),
            pl.Series("ex_div_cash", cash_vals, dtype=pl.Float64),
        ]
    )

    # 修法6（6b）近 10 日報酬：趨勢鏡頭，供報表區分「健康回踩（10 日≥−3%）」vs
    # 「下跌反彈（10 日<−5%、貼 MA20 的弱彈）」。除息還原比照 momentum_5d，避免除息季假負。
    # 此處 stock_df 仍與 stock_ids 同序，可用 positional Series 對齊。
    ret10_map = compute_n_day_return(stock_ids, price_history, n=_TREND_DAYS)
    ret10_vals = [ret10_map.get(sid, (None, 0))[0] for sid in stock_ids]
    stock_df = stock_df.with_columns(pl.Series("ret_10d", ret10_vals, dtype=pl.Float64))
    if dividends is not None and not dividends.is_empty():
        add10 = compute_dividend_addback(stock_ids, price_history, dividends, n=_TREND_DAYS)
        add10_vals = [add10.get(sid, (0.0, 0.0))[0] for sid in stock_ids]
        stock_df = stock_df.with_columns(
            (pl.col("ret_10d") + pl.Series("_ret10_add", add10_vals, dtype=pl.Float64)).alias(
                "ret_10d"
            )
        )

    # M-修法7（7a）進場區間：近 20/60 日收盤 min/max（絕對價）。
    # 供 T3 結構價（前波低 low_60d）與回檔深度檢核；不除息還原、與 close/MA 同口徑。
    # stock_df 與 stock_ids 同序，可用 positional Series 對齊。
    extrema = compute_rolling_extrema(stock_ids, price_history, windows=_RANGE_WINDOWS)
    extrema_series: list[pl.Series] = []
    for w in _RANGE_WINDOWS:
        pairs = [extrema.get(sid, {}).get(w) or (None, None) for sid in stock_ids]
        extrema_series.append(pl.Series(f"low_{w}d", [p[0] for p in pairs], dtype=pl.Float64))
        extrema_series.append(pl.Series(f"high_{w}d", [p[1] for p in pairs], dtype=pl.Float64))
    stock_df = stock_df.with_columns(extrema_series)

    # 法人淨買超：近 N 日合計 → inst_net（三大法人總和）/ foreign_net（外資）/
    # trust_net（投信）。只算一次，下游共用。自營＝inst_net−foreign_net−trust_net 可推得，不另存。
    _inst_cols = ("inst_net", "foreign_net", "trust_net")
    if (
        institutional is not None
        and not institutional.is_empty()
        and {"stock_id", "total_net", "foreign_net", "trust_net"}.issubset(
            institutional.columns
        )
    ):
        inst_agg = institutional.group_by("stock_id").agg(
            [
                pl.col("total_net").sum().alias("inst_net"),
                pl.col("foreign_net").sum().alias("foreign_net"),
                pl.col("trust_net").sum().alias("trust_net"),
            ]
        )
        # 先記「法人缺漏」（join 不到法人快取）再 fill 0：缺漏 vs 真實零買賣超要能區分。
        # numeric 仍填 0，下游排名/族群法人家數比行為不變；inst_missing 只供 writer 顯示空白+旗標。
        stock_df = (
            stock_df.join(inst_agg, on="stock_id", how="left")
            .with_columns(pl.col("inst_net").is_null().alias("inst_missing"))
            .with_columns([pl.col(c).fill_null(0.0).cast(pl.Float64) for c in _inst_cols])
        )
    else:
        stock_df = stock_df.with_columns(
            [pl.lit(0.0).alias(c) for c in _inst_cols] + [pl.lit(True).alias("inst_missing")]
        )

    # 修法6 + 分析層補窗：法人多窗揭露。三大法人/外資/投信各算近 5/10 日累計，補 20 日累計
    # 蓋住的近端轉向（如外資 20 日+、近 5 日−，不可記「雙強買」）。純揭露不設 gate
    # ——事件研究證近端外資反轉不預測弱勢；投信/三大法人同口徑補上（讀法見 docs/11）。
    _near_out = [
        f"{pref}_{w}d" for pref in _NEAR_SRC_TO_PREFIX.values() for w in _INST_NEAR_WINDOWS
    ]
    if (
        institutional is not None
        and not institutional.is_empty()
        and {"date", "stock_id", *_NEAR_SRC_TO_PREFIX}.issubset(institutional.columns)
    ):
        near_dates = institutional.select("date").unique().sort("date", descending=True)
        for w in _INST_NEAR_WINDOWS:
            wdates = near_dates.head(w)["date"].to_list()
            wagg = (
                institutional.filter(pl.col("date").is_in(wdates))
                .group_by("stock_id")
                .agg(
                    [
                        pl.col(src).sum().alias(f"{pref}_{w}d")
                        for src, pref in _NEAR_SRC_TO_PREFIX.items()
                    ]
                )
            )
            stock_df = stock_df.join(wagg, on="stock_id", how="left").with_columns(
                [
                    pl.col(f"{pref}_{w}d").fill_null(0.0).cast(pl.Float64)
                    for pref in _NEAR_SRC_TO_PREFIX.values()
                ]
            )
    else:
        stock_df = stock_df.with_columns([pl.lit(0.0).alias(c) for c in _near_out])

    # MA20 / MA60 距離 + MA60 斜率（% 偏離最新收盤價 / 季線上揚率）—— 不足則 null
    g_params = {**_DEFAULT_G_PULLBACK, **(g_pullback or {})}
    ma_dist_df = _compute_ma_dist(
        price_history, stock_df, slope_window=int(g_params["ma60_slope_window"])
    )
    stock_df = stock_df.join(ma_dist_df, on="stock_id", how="left")

    # F5（沿舊 07 TR1）：回踩品質軌跡欄——連跌天數/回踩量比/站上月線天數/止穩｜觀察｜破線。
    # 歷史不足（新股/上櫃缺口）→ 該檔 null 不臆造；trajectory_cfg=None＝不啟用（向後相容）。
    if trajectory_cfg is not None and not price_history.is_empty():
        from tw_screener.analysis.trajectory import compute_trajectories

        traj = compute_trajectories(price_history, volume_history, trajectory_cfg)
        if not traj.is_empty():
            stock_df = stock_df.join(traj, on="stock_id", how="left")

    # 量比（今日量 / 近 N 日均量）—— 需 ≥ 3 個歷史日才輸出有效值
    if (
        volume_history is not None
        and not volume_history.is_empty()
        and {"stock_id", "date", "trade_volume"}.issubset(volume_history.columns)
    ):
        latest_vol_date = volume_history["date"].max()
        today_vol = (
            volume_history.filter(pl.col("date") == latest_vol_date)
            .select(["stock_id", "trade_volume"])
            .rename({"trade_volume": "_vol_today"})
        )
        # 每檔只取今日之前「最近 vol_lookback 天」平均：與輸入列數脫鉤，避免餵 100 天
        # 卻算成 99 日均量（candidates 餵 21 天、named 餵 100 天，過去會得到不同量比）。
        prior_avg = (
            volume_history.filter(pl.col("date") < latest_vol_date)
            .group_by("stock_id")
            .agg(
                pl.col("trade_volume").sort_by("date").tail(vol_lookback).mean().alias("_vol_avg"),
                pl.col("trade_volume").sort_by("date").tail(vol_lookback).count().alias("_vol_days"),
            )
        )
        vol_df = (
            today_vol.join(prior_avg, on="stock_id", how="left")
            .with_columns(
                pl.when(
                    (pl.col("_vol_avg") > 0)
                    & pl.col("_vol_avg").is_not_null()
                    & (pl.col("_vol_days") >= 3)
                )
                .then(pl.col("_vol_today") / pl.col("_vol_avg"))
                .otherwise(0.0)
                .alias("vol_ratio")
            )
            .select(["stock_id", "vol_ratio"])
        )
        stock_df = stock_df.join(vol_df, on="stock_id", how="left").with_columns(
            pl.col("vol_ratio").fill_null(0.0)
        )
    else:
        stock_df = stock_df.with_columns(pl.lit(0.0).alias("vol_ratio"))

    # 策略 G 有效命中 = 基本面命中 ∧ 拉回 setup（季線上揚 + 乖離帶內 + 量縮）。
    # 把 in_g_growth_pullback 從「成長宇宙」收斂成「回踩季線的成長股」；無 G 欄則略過。
    if "in_g_growth_pullback" in stock_df.columns:
        has_slope_col = "ma60_slope_pct" in stock_df.columns
        slope_ok = (
            (pl.col("ma60_slope_pct") > g_params["ma60_slope_min_pct"])
            if has_slope_col
            else pl.lit(False)
        )
        pullback_setup = (
            slope_ok
            & pl.col("ma60_dist_pct").is_not_null()
            & pl.col("ma60_dist_pct").is_between(
                g_params["ma60_band_low"], g_params["ma60_band_high"]
            )
            & (pl.col("vol_ratio") > 0)
            & (pl.col("vol_ratio") <= g_params["vol_ratio_max"])
        )
        stock_df = stock_df.with_columns(
            (pl.col("in_g_growth_pullback") & pullback_setup.fill_null(False)).alias(
                "in_g_growth_pullback"
            )
        )

    # strategy_count = sum of all in_{sid} flags（在 G 收斂後計算，反映有效命中）
    strategy_count_expr: pl.Expr = pl.lit(0, dtype=pl.Int32)
    for sid in strategy_ids:
        strategy_count_expr = strategy_count_expr + pl.col(f"in_{sid}").cast(pl.Int32)
    stock_df = stock_df.with_columns(strategy_count_expr.alias("strategy_count"))

    # 丟掉「無任何有效策略命中」的成員：這些股只從 G 的基本面宇宙進來、未通過拉回
    # 過濾、又不在 D/E/F，留著會灌大族群成員數/入選率/廣度，污染強度排名。
    dropped_noise = int((stock_df["strategy_count"] == 0).sum())
    if dropped_noise:
        stock_df = stock_df.filter(pl.col("strategy_count") > 0)
        logger.info(
            "group_stocks: 排除 {} 檔無有效策略命中的宇宙雜訊（多為 G 未過濾股）", dropped_noise
        )

    # Join with industry classification
    if industry_df is not None and not industry_df.is_empty():
        stock_df = stock_df.join(
            industry_df.select(["stock_id", "industry_code", "industry_name"]),
            on="stock_id",
            how="left",
        )
    else:
        stock_df = stock_df.with_columns(
            [pl.lit("00").alias("industry_code"), pl.lit("未分類").alias("industry_name")]
        )

    stock_df = stock_df.with_columns(
        [
            pl.col("industry_code").fill_null("00"),
            pl.col("industry_name").fill_null("未分類"),
        ]
    )

    # Group-level aggregation
    agg_exprs: list[pl.Expr] = [
        pl.col("stock_id").count().alias("members_count"),
        pl.col("momentum_5d").median().alias("momentum_5d"),  # 中位數：抗單檔小型股灌水
        (pl.col("momentum_5d") > 0).cast(pl.Int32).sum().alias("up_count"),  # 上漲家數
        (pl.col("inst_net") > 0).cast(pl.Int32).sum().alias("inst_buy_count"),  # 法人買超家數
        pl.col("momentum_days_used").min().alias("momentum_5d_days_used"),
    ]
    for sid in strategy_ids:
        agg_exprs.append(
            pl.col(f"in_{sid}").cast(pl.Int32).sum().alias(f"count_{sid}")
        )

    groups = stock_df.group_by(["industry_code", "industry_name"]).agg(agg_exprs)

    # alias for backward compatibility (rs_avg now = 5-day median return)
    groups = groups.with_columns(pl.col("momentum_5d").alias("rs_avg"))

    # total_in_industry and entry_rate
    if industry_df is not None and not industry_df.is_empty():
        industry_total = industry_df.group_by("industry_code").agg(
            pl.col("stock_id").count().alias("total_in_industry")
        )
        groups = groups.join(industry_total, on="industry_code", how="left")
        groups = groups.with_columns(
            pl.col("total_in_industry").fill_null(pl.col("members_count"))
        )
    else:
        groups = groups.with_columns(pl.col("members_count").alias("total_in_industry"))

    groups = groups.with_columns(
        (
            pl.col("members_count").cast(pl.Float64)
            / pl.col("total_in_industry").cast(pl.Float64)
        ).alias("entry_rate")
    )

    # inst_score = 法人買超家數比（族群內 inst_net > 0 家數 / 成員數），落在 [0,1]
    groups = groups.with_columns(
        (
            pl.col("inst_buy_count").cast(pl.Float64)
            / pl.col("members_count").cast(pl.Float64)
        ).alias("inst_score")
    )

    # Composite score (動能主導)
    max_members = float(groups["members_count"].max() or 1)
    log_max = math.log1p(max_members)

    w_mom = weights.get("momentum", _DEFAULT_WEIGHTS["momentum"])
    w_er = weights.get("entry_rate", _DEFAULT_WEIGHTS["entry_rate"])
    w_inst = weights.get("institutional", _DEFAULT_WEIGHTS["institutional"])
    w_sz = weights.get("size", _DEFAULT_WEIGHTS["size"])

    # sigmoid: 5 日漲 5% → 0.5；漲 10% → 0.73；漲 20% → 0.88（避免大漲撞天花板）
    momentum_score_vals = [_sigmoid(v / 5.0) for v in groups["momentum_5d"].to_list()]
    groups = groups.with_columns(
        pl.Series("_momentum_score", momentum_score_vals, dtype=pl.Float64)
    )

    groups = groups.with_columns(
        (
            (pl.col("members_count").cast(pl.Float64) + 1.0).log(base=math.e) / log_max
        ).alias("_size_score")
    )

    groups = groups.with_columns(
        (
            w_mom * 100.0 * pl.col("_momentum_score")
            + w_er * 100.0 * pl.col("entry_rate")
            + w_inst * 100.0 * pl.col("inst_score")
            + w_sz * 100.0 * pl.col("_size_score")
        ).alias("score")
    ).drop(["_momentum_score", "_size_score"])

    # Filter and sort
    groups = groups.filter(pl.col("members_count") >= min_group_size).sort(
        "score", descending=True
    )

    logger.info(
        "group_stocks: {} 族群（入選率 ≥ {} 的共 {} 族群）",
        len(groups),
        min_group_size,
        len(groups),
    )
    return groups, stock_df


def rank_themes(
    members: pl.DataFrame,
    themes_long: pl.DataFrame,
    min_members: int = 2,
    concept_min_members: int = 3,
    vol_surge_ratio: float = 1.5,
    lead_weights: dict[str, float] | None = None,
) -> pl.DataFrame:
    """多標籤主題強度排名：候選股依所屬主題（次產業＋概念股）各自分群排名。

    members 需含 stock_id / momentum_5d（可選 inst_net / foreign_net / vol_ratio）；
    themes_long 為 (stock_id, theme, kind) long table，一檔可屬多主題、join 後各貢獻一列。
    因主題只涵蓋部分股、缺「全市場母體數」，故**不用入選率**。算兩種分數：

    - **score（漲幅強度・落後鏡頭，Section 2.6/2.7 用）**：
        70 * sigmoid(5日中位/5) + 15 * 法人買超家數比 + 15 * log 規模
    - **lead_score（領先分數・領先鏡頭，輪動雷達 Section 2.8 用）**：
        外資買超 breadth + 量能放大 breadth 主導、動能僅輔（預設 0.45/0.30/0.25）。
        專抓「籌碼/量能已進、價格未動」的次產業（漲幅排名後段但 lead_score 高＝下一棒）。

    vol_surge_ratio：量比 ≥ 此值才算「量能放大」（預設 1.5）。
    保留門檻依 kind 分流：次產業 >= min_members、概念股 >= concept_min_members。
    回傳含 members_count / momentum_5d / up_count / inst_buy_count / inst_score /
    foreign_buy_count / vol_surge_count / foreign_score / vol_surge_score / score /
    lead_score，按 score 降序（雷達排序在報表層用 lead_score 重排）。
    """
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND

    _empty = pl.DataFrame(
        schema={
            "theme": pl.Utf8,
            "kind": pl.Utf8,
            "members_count": pl.Int32,
            "momentum_5d": pl.Float64,
            "up_count": pl.Int32,
            "inst_buy_count": pl.Int32,
            "inst_score": pl.Float64,
            "foreign_buy_count": pl.Int32,
            "vol_surge_count": pl.Int32,
            "foreign_score": pl.Float64,
            "vol_surge_score": pl.Float64,
            "score": pl.Float64,
            "lead_score": pl.Float64,
        }
    )
    if (
        members.is_empty()
        or themes_long.is_empty()
        or "momentum_5d" not in members.columns
        or "theme" not in themes_long.columns
    ):
        return _empty
    # 一檔對多主題各貢獻一列：candidate × theme（inner join＝只算落在候選股的主題成分）
    df = members.join(themes_long, on="stock_id", how="inner")
    if df.is_empty():
        return _empty

    has_inst = "inst_net" in df.columns
    has_foreign = "foreign_net" in df.columns
    has_vol = "vol_ratio" in df.columns
    agg_exprs: list[pl.Expr] = [
        pl.col("stock_id").count().alias("members_count"),
        pl.col("momentum_5d").median().alias("momentum_5d"),
        (pl.col("momentum_5d") > 0).cast(pl.Int32).sum().alias("up_count"),
    ]
    if has_inst:
        agg_exprs.append((pl.col("inst_net") > 0).cast(pl.Int32).sum().alias("inst_buy_count"))
    if has_foreign:
        agg_exprs.append(
            (pl.col("foreign_net") > 0).cast(pl.Int32).sum().alias("foreign_buy_count")
        )
    if has_vol:
        agg_exprs.append(
            (pl.col("vol_ratio") >= vol_surge_ratio).cast(pl.Int32).sum().alias("vol_surge_count")
        )
    g = df.group_by(["theme", "kind"]).agg(agg_exprs)
    g = g.filter(
        pl.when(pl.col("kind") == SUB_INDUSTRY_KIND)
        .then(pl.col("members_count") >= min_members)
        .otherwise(pl.col("members_count") >= concept_min_members)
    )
    if g.is_empty():
        return _empty
    for col, present in (
        ("inst_buy_count", has_inst),
        ("foreign_buy_count", has_foreign),
        ("vol_surge_count", has_vol),
    ):
        if not present:
            g = g.with_columns(pl.lit(0, dtype=pl.Int32).alias(col))

    members_f = pl.col("members_count").cast(pl.Float64)
    g = g.with_columns(
        [
            (pl.col("inst_buy_count").cast(pl.Float64) / members_f).alias("inst_score"),
            (pl.col("foreign_buy_count").cast(pl.Float64) / members_f).alias("foreign_score"),
            (pl.col("vol_surge_count").cast(pl.Float64) / members_f).alias("vol_surge_score"),
        ]
    )
    log_max = math.log1p(float(g["members_count"].max() or 1))
    mom_score_vals = [_sigmoid(v / 5.0) for v in g["momentum_5d"].to_list()]
    g = g.with_columns(pl.Series("_mom", mom_score_vals, dtype=pl.Float64))
    g = g.with_columns(
        ((pl.col("members_count").cast(pl.Float64) + 1.0).log(base=math.e) / log_max).alias("_sz")
    )
    # 漲幅強度分數（落後鏡頭，Section 2.6/2.7）
    g = g.with_columns(
        (
            0.70 * 100.0 * pl.col("_mom")
            + 0.15 * 100.0 * pl.col("inst_score")
            + 0.15 * 100.0 * pl.col("_sz")
        ).alias("score")
    )
    # 領先分數（領先鏡頭，輪動雷達 Section 2.8）：外資/量能 breadth 主導、動能僅輔
    lw = {"foreign": 0.45, "vol_surge": 0.30, "momentum": 0.25, **(lead_weights or {})}
    g = g.with_columns(
        (
            lw["foreign"] * 100.0 * pl.col("foreign_score")
            + lw["vol_surge"] * 100.0 * pl.col("vol_surge_score")
            + lw["momentum"] * 100.0 * pl.col("_mom")
        ).alias("lead_score")
    ).drop(["_mom", "_sz"])
    return g.sort("score", descending=True)


def attach_rank_delta(
    radar: pl.DataFrame,
    prev: pl.DataFrame | None,
) -> pl.DataFrame:
    """為已按 lead_score 排序、含 radar_rank 的雷達表附上週對週 ΔRank。

    radar 需含 theme / radar_rank；prev 為上週快照（含 theme / radar_rank）。
    rank_delta = 上週 radar_rank − 本週 radar_rank（正＝排名跳升＝輪動進場）；
    無上週快照或該主題上週不在榜 → null（首次跑屬正常，不報錯）。
    """
    null_delta = pl.lit(None, dtype=pl.Int32).alias("rank_delta")
    if radar.is_empty():
        return radar.with_columns(null_delta)
    if (
        prev is None
        or prev.is_empty()
        or "theme" not in prev.columns
        or "radar_rank" not in prev.columns
    ):
        return radar.with_columns(null_delta)
    prev_map = prev.select(
        ["theme", pl.col("radar_rank").cast(pl.Int32).alias("_prev_rank")]
    ).unique(subset=["theme"], keep="first")
    return (
        radar.join(prev_map, on="theme", how="left")
        .with_columns(
            (pl.col("_prev_rank") - pl.col("radar_rank").cast(pl.Int32))
            .cast(pl.Int32)
            .alias("rank_delta")
        )
        .drop("_prev_rank")
    )
