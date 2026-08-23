"""G1/G2/G5 前瞻累積軌編排（docs/31 §11；自 cli.py 薄殼呼叫）。

純讀既有快取＋官方 OpenAPI 當下快照（不額外打 Goodinfo）→ 全市場當週 G1/G2/G5 判準
快照 → `research/g1_g2_g5_watch/ledger.csv` 底帳（append-only，(week, stock_id) 去重）。

⚠️ **手動指令，不掛在 `make week` 管線**（比照 `l6_g4_watch` 既有慣例）。**fundamentals
衍生欄位每季才更新一次**——同一季內連續跑幾週，這些欄位數值會完全相同，不是bug。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_g1_g2_g5_watch(settings: Path) -> None:
    """docs/31 §11：G1/G2/G5 前瞻累積軌——記錄本週快照，不做統計裁決（樣本還不夠）。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import (
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.analysis.valuation import build_valuation, compute_subind_relative
    from tw_screener.analysis.watchlist import load_latest_screener_results
    from tw_screener.backtest.g1_g2_g5_watch import (
        build_g1_g2_g5_snapshot,
        ledger_progress_summary,
        upsert_ledger,
    )
    from tw_screener.data.twse import create_client
    from tw_screener.screener.local.universe import build_local_universe

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    wc = cfg.get("backtest", {}).get("g1_g2_g5_watch", {})
    g1_delta_net_min = float(wc.get("g1_delta_net_margin_min", 1.5))
    g1_ma60_max = float(wc.get("g1_ma60_max_pct", 15.0))
    g2_roe_min = float(wc.get("g2_roe_min", 3.5))
    g2_debt_max = float(wc.get("g2_debt_max_pct", 60.0))
    g2_current_min = float(wc.get("g2_current_min", 1.2))
    g2_mktcap_min = float(wc.get("g2_mktcap_min_billion", 300.0))
    g5_val_pctile_max = float(wc.get("g5_val_pctile_max", 40.0))
    g5_amount_min = float(wc.get("g5_amount_min_million", 300.0))
    ma60_window = int(wc.get("ma60_window", 60))
    market_history_days = int(wc.get("market_history_days", 90))
    min_peers = int(wc.get("min_peers", 5))
    out_path = Path(wc.get("output_path", "research/g1_g2_g5_watch/ledger.csv"))

    client = create_client(settings)
    data_date = client.latest_trading_date()
    if data_date is None:
        console.print("[red]無法取得最近交易日（無日線快取）——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    week_tag, _ = load_latest_screener_results(settings)
    if not week_tag:
        console.print("[red]reports/ 下無任何週次目錄——本週尚未跑 make week[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]G1/G2/G5 前瞻快照：{week_tag}（資料日 {data_date}）...[/bold]")
    universe = build_local_universe(client)
    if universe.is_empty():
        console.print("[red]本地全市場宇宙為空——日線/市值/估值快取缺，先跑 make fetch-twse[/red]")
        raise typer.Exit(1)

    fundamentals = client.load_fundamentals_history()

    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    industry = load_industry_mapping(cache_dir)
    hand = list_subindustries()
    membership = build_peer_membership(hand, industry)

    gross_margin_peer = (
        compute_subind_relative(
            fundamentals, membership, value_col="gross_margin_pct", min_peers=min_peers
        )
        if not fundamentals.is_empty() and not membership.is_empty()
        else pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    )

    ratios = client.load_latest_valuation_ratios()
    valuation = (
        build_valuation(ratios, membership, min_peers=min_peers)
        if not ratios.is_empty() and not membership.is_empty()
        else pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    )

    # ma60_dist_pct：全市場累積日線快取算 rolling MA60，取最新一日
    market_hist = load_market_history(cache_dir, n_days=market_history_days)
    ma60_map: dict[str, float | None] = {}
    if not market_hist.is_empty():
        ma60_expr = (
            pl.col("close")
            .rolling_mean(ma60_window, min_samples=ma60_window)
            .over("stock_id")
            .alias("_ma60")
        )
        ma = (
            market_hist.sort(["stock_id", "date"])
            .with_columns(ma60_expr)
            .filter(pl.col("date") == pl.col("date").max())
            .with_columns(
                pl.when(pl.col("_ma60") > 0)
                .then((pl.col("close") - pl.col("_ma60")) / pl.col("_ma60") * 100)
                .otherwise(None)
                .alias("_dist")
            )
        )
        ma60_map = {
            str(r["stock_id"]): (float(r["_dist"]) if r["_dist"] is not None else None)
            for r in ma.iter_rows(named=True)
        }

    # amount_million：今日成交金額（daily_*/otc_daily_* 已有 trade_value，原始新台幣元）
    amount_map: dict[str, float | None] = {}
    for df in (client.fetch_daily_all(), client.fetch_otc_daily_all()):
        if df.is_empty() or "trade_value" not in df.columns:
            continue
        for r in df.iter_rows(named=True):
            tv = r.get("trade_value")
            amount_map[str(r["stock_id"])] = (float(tv) / 1e6) if tv is not None else None

    snapshot = build_g1_g2_g5_snapshot(
        universe, fundamentals, gross_margin_peer, valuation, ma60_map, amount_map,
        week_tag, data_date,
        g1_delta_net_margin_min=g1_delta_net_min, g1_ma60_max_pct=g1_ma60_max,
        g2_roe_min=g2_roe_min, g2_debt_max_pct=g2_debt_max,
        g2_current_min=g2_current_min, g2_mktcap_min_billion=g2_mktcap_min,
        g5_val_pctile_max=g5_val_pctile_max, g5_amount_min_million=g5_amount_min,
    )
    ledger = upsert_ledger(out_path, snapshot)
    summary = ledger_progress_summary(ledger)

    console.print(
        f"[green]本週命中：g1={int(snapshot['g1'].sum())}、g2={int(snapshot['g2'].sum())}、"
        f"g5={int(snapshot['g5'].sum())}（{snapshot.height} 檔，重複命中不去重計數）[/green]"
    )
    console.print(
        f"底帳累積 {summary['n_weeks']} 週｜g1 {summary['n_g1']}、g2 {summary['n_g2']}、"
        f"g5 {summary['n_g5']} 筆（跨週不去重）→ {out_path}"
    )
    console.print(
        "[yellow]fundamentals衍生欄位每季才更新一次——同季內連續週數值相同非bug"
        "（見docs/31 §11）。本指令只記錄，不判讀。[/yellow]"
    )
