"""次產業資金流向計算層（rotation 引擎 R1，docs/12-sector-rotation.md §2.2-2.3）。

純函式計算 + 薄 IO 載入，研究軌（R2 校準）與生產軌（R3 報表）共用。
視窗參數由 config/settings.yaml 的 `rotation` 段傳入，不寫死；
輸出欄名嵌入實際視窗天數（如 net_flow_5d），改視窗時欄名誠實跟著變。

設計記錄：
- flow_concentration 用「法人淨買股數 / 成交股數」（股/股，與既有「法人佔20日量%」
  概念一致）。docs/12 §2.3 原寫「/成交額」，但 daily_all_* 無成交額、法人淨額為股數，
  股/股才單位一致——此為對規劃書的明文修正。
- 法人快取（T86/TPEX）只列「當日有進出」的股票：缺紀錄＝當日淨額 0（建全網格補 0）。
- breadth 分母＝次產業全成員（含暫無法人紀錄者），絕對水位略偏低但時間變化不失真；
  R2 校準掃門檻時自然吸收。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from loguru import logger

from tw_screener.analysis.grouping import attach_rank_delta
from tw_screener.analysis.strength import clipped_daily_returns
from tw_screener.data.cache import select_recent_cache_files

_NET_COLS = ("total_net", "foreign_net", "trust_net")

# 各 net 欄在輸出的字首（total_net → net_flow、foreign_net → foreign_flow…）
_FLOW_PREFIX = {"total_net": "net_flow", "foreign_net": "foreign_flow", "trust_net": "trust_flow"}
_MOM_PREFIX = {"total_net": "flow", "foreign_net": "foreign", "trust_net": "trust"}

_BASKET_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8,
    "date": pl.Date,
    "basket_return_pct": pl.Float64,
    "basket_index": pl.Float64,
    "members_priced": pl.UInt32,
}

_MARKET_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "stock_id": pl.Utf8,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def load_market_history(
    cache_dir: Path,
    n_days: int = 250,
    patterns: tuple[str, ...] = ("daily_*.parquet", "otc_daily_*.parquet", "stock_day_*.parquet"),
) -> pl.DataFrame:
    """純讀日線快取 → (date, stock_id, close, volume)，取最近 n_days 個交易日。

    來源（預設優先序，同日去重 keep first）：
    1. daily_*.parquet（含 daily_all_*）：上市全市場日快取
    2. otc_daily_*.parquet：上櫃全市場日快取（fetch_otc_daily_all 逐日累積）
    3. stock_day_*.parquet：個股月快取（候選股回補 + backfill-otc-history）
    舊格式缺 volume 時補 null。無快取回空表。

    patterns：要讀的快取檔 glob（預設三來源全收）。個股層研究只框上市時可傳
    `("daily_*.parquet",)` 取上市全市場日線（otc_daily_/stock_day_ 不符此 glob）。
    """
    frames: list[pl.DataFrame] = []

    def _normalize(df: pl.DataFrame) -> pl.DataFrame:
        if "trade_volume" in df.columns and "volume" not in df.columns:
            df = df.rename({"trade_volume": "volume"})
        return df.select(
            [
                pl.col(c).cast(dt) if c in df.columns else pl.lit(None, dtype=dt).alias(c)
                for c, dt in _MARKET_SCHEMA.items()
            ]
        )

    # 先蒐集各 pattern 的檔（維持 pattern 優先序：daily → otc_daily → stock_day，
    # 供 unique(keep="first") 取上市全市場優先），再按檔名日期縮到近 n_days 窗（規劃書 01 P1）。
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(cache_dir.glob(pattern)))
    for f in select_recent_cache_files(candidates, n_days):
        try:
            df = pl.read_parquet(f)
            if not {"date", "stock_id", "close"}.issubset(df.columns):
                continue
            frames.append(_normalize(df))
        except Exception as e:
            logger.warning("讀取 {} 失敗：{}", f, e)
    if not frames:
        return pl.DataFrame(schema=_MARKET_SCHEMA)
    merged = pl.concat(frames).unique(subset=["date", "stock_id"], keep="first").sort("date")
    recent = merged["date"].unique().sort(descending=True).head(n_days).to_list()
    return merged.filter(pl.col("date").is_in(recent)).sort(["stock_id", "date"])


def compute_market_index(
    price_history: pl.DataFrame, clip_daily_return_pct: float = 10.0
) -> pl.DataFrame:
    """等權全市場指數：每日全市場個股日報酬均值累乘，首日 = 100（與次產業籃子同法）。

    日報酬夾限 ±clip%（漲跌停外＝減資/分割/未還原事件，夾住避免毒化指數）；設 0 停用。
    供 regime（趨勢/廣度位階）與 backtest（超額報酬基準）共用大盤基準。

    Returns: (date, market_index) 依日期排序；空輸入回空表（schema 一致）。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return pl.DataFrame(schema={"date": pl.Date, "market_index": pl.Float64})
    daily = (
        clipped_daily_returns(price_history, clip_daily_return_pct)
        .group_by("date")
        .agg(pl.col("_ret").mean().alias("_mkt_ret"))
        .sort("date")
    )
    return daily.with_columns(
        ((pl.col("_mkt_ret") + 1.0).cum_prod() * 100).alias("market_index")
    ).select(["date", "market_index"])


def otc_institutional_lag(cache_dir: Path) -> tuple[int, str | None, str | None]:
    """上櫃法人快取落後上市幾個交易日（TPEX 僅供最新日、缺日不可回補）。

    回傳 (落後交易日數, 上市最新日, 上櫃最新日)；以檔名日期比對。
    供 CLI 顯示警告：落後 ≥1 日代表該期間上櫃股的滾動資金流被低估。
    """
    listed = sorted(f.stem.split("_")[-1] for f in cache_dir.glob("institutional_2*.parquet"))
    otc = sorted(f.stem.split("_")[-1] for f in cache_dir.glob("institutional_otc_*.parquet"))
    if not listed or not otc:
        return (0, listed[-1] if listed else None, otc[-1] if otc else None)
    lag = sum(1 for d in listed if d > otc[-1])
    return (lag, listed[-1], otc[-1])


def compute_subindustry_baskets(
    membership: pl.DataFrame,
    price_history: pl.DataFrame,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """次產業等權籃子日報酬（docs/12 §2.2）。

    Args:
        membership: (sub_industry, stock_id) long table（多標籤股在各籃子都計入）
        price_history: (date, stock_id, close, ...) 全市場日線
        clip_daily_return_pct: 個股日報酬夾限（%）。台股漲跌停 ±10%，逾此＝
            減資/分割/除權息/停牌補跳等未還原事件（如國巨 2025-08-25 單日 −74%），
            夾住避免毒化籃子指數。設 0 停用。

    Returns:
        (sub_industry, date, basket_return_pct, basket_index, members_priced)
        basket_index 以各籃子首日 = 100 累積。
    """
    if membership.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    rets = clipped_daily_returns(price_history, clip_daily_return_pct)
    joined = membership.join(rets, on="stock_id", how="inner")
    if joined.is_empty():
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    return (
        joined.group_by(["sub_industry", "date"])
        .agg(
            (pl.col("_ret").mean() * 100).alias("basket_return_pct"),
            pl.col("stock_id").n_unique().alias("members_priced"),
        )
        .sort(["sub_industry", "date"])
        .with_columns(
            ((pl.col("basket_return_pct") / 100 + 1.0).cum_prod().over("sub_industry") * 100)
            .alias("basket_index")
        )
        .select(list(_BASKET_SCHEMA))
    )


def compute_fund_flows(
    membership: pl.DataFrame,
    institutional: pl.DataFrame,
    volume_history: pl.DataFrame | None = None,
    short_window: int = 5,
    long_window: int = 20,
    windows: tuple[int, ...] | None = None,
) -> pl.DataFrame:
    """次產業法人資金流向衍生訊號（docs/12 §2.3；M-MH Phase D 多窗化）。

    Args:
        membership: (sub_industry, stock_id)
        institutional: (date, stock_id, foreign_net, trust_net, ..., total_net)
            上市+上櫃合併（load_institutional_history 輸出），單位：股
        volume_history: (date, stock_id, volume) 可選；缺時 concentration 欄為 null
        short_window / long_window: momentum / breadth / concentration 的錨窗（交易日）
        windows: 多窗鏡頭集合（交易日）；每窗各產 flow 滾動加總欄。None → (short, long)。
            short/long 一律納入（為子集），故既有 _{S}d/_{L}d 逐值不變（純加法）。

    Returns:
        每 (sub_industry, date) 一列：
        - {net,foreign,trust}_flow_{w}d：各窗成員法人淨額滾動加總（股；含 short/long 子集）
        - {flow,foreign,trust}_momentum：短窗淨額 − 前一短窗淨額（加速度，錨在 short）
        - flow_breadth_{S}d / _{L}d、foreign_breadth_{S}d：滾動淨買超成員比 [0,1]
        - flow_concentration_{S}d / _{L}d：法人淨買股數/成交股數（資金力度，±）
        - members：籃子成員數（分母）
    """
    s, lw = short_window, long_window
    wins = tuple(sorted({s, lw} | set(windows or ())))
    if membership.is_empty() or institutional.is_empty():
        return pl.DataFrame(
            schema={"sub_industry": pl.Utf8, "date": pl.Date, "members": pl.UInt32}
        )

    # 全網格：交易日 × 成員股，無紀錄＝0（T86 只列有進出的股）
    dates = institutional.select(pl.col("date").unique()).sort("date")
    member_ids = membership.select(pl.col("stock_id").unique())
    nets = (
        dates.join(member_ids, how="cross")
        .join(
            institutional.select(["date", "stock_id", *_NET_COLS]).unique(
                subset=["date", "stock_id"]
            ),
            on=["date", "stock_id"],
            how="left",
        )
        .with_columns([pl.col(c).fill_null(0) for c in _NET_COLS])
        .sort(["stock_id", "date"])
    )

    # 個股層滾動加總（各窗）+ 動能（短窗 − 前一短窗）
    nets = nets.with_columns(
        [
            pl.col(c).rolling_sum(w, min_samples=1).over("stock_id").alias(f"_{c}_{w}")
            for c in _NET_COLS
            for w in wins
        ]
    ).with_columns(
        [
            (pl.col(f"_{c}_{s}") - pl.col(f"_{c}_{s}").shift(s).over("stock_id")).alias(
                f"_{c}_mom"
            )
            for c in _NET_COLS
        ]
    )

    # 成交量滾動加總（concentration 分母；缺 volume → null）
    has_vol = volume_history is not None and not volume_history.is_empty()
    if has_vol:
        vol = (
            volume_history.select(["date", "stock_id", "volume"])
            .unique(subset=["date", "stock_id"])
            .sort(["stock_id", "date"])
        )
        nets = nets.join(vol, on=["date", "stock_id"], how="left").with_columns(
            [
                pl.col("volume")
                .fill_null(0)
                .rolling_sum(w, min_samples=1)
                .over("stock_id")
                .alias(f"_vol_{w}")
                for w in (s, lw)
            ]
        )

    grouped = membership.join(nets, on="stock_id", how="inner")

    aggs: list[pl.Expr] = [pl.col("stock_id").n_unique().alias("members")]
    for c in _NET_COLS:
        fp = _FLOW_PREFIX[c]
        for w in wins:
            aggs.append(pl.col(f"_{c}_{w}").sum().alias(f"{fp}_{w}d"))
        aggs.append(pl.col(f"_{c}_mom").sum().alias(f"{_MOM_PREFIX[c]}_momentum"))
    # breadth：滾動淨買超成員比
    aggs += [
        (pl.col(f"_total_net_{s}") > 0).mean().alias(f"flow_breadth_{s}d"),
        (pl.col(f"_total_net_{lw}") > 0).mean().alias(f"flow_breadth_{lw}d"),
        (pl.col(f"_foreign_net_{s}") > 0).mean().alias(f"foreign_breadth_{s}d"),
    ]
    # concentration：淨買股數/成交股數
    for w in (s, lw):
        if has_vol:
            aggs.append(
                (
                    pl.when(pl.col(f"_vol_{w}").sum() > 0)
                    .then(pl.col(f"_total_net_{w}").sum() / pl.col(f"_vol_{w}").sum())
                    .otherwise(None)
                ).alias(f"flow_concentration_{w}d")
            )
        else:
            aggs.append(pl.lit(None, dtype=pl.Float64).alias(f"flow_concentration_{w}d"))

    return grouped.group_by(["sub_industry", "date"]).agg(aggs).sort(["sub_industry", "date"])


def rank_flows(
    flows: pl.DataFrame,
    by: str = "net_flow_20d",
    prev: pl.DataFrame | None = None,
    min_members: int = 1,
) -> pl.DataFrame:
    """最新一日的次產業資金流向排名快照 + 週對週 ΔRank。

    ΔRank 復用 grouping.attach_rank_delta（theme/radar_rank 介面），不另寫一套。
    prev 為上次快照（含 sub_industry 或 theme、radar_rank 欄）；無 → rank_delta 全 null。
    """
    if flows.is_empty():
        return flows
    latest = flows.filter(pl.col("date") == flows["date"].max()).filter(
        pl.col("members") >= min_members
    )
    ranked = (
        latest.sort(by, descending=True, nulls_last=True)
        .with_row_index("radar_rank", offset=1)
        .rename({"sub_industry": "theme"})
    )
    if prev is not None and "sub_industry" in prev.columns:
        prev = prev.rename({"sub_industry": "theme"})
    return attach_rank_delta(ranked, prev).rename({"theme": "sub_industry"})


# ─── F3 價格趨勢分數＋趨勢領頭板（規劃書 05 F3）──────────────────────────────

_TREND_WEIGHTS_DEFAULT = {"basket_ma": 0.40, "member_breadth": 0.35, "leader_rs": 0.25}


def _stock_window_returns(price_history: pl.DataFrame, rs_window: int) -> pl.DataFrame:
    """as-of（資料最新日）每檔 rs_window 交易日報酬（%）＋ 對全市場中位的 RS。

    close ≤ 0 髒資料先濾（0 收盤曾毒化等權指數，5ae7a94 同類防護）；
    視窗內資料不足（新股/停牌）→ 該檔無報酬、不進 RS 宇宙。
    """
    px = price_history.filter(pl.col("close") > 0).sort(["stock_id", "date"])
    if px.is_empty():
        return pl.DataFrame(
            schema={"stock_id": pl.Utf8, "ret_pct": pl.Float64, "rs_pct": pl.Float64}
        )
    last_date = px["date"].max()
    snap = (
        px.with_columns(
            ((pl.col("close") / pl.col("close").shift(rs_window).over("stock_id")) - 1.0)
            .alias("_ret")
        )
        .filter((pl.col("date") == last_date) & pl.col("_ret").is_not_null())
    )
    if snap.is_empty():
        return pl.DataFrame(
            schema={"stock_id": pl.Utf8, "ret_pct": pl.Float64, "rs_pct": pl.Float64}
        )
    median_ret = snap["_ret"].median()
    return snap.select(
        "stock_id",
        (pl.col("_ret") * 100).alias("ret_pct"),
        ((pl.col("_ret") - median_ret) * 100).alias("rs_pct"),
    )


def compute_trend_scores(
    baskets: pl.DataFrame,
    membership: pl.DataFrame,
    price_history: pl.DataFrame,
    ma_short: int = 20,
    ma_long: int = 60,
    rs_window: int = 20,
    weights: dict[str, float] | None = None,
) -> pl.DataFrame:
    """F3 價格證據優先的族群趨勢分數（0–100，輪動主排序鍵）。

    動機（規劃書 05 §1.5#2）：20 日流量排序天然落後——航空/金融 W23–W24 價格已
    走出來、流量榜 W27 才浮出。改以價格證據為主鍵、流量降為確認欄：
      basket_ma      籃子等權指數 vs 自身 MA{短}/MA{長}（各占一半）
      member_breadth 成員（as-of 有價者）站上自身 MA{長} 比例
      leader_rs      領頭成員 RS（rs_window 報酬 − 全市場中位）跨次產業百分位
    權重自動正規化；成分缺值以 0 計（誠實降級）；資料未滿窗時 MA 用可得窗。
    as-of＝輸入資料最新日——回放歷史週：把 baskets/price_history 濾到目標日即可。

    Returns:
        每次產業一列：sub_industry / trend_score / trend_ma_state（雙線上｜月線上｜
        季線上｜雙線下）/ above_ma60_breadth [0,1] / leader_stock_id / leader_rs_pct
    """
    if baskets.is_empty() or membership.is_empty() or price_history.is_empty():
        return pl.DataFrame(
            schema={
                "sub_industry": pl.Utf8,
                "trend_score": pl.Float64,
                "trend_ma_state": pl.Utf8,
                "above_ma60_breadth": pl.Float64,
                "leader_stock_id": pl.Utf8,
                "leader_rs_pct": pl.Float64,
            }
        )
    w = {**_TREND_WEIGHTS_DEFAULT, **(weights or {})}
    total = sum(w.values()) or 1.0
    w = {k: v / total for k, v in w.items()}

    # ① 籃子指數 vs 自身雙均線
    b = baskets.sort(["sub_industry", "date"]).with_columns(
        pl.col("basket_index").rolling_mean(ma_short, min_samples=1)
        .over("sub_industry").alias("_ma_s"),
        pl.col("basket_index").rolling_mean(ma_long, min_samples=1)
        .over("sub_industry").alias("_ma_l"),
    )
    ma = (
        b.filter(pl.col("date") == pl.col("date").max().over("sub_industry"))
        .select(
            "sub_industry",
            (pl.col("basket_index") > pl.col("_ma_s")).alias("_above_s"),
            (pl.col("basket_index") > pl.col("_ma_l")).alias("_above_l"),
        )
        .with_columns(
            (pl.col("_above_s").cast(pl.Float64) * 0.5
             + pl.col("_above_l").cast(pl.Float64) * 0.5).alias("_c_ma"),
            pl.when(pl.col("_above_s") & pl.col("_above_l")).then(pl.lit("雙線上"))
            .when(pl.col("_above_s")).then(pl.lit("月線上"))
            .when(pl.col("_above_l")).then(pl.lit("季線上"))
            .otherwise(pl.lit("雙線下")).alias("trend_ma_state"),
        )
    )

    # ② 成員站上自身 MA{長} 比例（as-of 有價成員為分母）
    px = price_history.filter(pl.col("close") > 0).sort(["stock_id", "date"])
    px = px.with_columns(
        pl.col("close").rolling_mean(ma_long, min_samples=1).over("stock_id").alias("_ma60")
    )
    snap = px.filter(pl.col("date") == px["date"].max())
    breadth = (
        membership.join(snap, on="stock_id", how="inner")
        .group_by("sub_industry")
        .agg((pl.col("close") > pl.col("_ma60")).mean().alias("above_ma60_breadth"))
    )

    # ③ 領頭成員 RS 跨次產業百分位
    rs = _stock_window_returns(price_history, rs_window)
    leaders = (
        membership.join(rs, on="stock_id", how="inner")
        .sort("rs_pct", descending=True)
        .group_by("sub_industry", maintain_order=True)
        .agg(
            pl.col("stock_id").first().alias("leader_stock_id"),
            pl.col("rs_pct").first().alias("leader_rs_pct"),
        )
    )
    if not leaders.is_empty() and leaders.height > 1:
        leaders = leaders.with_columns(
            ((pl.col("leader_rs_pct").rank(method="average") - 1) / (leaders.height - 1))
            .alias("_c_rs")
        )
    else:
        leaders = leaders.with_columns(pl.lit(0.5).alias("_c_rs"))

    out = (
        ma.join(breadth, on="sub_industry", how="full", coalesce=True)
        .join(leaders, on="sub_industry", how="full", coalesce=True)
        .with_columns(
            (
                (
                    pl.col("_c_ma").fill_null(0.0) * w["basket_ma"]
                    + pl.col("above_ma60_breadth").fill_null(0.0) * w["member_breadth"]
                    + pl.col("_c_rs").fill_null(0.0) * w["leader_rs"]
                )
                * 100
            )
            .round(1)
            .alias("trend_score"),
            pl.col("leader_rs_pct").round(1),
        )
    )
    return out.select(
        "sub_industry", "trend_score", "trend_ma_state",
        "above_ma60_breadth", "leader_stock_id", "leader_rs_pct",
    ).sort("trend_score", descending=True)


def compute_trend_leaders(
    membership: pl.DataFrame,
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
    top_n: int = 15,
    rs_window: int = 20,
    min_amount_million: float = 100.0,
    overheat_ma60_pct: float = 40.0,
    cross_trade_lots: float = 5000.0,
    cross_trade_rel_pct: float = 4.0,
    ma_long: int = 60,
) -> pl.DataFrame:
    """F3 趨勢領頭板：全市場 RS 前 N 強＋所屬次產業＋位階＋旗標。

    解「找不到趨勢標的」＋給 F2 被擋的延伸股合法出口（規劃書 05 §1.5）。
    **過熱／土洋對作不剔除、只標註**（旗標口徑同 propicks_flags：過熱＝距季線 >
    overheat_ma60_pct；土洋對作＝外資/投信 20 日反向、雙邊 ≥ cross_trade_lots 張
    且弱邊 ≥ 近 20 日成交量 cross_trade_rel_pct%）。妖股濾除：近 20 日均成交額 <
    min_amount_million（百萬）不入板（量能未知亦排除——寧缺勿雜）。
    institutional 空 → 土洋對作欄如實缺（只標過熱）。

    Returns:
        RS 遞減前 top_n：stock_id / sub_industry（多標籤「、」併列，未標＝空）/
        ret_pct（rs_window 報酬）/ rs_pct / dist_ma60_pct / flags
    """
    schema = {
        "stock_id": pl.Utf8, "sub_industry": pl.Utf8, "ret_pct": pl.Float64,
        "rs_pct": pl.Float64, "dist_ma60_pct": pl.Float64, "flags": pl.Utf8,
    }
    if price_history.is_empty():
        return pl.DataFrame(schema=schema)

    px = price_history.filter(pl.col("close") > 0).sort(["stock_id", "date"])
    px = px.with_columns(
        pl.col("close").rolling_mean(ma_long, min_samples=1).over("stock_id").alias("_ma60"),
        (pl.col("close") * pl.col("volume"))
        .rolling_mean(20, min_samples=1).over("stock_id").alias("_amt20"),
        pl.col("volume").rolling_sum(20, min_samples=1).over("stock_id").alias("_vol20"),
    )
    snap = px.filter(pl.col("date") == px["date"].max())
    rs = _stock_window_returns(price_history, rs_window)
    board = (
        snap.join(rs, on="stock_id", how="inner")
        .filter(pl.col("_amt20") >= min_amount_million * 1e6)
        .with_columns(((pl.col("close") / pl.col("_ma60") - 1) * 100).alias("dist_ma60_pct"))
        .sort("rs_pct", descending=True)
        .head(top_n)
    )
    if board.is_empty():
        return pl.DataFrame(schema=schema)

    # 旗標（只標註不剔除）：過熱＋土洋對作
    if not institutional.is_empty():
        recent = institutional.sort("date")
        cutoff_dates = recent["date"].unique().sort(descending=True).head(20).to_list()
        flows20 = (
            recent.filter(pl.col("date").is_in(cutoff_dates))
            .group_by("stock_id")
            .agg(
                pl.col("foreign_net").sum().alias("_f20"),
                pl.col("trust_net").sum().alias("_t20"),
            )
        )
        board = board.join(flows20, on="stock_id", how="left")
    else:
        board = board.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("_f20"),
            pl.lit(None, dtype=pl.Float64).alias("_t20"),
        )

    min_shares = cross_trade_lots * 1000
    weak_side = pl.min_horizontal(pl.col("_f20").abs(), pl.col("_t20").abs())
    cross = (
        (pl.col("_f20") * pl.col("_t20") < 0)
        & (weak_side >= min_shares)
        & (weak_side >= pl.col("_vol20") * cross_trade_rel_pct / 100)
    ).fill_null(False)
    overheat = (pl.col("dist_ma60_pct") > overheat_ma60_pct).fill_null(False)
    board = board.with_columns(
        pl.when(overheat & cross).then(pl.lit("過熱;土洋對作"))
        .when(overheat).then(pl.lit("過熱"))
        .when(cross).then(pl.lit("土洋對作"))
        .otherwise(pl.lit("")).alias("flags")
    )

    subs = membership.group_by("stock_id").agg(
        pl.col("sub_industry").sort().str.join("、").alias("sub_industry")
    )
    return (
        board.join(subs, on="stock_id", how="left")
        .with_columns(
            pl.col("sub_industry").fill_null(""),
            pl.col("ret_pct").round(1),
            pl.col("rs_pct").round(1),
            pl.col("dist_ma60_pct").round(1),
        )
        .select(list(schema))
    )
