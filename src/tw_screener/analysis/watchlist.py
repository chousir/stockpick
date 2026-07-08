"""庫存／觀察清單與篩選結果的讀檔＋enrich 共用邏輯（自 cli.py 下沉）。

原本內嵌在 cli.py 多個命令（analysis group/leaders、sector rotation、portfolio
check、cp candidates）裡，搬出後可獨立單測、跨命令重用。行為與搬出前一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from tw_screener.screener.goodinfo.url_builder import stock_detail_url

if TYPE_CHECKING:
    import polars as pl

    from tw_screener.data.twse import TWSEClient

console = Console()

# enrich 需要 MA60 → 快取列數低於此就回補單檔歷史（不只全空才補）。
# 全市場 daily_* 只能向未來累積：純觀察股（從未當過候選）沒有單檔快取，
# 只靠 daily_* 短窗會算不出 MA20/MA60（W28 曾 19/40 檔均線盲區、F2 無法查核）。
_MIN_HISTORY_ROWS = 60


def load_latest_screener_results(settings: Path) -> tuple[str, dict]:
    """找最新一週的 screen_result_*.csv，回傳 (week_tag, {strategy_id: DataFrame})。"""
    import polars as _pl
    import yaml as _yaml

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    rdir = Path(cfg["paths"]["reports_dir"])
    if not rdir.exists():
        return "", {}

    week_dirs = sorted(
        [d for d in rdir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not week_dirs:
        return "", {}

    week_dir = week_dirs[0]
    week_tag = week_dir.name

    results: dict = {}
    for csv_file in sorted(week_dir.glob("screen_result_*.csv")):
        sid = csv_file.stem.replace("screen_result_", "")
        try:
            df = _pl.read_csv(str(csv_file), infer_schema_length=1000)
            results[sid] = df
        except Exception as exc:  # noqa: BLE001 — 單檔壞不擋整批
            console.print(f"[yellow]讀取 {csv_file.name} 失敗：{exc}[/yellow]")

    return week_tag, results


def read_watchlist_csv(path: Path) -> list[str]:
    """讀觀察清單 CSV（欄：stock_id[,note]）→ 股號清單。檔不存在回空。"""
    import polars as _pl

    if not path.exists():
        return []
    try:
        df = _pl.read_csv(str(path), infer_schema_length=0)  # 全當字串、保留前導 0
    except Exception:  # noqa: BLE001 — 清單檔壞掉誠實回空、主流程照跑
        return []
    if "stock_id" not in df.columns:
        return []
    return [str(s).strip() for s in df["stock_id"].to_list() if s and str(s).strip()]


def read_holdings_csv(path: Path) -> dict:
    """讀庫存 CSV（欄：stock_id,buy_price[,shares,note]）→ {股號: {buy_price, shares}}。"""
    import polars as _pl

    if not path.exists():
        return {}
    try:
        df = _pl.read_csv(str(path), infer_schema_length=0)
    except Exception:  # noqa: BLE001 — 清單檔壞掉誠實回空、主流程照跑
        return {}
    if "stock_id" not in df.columns:
        return {}

    def _f(v: object) -> float | None:
        try:
            return float(str(v).replace(",", "")) if v not in (None, "") else None
        except ValueError:
            return None

    out: dict = {}
    for r in df.iter_rows(named=True):
        sid = str(r.get("stock_id") or "").strip()
        if sid:
            out[sid] = {"buy_price": _f(r.get("buy_price")), "shares": _f(r.get("shares"))}
    return out


def enrich_named_list(
    client: TWSEClient,
    stock_ids: list[str],
    industry_df: pl.DataFrame | None,
    institutional: pl.DataFrame,
    g_pullback: dict[str, float] | None,
    name_map: dict[str, str] | None = None,
    vol_lookback: int = 20,
    dividends: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, dict]:
    """把任意股票清單 enrich 成 (members, synth_screener)，reuse group_stocks 同套指標。

    各股先 fetch_stock_ohlcv 讀快取；快取沒有（多為上櫃股、不在上市 daily）就主動
    fetch_stock_history 補抓（OTC 自動走 TPEX），再丟 group_stocks 算 momentum/MA/量比/
    法人。回傳 members（含技術籌碼欄）＋ synth（供 writer 取 close/量）。
    """
    import polars as _pl

    from tw_screener.analysis.grouping import group_stocks

    ids = list(dict.fromkeys(str(s).strip() for s in stock_ids if str(s).strip()))

    # name fallback：name_map（月營收，僅上市）缺名時，補 industry_df.stock_name（含上櫃）
    name_fallback: dict[str, str] = {}
    if (
        industry_df is not None
        and not industry_df.is_empty()
        and {"stock_id", "stock_name"}.issubset(industry_df.columns)
    ):
        for _id, _nm in industry_df.select(["stock_id", "stock_name"]).iter_rows():
            name_fallback.setdefault(str(_id), str(_nm or ""))

    def _name(sid: str) -> str:
        return (name_map or {}).get(sid) or name_fallback.get(sid, "") or ""

    frames, rows = [], []
    for sid in ids:
        oh = client.fetch_stock_ohlcv(sid, n_days=100)
        if oh.height < _MIN_HISTORY_ROWS:
            # 快取不足 MA60 視窗（含全空）→ 主動抓歷史（上櫃股自動走 TPEX），再讀一次。
            # 過去月份永久快取、當月吃 TTL，重複呼叫近零成本（上市未滿 60 日者亦安全）。
            client.fetch_stock_history(sid, months=6)
            oh = client.fetch_stock_ohlcv(sid, n_days=100)
        if oh.is_empty():
            console.print(f"[yellow]  {sid}：抓不到 OHLCV，跳過（可能下市或代號錯）[/yellow]")
            continue
        oh = oh.sort("date")
        frames.append(oh.select(["stock_id", "date", "close", "trade_volume"]))
        d = oh.tail(1).to_dicts()[0]
        close = float(d.get("close") or 0.0)
        chg = float(d.get("change") or 0.0)
        prev = close - chg
        rows.append(
            {
                "stock_id": sid,
                "name": _name(sid),
                "close": close,
                "change_pct": round(chg / prev * 100.0, 2) if prev else 0.0,
                "amount_million": round(float(d.get("trade_value") or 0) / 1_000_000.0, 2),
                "volume_lots": round(float(d.get("trade_volume") or 0) / 1000.0),
                "pe_ratio": None,
                "pb_ratio": None,
                "goodinfo_url": stock_detail_url(sid),
                "strategy_id": "_list",
            }
        )
    if not rows:
        return _pl.DataFrame(), {}
    synth = _pl.DataFrame(rows)
    price_history = _pl.concat(frames, how="vertical")
    volume_history = price_history.select(["stock_id", "date", "trade_volume"])
    _, members = group_stocks(
        {"_list": synth},
        price_history,
        _pl.DataFrame(),
        industry_df=industry_df,
        institutional=institutional,
        volume_history=volume_history,
        g_pullback=g_pullback,
        vol_lookback=vol_lookback,
        dividends=dividends,
        skip_etf=False,  # 持股/觀察清單的 ETF 產輕量列（docs/21）；選股宇宙仍排除
    )
    # ETF 列 industry 標「ETF」（不混入「未分類」）；基本面/族群欄由後段誠實留 null
    if members.height and "industry_name" in members.columns:
        from tw_screener.analysis.grouping import is_etf_or_warrant

        etf_ids = [s for s in ids if is_etf_or_warrant(s)]
        if etf_ids:
            members = members.with_columns(
                _pl.when(_pl.col("stock_id").is_in(etf_ids))
                .then(_pl.lit("ETF"))
                .otherwise(_pl.col("industry_name"))
                .alias("industry_name")
            )
    return members, {"_list": synth}
