"""個股特徵面板（CP 值補漲研究 B1，docs/13-cp-value-research.md §4 Phase B）。

從現有日線 + 法人快取，建 (date, stock_id) → 特徵 長表，供 B2 個股層 event study /
B3 候選清單使用。純函式計算 + 薄 coverage meta，全離線、不打外部、不新增資料源。

設計記錄（與規格的明文出入，皆經使用者確認 2026-06-14）：
- 量能因子：規格寫「周轉率 z」，但真周轉率需流通在外股數（市值資料仍是缺口），
  故降級為「成交量 z」（個股自身短窗均量的滾動 z 分數）。meta 明標非真周轉率。
- 相對強度 vs 大盤：依 docs/13 §5「不新增資料源」，大盤基準＝由 daily_* 算的
  「等權全上市指數」（與 rotation 次產業籃子同法），非加權指數。
- 多標籤股 vs 次產業 RS：對其所屬各籃子的同窗報酬取平均，panel 維持單列鍵
  (date, stock_id)；無標股 → rs_subind null（不硬塞「無法判讀」）。
- 覆蓋：個股層只框上市（daily_*＋法人全覆蓋）；上櫃有缺口故由呼叫端排除，
  覆蓋率寫進 compute_coverage_meta。
- 法人快取只列「當日有進出」者：缺紀錄＝當日淨額 0（沿用 rotation 補 0 慣例）。
- 欄名嵌入實際視窗天數（如 net_flow_20d、above_low_60d_pct），改視窗欄名誠實跟著變。
"""

from __future__ import annotations

import polars as pl

# 與 rotation.py 一致的法人欄字首映射（不另立一套命名）
_NET_COLS = ("total_net", "foreign_net", "trust_net")
_FLOW_PREFIX = {"total_net": "net_flow", "foreign_net": "foreign_flow", "trust_net": "trust_flow"}
_MOM_PREFIX = {"total_net": "flow", "foreign_net": "foreign", "trust_net": "trust"}


def _rolling_z(col: str, window: int, min_periods: int, over: str = "stock_id") -> pl.Expr:
    """個股自身滾動 z 分數（與 backtest.standardize_signals 同義，改 partition 為個股）。

    std=0 或暖機（樣本 < min_periods）→ null，不誤判為訊號。
    """
    mean = pl.col(col).rolling_mean(window, min_samples=min_periods).over(over)
    std = pl.col(col).rolling_std(window, min_samples=min_periods).over(over)
    return pl.when(std > 0).then((pl.col(col) - mean) / std).otherwise(None)


def _market_returns(
    price_history: pl.DataFrame, rs_window: int, clip_daily_return_pct: float
) -> pl.DataFrame:
    """等權全市場 rs_window 日報酬（每日一列）。

    與 rotation.compute_subindustry_baskets 同法：個股日報酬夾 ±漲跌停後等權平均成
    市場日報酬 → cum_prod 成指數 → 取 rs_window 日報酬。回傳 (date, market_ret)。
    """
    ret = pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1.0
    if clip_daily_return_pct > 0:
        bound = clip_daily_return_pct / 100
        ret = ret.clip(-bound, bound)
    daily = (
        price_history.select(["date", "stock_id", "close"])
        .sort(["stock_id", "date"])
        .with_columns(ret.alias("_r"))
        .drop_nulls("_r")
        .group_by("date")
        .agg(pl.col("_r").mean().alias("_mkt_r"))
        .sort("date")
        .with_columns(((pl.col("_mkt_r") + 1.0).cum_prod() * 100).alias("_mkt_idx"))
    )
    return daily.with_columns(
        ((pl.col("_mkt_idx") / pl.col("_mkt_idx").shift(rs_window) - 1) * 100).alias("market_ret")
    ).select(["date", "market_ret"])


def _subindustry_returns(
    baskets: pl.DataFrame, membership: pl.DataFrame, rs_window: int
) -> pl.DataFrame:
    """個股所屬次產業籃子的 rs_window 日報酬，多標籤取平均 → (stock_id, date, subind_ret)。"""
    prev_idx = pl.col("basket_index").shift(rs_window).over("sub_industry")
    b = baskets.sort(["sub_industry", "date"]).with_columns(
        ((pl.col("basket_index") / prev_idx - 1) * 100).alias("_b_ret")
    )
    joined = membership.join(
        b.select(["sub_industry", "date", "_b_ret"]), on="sub_industry", how="inner"
    )
    return (
        joined.group_by(["stock_id", "date"])
        .agg(pl.col("_b_ret").mean().alias("subind_ret"))
    )


def build_stock_panel(
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
    membership: pl.DataFrame,
    baskets: pl.DataFrame,
    short_window: int = 5,
    long_window: int = 20,
    windows: tuple[int, ...] | None = None,
    ma_windows: tuple[int, ...] = (20, 60, 240),
    position_window: int = 60,
    rs_window: int = 20,
    z_window: int = 60,
    z_min_periods: int = 30,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """個股每日特徵長表（docs/13 §4 B1；M-MH Phase D 多窗化）。

    Args:
        price_history: (date, stock_id, close, volume) 上市全市場日線（呼叫端框上市）
        institutional: (date, stock_id, foreign_net, trust_net, dealer_net, total_net) 單位股
        membership: (sub_industry, stock_id) long table（多標籤股多列）
        baskets: rotation.compute_subindustry_baskets 輸出（含 basket_index）
        short_window / long_window: unsuffixed momentum / flow_decel / volume_z 的錨窗
        windows: 多窗鏡頭集合（交易日）；每窗各產 flow 滾動加總／自身 z／momentum。
            None → (short, long)。short/long 一律納入，故 _{S}d/_{L}d 逐值不變（純加法）。
        ma_windows: 均線視窗（距均線 %）；240 日需足夠歷史，暖機不足時該欄 null
        position_window: 位階回看視窗（距 N 日低 %）
        rs_window: 相對強度報酬視窗（個股 vs 大盤 / 次產業）
        z_window / z_min_periods: 個股自身滾動 z 視窗與暖機最低樣本
        clip_daily_return_pct: 市場/個股日報酬夾限（防未還原假跳動，0 停用）

    Returns:
        每 (date, stock_id) 一列，欄位：
        - 資金：{net,foreign,trust}_flow_{w}d（各窗滾動加總，股）＋同名 _z（個股自身 z）
        - 加速度：{flow,foreign,trust}_momentum_{w}d（各窗：本窗 − 前一同窗）＋
          unsuffixed {flow,foreign,trust}_momentum（＝短窗值，向後相容 cp_candidates）
        - 背離：flow_decel（短窗總淨流動能的環比變化，二階；<0＝買盤減速）、
          price_flow_div_{w}d（N 日價漲幅 − N 日總淨流 z 成長；>0＝價漲但資金未跟＝背離）
        - 價格位階：above_low_{P}d_pct（距 P 日低 %）、above_high_{P}d_pct（距 P 日高 %，
          ≤0；供 B3 L3 超跌反轉「深跌情境」判讀）、above_ma{W}_pct（距各均線 %）
        - 相對強度：ret_{w}d（各窗個股報酬 %）、rs_market_{R}d、rs_subind_{R}d（差，pp）
        - 量能：volume_z_{S}d（短窗均量 z，非真周轉率，缺流通股數）
        空輸入 → 空 DataFrame。
    """
    if price_history.is_empty():
        return pl.DataFrame()
    s, lw = short_window, long_window
    wins = tuple(sorted({s, lw} | set(windows or ())))

    base = (
        price_history.select(["date", "stock_id", "close", "volume"])
        .unique(subset=["date", "stock_id"], keep="first")
        .sort(["stock_id", "date"])
    )

    # 法人淨額：左接後補 0（T86 只列當日有進出者；無快取 → 全 0，誠實降級）
    if institutional.is_empty():
        panel = base.with_columns([pl.lit(0, dtype=pl.Int64).alias(c) for c in _NET_COLS])
    else:
        nets = institutional.select(["date", "stock_id", *_NET_COLS]).unique(
            subset=["date", "stock_id"]
        )
        panel = base.join(nets, on=["date", "stock_id"], how="left").with_columns(
            [pl.col(c).fill_null(0) for c in _NET_COLS]
        )

    # 資金：各窗滾動加總 ＋ 各窗加速度（本窗 − 前一同窗）
    panel = panel.with_columns(
        [
            pl.col(c).rolling_sum(w, min_samples=1).over("stock_id").alias(f"_{c}_{w}")
            for c in _NET_COLS
            for w in wins
        ]
    ).with_columns(
        [
            (pl.col(f"_{c}_{w}") - pl.col(f"_{c}_{w}").shift(w).over("stock_id")).alias(
                f"{_MOM_PREFIX[c]}_momentum_{w}d"
            )
            for c in _NET_COLS
            for w in wins
        ]
    )
    # unsuffixed momentum＝短窗值（向後相容 cp_candidates / stock_calib 既有讀法）
    panel = panel.with_columns(
        [
            pl.col(f"{_MOM_PREFIX[c]}_momentum_{s}d").alias(f"{_MOM_PREFIX[c]}_momentum")
            for c in _NET_COLS
        ]
    )
    # 對外輸出的滾動加總改名（_total_net_20 → net_flow_20d…）
    panel = panel.rename(
        {f"_{c}_{w}": f"{_FLOW_PREFIX[c]}_{w}d" for c in _NET_COLS for w in wins}
    )

    # 資金 z：個股自身滾動 z（各窗；跨個股可比；std=0 或暖機 → null 不誤判）
    z_targets = [f"{_FLOW_PREFIX[c]}_{w}d" for c in _NET_COLS for w in wins]
    panel = panel.with_columns(
        [_rolling_z(t, z_window, z_min_periods).alias(f"{t}_z") for t in z_targets]
    )

    # 背離因子①：flow_decel＝短窗總淨流動能的環比變化（二階差分）。
    # <0＝短窗買盤在減速（過熱-退潮早訊）；>0＝加速。錨在 short（與 unsuffixed momentum 一致）。
    panel = panel.with_columns(
        (
            pl.col("flow_momentum") - pl.col("flow_momentum").shift(s).over("stock_id")
        ).alias("flow_decel")
    )

    # 價格位階：距 position_window 日低 %、距 position_window 日高 %（≤0）、距各均線 %
    pos_exprs: list[pl.Expr] = [
        (
            (
                pl.col("close")
                / pl.col("close").rolling_min(position_window, min_samples=1).over("stock_id")
                - 1
            )
            * 100
        ).alias(f"above_low_{position_window}d_pct"),
        (
            (
                pl.col("close")
                / pl.col("close").rolling_max(position_window, min_samples=1).over("stock_id")
                - 1
            )
            * 100
        ).alias(f"above_high_{position_window}d_pct"),
    ]
    for w in ma_windows:
        ma = pl.col("close").rolling_mean(w, min_samples=w).over("stock_id")
        pos_exprs.append(((pl.col("close") / ma - 1) * 100).alias(f"above_ma{w}_pct"))
    panel = panel.with_columns(pos_exprs)

    # 量能 z：短窗均量的自身滾動 z（降級替代周轉率）
    panel = panel.with_columns(
        pl.col("volume").rolling_mean(s, min_samples=1).over("stock_id").alias("_avg_vol")
    ).with_columns(_rolling_z("_avg_vol", z_window, z_min_periods).alias(f"volume_z_{s}d"))

    # 個股報酬：各窗 + rs_window（rs 用 rs_window；price_flow_div 用各窗）
    ret_windows = tuple(sorted({rs_window} | set(wins)))
    panel = panel.with_columns(
        [
            ((pl.col("close") / pl.col("close").shift(w).over("stock_id") - 1) * 100).alias(
                f"ret_{w}d"
            )
            for w in ret_windows
        ]
    )
    # 相對強度：個股報酬 − 大盤 / 次產業同窗報酬（錨在 rs_window）
    panel = panel.join(
        _market_returns(price_history, rs_window, clip_daily_return_pct), on="date", how="left"
    )
    if not baskets.is_empty() and not membership.is_empty():
        subind = _subindustry_returns(baskets, membership, rs_window)
        panel = panel.join(subind, on=["stock_id", "date"], how="left")
    else:
        panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias("subind_ret"))
    panel = panel.with_columns(
        (pl.col(f"ret_{rs_window}d") - pl.col("market_ret")).alias(f"rs_market_{rs_window}d"),
        (pl.col(f"ret_{rs_window}d") - pl.col("subind_ret")).alias(f"rs_subind_{rs_window}d"),
    )

    # 背離因子②：price_flow_div_{w}d＝N 日價漲幅 − N 日總淨流 z 成長。
    # 價漲（%）但資金 z 未同步成長（z 單位）→ 值偏正＝量價/資金背離（過熱-退潮佐證）。
    # 註：% 與 z 量綱不同，為序數型背離訊號，門檻待 Phase 2 校準。
    panel = panel.with_columns(
        [
            (
                pl.col(f"ret_{w}d")
                - (
                    pl.col(f"net_flow_{w}d_z")
                    - pl.col(f"net_flow_{w}d_z").shift(w).over("stock_id")
                )
            ).alias(f"price_flow_div_{w}d")
            for w in wins
        ]
    )

    # 收尾：丟暫存欄、固定欄序、依 (stock_id, date) 排序
    flow_cols = [f"{_FLOW_PREFIX[c]}_{w}d" for c in _NET_COLS for w in wins]
    z_cols = [f"{t}_z" for t in z_targets]
    mom_cols = [f"{_MOM_PREFIX[c]}_momentum_{w}d" for c in _NET_COLS for w in wins] + [
        f"{_MOM_PREFIX[c]}_momentum" for c in _NET_COLS
    ]
    div_cols = ["flow_decel"] + [f"price_flow_div_{w}d" for w in wins]
    pos_cols = [
        f"above_low_{position_window}d_pct",
        f"above_high_{position_window}d_pct",
    ] + [f"above_ma{w}_pct" for w in ma_windows]
    ret_cols = [f"ret_{w}d" for w in ret_windows]
    rs_cols = [f"rs_market_{rs_window}d", f"rs_subind_{rs_window}d"]
    ordered = (
        ["date", "stock_id", "close", "volume"]
        + flow_cols
        + z_cols
        + mom_cols
        + div_cols
        + pos_cols
        + ret_cols
        + rs_cols
        + [f"volume_z_{s}d"]
    )
    return panel.select(ordered).sort(["stock_id", "date"])


def compute_coverage_meta(
    panel: pl.DataFrame,
    institutional: pl.DataFrame,
    membership: pl.DataFrame,
    universe: str = "listed",
) -> dict:
    """面板覆蓋率 meta（docs/13 B1「覆蓋率寫進產出 meta」）。

    回傳統計與誠實但書，供下游寫進 research/cp_value/ 的 meta 檔。
    """
    if panel.is_empty():
        return {"universe": universe, "n_stocks": 0, "n_stock_days": 0, "notes": []}

    keys = panel.select(["date", "stock_id"])
    inst_keys = institutional.select(["date", "stock_id"]).unique()
    covered = keys.join(inst_keys, on=["date", "stock_id"], how="inner").height

    stocks = panel.select("stock_id").unique()
    tagged = membership.select("stock_id").unique()
    n_tagged = stocks.join(tagged, on="stock_id", how="inner").height

    n_stocks = panel["stock_id"].n_unique()
    return {
        "universe": universe,
        "n_stocks": n_stocks,
        "n_trading_days": panel["date"].n_unique(),
        "n_stock_days": panel.height,
        "date_min": str(panel["date"].min()),
        "date_max": str(panel["date"].max()),
        "inst_coverage_pct": round(100 * covered / panel.height, 2),
        "subind_coverage_pct": round(100 * n_tagged / n_stocks, 2) if n_stocks else 0.0,
        "notes": [
            "量能因子＝成交量 z（非真周轉率，缺流通在外股數）",
            "相對強度大盤基準＝等權全上市指數（非加權指數，依不新增資料源原則）",
            "個股層只框上市；上櫃有缺口故排除",
        ],
    }
