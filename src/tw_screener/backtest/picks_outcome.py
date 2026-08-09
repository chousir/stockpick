"""backtest/picks_outcome.py — pick 閉環：命中率×α＋反事實追蹤（規劃書 05 F1 PO2–PO4）。

設計意圖（規劃書 05 §F1）：
  讀 pick_store 持久化的 picks.csv／excluded.csv，回答「上週的 pick 對不對、
  被剔除的錯不錯」：

  - PO2 到期快照（to-date）：entry＝資料日次一交易日收盤、exit＝指定日（預設快取最新日）
    收盤，分層算勝率／平均／中位／路徑最大回撤，同列 vs 大盤（快取宇宙等權「中位」，
    對齊 2026-07-02 實證審查 §1.2 口徑）與 vs 所屬次產業籃兩個超額。
  - PO2 固定持有窗：直接複用 strategies.compute_forward_returns／strategy_summary
    （前視防護／除息還原／下市 null／未到期排除皆沿用），layer 當 strategy_id。
  - PO4 反事實：對 excluded.csv 算同樣的到期報酬——各旗標的偽陰性帳（擋掉多少報酬）。
  - PO3 翻轉解剖：週對週降級標的＋降級當週 enriched 上可見的翻轉前訊號。
  - M3.1 停損延遲帳（委託書 2026-08-08）：「訊號日掛條件單」vs「等下週報覆核」的
    執行價差——把 W29/W30「訊號 −6%、執行 −25%」的週頻延遲量化成 pp。

  純函式計算；IO（載快取／store／enriched）由 picks_outcome_runner 負責。
"""

from __future__ import annotations

import re
from datetime import date
from typing import cast

import polars as pl

from tw_screener.backtest.strategies import _div_addback

# 到期快照計算欄（附加在輸入列之後；輸入至少需 week/data_date/stock_id/name）
_COMPUTED_SCHEMA: dict[str, type[pl.DataType]] = {
    "entry_date": pl.Date,
    "entry_price": pl.Float64,
    "exit_date": pl.Date,
    "exit_price": pl.Float64,
    "div_addback_pct": pl.Float64,
    "return_pct": pl.Float64,          # 含除息加回（基準未加回，差異見 div_addback_pct）
    "path_min_return_pct": pl.Float64,  # 期間收盤最深回撤（min close vs entry）
    "market_return_pct": pl.Float64,   # 同窗快取宇宙等權「中位」報酬（§1.2 口徑）
    "alpha_market_pct": pl.Float64,
    "subind_return_pct": pl.Float64,   # 同窗所屬次產業成員等權中位報酬
    "subind_n": pl.Int64,              # 次產業同窗有價成員數（< 門檻 → null 超額）
    "alpha_subind_pct": pl.Float64,
    "status": pl.Utf8,                 # matured | no_entry | no_window
}

_LAYER_ORDER = {"core": 0, "opportunity": 1, "pool": 2}
_LAYER_LABEL = {"core": "核心", "opportunity": "機會", "pool": "補充池"}
_ABSENT = "（除名）"


def median_window_return(
    px: pl.DataFrame,
    entry_date: date,
    exit_date: date,
    member_ids: list[str] | None = None,
) -> tuple[float | None, int]:
    """entry_date→exit_date 兩交易日皆有收盤的股票，窗報酬%的等權中位數與樣本數。

    px 需含 (date, stock_id, close)。member_ids 給定時只計該子集（次產業籃）。
    兩端點皆須「當日有收盤」（缺一端的股票剔除——與 §1.2「同窗有快取」口徑一致）。
    """
    if px.is_empty():
        return None, 0
    base = px.filter(pl.col("stock_id").is_in(member_ids)) if member_ids is not None else px
    e = base.filter(pl.col("date") == entry_date).select("stock_id", pl.col("close").alias("_e"))
    x = base.filter(pl.col("date") == exit_date).select("stock_id", pl.col("close").alias("_x"))
    j = e.join(x, on="stock_id", how="inner").filter(pl.col("_e") > 0)
    if j.is_empty():
        return None, 0
    med = j.select(((pl.col("_x") / pl.col("_e") - 1.0) * 100).median()).item()
    return (float(med) if med is not None else None), j.height


def compute_todate_returns(
    rows: pl.DataFrame,
    price_history: pl.DataFrame,
    dividends: pl.DataFrame | None = None,
    exit_date: date | None = None,
    membership: pl.DataFrame | None = None,
    min_subind_members: int = 3,
) -> pl.DataFrame:
    """到期快照：entry＝data_date 次一交易日收盤、exit＝exit_date（含）前最後收盤。

    Args:
        rows: 至少含 week/data_date/stock_id/name；有 sub_industry 欄才算族群超額。
              其餘欄位原樣帶出（layer／reason／ext_ma60_pct…）。
        price_history: 全市場日線 (date, stock_id, close)。
        dividends: (stock_id, ex_date, cash_dividend)；ex_date ∈ (entry, exit] 加回。
        exit_date: 評估截止日（預設快取最新交易日）。基準與個股用同一市場截止交易日。
        membership: (sub_industry, stock_id) long table（list_subindustries 輸出）。
        min_subind_members: 次產業同窗有價成員低於此 → 族群超額標 null（樣本太小）。

    Returns:
        輸入欄＋_COMPUTED_SCHEMA 欄。entry 找不到（資料日晚於快取）→ status=no_entry；
        entry 後無後續交易日 → status=no_window；其餘 matured。
    """
    out_schema: dict[str, pl.DataType | type[pl.DataType]] = {
        **dict(rows.schema), **_COMPUTED_SCHEMA
    }
    if rows.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=out_schema)

    px = (
        price_history.select(["date", "stock_id", "close"])
        .drop_nulls("close")
        .filter(pl.col("close") > 0)
        .unique(subset=["stock_id", "date"], keep="first")
        .sort(["stock_id", "date"])
    )
    market_last = px["date"].max()
    cutoff = exit_date if exit_date is not None else market_last
    px_win = px.filter(pl.col("date") <= cutoff)
    if px_win.is_empty():
        return pl.DataFrame(schema=out_schema)
    market_exit = cast(date, px_win["date"].max())

    # entry＝第一筆 date > data_date（join_asof forward，key 用 data_date+1；沿 V1 防前視）
    entries = (
        rows.drop_nulls("data_date")
        .with_columns((pl.col("data_date") + pl.duration(days=1)).alias("_ek"))
        .sort("_ek")
        .join_asof(
            px_win.sort("date").rename({"date": "entry_date", "close": "entry_price"}),
            left_on="_ek",
            right_on="entry_date",
            by="stock_id",
            strategy="forward",
            check_sortedness=False,  # 兩側已明確 sort（by 群組下 polars 無法自檢）
        )
        .drop("_ek")
    )
    # exit＝該股 ≤ market_exit 的最後收盤
    last_close = (
        px_win.group_by("stock_id")
        .agg(pl.col("date").last().alias("exit_date"), pl.col("close").last().alias("exit_price"))
    )
    joined = entries.join(last_close, on="stock_id", how="left").with_row_index("_rid")

    # 期間最深收盤回撤：entry_date ≤ date ≤ exit_date 的 min close
    path = (
        joined.select("_rid", "stock_id", "entry_date", "exit_date")
        .drop_nulls(["entry_date", "exit_date"])
        .join(px_win, on="stock_id", how="inner")
        .filter((pl.col("date") >= pl.col("entry_date")) & (pl.col("date") <= pl.col("exit_date")))
        .group_by("_rid")
        .agg(pl.col("close").min().alias("_min_close"))
    )
    joined = joined.join(path, on="_rid", how="left")

    # 基準快取：每個 entry_date 一次（市場中位）；(sub_industry, entry_date) 一次（族群中位）
    mkt_cache: dict[date, tuple[float | None, int]] = {}
    sub_cache: dict[tuple[str, date], tuple[float | None, int]] = {}
    members_by_sub: dict[str, list[str]] = {}
    if membership is not None and not membership.is_empty():
        for sub_key, grp in membership.group_by("sub_industry"):
            members_by_sub[str(sub_key[0])] = grp["stock_id"].to_list()

    out_rows: list[dict] = []
    passthrough = [c for c in rows.columns]
    for r in joined.iter_rows(named=True):
        base = {c: r[c] for c in passthrough}
        entry_d = cast("date | None", r["entry_date"])
        entry_p = cast("float | None", r["entry_price"])
        exit_d = cast("date | None", r["exit_date"])
        exit_p = cast("float | None", r["exit_price"])
        row = {**base, **{c: None for c in _COMPUTED_SCHEMA}}
        if entry_d is None or entry_p is None or entry_d > market_exit:
            row["status"] = "no_entry"
            out_rows.append(row)
            continue
        row["entry_date"], row["entry_price"] = entry_d, entry_p
        if exit_d is None or exit_p is None or exit_d <= entry_d:
            row["status"] = "no_window"
            out_rows.append(row)
            continue
        ret = (exit_p - entry_p) / entry_p * 100
        addback = (
            _div_addback(dividends, r["stock_id"], entry_d, exit_d, entry_p)
            if dividends is not None and not dividends.is_empty()
            else 0.0
        )
        if entry_d not in mkt_cache:
            mkt_cache[entry_d] = median_window_return(px_win, entry_d, market_exit)
        mkt_ret, _ = mkt_cache[entry_d]
        sub = cast("str | None", base.get("sub_industry"))
        sub_ret: float | None = None
        sub_n = 0
        if sub and sub in members_by_sub:
            if (sub, entry_d) not in sub_cache:
                sub_cache[(sub, entry_d)] = median_window_return(
                    px_win, entry_d, market_exit, members_by_sub[sub]
                )
            sub_ret, sub_n = sub_cache[(sub, entry_d)]
            if sub_n < min_subind_members:
                sub_ret = None
        min_close = r["_min_close"]
        row.update(
            exit_date=exit_d,
            exit_price=exit_p,
            div_addback_pct=addback,
            return_pct=ret + addback,
            path_min_return_pct=(
                (min_close - entry_p) / entry_p * 100 if min_close is not None else None
            ),
            market_return_pct=mkt_ret,
            alpha_market_pct=(ret + addback - mkt_ret) if mkt_ret is not None else None,
            subind_return_pct=sub_ret,
            subind_n=sub_n,
            alpha_subind_pct=(ret + addback - sub_ret) if sub_ret is not None else None,
            status="matured",
        )
        out_rows.append(row)

    return pl.DataFrame(out_rows, schema=out_schema)


def layer_summary(todate: pl.DataFrame) -> pl.DataFrame:
    """到期快照按 layer 彙總：n／勝率／平均／中位／α（vs 大盤、vs 族群）／最深回撤。"""
    schema = {
        "layer": pl.Utf8,
        "n": pl.UInt32,
        "win_rate": pl.Float64,
        "avg_return_pct": pl.Float64,
        "median_return_pct": pl.Float64,
        "avg_alpha_market_pct": pl.Float64,
        "win_vs_market_rate": pl.Float64,
        "avg_alpha_subind_pct": pl.Float64,
        "n_subind": pl.UInt32,
        "worst_return_pct": pl.Float64,
        "worst_path_min_pct": pl.Float64,
    }
    valid = todate.filter((pl.col("status") == "matured") & pl.col("return_pct").is_not_null())
    if valid.is_empty():
        return pl.DataFrame(schema=schema)
    return (
        valid.group_by("layer")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            (pl.col("return_pct") > 0).mean().alias("win_rate"),
            pl.col("return_pct").mean().alias("avg_return_pct"),
            pl.col("return_pct").median().alias("median_return_pct"),
            pl.col("alpha_market_pct").mean().alias("avg_alpha_market_pct"),
            (pl.col("alpha_market_pct") > 0).mean().alias("win_vs_market_rate"),
            pl.col("alpha_subind_pct").mean().alias("avg_alpha_subind_pct"),
            pl.col("alpha_subind_pct").is_not_null().sum().cast(pl.UInt32).alias("n_subind"),
            pl.col("return_pct").min().alias("worst_return_pct"),
            pl.col("path_min_return_pct").min().alias("worst_path_min_pct"),
        )
        .with_columns(pl.col("layer").replace_strict(_LAYER_ORDER, default=9).alias("_o"))
        .sort("_o")
        .drop("_o")
        .select(list(schema))
    )


def weekly_layer_table(todate: pl.DataFrame, layer: str = "core") -> pl.DataFrame:
    """單一層的逐週表：n／平均報酬／同窗市場中位／α／勝檔數——§1.2 的重現格式。"""
    schema = {
        "week": pl.Utf8,
        "n": pl.UInt32,
        "avg_return_pct": pl.Float64,
        "market_return_pct": pl.Float64,
        "avg_alpha_pct": pl.Float64,
        "wins": pl.UInt32,
    }
    valid = todate.filter(
        (pl.col("layer") == layer)
        & (pl.col("status") == "matured")
        & pl.col("return_pct").is_not_null()
    )
    if valid.is_empty():
        return pl.DataFrame(schema=schema)
    return (
        valid.group_by("week")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            pl.col("return_pct").mean().alias("avg_return_pct"),
            pl.col("market_return_pct").mean().alias("market_return_pct"),
            pl.col("alpha_market_pct").mean().alias("avg_alpha_pct"),
            (pl.col("return_pct") > 0).sum().cast(pl.UInt32).alias("wins"),
        )
        .sort("week")
        .select(list(schema))
    )


def counterfactual_summary(excluded_todate: pl.DataFrame) -> pl.DataFrame:
    """PO4 偽陰性帳：被旗標剔除的股按 reason 彙總同窗表現——每個旗標擋掉多少報酬。"""
    schema = {
        "reason": pl.Utf8,
        "n": pl.UInt32,
        "avg_return_pct": pl.Float64,
        "median_return_pct": pl.Float64,
        "avg_alpha_market_pct": pl.Float64,
        "beat_market_rate": pl.Float64,  # 偽陰性率：被剔除卻跑贏大盤的比例
        "best_stock": pl.Utf8,
        "best_return_pct": pl.Float64,
    }
    valid = excluded_todate.filter(
        (pl.col("status") == "matured") & pl.col("return_pct").is_not_null()
    )
    if valid.is_empty():
        return pl.DataFrame(schema=schema)
    best = (
        valid.sort("return_pct", descending=True)
        .group_by("reason", maintain_order=True)
        .agg(
            pl.col("name").first().alias("best_stock"),
            pl.col("return_pct").first().alias("best_return_pct"),
        )
    )
    return (
        valid.group_by("reason")
        .agg(
            pl.len().cast(pl.UInt32).alias("n"),
            pl.col("return_pct").mean().alias("avg_return_pct"),
            pl.col("return_pct").median().alias("median_return_pct"),
            pl.col("alpha_market_pct").mean().alias("avg_alpha_market_pct"),
            (pl.col("alpha_market_pct") > 0).mean().alias("beat_market_rate"),
        )
        .join(best, on="reason", how="left")
        .sort("avg_return_pct", descending=True)
        .select(list(schema))
    )


# ── M3.1 停損延遲帳（委託書 M3・修「訊號 −6%、執行 −25%」的週頻延遲）────────────
#
# 事後帳：W29/W30 的停損訊號在 MA60 −6% 觸發，動作卻在 −25% 才做——因為週報只寫
# 「跌破季線停損」、要等下週覆核才動作。這本帳把那段延遲量化成 pp，讓「條件單語意」
# 的改制有裁判：訊號日就掛條件單成交 vs 等下週報覆核才成交，差幾 %。
#
# 誠實邊界（三條，全部落在 status 欄，不靜默吞掉）：
#   1. `stop` 是自由文字，價格用 regex 抽——抽不到／抽到不合理值（高於進場價）＝
#      `unparsed`，計數揭露、不猜。
#   2. 快取只有收盤，沒有開盤——「隔日開盤減半」的語意用**次一交易日收盤**代理，
#      報表明標。這會低估延遲成本（急殺日開盤通常低於收盤），方向保守。
#   3. 訊號後還沒有下一份週報 → `pending_review`（未到期，不進統計）。
_STOP_DELAY_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "layer": pl.Utf8,
    "stop_price": pl.Float64,       # 從 stop 自由文字抽出的絕對停損價
    "entry_price": pl.Float64,      # 資料日次一交易日收盤（合理性檢核基準）
    "signal_date": pl.Date,         # 首次「收盤 < stop_price」的交易日
    "signal_close": pl.Float64,
    "cond_exec_date": pl.Date,      # 條件單語意執行日＝訊號日次一交易日
    "cond_exec_price": pl.Float64,
    "review_date": pl.Date,         # 訊號後第一份週報的資料日（週頻覆核點）
    "weekly_exec_date": pl.Date,    # 週頻語意執行日＝覆核資料日次一交易日
    "weekly_exec_price": pl.Float64,
    "delay_td": pl.Int64,           # 條件單執行日→週頻執行日的交易日數
    "delay_cost_pct": pl.Float64,   # 週頻執行價 / 條件單執行價 − 1（負＝延遲多賠）
    "status": pl.Utf8,              # measured|not_triggered|pending_review|unparsed|no_price
    "stop_text": pl.Utf8,
}

# 停損文字裡的「非價格數字」token（MA60／low20／近5日／−5%／T1…）先遮蔽，
# 免得「跌破 MA60 20.35」把 60 當成停損價。遮蔽字元刻意不含數字。
_STOP_MASK_RE = re.compile(r"(?:MA|Ma|ma)\s*\d+|low_?\d+|\d+\s*日|\d+(?:\.\d+)?\s*%|\bT[1-9]\b")
# 「破」之後、下一串數字之前允許夾任意非數字（遮蔽後的標籤、空白、全形括號）。
_STOP_PRICE_RE = re.compile(r"破[^0-9]*?(\d+(?:\.\d+)?)")


def parse_stop_price(stop_text: str | None) -> float | None:
    """從 `stop` 自由文字抽出絕對停損價；抽不到回 None（不猜）。

    底帳實例（reports/2026-W3x/picks.csv）涵蓋的四種寫法都要過：
      「收盤跌破41.5(MA60)、隔日未收復出場」                     → 41.5
      「收盤確認跌破 MA60 20.35、隔日未收復出場」                 → 20.35
      「MA60 10.29 高於收盤不可用→收盤跌破 low60 9.90」          → 9.90
      「收盤跌破 215.0、隔日未收復出場（MA60 236.87 高於收盤不可用）」→ 215.0

    做法：先把 MA\\d+／low\\d+／N日／N% 這類**標籤數字**遮成無數字字元，再取第一個
    「破」之後的數字。取第一個而非最後一個——第三例的正解在第一個「破」之後。
    """
    if not stop_text:
        return None
    masked = _STOP_MASK_RE.sub("◇", str(stop_text))
    m = _STOP_PRICE_RE.search(masked)
    if not m:
        return None
    try:
        price = float(m.group(1))
    except ValueError:  # regex 已保證是數字，防禦性
        return None
    return price if price > 0 else None


def stop_delay_ledger(
    picks: pl.DataFrame,
    price_history: pl.DataFrame,
    entry_tolerance: float = 1.02,
) -> pl.DataFrame:
    """M3.1 停損延遲帳：每筆 pick 的「條件單語意執行」vs「週頻覆核執行」價差。

    Args:
        picks: pick_store 底帳（需 week／data_date／stock_id／name／layer／stop）。
        price_history: 全市場日線 (date, stock_id, close)。
        entry_tolerance: 抽出的停損價 > 進場價 × 此倍數 → 判 regex 抽錯，記 `unparsed`
            （停損價高於進場價在語意上不成立，寧可標抽不到也不要算出假帳）。

    Returns:
        _STOP_DELAY_SCHEMA；每列一筆 pick。只有 status=measured 的列有 delay_cost_pct。
        **除息未還原**：延遲窗通常 ≤10 個交易日，但窗內遇除息會把價差誇大成負——
        renderer 一併標註，季度覆盤時以個案剔除，不在此靜默加回（加回會與
        compute_todate_returns 的口徑混淆）。
    """
    if picks.is_empty() or price_history.is_empty():
        return pl.DataFrame(schema=_STOP_DELAY_SCHEMA)
    needed = {"week", "data_date", "stock_id", "stop"}
    if not needed.issubset(picks.columns):
        return pl.DataFrame(schema=_STOP_DELAY_SCHEMA)

    px = (
        price_history.select(["date", "stock_id", "close"])
        .drop_nulls(["close", "date"])
        .filter(pl.col("close") > 0)
        .unique(subset=["stock_id", "date"], keep="first")
        .sort(["stock_id", "date"])
    )
    # 每股一條 (dates, closes) 供逐列掃描——底帳規模是數十~數百筆，直掃比 join 清楚
    by_stock: dict[str, tuple[list[date], list[float]]] = {}
    for sid_key, grp in px.group_by("stock_id", maintain_order=True):
        key = sid_key[0] if isinstance(sid_key, tuple) else sid_key
        by_stock[str(key)] = (
            grp["date"].to_list(),
            [float(c) for c in grp["close"].to_list()],
        )
    # 週頻覆核點＝底帳裡所有週的資料日（升冪）。訊號日之後第一個資料日＝該筆若走
    # 舊制（等下週報覆核）最早能被寫進報告的日子。
    review_dates = sorted({d for d in picks["data_date"].to_list() if d is not None})

    rows: list[dict[str, object]] = []
    for r in picks.iter_rows(named=True):
        sid = str(r["stock_id"])
        base: dict[str, object] = {
            "week": r.get("week"),
            "stock_id": sid,
            "name": r.get("name"),
            "layer": r.get("layer"),
            "stop_price": None,
            "entry_price": None,
            "signal_date": None,
            "signal_close": None,
            "cond_exec_date": None,
            "cond_exec_price": None,
            "review_date": None,
            "weekly_exec_date": None,
            "weekly_exec_price": None,
            "delay_td": None,
            "delay_cost_pct": None,
            "status": "unparsed",
            "stop_text": r.get("stop"),
        }
        stop_price = parse_stop_price(r.get("stop"))
        data_date = r.get("data_date")
        series = by_stock.get(sid)
        if series is None or data_date is None:
            base["status"] = "no_price"
            base["stop_price"] = stop_price
            rows.append(base)
            continue
        dates, closes = series
        # 進場＝資料日次一交易日（沿 compute_todate_returns 的防前視口徑）
        entry_i = next((i for i, d in enumerate(dates) if d > data_date), None)
        if entry_i is None:
            base["status"] = "no_price"
            base["stop_price"] = stop_price
            rows.append(base)
            continue
        entry_price = closes[entry_i]
        base["entry_price"] = entry_price
        if stop_price is None or stop_price > entry_price * entry_tolerance:
            rows.append(base)  # status 維持 unparsed（含「抽到不合理值」）
            continue
        base["stop_price"] = stop_price

        sig_i = next(
            (i for i in range(entry_i, len(closes)) if closes[i] < stop_price), None
        )
        if sig_i is None:
            base["status"] = "not_triggered"
            rows.append(base)
            continue
        base["signal_date"] = dates[sig_i]
        base["signal_close"] = closes[sig_i]
        if sig_i + 1 >= len(closes):
            base["status"] = "pending_review"  # 訊號日就是快取最後一天，還沒得執行
            rows.append(base)
            continue
        base["cond_exec_date"] = dates[sig_i + 1]
        base["cond_exec_price"] = closes[sig_i + 1]

        review = next((d for d in review_dates if d > dates[sig_i]), None)
        if review is None:
            base["status"] = "pending_review"  # 訊號後還沒有下一份週報
            rows.append(base)
            continue
        base["review_date"] = review
        wk_i = next((i for i, d in enumerate(dates) if d > review), None)
        if wk_i is None:
            base["status"] = "pending_review"  # 覆核日之後尚無交易日快取
            rows.append(base)
            continue
        base["weekly_exec_date"] = dates[wk_i]
        base["weekly_exec_price"] = closes[wk_i]
        base["delay_td"] = wk_i - (sig_i + 1)
        cond = closes[sig_i + 1]
        base["delay_cost_pct"] = (closes[wk_i] / cond - 1.0) * 100.0 if cond > 0 else None
        base["status"] = "measured" if base["delay_cost_pct"] is not None else "no_price"
        rows.append(base)

    return pl.DataFrame(rows, schema=_STOP_DELAY_SCHEMA)


def stop_delay_summary(ledger: pl.DataFrame) -> dict[str, object]:
    """停損延遲帳一行摘要（決策卡「上週帳」的第三格・M6 patch-4 消費）。

    回 dict：n_measured／avg_cost_pct／median_cost_pct／worst_* ＋各 status 計數。
    無可量測樣本 → n_measured=0、成本欄 None（呼叫端印「未取得」，不印 0%）。
    """
    counts = {s: 0 for s in ("measured", "not_triggered", "pending_review", "unparsed", "no_price")}
    if not ledger.is_empty():
        for r in ledger.group_by("status").agg(pl.len().alias("n")).iter_rows(named=True):
            counts[str(r["status"])] = int(r["n"])
    measured = (
        ledger.filter(
            (pl.col("status") == "measured") & pl.col("delay_cost_pct").is_not_null()
        )
        if not ledger.is_empty()
        else pl.DataFrame(schema=_STOP_DELAY_SCHEMA)
    )
    out: dict[str, object] = {
        "n_measured": measured.height,
        "avg_cost_pct": None,
        "median_cost_pct": None,
        "worst_stock": None,
        "worst_cost_pct": None,
        "counts": counts,
    }
    if measured.is_empty():
        return out
    out["avg_cost_pct"] = float(cast(float, measured["delay_cost_pct"].mean() or 0.0))
    out["median_cost_pct"] = float(cast(float, measured["delay_cost_pct"].median() or 0.0))
    worst = measured.sort("delay_cost_pct").row(0, named=True)
    out["worst_stock"] = worst["name"] or worst["stock_id"]
    out["worst_cost_pct"] = float(worst["delay_cost_pct"])
    return out


def render_stop_delay_section(ledger: pl.DataFrame) -> list[str]:
    """停損延遲帳的報表段（render_outcome_report §6 用）。"""
    s = stop_delay_summary(ledger)
    c = cast(dict[str, int], s["counts"])
    lines = [
        "## 6. 停損延遲帳（M3.1）",
        "",
        "> 量化「訊號日掛條件單」與「等下週報覆核」的執行價差——修 W29/W30「訊號 −6%、"
        "執行 −25%」的週頻延遲。**執行價一律用次一交易日收盤代理**（快取無開盤價），",
        "> 故延遲成本偏保守（急殺日開盤多半低於收盤）；**除息未還原**，窗內遇除息的個案"
        "價差會被誇大，覆盤時個案剔除。",
        "",
        "> ⚠️ **本帳量測的是「制度性最小延遲」＝一個覆核週期**（訊號日 → 下一份週報資料日）。"
        "委託書事後帳描述的創見 −24.9%，成因是**連續多週續抱不執行**的行為延遲，",
        "> 不是單一覆核週期的結構延遲——那一段本帳看不到，需要逐週 layer 軌跡才量得出來"
        "（PO3 翻轉解剖的鄰接題）。**讀本段時不要把兩者當同一件事。**",
        "",
        f"- 可量測 {c['measured']} 筆｜未觸發 {c['not_triggered']}｜"
        f"未到覆核點 {c['pending_review']}｜停損價無法解析 {c['unparsed']}｜無報價 {c['no_price']}",
    ]
    if s["n_measured"] == 0:
        lines += ["", "> 尚無可量測樣本——本段僅佔位，樣本到齊自動補。", ""]
        return lines
    lines += [
        f"- **平均延遲成本 {cast(float, s['avg_cost_pct']):+.2f}%**"
        f"（中位 {cast(float, s['median_cost_pct']):+.2f}%）"
        f"；最痛 {s['worst_stock']} {cast(float, s['worst_cost_pct']):+.2f}%",
        "",
        "| 週 | 股票 | 停損價 | 訊號日收盤 | 條件單執行 | 週頻覆核執行 | 延遲(交易日) | 延遲成本 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    measured = ledger.filter(pl.col("status") == "measured").sort("delay_cost_pct")
    for r in measured.iter_rows(named=True):
        lines.append(
            f"| {r['week']} | {r['name'] or r['stock_id']} | {r['stop_price']:.2f} "
            f"| {r['signal_date']} {r['signal_close']:.2f} "
            f"| {r['cond_exec_date']} {r['cond_exec_price']:.2f} "
            f"| {r['weekly_exec_date']} {r['weekly_exec_price']:.2f} "
            f"| {r['delay_td']} | {r['delay_cost_pct']:+.2f}% |"
        )
    unparsed = ledger.filter(pl.col("status") == "unparsed")
    if not unparsed.is_empty():
        names = "、".join(
            str(r["name"] or r["stock_id"]) for r in unparsed.head(8).iter_rows(named=True)
        )
        lines += [
            "",
            f"> 停損價無法解析 {unparsed.height} 筆（{names}）——`stop` 欄未寫成"
            f"「收盤跌破 ○○.○」的絕對價形式。M7 patch-2 要求週報停損欄一律印可掛條件單的"
            f"絕對價，這個計數就是該規則的達成率指標（應逐週趨近 0）。",
        ]
    lines.append("")
    return lines


# ── M1.6 左側解禁自動回收條款（委託書 M1.6・裁決 A 的必要對價）──────────────
#
# 裁決 A 是**人工覆寫**，不是新證據：被 docs/24 §3.1 否證的是「轉買 × 貼低」兩條件桶
# （201 週、2,477 股×週、lift r+20 −2.30%），委託版三條件桶未被直接檢驗。這本帳就是
# 對價——左側票單獨記帳，α 與勝率同時劣於一般機會層即印回收警告。
#
# 統計效力誠實話：門檻是 10 筆／12 週，對上已否證的 2,477 樣本**極不對稱**。這本帳
# 能抓到的是「明顯很糟」，抓不到「小幅為負」；它是煞車，不是裁決依據。
_CONTRARIAN_RECALL_HORIZONS = (2, 4)  # hold_weeks → 對應 r+10 / r+20（tdpw=5）


def contrarian_recall_check(
    picks: pl.DataFrame,
    returns_by_hold: dict[int, pl.DataFrame],
    prefix: str = "左側M-BR1",
    min_picks: int = 10,
    min_weeks: int = 12,
    unblocked_since: date | None = None,
    as_of: date | None = None,
) -> dict[str, object]:
    """M1.6：左側票（thesis 前綴）vs 一般機會層的 r+10/r+20 α 與勝率對照。

    Args:
        picks: pick_store 底帳（需 week／stock_id／layer／thesis_tag）。
        returns_by_hold: {hold_weeks: compute_forward_returns 輸出}（strategy_id＝layer）。
        prefix: 左側票的 thesis 前綴（settings.contrarian_base.thesis_prefix）。
        min_picks / min_weeks: 任一達標即進入判定（委託書 M1.6）。
        unblocked_since: 解禁日，用來算已過週數。
        as_of: 判定基準日（預設今天）。

    Returns:
        dict：n_left／weeks_elapsed／eligible／warn／rows（逐窗對照）／note。
        **warn 只在「已達判定門檻 ∧ 兩組皆有樣本 ∧ α 與勝率同時較差」時為 True**——
        任一組樣本為空 → 不判（不用空集合宣告劣化）。
    """
    out: dict[str, object] = {
        "n_left": 0, "weeks_elapsed": None, "eligible": False,
        "warn": False, "rows": [], "note": "",
    }
    if picks.is_empty() or not {"layer", "thesis_tag", "week", "stock_id"} <= set(picks.columns):
        out["note"] = "底帳缺 layer／thesis_tag 欄——無法分層記帳"
        return out

    left = picks.filter(
        (pl.col("layer") == "opportunity")
        & pl.col("thesis_tag").is_not_null()
        & pl.col("thesis_tag").str.starts_with(prefix)
    )
    left_keys = {(str(r["week"]), str(r["stock_id"])) for r in left.iter_rows(named=True)}
    out["n_left"] = len(left_keys)

    if unblocked_since is not None:
        ref = as_of or date.today()
        out["weeks_elapsed"] = max((ref - unblocked_since).days // 7, 0)
    weeks_elapsed = cast(int | None, out["weeks_elapsed"])
    out["eligible"] = len(left_keys) >= min_picks or (
        weeks_elapsed is not None and weeks_elapsed >= min_weeks
    )

    rows: list[dict[str, object]] = []
    worse_alpha = worse_win = True
    comparable = 0
    for hold in _CONTRARIAN_RECALL_HORIZONS:
        df = returns_by_hold.get(hold)
        if df is None or df.is_empty():
            continue
        valid = df.filter(
            (pl.col("status") == "matured")
            & pl.col("return_pct").is_not_null()
            & (pl.col("strategy_id") == "opportunity")
        )
        if valid.is_empty():
            continue
        is_left = pl.struct("week_tag", "stock_id").map_elements(
            lambda s: (str(s["week_tag"]), str(s["stock_id"])) in left_keys,
            return_dtype=pl.Boolean,
        )
        tagged = valid.with_columns(is_left.alias("_left"))
        for label, sub in (
            ("左側M-BR1", tagged.filter(pl.col("_left"))),
            ("一般機會層", tagged.filter(~pl.col("_left"))),
        ):
            rows.append({
                "horizon_td": hold * 5,
                "group": label,
                "n": sub.height,
                "alpha_pct": (
                    float(cast(float, sub["excess_return_pct"].mean()))
                    if sub.height and sub["excess_return_pct"].null_count() < sub.height
                    else None
                ),
                "win_rate": (
                    float(cast(float, (sub["return_pct"] > 0).mean())) if sub.height else None
                ),
            })
        lf, gf = rows[-2], rows[-1]
        if lf["n"] and gf["n"] and lf["alpha_pct"] is not None and gf["alpha_pct"] is not None:
            comparable += 1
            worse_alpha &= cast(float, lf["alpha_pct"]) < cast(float, gf["alpha_pct"])
            worse_win &= cast(float, lf["win_rate"]) < cast(float, gf["win_rate"])
    out["rows"] = rows
    if not comparable:
        out["note"] = "兩組尚無可對照的到期樣本——不判定（不用空集合宣告劣化）"
        return out
    out["warn"] = bool(out["eligible"] and worse_alpha and worse_win)
    return out


def render_contrarian_recall_section(check: dict[str, object]) -> list[str]:
    """M1.6 回收條款的報表段（render_outcome_report §7）。"""
    lines = [
        "## 7. 左側解禁記帳（M1.6 回收條款）",
        "",
        "> 2026-08-08 裁決 A 以**人工覆寫**解禁 M-BR1 左側票進機會層（小注、永不核心）。"
        "被 docs/24 §3.1 否證的是「轉買 × 貼低」**兩條件桶**（201 週、2,477 股×週、",
        "> lift r+20 −2.30%、CI95 [−3.52,−1.26]）；委託版**三條件桶未測、且先驗不利**。"
        "本段是解禁的對價——α 與勝率同時劣於一般機會層即印回收警告。",
        "> ⚠️ **統計效力不對稱**：判定門檻 10 筆／12 週 vs 已否證的 2,477 樣本。"
        "本帳是煞車，抓得到「明顯很糟」，抓不到「小幅為負」。",
        "",
        f"- 左側票累積 **{check['n_left']}** 筆"
        + (f"；解禁後 {check['weeks_elapsed']} 週" if check["weeks_elapsed"] is not None else "")
        + f"；判定門檻{'已' if check['eligible'] else '未'}達成",
    ]
    rows = cast(list[dict[str, object]], check["rows"])
    if not rows:
        lines += ["", f"> {check['note'] or '尚無到期樣本——本段僅佔位。'}", ""]
        return lines
    lines += ["", "| 窗 | 組 | n | α vs 大盤 | 勝率 |", "|---|---|---|---|---|"]
    for r in rows:
        a = f"{cast(float, r['alpha_pct']):+.2f}pp" if r["alpha_pct"] is not None else "—"
        w = f"{cast(float, r['win_rate']):.0%}" if r["win_rate"] is not None else "—"
        lines.append(f"| r+{r['horizon_td']} | {r['group']} | {r['n']} | {a} | {w} |")
    if check["warn"]:
        lines += [
            "",
            "> 🔴 **左側解禁回收警告**：α 與勝率在所有可對照窗皆劣於一般機會層，且已達判定"
            "門檻。依 M1.6，週報必印本警告；回退動作＝把 "
            "`settings.contrarian_base.picks_unblocked` 改 `false`（一行，不刪碼）。",
        ]
    elif check["note"]:
        lines += ["", f"> {check['note']}"]
    lines.append("")
    return lines


def week_over_week_diff(
    picks: pl.DataFrame, enriched_by_week: dict[str, pl.DataFrame] | None = None
) -> pl.DataFrame:
    """PO3 翻轉解剖：相鄰「有紀錄」週之間的降級標的＋降級當週 enriched 可見訊號。

    降級＝layer 排序變差（core→opportunity→pool→除名）。訊號欄取降級當週（後週）
    candidates_enriched 的位階／法人近端欄；該股不在後週 enriched → 訊號留 null。
    """
    schema = {
        "from_week": pl.Utf8,
        "to_week": pl.Utf8,
        "stock_id": pl.Utf8,
        "name": pl.Utf8,
        "from_layer": pl.Utf8,
        "to_layer": pl.Utf8,
        "ma20_dist_pct": pl.Float64,
        "ma60_dist_pct": pl.Float64,
        "foreign_net_5d_lots": pl.Float64,
        "foreign_net_lots": pl.Float64,
        "trust_net_5d_lots": pl.Float64,
        "flags": pl.Utf8,
    }
    if picks.is_empty():
        return pl.DataFrame(schema=schema)
    weeks = sorted(picks["week"].unique().to_list())
    signal_cols = [
        "ma20_dist_pct", "ma60_dist_pct",
        "foreign_net_5d_lots", "foreign_net_lots", "trust_net_5d_lots", "flags",
    ]
    rows: list[dict] = []
    for w1, w2 in zip(weeks, weeks[1:]):
        prev = picks.filter(pl.col("week") == w1)
        curr = picks.filter(pl.col("week") == w2)
        curr_layer = dict(zip(curr["stock_id"].to_list(), curr["layer"].to_list()))
        enriched = (enriched_by_week or {}).get(w2)
        for r in prev.iter_rows(named=True):
            to_layer = curr_layer.get(r["stock_id"])
            r1 = _LAYER_ORDER.get(r["layer"], 9)
            r2 = _LAYER_ORDER.get(to_layer, 3) if to_layer else 3
            if r2 <= r1:
                continue
            row: dict = {
                "from_week": w1,
                "to_week": w2,
                "stock_id": r["stock_id"],
                "name": r["name"],
                "from_layer": r["layer"],
                "to_layer": to_layer or _ABSENT,
                **{c: None for c in signal_cols},
            }
            if enriched is not None and not enriched.is_empty():
                hit = enriched.filter(pl.col("stock_id").cast(pl.Utf8) == r["stock_id"])
                if not hit.is_empty():
                    h = hit.row(0, named=True)
                    for c in signal_cols:
                        if c in hit.columns:
                            row[c] = h[c]
            rows.append(row)
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema_overrides=schema).select(list(schema))


def _pct(v: object) -> str:
    return f"{float(v):+.1f}%" if isinstance(v, (int, float)) else "—"


def _rate(v: object) -> str:
    return f"{float(v):.0%}" if isinstance(v, (int, float)) else "—"


def _entry_date_cell(entry_date: object, late: bool) -> str:
    """WS-L：進場日欄值；late_entry=True → 加 `*`（footnote 見呼叫端）。"""
    if not isinstance(entry_date, date):
        return "—"
    return f"{entry_date}*" if late else str(entry_date)


def _layer_label(layer: str | None) -> str:
    return _LAYER_LABEL.get(layer or "", layer or "—")


def render_outcome_report(
    todate: pl.DataFrame,
    layers: pl.DataFrame,
    weekly_core: pl.DataFrame,
    hold_summary: pl.DataFrame,
    counterfactual: pl.DataFrame,
    excluded_todate: pl.DataFrame,
    missing_weeks: list[str],
    exit_date: date | None,
    data_range: tuple,
    diff: pl.DataFrame | None = None,
    min_sample_warn: int = 20,
    stop_delay: pl.DataFrame | None = None,
    contrarian_recall: dict[str, object] | None = None,
) -> str:
    """pick 閉環 markdown 報告（PO2 快照＋持有窗、PO4 偽陰性、PO3 翻轉解剖、M3.1 停損延遲、
    M1.6 左側解禁記帳）。"""
    d0, d1 = data_range
    n_weeks = todate["week"].n_unique() if not todate.is_empty() else 0
    lines = [
        "# Pick 閉環報告（規劃書 05 F1）",
        "",
        f"- 覆蓋週次：{n_weeks} 週；評估截止：{exit_date or d1}；價格資料 {d0} ~ {d1}。",
        "- entry＝pick 資料日次一交易日收盤（防前視）；個股報酬含現金股利加回；"
        "大盤基準＝同窗快取宇宙等權**中位**（未加回股利，§1.2 口徑）。",
        "- 樣本僅數週、跨單一 regime——**方向性使用**，門檻校準每季隨樣本變厚重跑。",
    ]
    if missing_weeks:
        lines.append(
            f"- ⚠️ 產物斷供如實標：{('、'.join(missing_weeks))} 有篩選結果但無 picks.csv。"
        )
    n_total = todate.filter(pl.col("status") == "matured").height if not todate.is_empty() else 0
    if n_total < min_sample_warn:
        lines.append(f"- ⚠️ 有效樣本 {n_total} < {min_sample_warn}，結論僅供方向性參考。")

    lines += ["", "## 1. 分層到期快照（entry → 截止日）", ""]
    if layers.is_empty():
        lines.append("> 無可統計樣本。")
    else:
        lines += [
            "| 層 | n | 勝率 | 平均報酬 | 中位報酬 | α vs 大盤 | 勝過大盤 "
            "| α vs 族群(n) | 最差單檔 | 最深回撤 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in layers.iter_rows(named=True):
            sub = (
                f"{_pct(r['avg_alpha_subind_pct'])}({r['n_subind']})"
                if r["n_subind"] else "—"
            )
            lines.append(
                f"| {_layer_label(r['layer'])} | {r['n']} | {_rate(r['win_rate'])} "
                f"| {_pct(r['avg_return_pct'])} | {_pct(r['median_return_pct'])} "
                f"| {_pct(r['avg_alpha_market_pct'])} | {_rate(r['win_vs_market_rate'])} "
                f"| {sub} | {_pct(r['worst_return_pct'])} | {_pct(r['worst_path_min_pct'])} |"
            )

    lines += ["", "## 2. 核心層逐週 α（§1.2 重現格式）", ""]
    if weekly_core.is_empty():
        lines.append("> 無核心層樣本。")
    else:
        lines += [
            "| 週次 | n | 核心平均報酬 | 同窗市場中位 | 組合 α | 勝率 |",
            "|---|---|---|---|---|---|",
        ]
        for r in weekly_core.iter_rows(named=True):
            lines.append(
                f"| {r['week']} | {r['n']} | {_pct(r['avg_return_pct'])} "
                f"| {_pct(r['market_return_pct'])} | {_pct(r['avg_alpha_pct'])} "
                f"| {r['wins']}/{r['n']} |"
            )

    lines += ["", "## 3. 固定持有窗（複用 V1 機制；未到期排除、除息還原）", ""]
    if hold_summary.is_empty():
        lines.append("> 無到期樣本（持有窗未滿，隨週數累積變厚）。")
    else:
        lines += [
            "| 層 | 持有週 | 樣本 | 勝率 | 平均報酬 | 中位報酬 | 最差單檔 | 平均超額 | 勝過大盤 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in hold_summary.iter_rows(named=True):
            n = r["sample_count"] or 0
            warn = " ⚠️" if n < min_sample_warn else ""
            lines.append(
                f"| {_layer_label(r['strategy_id'])} | {r['hold_weeks']} | {n}{warn} "
                f"| {_rate(r['win_rate'])} | {_pct(r['avg_return_pct'])} "
                f"| {_pct(r['median_return_pct'])} | {_pct(r['max_drawdown_pct'])} "
                f"| {_pct(r['avg_excess_pct'])} | {_rate(r['win_rate_vs_market'])} |"
            )

    lines += ["", "## 4. 反事實：被旗標剔除的股（PO4 偽陰性帳）", ""]
    if counterfactual.is_empty():
        lines.append("> 無 excluded.csv 紀錄或全部無法起算。")
    else:
        core_avg = None
        if not layers.is_empty():
            core_rows = layers.filter(pl.col("layer") == "core")
            if not core_rows.is_empty():
                core_avg = core_rows["avg_return_pct"][0]
        lines += [
            "| 剔除原因 | n | 平均報酬 | 中位報酬 | α vs 大盤 | 跑贏大盤比例 | 最大遺珠 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in counterfactual.iter_rows(named=True):
            best = (
                f"{r['best_stock']} {_pct(r['best_return_pct'])}" if r["best_stock"] else "—"
            )
            lines.append(
                f"| {r['reason']} | {r['n']} | {_pct(r['avg_return_pct'])} "
                f"| {_pct(r['median_return_pct'])} | {_pct(r['avg_alpha_market_pct'])} "
                f"| {_rate(r['beat_market_rate'])} | {best} |"
            )
        n_ex = excluded_todate.filter(pl.col("status") == "matured").height
        if core_avg is not None and n_ex:
            all_ex = excluded_todate.filter(
                (pl.col("status") == "matured") & pl.col("return_pct").is_not_null()
            )
            lines += [
                "",
                f"> 被剔除股全體（n={n_ex}）同窗平均 {_pct(all_ex['return_pct'].mean())}"
                f"、中位 {_pct(all_ex['return_pct'].median())} vs 核心層平均 {_pct(core_avg)}——"
                "「跑贏大盤比例」即該旗標的偽陰性率；樣本薄，按季複核後才動旗標規則。",
            ]

    if diff is not None:
        lines += ["", "## 5. 翻轉解剖（PO3：週對週降級＋翻轉前訊號）", ""]
        if diff.is_empty():
            lines.append("> 相鄰紀錄週之間無降級標的。")
        else:
            lines += [
                "| 週 | 股票 | 降級 | 距月線 | 距季線 | 外資近5日 | 外資20日 | 投信近5日 | 旗標 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for r in diff.iter_rows(named=True):
                def _lots(v: object) -> str:
                    return f"{float(v):+,.0f}" if isinstance(v, (int, float)) else "—"
                lines.append(
                    f"| {r['from_week']}→{r['to_week']} | {r['stock_id']} {r['name']} "
                    f"| {_layer_label(r['from_layer'])}→{_layer_label(r['to_layer'])} "
                    f"| {_pct(r['ma20_dist_pct'])} | {_pct(r['ma60_dist_pct'])} "
                    f"| {_lots(r['foreign_net_5d_lots'])} | {_lots(r['foreign_net_lots'])} "
                    f"| {_lots(r['trust_net_5d_lots'])} | {r['flags'] or '—'} |"
                )

    if stop_delay is not None:
        lines += ["", *render_stop_delay_section(stop_delay)]
    if contrarian_recall is not None:
        lines += ["", *render_contrarian_recall_section(contrarian_recall)]

    return "\n".join(lines)


def render_weekly_brief(
    picks_r: pl.DataFrame,
    excluded_r: pl.DataFrame,
    week: str,
    data_date: date | None,
    horizon_td: int = 5,
    bucket_ledger: pl.DataFrame | None = None,
    stop_summary: dict[str, object] | None = None,
    bucket_horizons: tuple[int, ...] = (5, 20),
) -> str:
    """WS-A3 一頁 brief：上週 picks r+{h}／α／勝率＋excluded 偽陰性（進下週輸入包）。

    picks_r／excluded_r＝compute_forward_returns(hold_weeks=1) 輸出（strategy_id 欄
    分別承載 layer／剔除 reason），可選帶 late_entry 欄（bool，來自 pick_store 底帳，
    WS-L）——picks 表「進場日」欄值後加 `*` 並附註腳，標出基準窗與同週其他列不同步的列。
    只讀 matured 列；無到期樣本 → 誠實標註不編數字。
    """
    lines = [
        f"# 上週 picks 短櫃（{week}・r+{horizon_td}）",
        "",
        f"- 評估週 {week}（data_date {data_date or '未取得'}）；entry＝次一交易日收盤、"
        f"exit＝entry 後第 {horizon_td} 交易日；除息還原；α＝減同窗等權全市場。",
        "- 用途：下週選股輸入包的「上週結果回饋」一頁；完整分層/反事實帳見季度 "
        "`make pick-outcome`。",
        "",
        # M6：決策卡固定一行。放最上面＝週報 prompt 抄一行就好，不必讀完整頁。
        weekly_ledger_line(
            picks_r, bucket_ledger if bucket_ledger is not None else pl.DataFrame(),
            stop_summary, horizon_td,
        ),
        "",
    ]
    bucket_lines = render_excluded_buckets(
        bucket_ledger if bucket_ledger is not None else pl.DataFrame(), bucket_horizons
    )
    valid = _matured(picks_r)
    if valid.is_empty():
        lines.append("> picks r+5 尚無到期樣本（快取未跨窗）——本頁僅佔位，資料到齊自動補。")
        return "\n".join(lines + bucket_lines)

    def _fmean(df: pl.DataFrame, col: str) -> float:
        v = df[col].mean()
        return float(v) if isinstance(v, (int, float)) else 0.0

    n = valid.height
    win = valid.filter(pl.col("return_pct") > 0).height
    beat = valid.filter(pl.col("excess_return_pct") > 0).height
    avg_r = _fmean(valid, "return_pct")
    avg_a = _fmean(valid, "excess_return_pct")
    lines += [
        f"## picks（n={n}）",
        "",
        f"- 勝率（絕對）**{win}/{n}**・勝過大盤 **{beat}/{n}**・"
        f"平均 r+{horizon_td} **{avg_r:+.2f}%**・平均 α **{avg_a:+.2f}pp**。",
        "",
        "| 層 | 股票 | 進場日 | r+5 | 大盤 | α |",
        "|---|---|---|---|---|---|",
    ]
    order = {"core": 0, "opportunity": 1, "pool": 2}
    ordered = valid.with_columns(
        pl.col("strategy_id").replace_strict(order, default=9).alias("_o")
    ).sort("_o", "excess_return_pct", descending=[False, True])
    has_late = "late_entry" in ordered.columns
    any_late = False
    for r in ordered.iter_rows(named=True):
        late = bool(r.get("late_entry")) if has_late else False
        any_late = any_late or late
        lines.append(
            f"| {_layer_label(r['strategy_id'])} | {r['stock_id']} {r['name'] or ''} "
            f"| {_entry_date_cell(r['entry_date'], late)} "
            f"| {_pct(r['return_pct'])} | {_pct(r['market_return_pct'])} "
            f"| {_pct(r['excess_return_pct'])} |"
        )
    if any_late:
        lines += [
            "",
            "> `*`＝該檔 late_entry（快取缺資料等致 entry 順延，基準窗與同週其他列不同步、"
            "數字不可比）。",
        ]

    lines += ["", "## excluded 偽陰性（同窗）", ""]
    evalid = (
        excluded_r.filter((pl.col("status") == "matured") & pl.col("return_pct").is_not_null())
        if not excluded_r.is_empty()
        else pl.DataFrame()
    )
    if evalid.is_empty():
        lines.append("> 該週無 excluded 紀錄或尚未到期。")
    else:
        en = evalid.height
        ebeat = evalid.filter(pl.col("excess_return_pct") > 0)
        lines.append(
            f"- 剔除 {en} 檔、其中 **{ebeat.height} 檔跑贏大盤**（偽陰性候選）；"
            f"剔除組平均 r+{horizon_td} {_fmean(evalid, 'return_pct'):+.2f}%。"
        )
        if not ebeat.is_empty():
            lines += [
                "",
                "| 旗標 | 股票 | r+5 | α |",
                "|---|---|---|---|",
                *[
                    f"| {r['strategy_id']} | {r['stock_id']} {r['name'] or ''} "
                    f"| {_pct(r['return_pct'])} | {_pct(r['excess_return_pct'])} |"
                    for r in ebeat.sort("excess_return_pct", descending=True).iter_rows(
                        named=True
                    )
                ],
            ]
    return "\n".join(lines + bucket_lines)


# ── M6 偽陰性帳上決策卡（委託書 M6）─────────────────────────────────────────

_BUCKET_SCHEMA: dict[str, type[pl.DataType]] = {
    "reason": pl.Utf8,
    "horizon_td": pl.Int64,
    "n": pl.Int64,
    "n_beat_market": pl.Int64,
    "avg_return_pct": pl.Float64,
    "picks_avg_return_pct": pl.Float64,
    "gap_pp": pl.Float64,
}


def excluded_bucket_ledger(
    returns_by_horizon: dict[int, tuple[pl.DataFrame, pl.DataFrame]],
) -> pl.DataFrame:
    """M6 每個 excluded `reason` 桶 × 各前瞻窗的報酬，並排同窗 picks 平均。

    Args:
        returns_by_horizon: {交易日窗: (picks_returns, excluded_returns)}——兩者皆
            `compute_forward_returns` 輸出，`strategy_id` 分別承載 layer／剔除 reason。

    Returns:
        `_BUCKET_SCHEMA` 長表，每列＝(reason, horizon)。`gap_pp`＝該桶平均 − 同窗
        picks 平均：**正值＝剔除掉的比選進來的還會漲**（偽陰性代價）。

    為什麼要並排 picks 而不是只看桶內絕對報酬：整週大盤漲 3% 時，剔除桶 +2% 看起來
    「漏掉了」，但同期 picks +5%——剔除其實是對的。沒有同窗基準的桶平均會系統性地
    製造「早知道就別剔除」的錯覺（docs/22 §2 flow_turn 的同型錯誤）。

    受控詞彙（docs/11）讓桶數收斂在 10 種內；詞彙自由發揮＝每桶 1–2 筆、統計碎裂。
    """
    rows: list[dict] = []
    for h in sorted(returns_by_horizon):
        picks_r, excl_r = returns_by_horizon[h]
        pv = _matured(picks_r)
        ev = _matured(excl_r)
        if ev.is_empty():
            continue
        picks_avg = (
            float(cast(float, pv["return_pct"].mean() or 0.0)) if not pv.is_empty() else None
        )
        grouped = ev.group_by("strategy_id").agg(
            pl.len().alias("n"),
            (pl.col("excess_return_pct") > 0).sum().alias("n_beat_market"),
            pl.col("return_pct").mean().alias("avg_return_pct"),
        )
        for r in grouped.iter_rows(named=True):
            avg = float(r["avg_return_pct"]) if r["avg_return_pct"] is not None else None
            rows.append({
                "reason": str(r["strategy_id"]),
                "horizon_td": int(h),
                "n": int(r["n"]),
                "n_beat_market": int(r["n_beat_market"]),
                "avg_return_pct": avg,
                "picks_avg_return_pct": picks_avg,
                "gap_pp": (
                    round(avg - picks_avg, 2)
                    if avg is not None and picks_avg is not None
                    else None
                ),
            })
    if not rows:
        return pl.DataFrame(schema=_BUCKET_SCHEMA)
    return pl.DataFrame(rows, schema=_BUCKET_SCHEMA).sort(
        ["horizon_td", "gap_pp"], descending=[False, True], nulls_last=True
    )


def _matured(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "status" not in df.columns:
        return pl.DataFrame()
    return df.filter(
        (pl.col("status") == "matured") & pl.col("return_pct").is_not_null()
    )


def worst_bucket(ledger: pl.DataFrame, horizon_td: int = 5) -> dict[str, object] | None:
    """該窗「最痛」的剔除桶＝`gap_pp` 最大者（剔除掉的比選進來的漲最多）。

    回 None＝該窗沒有可判讀的桶（無到期樣本／picks 同窗無基準）。**不回傳「最痛桶
    ＝空」的假象**：呼叫端要印「未取得」而不是印 0。
    """
    if ledger.is_empty():
        return None
    sub = ledger.filter(
        (pl.col("horizon_td") == horizon_td) & pl.col("gap_pp").is_not_null()
    ).sort("gap_pp", descending=True)
    if sub.is_empty():
        return None
    return dict(sub.row(0, named=True))


def weekly_ledger_line(
    picks_returns: pl.DataFrame,
    ledger: pl.DataFrame,
    stop_summary: dict[str, object] | None = None,
    horizon_td: int = 5,
) -> str:
    """M6 決策卡固定一行「上週帳」（委託書 M6／M7 patch-4）。

    格式：`**上週帳**：picks r+5 中位 ○%｜excluded 最痛桶「○○」r+5 ○%（漏 N 檔）｜
    停損延遲成本 ○%`。三格各自獨立降級為「未取得」——**任何一格缺資料都不編數字，
    也不讓整行消失**（整行消失＝週報少一行沒人會發現；印「未取得」才會被追）。
    """
    pv = _matured(picks_returns)
    if pv.is_empty():
        picks_cell = f"picks r+{horizon_td} 中位 未取得"
    else:
        med = pv["return_pct"].median()
        picks_cell = (
            f"picks r+{horizon_td} 中位 {float(cast(float, med)):+.1f}%（n={pv.height}）"
            if med is not None
            else f"picks r+{horizon_td} 中位 未取得"
        )

    wb = worst_bucket(ledger, horizon_td)
    if wb is None:
        bucket_cell = "excluded 最痛桶 未取得（該窗無到期剔除樣本或無 picks 基準）"
    else:
        bucket_cell = (
            f"excluded 最痛桶「{wb['reason']}」r+{horizon_td} "
            f"{float(cast(float, wb['avg_return_pct'])):+.1f}%"
            f"（vs picks {float(cast(float, wb['picks_avg_return_pct'])):+.1f}%、"
            f"漏 {wb['n_beat_market']}/{wb['n']} 檔跑贏大盤）"
        )

    avg_cost = (stop_summary or {}).get("avg_cost_pct")
    if avg_cost is None:
        # 「未取得」要說得出為什麼——0 筆可量測多半是停損欄寫成「跌破季線」這類
        # 抽不出絕對價的敘述（M3／patch-2 正是要修這件事），不寫出來就沒人會去改。
        counts = cast("dict[str, int]", (stop_summary or {}).get("counts") or {})
        n_all = sum(counts.values())
        stop_cell = (
            f"停損延遲成本 未取得（{n_all} 筆中 0 筆可量測）" if n_all
            else "停損延遲成本 未取得"
        )
    else:
        n_meas = (stop_summary or {}).get("n_measured", 0)
        stop_cell = f"停損延遲成本 {float(cast(float, avg_cost)):+.2f}%（n={n_meas}）"

    return f"**上週帳**：{picks_cell}｜{bucket_cell}｜{stop_cell}"


def render_excluded_buckets(
    ledger: pl.DataFrame, horizons: tuple[int, ...] = (5, 20)
) -> list[str]:
    """M6 brief 的「excluded 分桶回饋帳」段。無資料時印缺席原因，不省略整段。"""
    lines = ["", "## excluded 分桶回饋帳（委託書 M6）", ""]
    if ledger.is_empty():
        lines.append(
            "> **回饋帳缺席**：該週無 excluded 到期樣本（或底帳無 excluded 紀錄）。"
            "本段不編數字。"
        )
        return lines
    lines += [
        "- `gap`＝該桶平均 − **同窗 picks 平均**：**正＝剔除掉的比選進來的還會漲**"
        "（偽陰性代價）；負＝剔除是對的。只看桶內絕對報酬會在多頭週系統性製造"
        "「早知道就別剔除」的錯覺，故一律並排基準。",
        "- 樣本量小（多數桶個位數）＝**趨勢參考、非統計結論**；受控詞彙見 docs/11。",
        "",
        "| 窗 | 剔除理由 | n | 跑贏大盤 | 桶平均 | 同窗 picks | gap |",
        "|---|---|---|---|---|---|---|",
    ]
    missing: list[int] = []
    for h in horizons:
        sub = ledger.filter(pl.col("horizon_td") == h)
        if sub.is_empty():
            missing.append(h)
            continue
        for r in sub.iter_rows(named=True):
            lines.append(
                f"| r+{h} | {r['reason']} | {r['n']} | {r['n_beat_market']} "
                f"| {_pct(r['avg_return_pct'])} | {_pct(r['picks_avg_return_pct'])} "
                f"| {_pct(r['gap_pp'])} |"
            )
    if missing:
        # 某窗整個消失＝沒人會發現的資料缺口。明寫比默默少一段誠實。
        lines += [
            "",
            "> **"
            + "／".join(f"r+{h}" for h in missing)
            + " 該窗尚無到期樣本**（日線快取未跨到該窗終點）——非「無偽陰性」，"
            "是**還沒到期**，資料到齊會自動補上。",
        ]
    return lines
