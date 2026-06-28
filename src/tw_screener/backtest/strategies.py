"""backtest/strategies.py — D/E/F/G 選股策略回測閉環（規劃書 03 V1）。

設計意圖（規劃書 03 §V1）：
  讀取 reports/ 下各週 screen_result_*.csv（純 Goodinfo 入選快照，未被後處理改寫），
  對比入選「之後」可成交價持有 N 週的實際報酬，量化各策略的勝率 / 平均報酬 /
  中位報酬 / 最大回撤 / 樣本數，並與同期等權全市場大盤對比（超額報酬）。

  純函式計算，IO 由呼叫端（cli.py）負責載入：價格用 rotation.load_market_history、
  除息用 twse.load_recent_dividends、大盤基準沿用全市場等權籃子概念。

關鍵防偏誤約定（規劃書 03 §V1 風險）：
  - 前視偏誤：entry 一律用入選日「次一交易日收盤」（嚴格晚於 screened_at），
    禁用入選當日收盤回看。
  - 未到期：大盤最新交易日不足 entry + 持有窗 → 整批排除、不污染統計。
  - 下市/停牌：大盤資料夠長但個股序列提早結束 → exit 標 null（不當 0）。
  - 除權息：ex_date 落在 (entry, exit] 的現金股利加回總報酬（價＋息）。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from loguru import logger

# load_historical_screens 輸出 schema（screened_at 為入選基準日，計算 entry 必需）
_SCREEN_SCHEMA: dict[str, type[pl.DataType]] = {
    "week_tag": pl.Utf8,
    "screened_at": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "close": pl.Float64,
    "change_pct": pl.Float64,
    "strategy_id": pl.Utf8,
}

# compute_forward_returns 輸出 schema
_RETURN_SCHEMA: dict[str, type[pl.DataType]] = {
    "week_tag": pl.Utf8,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "strategy_id": pl.Utf8,
    "hold_weeks": pl.Int64,
    "screened_at": pl.Date,
    "entry_date": pl.Date,
    "exit_date": pl.Date,
    "entry_price": pl.Float64,
    "exit_price": pl.Float64,
    "return_pct": pl.Float64,
    "market_return_pct": pl.Float64,
    "excess_return_pct": pl.Float64,
    "status": pl.Utf8,  # matured（含 null exit 的 delisted）— immature 不入表
}


def load_historical_screens(reports_dir: Path) -> pl.DataFrame:
    """讀取所有週次的篩選結果，合併成含 week_tag 欄位的長表。

    掃 reports_dir/<week_tag>/screen_result_*.csv（如 2026-W21/...），每檔的 strategy_id
    欄已是策略代號（d/e/f/g_*），screened_at 為入選基準日。stock_id 一律當字串（保留
    ETF/權證前導碼）。

    Args:
        reports_dir: reports/ 根目錄。

    Returns:
        schema: week_tag, screened_at, stock_id, name, close, change_pct, strategy_id
        無檔案 → 空表（schema 一致）。
    """
    frames: list[pl.DataFrame] = []
    for csv in sorted(reports_dir.glob("*/screen_result_*.csv")):
        week_tag = csv.parent.name
        try:
            df = pl.read_csv(csv, schema_overrides={"stock_id": pl.Utf8})
        except Exception as e:  # noqa: BLE001 — 單檔壞掉不該擋整批回測
            logger.warning("讀取篩選快照失敗 {}：{}", csv, e)
            continue
        if df.is_empty() or "stock_id" not in df.columns:
            continue
        frames.append(
            df.with_columns(
                pl.lit(week_tag).alias("week_tag"),
                pl.col("screened_at").str.to_date(strict=False).alias("screened_at"),
            ).select(
                [
                    pl.col(c).cast(dt)
                    if c in df.columns or c == "week_tag"
                    else pl.lit(None, dtype=dt).alias(c)
                    for c, dt in _SCREEN_SCHEMA.items()
                ]
            )
        )
    if not frames:
        return pl.DataFrame(schema=_SCREEN_SCHEMA)
    # 同股同週同策略去重（理論上不會重複，保險）
    return pl.concat(frames).unique(
        subset=["week_tag", "stock_id", "strategy_id"], keep="first"
    )


def _market_index(price_history: pl.DataFrame, clip_daily_return_pct: float = 10.0) -> pl.DataFrame:
    """等權全市場指數：每日全市場個股日報酬均值累乘，首日 = 100（沿用 rotation 籃子作法）。

    日報酬夾限 ±clip%（漲跌停外＝減資/分割/未還原事件，夾住避免毒化指數）。
    Returns: (date, market_index) 依日期排序。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return pl.DataFrame(schema={"date": pl.Date, "market_index": pl.Float64})
    ret = pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1.0
    if clip_daily_return_pct > 0:
        bound = clip_daily_return_pct / 100
        ret = ret.clip(-bound, bound)
    daily = (
        price_history.select(["date", "stock_id", "close"])
        .sort(["stock_id", "date"])
        .with_columns(ret.alias("_ret"))
        .drop_nulls("_ret")
        .group_by("date")
        .agg(pl.col("_ret").mean().alias("_mkt_ret"))
        .sort("date")
    )
    return daily.with_columns(
        ((pl.col("_mkt_ret") + 1.0).cum_prod() * 100).alias("market_index")
    ).select(["date", "market_index"])


def compute_forward_returns(
    screens: pl.DataFrame,
    price_history: pl.DataFrame,
    hold_weeks: int = 4,
    dividends: pl.DataFrame | None = None,
    trading_days_per_week: int = 5,
    clip_daily_return_pct: float = 10.0,
) -> pl.DataFrame:
    """計算入選後持有 hold_weeks 週的報酬（含除息還原與大盤超額）。

    entry = 入選日 screened_at「次一交易日」收盤（嚴格晚於 screened_at，防前視）。
    exit  = entry 起算第 hold_weeks*trading_days_per_week 個交易日收盤。
    - 大盤最新日不足 exit 所需窗 → 該列「未到期」排除（不入表、不污染）。
    - 個股序列提早結束（大盤夠長但該股無 exit 列）→ 下市/停牌 → exit 標 null、status=delisted。
    - 除息：dividends 中 ex_date ∈ (entry_date, exit_date] 的現金股利加回 return_pct。

    Args:
        screens: load_historical_screens() 的輸出。
        price_history: 全市場日線（date/stock_id/close）。
        hold_weeks: 持有週數。
        dividends: load_recent_dividends() 輸出（stock_id/ex_date/cash_dividend）；None → 不還原。
        trading_days_per_week: 週→交易日換算（預設 5）。
        clip_daily_return_pct: 大盤指數日報酬夾限。

    Returns:
        schema 見 _RETURN_SCHEMA。未到期列不出現；下市列 exit_price/return 為 null。
    """
    if screens.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=_RETURN_SCHEMA)

    hold_days = hold_weeks * trading_days_per_week

    # 個股序列：每股一次標出組內序(_i)與日期，供 entry/exit 用索引定位（向量化、掃一次）
    px = (
        price_history.select(["date", "stock_id", "close"])
        .drop_nulls("close")
        .unique(subset=["stock_id", "date"], keep="first")
        .sort(["stock_id", "date"])
        .with_columns(pl.int_range(pl.len()).over("stock_id").alias("_i"))
    )
    mkt = _market_index(price_history, clip_daily_return_pct=clip_daily_return_pct)

    out_rows: list[dict] = []
    # entry = 該股第一筆 date > screened_at 的列；用 join_asof（strategy="forward"）一次配對
    sel = screens.select(
        ["week_tag", "stock_id", "name", "strategy_id", "screened_at"]
    ).drop_nulls("screened_at")
    if sel.is_empty():
        return pl.DataFrame(schema=_RETURN_SCHEMA)

    # 對每股找 entry 列（date 嚴格 > screened_at 的第一筆）。join_asof forward 取 date >= key，
    # 故 key 用 screened_at+1 日 → 等價「第一個 date > screened_at」（不論入選日本身是否交易日）。
    entries = (
        sel.with_columns((pl.col("screened_at") + pl.duration(days=1)).alias("_entry_key"))
        .sort("_entry_key")
        .join_asof(
            px.sort("date").rename({"date": "entry_date", "close": "entry_price", "_i": "entry_i"}),
            left_on="_entry_key",
            right_on="entry_date",
            by="stock_id",
            strategy="forward",
            check_sortedness=False,  # 兩側已明確 sort（by 群組下 polars 無法自檢、會發警告）
        )
        .drop("_entry_key")
        .filter(pl.col("entry_date").is_not_null() & (pl.col("entry_date") > pl.col("screened_at")))
    )
    if entries.is_empty():
        return pl.DataFrame(schema=_RETURN_SCHEMA)

    # exit 列：同股 _i == entry_i + hold_days
    exit_px = px.rename(
        {"date": "exit_date", "close": "exit_price", "_i": "exit_i"}
    )
    joined = entries.with_columns(
        (pl.col("entry_i") + hold_days).alias("exit_i")
    ).join(exit_px, on=["stock_id", "exit_i"], how="left")

    for r in joined.iter_rows(named=True):
        entry_date = r["entry_date"]
        exit_date = r["exit_date"]
        entry_price = r["entry_price"]
        # 未到期：大盤資料整體還沒走到 entry + 持有窗 → 排除（市場層判定，與個股下市區分）
        # 估算需要的最晚日 ≈ entry_date 後 hold_days 個交易日；用市場日曆對齊
        mkt_after = mkt.filter(pl.col("date") >= entry_date)
        if mkt_after.height <= hold_days:
            # 市場本身在 entry 後不足 hold_days 個交易日 → 全市場都未到期 → 排除
            continue
        if exit_date is None or r["exit_price"] is None:
            # 大盤夠長但該股無 exit 列 → 下市/停牌
            out_rows.append(
                _make_row(r, hold_weeks, entry_date, None, entry_price, None, None, "delisted")
            )
            continue
        exit_price = r["exit_price"]
        ret = (exit_price - entry_price) / entry_price * 100 if entry_price else None
        # 除息還原：ex_date ∈ (entry_date, exit_date] 的現金股利 / entry_price
        if ret is not None and dividends is not None and not dividends.is_empty():
            addback = _div_addback(dividends, r["stock_id"], entry_date, exit_date, entry_price)
            ret += addback
        # 大盤同期報酬
        mkt_ret = _index_return(mkt, entry_date, exit_date)
        excess = ret - mkt_ret if (ret is not None and mkt_ret is not None) else None
        out_rows.append(
            _make_row(
                r, hold_weeks, entry_date, exit_date, entry_price, exit_price, ret, "matured",
                mkt_ret, excess,
            )
        )

    if not out_rows:
        return pl.DataFrame(schema=_RETURN_SCHEMA)
    return pl.DataFrame(out_rows, schema=_RETURN_SCHEMA)


def _make_row(
    r: dict,
    hold_weeks: int,
    entry_date,
    exit_date,
    entry_price,
    exit_price,
    ret,
    status: str,
    mkt_ret=None,
    excess=None,
) -> dict:
    return {
        "week_tag": r["week_tag"],
        "stock_id": r["stock_id"],
        "name": r["name"],
        "strategy_id": r["strategy_id"],
        "hold_weeks": hold_weeks,
        "screened_at": r["screened_at"],
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "return_pct": ret,
        "market_return_pct": mkt_ret,
        "excess_return_pct": excess,
        "status": status,
    }


def _div_addback(
    dividends: pl.DataFrame, stock_id: str, entry_date, exit_date, entry_price: float
) -> float:
    """(entry_date, exit_date] 內現金股利合計 / entry_price × 100（無則 0）。"""
    if not {"stock_id", "ex_date", "cash_dividend"}.issubset(dividends.columns) or not entry_price:
        return 0.0
    total = (
        dividends.filter(
            (pl.col("stock_id").cast(pl.Utf8) == stock_id)
            & pl.col("cash_dividend").is_not_null()
            & (pl.col("cash_dividend") > 0)
            & (pl.col("ex_date") > entry_date)
            & (pl.col("ex_date") <= exit_date)
        )["cash_dividend"]
        .sum()
    )
    return float(total) / entry_price * 100 if total else 0.0


def _index_return(mkt: pl.DataFrame, entry_date, exit_date) -> float | None:
    """大盤指數 entry→exit 報酬%（用 ≤ 各日的最近指數值，對齊個股 entry/exit 交易日）。"""
    if mkt.is_empty():
        return None
    e = mkt.filter(pl.col("date") <= entry_date)["market_index"]
    x = mkt.filter(pl.col("date") <= exit_date)["market_index"]
    if e.is_empty() or x.is_empty():
        return None
    e0, x0 = e[-1], x[-1]
    if e0 is None or x0 is None or e0 == 0:
        return None
    return float((x0 - e0) / e0 * 100)


def strategy_summary(returns: pl.DataFrame) -> pl.DataFrame:
    """按 (strategy_id, hold_weeks) 計算勝率、平均/中位報酬、最大回撤、樣本數＋大盤超額。

    - 統計只計 status=matured 且 return_pct 非 null 的列；下市（delisted/null）另計 n_delisted。
    - max_drawdown_pct：該組「最差單檔報酬」（min return_pct，最負值）。V1 不建組合權益曲線
      （組合層回撤屬 V3），此欄定義為單檔最壞情形，誠實標註。
    - win_rate_vs_market：excess_return_pct > 0 的比例。

    Args:
        returns: compute_forward_returns() 的輸出（可為多窗 concat）。

    Returns:
        schema: strategy_id, hold_weeks, sample_count, n_delisted, win_rate, avg_return_pct,
                median_return_pct, max_drawdown_pct, avg_excess_pct, median_excess_pct,
                win_rate_vs_market
    """
    schema = {
        "strategy_id": pl.Utf8,
        "hold_weeks": pl.Int64,
        "sample_count": pl.UInt32,
        "n_delisted": pl.UInt32,
        "win_rate": pl.Float64,
        "avg_return_pct": pl.Float64,
        "median_return_pct": pl.Float64,
        "max_drawdown_pct": pl.Float64,
        "avg_excess_pct": pl.Float64,
        "median_excess_pct": pl.Float64,
        "win_rate_vs_market": pl.Float64,
    }
    if returns.is_empty():
        return pl.DataFrame(schema=schema)

    delisted = (
        returns.filter(pl.col("status") == "delisted")
        .group_by(["strategy_id", "hold_weeks"])
        .agg(pl.len().alias("n_delisted"))
    )
    valid = returns.filter(
        (pl.col("status") == "matured") & pl.col("return_pct").is_not_null()
    )
    if valid.is_empty():
        # 只有下市列 → 仍回報 n_delisted、其餘 null
        base = delisted.with_columns(
            pl.lit(0).cast(pl.UInt32).alias("sample_count"),
            *[pl.lit(None, dtype=dt).alias(c) for c, dt in schema.items()
              if c not in ("strategy_id", "hold_weeks", "n_delisted", "sample_count")],
        )
        return base.select(list(schema)).sort(["strategy_id", "hold_weeks"])

    summary = valid.group_by(["strategy_id", "hold_weeks"]).agg(
        pl.len().cast(pl.UInt32).alias("sample_count"),
        (pl.col("return_pct") > 0).mean().alias("win_rate"),
        pl.col("return_pct").mean().alias("avg_return_pct"),
        pl.col("return_pct").median().alias("median_return_pct"),
        pl.col("return_pct").min().alias("max_drawdown_pct"),
        pl.col("excess_return_pct").mean().alias("avg_excess_pct"),
        pl.col("excess_return_pct").median().alias("median_excess_pct"),
        (pl.col("excess_return_pct") > 0).mean().alias("win_rate_vs_market"),
    )
    return (
        summary.join(delisted, on=["strategy_id", "hold_weeks"], how="left")
        .with_columns(pl.col("n_delisted").fill_null(0).cast(pl.UInt32))
        .select(list(schema))
        .sort(["strategy_id", "hold_weeks"])
    )


_STRATEGY_LABEL = {
    "d_quality_leader": "D 體質龍頭",
    "e_growth_momentum": "E 成長動能",
    "f_value_rebound": "F 價值回升",
    "g_growth_pullback": "G 成長回檔",
}


def render_backtest_report(
    summary: pl.DataFrame,
    returns: pl.DataFrame,
    data_range: tuple,
    n_weeks: int,
    min_sample_warn: int = 20,
) -> str:
    """產策略回測 markdown 報告（研究產物，寫 research/strategy_backtest/）。

    Args:
        summary: strategy_summary() 輸出。
        returns: compute_forward_returns() 多窗 concat（供樣本/覆蓋註記）。
        data_range: (價格最早日, 價格最新日)。
        n_weeks: 涵蓋的 reports/ 週數。
        min_sample_warn: 樣本低於此 → 標「樣本太小僅供參考」。
    """
    d0, d1 = data_range
    lines = [
        "# 策略回測閉環（規劃書 03 V1）",
        "",
        f"- 涵蓋 reports 週數：{n_weeks} 週",
        f"- 價格資料範圍：{d0} ~ {d1}",
        "- entry＝入選日次一交易日收盤（防前視）；報酬含現金股利還原；超額＝減同期等權全市場。",
        "- max_drawdown_pct＝該組最差單檔報酬（非組合權益曲線回撤，V1 不建組合）。",
        "",
    ]
    if summary.is_empty():
        lines.append("> 無可統計樣本（資料不足或全部未到期）。")
        return "\n".join(lines)

    header = (
        "| 策略 | 持有週 | 樣本 | 勝率 | 平均報酬 | 中位報酬 | 最差單檔 "
        "| 平均超額 | 勝過大盤 | 下市 |"
    )
    lines += [header, "|---|---|---|---|---|---|---|---|---|---|"]

    def _pct(v: object) -> str:
        return f"{float(v):+.1f}%" if isinstance(v, (int, float)) else "—"

    def _rate(v: object) -> str:
        return f"{float(v):.0%}" if isinstance(v, (int, float)) else "—"

    for r in summary.iter_rows(named=True):
        label = _STRATEGY_LABEL.get(r["strategy_id"], r["strategy_id"])
        n = r["sample_count"] or 0
        warn = " ⚠️" if n < min_sample_warn else ""
        lines.append(
            f"| {label} | {r['hold_weeks']} | {n}{warn} | {_rate(r['win_rate'])} "
            f"| {_pct(r['avg_return_pct'])} | {_pct(r['median_return_pct'])} "
            f"| {_pct(r['max_drawdown_pct'])} | {_pct(r['avg_excess_pct'])} "
            f"| {_rate(r['win_rate_vs_market'])} | {r['n_delisted']} |"
        )

    small = summary.filter(pl.col("sample_count") < min_sample_warn).height
    if small:
        lines += [
            "",
            f"> ⚠️ {small} 組樣本 < {min_sample_warn}，結論僅供方向性參考、未達統計顯著；"
            "隨 reports/ 週數變厚每季重算（與 rotation-calib 同節奏）。",
        ]
    return "\n".join(lines)
