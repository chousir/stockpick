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


def load_market_history(cache_dir: Path, n_days: int = 250) -> pl.DataFrame:
    """純讀 daily_*.parquet（含 daily_all_*）→ 全市場 (date, stock_id, close, volume)。

    舊格式缺 volume 時補 null；取最近 n_days 個交易日。無快取回空表。
    """
    frames: list[pl.DataFrame] = []
    for f in sorted(cache_dir.glob("daily_*.parquet")):
        try:
            df = pl.read_parquet(f)
            if not {"date", "stock_id", "close"}.issubset(df.columns):
                continue
            frames.append(
                df.select(
                    [
                        pl.col(c).cast(dt) if c in df.columns else pl.lit(None, dtype=dt).alias(c)
                        for c, dt in _MARKET_SCHEMA.items()
                    ]
                )
            )
        except Exception as e:
            logger.warning("讀取 {} 失敗：{}", f, e)
    if not frames:
        return pl.DataFrame(schema=_MARKET_SCHEMA)
    merged = pl.concat(frames).unique(subset=["date", "stock_id"], keep="first").sort("date")
    recent = merged["date"].unique().sort(descending=True).head(n_days).to_list()
    return merged.filter(pl.col("date").is_in(recent)).sort(["stock_id", "date"])


def compute_subindustry_baskets(
    membership: pl.DataFrame,
    price_history: pl.DataFrame,
) -> pl.DataFrame:
    """次產業等權籃子日報酬（docs/12 §2.2）。

    Args:
        membership: (sub_industry, stock_id) long table（多標籤股在各籃子都計入）
        price_history: (date, stock_id, close, ...) 全市場日線

    Returns:
        (sub_industry, date, basket_return_pct, basket_index, members_priced)
        basket_index 以各籃子首日 = 100 累積。
    """
    if membership.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=_BASKET_SCHEMA)
    rets = (
        price_history.select(["date", "stock_id", "close"])
        .sort(["stock_id", "date"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1.0).alias("_ret")
        )
        .drop_nulls("_ret")
    )
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
) -> pl.DataFrame:
    """次產業法人資金流向衍生訊號（docs/12 §2.3）。

    Args:
        membership: (sub_industry, stock_id)
        institutional: (date, stock_id, foreign_net, trust_net, ..., total_net)
            上市+上櫃合併（load_institutional_history 輸出），單位：股
        volume_history: (date, stock_id, volume) 可選；缺時 concentration 欄為 null
        short_window / long_window: 滾動視窗（交易日），嵌入輸出欄名

    Returns:
        每 (sub_industry, date) 一列：
        - {net,foreign,trust}_flow_{S}d / _{L}d：成員法人淨額滾動加總（股）
        - {flow,foreign,trust}_momentum：短窗淨額 − 前一短窗淨額（加速度）
        - flow_breadth_{S}d / _{L}d、foreign_breadth_{S}d：滾動淨買超成員比 [0,1]
        - flow_concentration_{S}d / _{L}d：法人淨買股數/成交股數（資金力度，±）
        - members：籃子成員數（分母）
    """
    s, lw = short_window, long_window
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

    # 個股層滾動加總 + 動能（短窗 − 前一短窗）
    nets = nets.with_columns(
        [
            pl.col(c).rolling_sum(w, min_samples=1).over("stock_id").alias(f"_{c}_{w}")
            for c in _NET_COLS
            for w in (s, lw)
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
        for w in (s, lw):
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
        latest.sort(by, descending=True)
        .with_row_index("radar_rank", offset=1)
        .rename({"sub_industry": "theme"})
    )
    if prev is not None and "sub_industry" in prev.columns:
        prev = prev.rename({"sub_industry": "theme"})
    return attach_rank_delta(ranked, prev).rename({"theme": "sub_industry"})
