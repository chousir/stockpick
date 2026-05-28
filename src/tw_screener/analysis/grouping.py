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

from tw_screener.analysis.momentum import compute_n_day_return

_DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.50,
    "entry_rate": 0.25,
    "institutional": 0.15,
    "size": 0.10,
}

_MOMENTUM_DAYS = 5

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
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Group screened stocks by industry and compute group strength scores (動能主導).

    institutional: 近 N 日三大法人（含 stock_id / total_net）；提供時計入族群法人強度。
        個股層聚合為 inst_net（近 N 日合計淨買超股數），族群層 inst_score =
        法人買超家數比（inst_net > 0 的家數 / 成員數）。未提供 → inst_net = 0、inst_score = 0。
    volume_history: 含 stock_id / date / trade_volume；提供時計算量比
        （今日量 / 近 vol_lookback 日均量）。未提供 → vol_ratio = 0。
    vol_lookback: 量比均量視窗（交易日）。每檔只取今日之前最近 vol_lookback 天平均，
        與輸入的 volume_history 列數無關 → 不論餵幾天都是一致的 N 日均量。
    g_pullback: 策略 G 的拉回 setup 門檻（見 _DEFAULT_G_PULLBACK）；若 screener 含
        in_g_growth_pullback，G 的有效命中會被收斂為「基本面命中 ∧ 季線上揚 ∧ 乖離帶內 ∧ 量縮」。

    Returns (groups_df, enriched_stocks_df):
    - groups_df: industry-level stats sorted by score desc (filtered by min_group_size)
        欄位：industry_code / industry_name / members_count / total_in_industry /
              entry_rate / momentum_5d（族群中位數）/ up_count / momentum_5d_days_used /
              rs_avg(alias) / inst_buy_count / inst_score / count_{sid} / score
    - enriched_stocks_df: per-stock data with industry, rs (= n_day_return), strategy flags,
        momentum_5d, momentum_days_used, inst_net（三大法人總和）, foreign_net（外資）, trust_net（投信）
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
            if is_etf_or_warrant(stock_id):
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

    # MA20 / MA60 距離 + MA60 斜率（% 偏離最新收盤價 / 季線上揚率）—— 不足則 null
    g_params = {**_DEFAULT_G_PULLBACK, **(g_pullback or {})}
    ma_dist_df = _compute_ma_dist(
        price_history, stock_df, slope_window=int(g_params["ma60_slope_window"])
    )
    stock_df = stock_df.join(ma_dist_df, on="stock_id", how="left")

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
        logger.info("group_stocks: 排除 {} 檔無有效策略命中的宇宙雜訊（多為 G 未過濾股）", dropped_noise)

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
) -> pl.DataFrame:
    """多標籤主題強度排名：候選股依所屬主題（次產業＋概念股）各自分群排名。

    members 需含 stock_id / momentum_5d；themes_long 為 (stock_id, theme, kind) long table，
    一檔可屬多主題、join 後各貢獻一列。因主題只涵蓋部分股、缺「全市場母體數」，故**不用
    入選率**，把其 25% 權重併入動能：
        score = 70 * sigmoid(5日中位/5) + 15 * 法人買超家數比 + 15 * log 規模
    保留門檻依 kind 分流：次產業 >= min_members、概念股 >= concept_min_members。
    回傳 theme / kind / members_count / momentum_5d / up_count / inst_buy_count /
    inst_score / score，按 score 降序。
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
            "score": pl.Float64,
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
    agg_exprs: list[pl.Expr] = [
        pl.col("stock_id").count().alias("members_count"),
        pl.col("momentum_5d").median().alias("momentum_5d"),
        (pl.col("momentum_5d") > 0).cast(pl.Int32).sum().alias("up_count"),
    ]
    if has_inst:
        agg_exprs.append((pl.col("inst_net") > 0).cast(pl.Int32).sum().alias("inst_buy_count"))
    g = df.group_by(["theme", "kind"]).agg(agg_exprs)
    g = g.filter(
        pl.when(pl.col("kind") == SUB_INDUSTRY_KIND)
        .then(pl.col("members_count") >= min_members)
        .otherwise(pl.col("members_count") >= concept_min_members)
    )
    if g.is_empty():
        return _empty
    if not has_inst:
        g = g.with_columns(pl.lit(0, dtype=pl.Int32).alias("inst_buy_count"))

    g = g.with_columns(
        (
            pl.col("inst_buy_count").cast(pl.Float64)
            / pl.col("members_count").cast(pl.Float64)
        ).alias("inst_score")
    )
    log_max = math.log1p(float(g["members_count"].max() or 1))
    mom_score_vals = [_sigmoid(v / 5.0) for v in g["momentum_5d"].to_list()]
    g = g.with_columns(pl.Series("_mom", mom_score_vals, dtype=pl.Float64))
    g = g.with_columns(
        ((pl.col("members_count").cast(pl.Float64) + 1.0).log(base=math.e) / log_max).alias("_sz")
    )
    g = g.with_columns(
        (
            0.70 * 100.0 * pl.col("_mom")
            + 0.15 * 100.0 * pl.col("inst_score")
            + 0.15 * 100.0 * pl.col("_sz")
        ).alias("score")
    ).drop(["_mom", "_sz"])
    return g.sort("score", descending=True)
