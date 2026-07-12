"""backtest/rotation_efficacy.py — WS-C 族群輪動欄效度（forward basket 檢驗）。

生產快照只有 W24+（5 週、r+20 幾乎全未到期）——樣本天花板太低。本模組把
rotation 各欄（趨勢分／四象限／★entry／ΔRank／flow_turn／freshness）**歷史重建**
成逐週 point-in-time 序列（~50 週 × 46 族群），對面板 forward basket 報酬檢驗。

無前視保證：
- flows z＝rolling（rotation_calib.standardize_signals），每日值只看自身尾窗；
- 位階/趨勢分全部 rolling 化（與生產「as-of＝輸入最新日」語義逐日等價；
  compute_trend_scores docstring 明言回放＝濾到目標日）；
- forward 報酬直接取面板 r{h}/alpha{h}（entry=次一交易日，WS-A2 已驗）。

重建正確性驗法：與 W24+ 生產 sector_rotation.csv 同日對表（trend_score 差、
quadrant/freshness/flow_turn 一致率）——對不上＝重建作廢，不硬用。

純函式；IO 由 rotation_efficacy_runner 負責。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from tw_screener.backtest.factor_lab import bootstrap_mean_ci

_TREND_WEIGHTS = {"basket_ma": 0.40, "member_breadth": 0.35, "leader_rs": 0.25}
Q_NEXT, Q_TREND, Q_DISTRIBUTE, Q_COOL = "下一棒", "主升續勢", "出貨警訊", "退潮觀察"

# WS-J.2 membership robustness：族群層 runner 的 membership 來源選項。
MEMBERSHIP_SOURCES = ("concepts", "official")


def weekly_snapshot_dates(dates: list[date]) -> list[date]:
    """每 ISO 週最後一個交易日（鏡射週報節奏；避免日頻重疊窗灌水）。"""
    if not dates:
        return []
    df = pl.DataFrame({"date": sorted(set(dates))})
    return (
        df.with_columns(
            pl.col("date").dt.year().alias("_y"), pl.col("date").dt.week().alias("_w")
        )
        .group_by("_y", "_w")
        .agg(pl.col("date").max())
        .sort("date")["date"]
        .to_list()
    )


def basket_position_series(baskets: pl.DataFrame, position_window: int = 60) -> pl.DataFrame:
    """逐日位階：above_low_pct＝籃子指數距 position_window 日低 %（NaN → null）。

    與 rotation_report._basket_position 同語義的 rolling 版（該函式只算最新日）。
    """
    if baskets.is_empty():
        return pl.DataFrame(
            schema={"sub_industry": pl.Utf8, "date": pl.Date, "above_low_pct": pl.Float64}
        )
    idx = pl.col("basket_index")
    low = idx.rolling_min(position_window, min_samples=1).over("sub_industry")
    return (
        baskets.sort(["sub_industry", "date"])
        .with_columns(((idx / low - 1) * 100).alias("above_low_pct"))
        .with_columns(
            pl.when(pl.col("above_low_pct").is_nan())
            .then(None)
            .otherwise(pl.col("above_low_pct"))
            .alias("above_low_pct")
        )
        .select("sub_industry", "date", "above_low_pct")
    )


def trend_score_series(
    price: pl.DataFrame,
    membership: pl.DataFrame,
    baskets: pl.DataFrame,
    ma_short: int = 20,
    ma_long: int = 60,
    rs_window: int = 20,
) -> pl.DataFrame:
    """逐日 F3 趨勢分（compute_trend_scores 的 rolling 鏡像；權重同生產預設）。

    成分（各自逐日）：
    - c_ma：籃子指數 > 自身 MA{短}/MA{長} 各 0.5（min_samples=1，同生產）；
    - breadth：成員 close > 自身 MA{長}（min_samples=1）比例；
    - c_rs：領頭成員 RS（rs_window 報酬 − 全市場中位）跨次產業百分位。
    """
    schema = {"sub_industry": pl.Utf8, "date": pl.Date, "trend_score": pl.Float64}
    if price.is_empty() or membership.is_empty() or baskets.is_empty():
        return pl.DataFrame(schema=schema)
    w = _TREND_WEIGHTS

    idx = pl.col("basket_index")
    ma = (
        baskets.sort(["sub_industry", "date"])
        .with_columns(
            (idx > idx.rolling_mean(ma_short, min_samples=1).over("sub_industry"))
            .alias("_above_s"),
            (idx > idx.rolling_mean(ma_long, min_samples=1).over("sub_industry"))
            .alias("_above_l"),
        )
        .select(
            "sub_industry", "date",
            (pl.col("_above_s").cast(pl.Float64) * 0.5
             + pl.col("_above_l").cast(pl.Float64) * 0.5).alias("_c_ma"),
        )
    )

    px = (
        price.filter(pl.col("close") > 0)
        .sort(["stock_id", "date"])
        .with_columns(
            (pl.col("close")
             > pl.col("close").rolling_mean(ma_long, min_samples=1).over("stock_id"))
            .alias("_above_ma_l"),
            ((pl.col("close") / pl.col("close").shift(rs_window).over("stock_id")) - 1.0)
            .alias("_ret_w"),
        )
    )
    member_px = membership.join(px, on="stock_id", how="inner")
    breadth = (
        member_px.group_by("sub_industry", "date")
        .agg(pl.col("_above_ma_l").mean().alias("_breadth"))
    )
    # RS：全市場（price 宇宙）同日中位為基準 → 領頭成員 → 跨次產業百分位
    rs = px.drop_nulls("_ret_w").with_columns(
        (pl.col("_ret_w") - pl.col("_ret_w").median().over("date")).alias("_rs")
    )
    leader = (
        membership.join(rs, on="stock_id", how="inner")
        .group_by("sub_industry", "date")
        .agg(pl.col("_rs").max().alias("_leader_rs"))
    )
    n_subs = pl.col("_leader_rs").count().over("date")
    leader = leader.with_columns(
        pl.when(n_subs > 1)
        .then((pl.col("_leader_rs").rank(method="average").over("date") - 1) / (n_subs - 1))
        .otherwise(0.5)
        .alias("_c_rs")
    )

    keys = ["sub_industry", "date"]
    return (
        ma.join(breadth, on=keys, how="left")
        .join(leader.select("sub_industry", "date", "_c_rs"), on=keys, how="left")
        .with_columns(
            (
                (
                    pl.col("_c_ma").fill_null(0.0) * w["basket_ma"]
                    + pl.col("_breadth").fill_null(0.0) * w["member_breadth"]
                    + pl.col("_c_rs").fill_null(0.0) * w["leader_rs"]
                )
                * 100
            )
            .round(1)
            .alias("trend_score")
        )
        .select(list(schema))
        .sort(["sub_industry", "date"])
    )


def rotation_signal_panel(
    flows_z: pl.DataFrame,
    position: pl.DataFrame,
    trend: pl.DataFrame,
    snapshot_dates: list[date],
    short_window: int = 5,
    long_window: int = 20,
    position_low_pct: float = 10.0,
    entry_col: str = "trust_flow_20d_z",
    entry_threshold: float = 1.0,
    min_members: int = 5,
) -> pl.DataFrame:
    """逐週 point-in-time 輪動訊號面板（quadrant/freshness/flow_turn 表達式同生產）。

    on_board＝members ≥ min_members（生產 rank_flows 榜外規則）；radar_rank 只在
    on_board 內按 trend_score 排（同生產 rank_by）；rank_delta＝上週名次 − 本週
    （正＝上升）。榜外列保留（機會成本分析的主角），rank 欄 null。
    """
    if flows_z.is_empty() or not snapshot_dates:
        return pl.DataFrame()
    s, lw = short_window, long_window
    snap = flows_z.filter(pl.col("date").is_in(snapshot_dates))
    snap = snap.join(position, on=["sub_industry", "date"], how="left")
    if not trend.is_empty():
        snap = snap.join(trend, on=["sub_industry", "date"], how="left")
    else:
        snap = snap.with_columns(pl.lit(None, dtype=pl.Float64).alias("trend_score"))

    inflow = pl.col(f"net_flow_{lw}d") > 0
    risen = pl.col("above_low_pct") > position_low_pct
    pos_unknown = pl.col("above_low_pct").is_null()
    long_flow, short_flow = pl.col(f"net_flow_{lw}d"), pl.col(f"net_flow_{s}d")
    entry = (
        (pl.col(entry_col) > entry_threshold).fill_null(False)
        if entry_col in snap.columns
        else pl.lit(False)
    )
    snap = snap.with_columns(
        pl.when(pos_unknown).then(pl.lit(None, dtype=pl.Utf8))
        .when(inflow & ~risen).then(pl.lit(Q_NEXT))
        .when(inflow & risen).then(pl.lit(Q_TREND))
        .when(~inflow & risen).then(pl.lit(Q_DISTRIBUTE))
        .otherwise(pl.lit(Q_COOL))
        .alias("quadrant"),
        pl.when(pl.col("flow_momentum") > 0).then(pl.lit("加速"))
        .when(pl.col("flow_momentum") < 0).then(pl.lit("放緩"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
        .alias("freshness"),
        pl.when((long_flow < 0) & (short_flow > 0)).then(pl.lit("資金回流"))
        .when((long_flow > 0) & (short_flow < 0)).then(pl.lit("退潮"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
        .alias("flow_turn"),
        entry.alias("entry_triggered"),
        (pl.col("members") >= min_members).alias("on_board"),
    )
    ranked = snap.with_columns(
        pl.when(pl.col("on_board") & pl.col("trend_score").is_not_null())
        .then(
            pl.col("trend_score")
            .rank(method="ordinal", descending=True)
            .over("date", "on_board")
        )
        .otherwise(None)
        .alias("radar_rank")
    ).sort(["sub_industry", "date"])
    return ranked.with_columns(
        (pl.col("radar_rank").shift(1).over("sub_industry") - pl.col("radar_rank"))
        .alias("rank_delta")
    )


def basket_forward_returns(
    panel: pl.DataFrame,
    membership: pl.DataFrame,
    horizons: tuple[int, ...] = (5, 10, 20),
    min_members: int = 3,
) -> pl.DataFrame:
    """(sub_industry, date) 等權 forward basket 報酬：成員 r{h}/alpha{h} 平均。

    有 r{h} 的成員 < min_members → 該欄 null（樣本太薄不硬算）。
    """
    if panel.is_empty() or membership.is_empty():
        return pl.DataFrame()
    cols = [c for h in horizons for c in (f"r{h}", f"alpha{h}")]
    member_rows = membership.join(
        panel.select("date", "stock_id", *cols), on="stock_id", how="inner"
    )
    aggs: list[pl.Expr] = []
    for h in horizons:
        n_ok = pl.col(f"r{h}").is_not_null().sum()
        aggs += [
            pl.when(n_ok >= min_members).then(pl.col(f"r{h}").mean())
            .otherwise(None).alias(f"basket_r{h}"),
            pl.when(n_ok >= min_members).then(pl.col(f"alpha{h}").mean())
            .otherwise(None).alias(f"basket_alpha{h}"),
            n_ok.cast(pl.UInt32).alias(f"n_ret{h}"),
        ]
    return member_rows.group_by("sub_industry", "date").agg(aggs).sort(
        ["sub_industry", "date"]
    )


def category_lift_table(
    df: pl.DataFrame,
    cat_col: str,
    target: str,
    n_boot: int = 1000,
    include_null_as: str | None = None,
) -> pl.DataFrame:
    """類別欄 lift 表：各類 n／mean＋bootstrap CI95／median／win_rate＋全體基準列。

    include_null_as：null 類別要不要當一類報（如 flow_turn 的「同號」）；None＝丟棄。
    """
    schema = {
        "category": pl.Utf8, "n": pl.UInt32, "mean": pl.Float64,
        "ci_lo": pl.Float64, "ci_hi": pl.Float64,
        "median": pl.Float64, "win_rate": pl.Float64,
    }
    if df.is_empty() or not {cat_col, target}.issubset(df.columns):
        return pl.DataFrame(schema=schema)
    base = df.drop_nulls([target])
    if include_null_as is not None:
        base = base.with_columns(pl.col(cat_col).fill_null(include_null_as))
    else:
        base = base.drop_nulls([cat_col])
    if base.is_empty():
        return pl.DataFrame(schema=schema)
    rows: list[dict] = []
    groups = [("（全體）", base)] + [
        (str(k[0]), g) for k, g in base.group_by(cat_col, maintain_order=True)
    ]
    for label, g in groups:
        vals = [float(v) for v in g[target].to_list() if v is not None]
        lo, hi = bootstrap_mean_ci(vals, n_boot=n_boot)
        med = pl.Series(vals).median() if vals else None
        rows.append(
            {
                "category": label,
                "n": len(vals),
                "mean": (sum(vals) / len(vals)) if vals else None,
                "ci_lo": lo,
                "ci_hi": hi,
                "median": float(med) if isinstance(med, (int, float)) else None,
                "win_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
            }
        )
    return pl.DataFrame(rows, schema=schema).sort(
        pl.col("category") == "（全體）", descending=True
    )


def compare_with_production(
    recon: pl.DataFrame, production: pl.DataFrame
) -> pl.DataFrame:
    """重建 vs 生產快照同日對表：trend_score 平均絕對差＋類別欄一致率。

    production＝各週 sector_rotation.csv concat（需含 date/sub_industry 欄）。
    對不上（一致率低）＝重建作廢的證據，如實回報。
    """
    schema = {
        "date": pl.Date, "n": pl.UInt32, "trend_mad": pl.Float64,
        "quadrant_match": pl.Float64, "freshness_match": pl.Float64,
        "flow_turn_match": pl.Float64,
    }
    need = {"date", "sub_industry"}
    if recon.is_empty() or production.is_empty() or not need.issubset(production.columns):
        return pl.DataFrame(schema=schema)
    joined = recon.join(
        production.select(
            "date", "sub_industry",
            pl.col("trend_score").alias("_p_trend"),
            pl.col("quadrant").alias("_p_quad"),
            pl.col("freshness").alias("_p_fresh"),
            pl.col("flow_turn").alias("_p_turn"),
        ),
        on=["date", "sub_industry"],
        how="inner",
    )
    if joined.is_empty():
        return pl.DataFrame(schema=schema)

    def _match(a: str, b: str) -> pl.Expr:
        both_null = pl.col(a).is_null() & pl.col(b).is_null()
        return (both_null | (pl.col(a) == pl.col(b))).mean()

    return (
        joined.group_by("date")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            (pl.col("trend_score") - pl.col("_p_trend")).abs().mean().alias("trend_mad"),
            _match("quadrant", "_p_quad").alias("quadrant_match"),
            _match("freshness", "_p_fresh").alias("freshness_match"),
            _match("flow_turn", "_p_turn").alias("flow_turn_match"),
        )
        .sort("date")
        .select(list(schema))
    )


def official_membership_frame(industry: pl.DataFrame) -> pl.DataFrame:
    """load_industry_mapping() 輸出 → 與 list_subindustries() 同 schema 的 membership frame。

    WS-J.2 membership robustness：以 TWSE/OTC 官方產業別（粗分類）取代手標次產業，
    industry_name 直接當 sub_industry 用——粒度差異（如官方「電子零組件業」大類 vs
    手標「IC設計」細分）如實使用，不做映射補償，這正是 robustness 測試的目的。

    industry_name 缺（null 或空字串，如新股尚未歸類）者誠實排除、不兜底；呼叫端可用
    `industry.height − 回傳值.height` 算出被排除檔數並記入報告（docs/22 §7.3 誠實帳）。
    """
    schema = {"sub_industry": pl.Utf8, "stock_id": pl.Utf8}
    if industry.is_empty():
        return pl.DataFrame(schema=schema)
    return (
        industry.filter(
            pl.col("industry_name").is_not_null() & (pl.col("industry_name") != "")
        )
        .select(pl.col("industry_name").alias("sub_industry"), pl.col("stock_id"))
        .unique(maintain_order=True)
        .sort(["sub_industry", "stock_id"])
    )
