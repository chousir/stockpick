"""官方族群前5前瞻累積軌編排（docs/31 §12；自 cli.py 薄殼呼叫）。

重用 §10（`official_sector_grid.py`）的映射/籃子函式，只算**最新一天**的排名快照
（不逐週回測——歷史回測見 `official_sector_grid_runner.py`）→
`research/official_sector_watch/ledger.csv`。CLI 只保留參數解析。

⚠️ **手動指令，不掛在 `make week` 管線**（比照 l6_g4_watch/g1_g2_g5_watch 慣例）。
每次執行都會新抓當週那天的官方族群指數（`fetch_sector_index_historical`），
跟其他 watch 指令抓當下 PE/營收現況同一類型，非新增網路呼叫類型。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_official_sector_watch(settings: Path) -> None:
    """docs/31 §12：官方族群前5前瞻累積軌——記錄本週快照，不做統計裁決。"""
    import yaml

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.analysis.watchlist import load_latest_screener_results
    from tw_screener.backtest import official_sector_grid as osg
    from tw_screener.backtest.official_sector_watch import (
        latest_top5_snapshot,
        ledger_progress_summary,
        upsert_ledger,
    )
    from tw_screener.backtest.rotation_efficacy import trend_score_series
    from tw_screener.data.twse import create_client
    from tw_screener.screener.local.universe import build_local_universe

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    wc = cfg.get("backtest", {}).get("official_sector_watch", {})
    min_purity = float(wc.get("min_purity", 0.5))
    top_n = int(wc.get("top_n_groups", 5))
    market_history_days = int(wc.get("market_history_days", 90))
    out_path = Path(wc.get("output_path", "research/official_sector_watch/ledger.csv"))

    client = create_client(settings)
    data_date = client.latest_trading_date()
    if data_date is None:
        console.print("[red]無法取得最近交易日（無日線快取）——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    week_tag, _ = load_latest_screener_results(settings)
    if not week_tag:
        console.print("[red]reports/ 下無任何週次目錄——本週尚未跑 make week[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]官方族群前5前瞻快照：{week_tag}（資料日 {data_date}）...[/bold]")

    # 確保當週那天的官方族群指數已落地（歷史部分已回補至2026-08-21，這裡補當下）
    client.fetch_sector_index_historical(data_date)
    sector_index = client.load_sector_index_history()
    if sector_index.is_empty():
        console.print(
            "[red]無官方族群指數快取——先跑 "
            "`tw-screener data backfill-sector-index --start ... --end ...`[/red]"
        )
        raise typer.Exit(1)

    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    industry = load_industry_mapping(cache_dir)
    hand = list_subindustries()
    if industry.is_empty() or hand.is_empty():
        console.print("[red]缺官方產業分類或手標次產業——無法算purity映射[/red]")
        raise typer.Exit(1)
    purity = osg.compute_subindustry_purity(hand, industry)
    membership = osg.build_hand_sector_membership(hand, purity, min_purity=min_purity)
    baskets = osg.build_hand_sector_baskets(sector_index, purity, min_purity=min_purity)
    if membership.is_empty() or baskets.is_empty():
        console.print("[red]映射後族群/籃子為空，無法計算[/red]")
        raise typer.Exit(1)

    market_hist = load_market_history(cache_dir, n_days=market_history_days)
    if market_hist.is_empty():
        console.print("[red]無日線快取——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    price = market_hist.select("date", "stock_id", "close")
    trend = trend_score_series(price, membership, baskets)

    universe = build_local_universe(client)
    names = {
        str(r["stock_id"]): r.get("name")
        for r in universe.iter_rows(named=True)
    } if not universe.is_empty() else {}

    snapshot = latest_top5_snapshot(
        membership, trend, names, week_tag, data_date, min_purity, top_n_groups=top_n
    )
    ledger = upsert_ledger(out_path, snapshot)
    summary = ledger_progress_summary(ledger)

    console.print(
        f"[green]本週前5名群組展開個股：{snapshot.height} 列"
        f"（{snapshot['stock_id'].n_unique() if not snapshot.is_empty() else 0} 檔不重複）"
        f"[/green]"
    )
    console.print(
        f"底帳累積 {summary['n_weeks']} 週｜{summary['n_rows']} 列｜"
        f"{summary['n_unique_stocks']} 檔不重複 → {out_path}"
    )
    console.print(
        "[yellow]群組層訊號，本指令只記錄不判讀——需累積足夠不重疊週次後才有資格"
        "重新檢定（docs/31 §12）。[/yellow]"
    )
