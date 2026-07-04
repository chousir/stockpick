"""個股報告資料收集器：從快取抓取並格式化個股所需資料。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

from tw_screener.data.twse import create_client

_STRATEGY_LABEL: dict[str, str] = {
    "a_breakout": "A（動能突破）",
    "b_growth_institutional": "B（成長主力）",
    "c_dividend_steady": "C（穩健存股）",
    "c_quality_value": "C（品質價值）",
    "d_quality_leader": "D（品質龍頭）",
    "e_growth_momentum": "E（成長動能）",
    "f_value_rebound": "F（價值反彈）",
}


def _format_price_summary(df: pl.DataFrame) -> str:
    if df.is_empty():
        return "未取得（請先執行 make fetch-twse 累積日線快取）"

    latest = df.tail(1).row(0, named=True)
    oldest = df.head(1).row(0, named=True)
    closes = df["close"].to_list()
    ma20 = round(sum(closes[-20:]) / min(20, len(closes)), 2) if closes else 0.0
    ma60 = round(sum(closes[-60:]) / min(60, len(closes)), 2) if closes else 0.0

    lines = [
        f"期間：{oldest['date']} ～ {latest['date']}（共 {len(df)} 個交易日）",
        f"最新收盤：{latest['close']:.1f}  漲跌：{latest.get('change', 0):+.2f}",
        f"期間最高：{df['high'].max():.1f}  期間最低：{df['low'].min():.1f}",
        f"MA20：{ma20:.1f}  MA60：{ma60:.1f}",
        f"最新成交量：{latest.get('trade_volume', 0):,} 張",
    ]
    return "\n".join(lines)


def _format_revenue_summary(df: pl.DataFrame) -> str:
    if df.is_empty():
        return "未取得（請先執行 make fetch-twse 累積月營收快取）"

    header = f"{'月份':>8}  {'月營收(千元)':>14}  {'YoY(%)':>8}"
    lines = [header, "-" * 36]
    for row in df.sort("year_month").iter_rows(named=True):
        yoy = f"{row['yoy_pct']:+.1f}%" if row.get("yoy_pct") is not None else "  N/A"
        rev = f"{row['revenue']:,}" if row.get("revenue") else "N/A"
        lines.append(f"{row['year_month']:>8}  {rev:>14}  {yoy:>8}")
    return "\n".join(lines)


def _format_institutional_summary(df: pl.DataFrame) -> str:
    if df.is_empty():
        return "未取得（T86 端點目前不可用，或快取尚無此股資料）"

    header = f"{'日期':>12}  {'外資(張)':>10}  {'投信(張)':>8}  {'自營商(張)':>10}  {'合計(張)':>8}"
    lines = [header, "-" * 56]
    for row in df.sort("date").iter_rows(named=True):
        lines.append(
            f"{str(row['date']):>12}  {row.get('foreign_net', 0):>10,}  "
            f"{row.get('trust_net', 0):>8,}  {row.get('dealer_net', 0):>10,}  "
            f"{row.get('total_net', 0):>8,}"
        )

    totals = df.select(
        pl.col("foreign_net").sum(),
        pl.col("trust_net").sum(),
        pl.col("dealer_net").sum(),
        pl.col("total_net").sum(),
    ).row(0)
    lines.append(
        f"\n累計 {len(df)} 日：外資 {totals[0]:+,} 張、投信 {totals[1]:+,} 張、"
        f"自營商 {totals[2]:+,} 張、合計 {totals[3]:+,} 張"
    )
    return "\n".join(lines)


def _format_big_holder_summary(row: dict | None, data_date: object) -> str:
    """格式化單檔集保大戶持股比（占集保庫存≈流通量，非占股本）＋WoW。"""
    if not row or row.get("big_holder_pct") is None:
        return "未取得（TDCC 集保分散表本週無此股資料；可執行 make fetch-tdcc 累積）"

    def _pct(v: object) -> str:
        return f"{float(v):.2f}%" if v is not None else "未取得"  # type: ignore[arg-type]

    def _pp(v: object) -> str:
        return f"週變化 {float(v):+.2f}pp" if v is not None else "週變化 N/A（僅一週資料）"  # type: ignore[arg-type]

    return "\n".join(
        [
            f"資料日：{data_date}（占集保庫存≈流通量比例，非占股本）",
            f"≥400 張大戶：{_pct(row.get('big_holder_pct'))}（{_pp(row.get('big_holder_wow'))}）",
            f"≥1000 張（千張大戶）：{_pct(row.get('big_holder_1000_pct'))}"
            f"（{_pp(row.get('big_holder_1000_wow'))}）",
        ]
    )


def _format_margin_summary(row: dict | None) -> str:
    """格式化單檔上市融資融券（MI_MARGN，單位張）＋券資比。"""
    if not row or row.get("margin_balance") is None:
        return "未取得（上市 MI_MARGN 無此股，或上櫃股未涵蓋；可執行 make fetch-twse 累積）"

    def _i(v: object) -> int | None:
        return int(v) if v is not None else None  # type: ignore[call-overload]

    mb = _i(row.get("margin_balance"))
    mc = _i(row.get("margin_chg"))
    mc5 = _i(row.get("margin_chg_5d"))
    sb = _i(row.get("short_balance"))
    sc = _i(row.get("short_chg"))
    chg5 = f"近5日 {mc5:+,}" if mc5 is not None else "近5日 N/A（資料累積中）"
    short_ratio = (
        f"，券資比 {sb / mb * 100:.1f}%" if (mb and mb > 0 and sb is not None) else ""
    )
    return "\n".join(
        [
            f"資料日：{row.get('date', '')}（上市，單位：張）",
            f"融資餘額 {mb:,} 張（日增減 {mc:+,}、{chg5}）",
            f"融券餘額 {sb:,} 張（日增減 {sc:+,}）{short_ratio}",
        ]
    )


def _format_fundamentals_summary(row: dict | None) -> str:
    """格式化單檔單季財報體質（D5）：獲利能力＋財務結構，缺值誠實標未取得、不硬湊。"""
    if not row:
        return "未取得（fundamentals 快取無此股；可執行 make fetch-twse 累積）"

    def _p(v: object, nd: int = 1) -> str:
        return f"{float(v):.{nd}f}%" if v is not None else "未取得"  # type: ignore[arg-type]

    def _f(v: object, nd: int = 2) -> str:
        return f"{float(v):.{nd}f}" if v is not None else "未取得"  # type: ignore[arg-type]

    y, q = row.get("year"), row.get("quarter")
    lines = [
        f"資料季：{y} Q{q}（單季）" if y and q else "資料季：未知",
        f"獲利能力：毛利率 {_p(row.get('gross_margin_pct'))}、"
        f"營益率 {_p(row.get('op_margin_pct'))}、"
        f"稅前純益率 {_p(row.get('pretax_margin_pct'))}、"
        f"稅後純益率 {_p(row.get('net_margin_pct'))}",
        f"單季EPS：{_f(row.get('eps'))} 元　"
        f"單季ROE：{_p(row.get('roe_q_pct'))}（＝EPS/每股淨值，未年化）",
        f"財務結構：負債比 {_p(row.get('debt_ratio_pct'))}、"
        f"流動比 {_f(row.get('current_ratio'))}、"
        f"每股淨值 {_f(row.get('bvps'))} 元",
        "（負債比/ROE 僅一般業；金融業與營業現金流、存貨/應收週轉率 OpenAPI 未提供 → 未取得）",
    ]
    return "\n".join(lines)


def _get_stock_name(stock_id: str, settings_path: Path) -> str:
    """Try to get stock name from screener CSVs (most reliable source)."""
    with open(settings_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    rdir = Path(cfg["paths"]["reports_dir"])
    if not rdir.exists():
        return ""

    week_dirs = sorted(
        [d for d in rdir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for week_dir in week_dirs[:3]:
        for csv_file in week_dir.glob("screen_result_*.csv"):
            try:
                df = pl.read_csv(str(csv_file), infer_schema_length=100)
                if "stock_id" not in df.columns or "name" not in df.columns:
                    continue
                row = df.filter(pl.col("stock_id").cast(pl.Utf8) == str(stock_id))
                if not row.is_empty():
                    return str(row["name"][0])
            except Exception:  # noqa: BLE001 — best-effort 欄位，缺就留空
                pass
    return ""


def _find_group_info(stock_id: str, settings_path: Path) -> dict:
    """Find which strategies the stock appeared in and its group ranking."""
    with open(settings_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    rdir = Path(cfg["paths"]["reports_dir"])
    if not rdir.exists():
        return {}

    week_dirs = sorted(
        [d for d in rdir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not week_dirs:
        return {}

    week_dir = week_dirs[0]
    strategy_labels: list[str] = []

    for csv_file in sorted(week_dir.glob("screen_result_*.csv")):
        sid = csv_file.stem.replace("screen_result_", "")
        try:
            df = pl.read_csv(str(csv_file), infer_schema_length=100)
            if df.is_empty():
                continue
            row = df.filter(pl.col("stock_id").cast(pl.Utf8) == str(stock_id))
            if not row.is_empty():
                strategy_labels.append(_STRATEGY_LABEL.get(sid, sid))
        except Exception:  # noqa: BLE001 — best-effort 欄位，缺就留空
            pass

    return {
        "week_tag": week_dir.name,
        "strategy_str": "、".join(strategy_labels) if strategy_labels else "未在本週篩選結果中",
    }


def fetch_stock_bundle(stock_id: str, settings_path: Path) -> dict:
    """一次取得個股報告所需的所有資料。"""
    logger.info("抓取 {} 個股資料包", stock_id)
    client = create_client(settings_path)

    # 先補 60 日 OHLCV 歷史（首次跑會打 3 次 STOCK_DAY，每月一次；後續吃快取）
    logger.info("確保 {} 近 3 個月 OHLCV 歷史已存（第一次跑約 5-10 秒）", stock_id)
    client.fetch_stock_history(stock_id, months=3)

    price_history = client.fetch_stock_ohlcv(stock_id, n_days=60)
    monthly_revenue = client.fetch_stock_revenue(stock_id, n_months=12)
    institutional = client.fetch_stock_institutional(stock_id, n_days=20)
    group_info = _find_group_info(stock_id, settings_path)
    name = _get_stock_name(stock_id, settings_path)

    # 集保大戶持股比（D3）：純讀 TDCC 快取（make fetch-tdcc 累積），無此股則誠實標未取得
    from tw_screener.data.tdcc import create_tdcc_client

    bh_df = create_tdcc_client(settings_path).load_big_holders()
    bh_row = None
    bh_date: object = ""
    if not bh_df.is_empty():
        match = bh_df.filter(pl.col("stock_id") == stock_id)
        if not match.is_empty():
            bh_row = match.row(0, named=True)
            bh_date = bh_row.get("data_date", "")

    # 上市融資融券（D4）：純讀快取（make fetch-twse 累積），無此股（含上櫃）則誠實標未取得
    margin_df = client.load_margin_signals()
    margin_row = None
    if not margin_df.is_empty():
        m = margin_df.filter(pl.col("stock_id") == stock_id)
        if not m.is_empty():
            margin_row = m.row(0, named=True)

    # 單季財報體質（D5）：純讀 fundamentals 快取，無此股則誠實標未取得
    fund_df = client.load_latest_fundamentals()
    fund_row = None
    if not fund_df.is_empty():
        f = fund_df.filter(pl.col("stock_id") == stock_id)
        if not f.is_empty():
            fund_row = f.row(0, named=True)

    # Industry
    listed_df = client.fetch_listed_industry()
    otc_df = client.fetch_otc_industry()
    if not listed_df.is_empty() and not otc_df.is_empty():
        combined = pl.concat([listed_df, otc_df])
    elif not listed_df.is_empty():
        combined = listed_df
    else:
        combined = otc_df

    industry_name = ""
    if not combined.is_empty():
        row = combined.filter(pl.col("stock_id") == stock_id)
        if not row.is_empty():
            industry_name = str(row["industry_name"][0])

    return {
        "stock_id": stock_id,
        "name": name,
        "industry_name": industry_name,
        "goodinfo_url": f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={stock_id}",
        "price_history": price_history,
        "monthly_revenue": monthly_revenue,
        "institutional": institutional,
        "price_summary": _format_price_summary(price_history),
        "revenue_summary": _format_revenue_summary(monthly_revenue),
        "institutional_summary": _format_institutional_summary(institutional),
        "big_holder_summary": _format_big_holder_summary(bh_row, bh_date),
        "margin_summary": _format_margin_summary(margin_row),
        "fundamentals_summary": _format_fundamentals_summary(fund_row),
        "group_info": group_info,
        "fetched_at": date.today().isoformat(),
    }
