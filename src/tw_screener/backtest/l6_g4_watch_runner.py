"""L6/G4 前瞻累積軌編排（docs/31 §9 item4；自 cli.py 薄殼呼叫）。

純讀既有快取＋官方 OpenAPI 當下快照（不額外打 Goodinfo）→ 全市場當週 L6/G4 判準快照 →
`research/l6_g4_watch/ledger.csv` 底帳（append-only，(week, stock_id) 去重）。

⚠️ **這是手動指令，不掛在 `make week` 管線裡**（比照 `picks record` 的既有慣例——底帳
需要人決定「這週算不算」，且本檔只讀快取不改動任何 picks/報告輸出，非 gating）。
**若這週沒手動跑這個指令，這週的 pe_ratio/cum_rev_yoy_pct/rev_yoy_pct/trust_net_5d
快照就永久遺失、無法回頭補**（資料源見 l6_g4_watch.py docstring）——累積軌的價值完全
取決於「有沒有每週跑」，必須排進使用者的週報 SOP，不能假設會自動發生。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import typer
from rich.console import Console

console = Console()


def run_l6_g4_watch(settings: Path) -> None:
    """docs/31 §9 item4：L6/G4 前瞻累積軌——記錄本週快照，不做統計裁決（樣本還不夠）。"""
    import yaml

    from tw_screener.analysis.watchlist import load_latest_screener_results
    from tw_screener.backtest.l6_g4_watch import (
        build_l6_g4_snapshot,
        ledger_progress_summary,
        upsert_l6_g4_ledger,
    )
    from tw_screener.data.twse import create_client
    from tw_screener.screener.local.universe import build_local_universe

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    wc = cfg.get("backtest", {}).get("l6_g4_watch", {})
    yoy_min = float(wc.get("l6_yoy_min", 20.0))
    pe_max = float(wc.get("l6_pe_max", 25.0))
    mktcap_min = float(wc.get("l6_mktcap_min_billion", 100.0))
    flow_window = int(wc.get("trust_flow_window_td", 5))
    out_path = Path(wc.get("output_path", "research/l6_g4_watch/ledger.csv"))

    client = create_client(settings)
    data_date = client.latest_trading_date()
    if data_date is None:
        console.print("[red]無法取得最近交易日（無日線快取）——先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    week_tag, _ = load_latest_screener_results(settings)
    if not week_tag:
        console.print("[red]reports/ 下無任何週次目錄——本週尚未跑 make week[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]L6/G4 前瞻快照：{week_tag}（資料日 {data_date}）...[/bold]")
    universe = build_local_universe(client)
    if universe.is_empty():
        console.print("[red]本地全市場宇宙為空——日線/市值/估值快取缺，先跑 make fetch-twse[/red]")
        raise typer.Exit(1)
    revenue = client.fetch_revenue()
    yoy_deltas = client.load_revenue_yoy_deltas()

    inst = client.load_institutional_history(n_days=flow_window, as_of=data_date)
    trust_net_5d: dict[str, float | None] = {}
    if not inst.is_empty() and {"stock_id", "trust_net"}.issubset(inst.columns):
        import polars as pl

        agg = inst.group_by("stock_id").agg(pl.col("trust_net").sum().alias("_s"))
        trust_net_5d = {
            str(r["stock_id"]): (float(r["_s"]) if r["_s"] is not None else None)
            for r in agg.iter_rows(named=True)
        }

    snapshot = build_l6_g4_snapshot(
        universe, revenue, yoy_deltas, trust_net_5d, week_tag, data_date,
        l6_yoy_min=yoy_min, l6_pe_max=pe_max, l6_mktcap_min_billion=mktcap_min,
    )
    ledger = upsert_l6_g4_ledger(out_path, snapshot)
    summary = ledger_progress_summary(ledger)

    console.print(
        f"[green]本週命中：l6_2cond={int(snapshot['l6_2cond'].sum())}、"
        f"l6_4cond={int(snapshot['l6_4cond'].sum())}、g4={int(snapshot['g4'].sum())}"
        f"（{snapshot.height} 檔，重複命中不去重計數）[/green]"
    )
    console.print(
        f"底帳累積 {summary['n_weeks']} 週｜l6_2cond {summary['n_l6_2cond']}、"
        f"l6_4cond {summary['n_l6_4cond']}、g4 {summary['n_g4']} 筆（跨週不去重）｜"
        f"前視風險旗標 {summary['n_preview_risk']} 筆 → {out_path}"
    )
    n_weeks = cast(int, summary["n_weeks"])
    if n_weeks and n_weeks < 8:
        console.print(
            "[yellow]樣本仍遠不足以重新檢定（docs/31 §9 item4：需累積到不與 W29-30 "
            "重疊的新週次夠多）——本指令只記錄，不判讀。[/yellow]"
        )
