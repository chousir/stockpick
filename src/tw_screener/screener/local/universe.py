"""本地候選宇宙：不打 Goodinfo，純用 TWSE/TPEX 官方快取拼出全市場寬表。

Goodinfo 篩選器端點被 Cloudflare 擋下後（見 playbook/90），部分策略（如 F 價值反彈）
的門檻其實已可由官方 API 覆蓋（市值、PE、殖利率、累計營收YoY），本模組把這些欄位
併成一張全市場寬表，供 `filter.apply_local_filters()` 套用策略門檔。

純讀既有快取（各 client.fetch_*/load_latest_* 各自處理快取/重抓邏輯，本模組不額外
決定 TTL），全離線測試需求靠 `apply_local_filters()` 的純函式邊界滿足——本檔的
`build_local_universe()` 需真實 client，測試走 tests/screener/local/。
"""

from __future__ import annotations

import polars as pl
from loguru import logger

_UNIVERSE_SCHEMA: dict[str, type[pl.DataType]] = {
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "market": pl.Utf8,
    "close": pl.Float64,
    "market_cap_billion": pl.Float64,
    "pe_ratio": pl.Float64,
    "pb_ratio": pl.Float64,
    "dividend_yield_pct": pl.Float64,
    "cum_rev_yoy_pct": pl.Float64,
}


def build_local_universe(client) -> pl.DataFrame:  # noqa: ANN001 — TWSEClient，避免循環 import
    """併「收盤價 + 市值 + PE + 殖利率 + 累計營收YoY」成全市場（上市+上櫃）寬表。

    任一來源缺快取時該欄整體 null（不擋其他欄位），呼叫端（`apply_local_filters`）
    的 null 排除語意會自然把缺資料股踢出結果，不用在這裡特判。
    """
    from tw_screener.analysis.valuation import market_cap_billion

    listed = client.fetch_daily_all()
    otc = client.fetch_otc_daily_all()
    frames = [df for df in (listed, otc) if not df.is_empty()]
    if not frames:
        logger.warning("build_local_universe：上市+上櫃日線皆無快取，回空表")
        return pl.DataFrame(schema=_UNIVERSE_SCHEMA)
    daily = pl.concat(frames, how="diagonal_relaxed").unique("stock_id", keep="first")

    shares_rows: dict[str, int | None] = {}
    for shares_df in (client.fetch_listed_shares(), client.fetch_otc_shares()):
        if shares_df.is_empty() or "stock_id" not in shares_df.columns:
            continue
        for r in shares_df.iter_rows(named=True):
            shares_rows[str(r["stock_id"])] = r.get("shares_outstanding")

    val_df = client.load_latest_valuation_ratios()
    val_rows: dict[str, dict] = (
        {str(r["stock_id"]): r for r in val_df.iter_rows(named=True)}
        if not val_df.is_empty()
        else {}
    )

    rev_df = client.fetch_revenue()
    cum_yoy_rows: dict[str, float | None] = {}
    if not rev_df.is_empty() and "stock_id" in rev_df.columns:
        rdf = rev_df
        if "year_month" in rev_df.columns:
            rdf = rev_df.sort("year_month", descending=True)
        for r in rdf.iter_rows(named=True):
            cum_yoy_rows.setdefault(str(r["stock_id"]), r.get("cum_yoy_pct"))

    out_rows = []
    for r in daily.iter_rows(named=True):
        sid = str(r["stock_id"])
        close = r.get("close")
        shares = shares_rows.get(sid)
        vrow = val_rows.get(sid) or {}
        out_rows.append(
            {
                "stock_id": sid,
                "name": r.get("name"),
                "market": "上市" if sid in _listed_ids(listed) else "上櫃",
                "close": close,
                "market_cap_billion": market_cap_billion(shares, close),
                "pe_ratio": vrow.get("pe"),
                "pb_ratio": vrow.get("pbr"),
                "dividend_yield_pct": vrow.get("dividend_yield"),
                "cum_rev_yoy_pct": cum_yoy_rows.get(sid),
            }
        )
    return pl.DataFrame(out_rows, schema=_UNIVERSE_SCHEMA)


def _listed_ids(listed_daily: pl.DataFrame) -> frozenset[str]:
    if listed_daily.is_empty() or "stock_id" not in listed_daily.columns:
        return frozenset()
    return frozenset(str(sid) for sid in listed_daily["stock_id"].to_list())
