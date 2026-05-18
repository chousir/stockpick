"""CLI 入口：tw-screener 命令。"""

from pathlib import Path

import typer
from rich.console import Console

from tw_screener import __version__

app = typer.Typer(help="台股波段選股與分析工具", no_args_is_help=True)
console = Console()

# ─── 子群組 ───────────────────────────────────────────────────────────────────

data_app = typer.Typer(help="資料抓取指令（TWSE OpenAPI）", no_args_is_help=True)
app.add_typer(data_app, name="data")

screen_app = typer.Typer(help="選股篩選指令（Goodinfo）", no_args_is_help=True)
app.add_typer(screen_app, name="screen")

analysis_app = typer.Typer(help="族群分析指令", no_args_is_help=True)
app.add_typer(analysis_app, name="analysis")

report_app = typer.Typer(help="個股報告指令", no_args_is_help=True)
app.add_typer(report_app, name="report")

# ─── 頂層指令 ─────────────────────────────────────────────────────────────────


@app.command()
def hello() -> None:
    """確認安裝正常。"""
    console.print("Hello from tw-stock-screener")


@app.command()
def version() -> None:
    """顯示版本。"""
    console.print(f"tw-stock-screener {__version__}")


# ─── data 子指令 ──────────────────────────────────────────────────────────────


@data_app.command("fetch-twse")
def data_fetch_twse(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """抓取 TWSE 全市場日線 + 三大法人 + 月營收，寫入本地快取。"""
    from tw_screener.data.twse import create_client

    client = create_client(settings)

    console.print("[bold]抓取全市場日線...[/bold]")
    df_daily = client.fetch_daily_all()
    console.print(f"  日線：{len(df_daily)} 檔")

    console.print("[bold]抓取三大法人買賣超...[/bold]")
    df_inst = client.fetch_institutional()
    console.print(f"  法人：{len(df_inst)} 筆")

    console.print("[bold]抓取月營收...[/bold]")
    df_rev = client.fetch_revenue()
    console.print(f"  月營收：{len(df_rev)} 筆")

    console.print("[green]fetch-twse 完成[/green]")


@data_app.command("fetch-stock")
def data_fetch_stock(
    stock_id: str = typer.Argument(help="股票代號，如 2330"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """抓取單檔個股完整資料（60 天 OHLCV、12 月營收、20 日法人）。"""
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    console.print(f"[bold]抓取 {stock_id} 個股資料...[/bold]")

    ohlcv = client.fetch_stock_ohlcv(stock_id)
    console.print(f"  OHLCV：{len(ohlcv)} 個交易日")

    revenue = client.fetch_stock_revenue(stock_id)
    console.print(f"  月營收：{len(revenue)} 個月（累積快取中）")

    institutional = client.fetch_stock_institutional(stock_id)
    console.print(f"  三大法人：{len(institutional)} 日（累積快取中）")

    console.print("[green]fetch-stock 完成[/green]")


@data_app.command("fetch-candidates-history")
def data_fetch_candidates_history(
    week: str = typer.Option("", "--week", help="週別標籤，預設取最新一週"),
    months: int = typer.Option(2, "--months", help="每檔回補的月份數，預設 2"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """對本週篩選結果聯集去重的個股，補抓 STOCK_DAY 歷史（用於 5 日動能計算）。

    過去月份永久快取（首次跑後不會再打網），首次大約 ~5–15 分鐘（200 檔 × 4 秒）。
    """
    import polars as _pl
    import yaml as _yaml

    from tw_screener.analysis.grouping import is_etf_or_warrant
    from tw_screener.data.twse import create_client

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    rdir = Path(cfg["paths"]["reports_dir"])
    if not rdir.exists():
        console.print("[red]找不到 reports/，請先執行 make screen-all[/red]")
        raise typer.Exit(1)

    if week:
        week_dir = rdir / week
    else:
        week_dirs = sorted(
            [d for d in rdir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        if not week_dirs:
            console.print("[red]找不到本週報告目錄[/red]")
            raise typer.Exit(1)
        week_dir = week_dirs[0]

    csv_files = sorted(week_dir.glob("screen_result_*.csv"))
    if not csv_files:
        console.print(f"[red]{week_dir} 內無 screen_result_*.csv[/red]")
        raise typer.Exit(1)

    candidate_ids: set[str] = set()
    for csv_file in csv_files:
        try:
            df = _pl.read_csv(str(csv_file), infer_schema_length=1000)
        except Exception as exc:
            console.print(f"[yellow]讀取 {csv_file.name} 失敗：{exc}[/yellow]")
            continue
        for sid in df["stock_id"].cast(_pl.Utf8).to_list():
            sid = str(sid).strip()
            if not is_etf_or_warrant(sid):
                candidate_ids.add(sid)

    if not candidate_ids:
        console.print("[yellow]無候選股可補抓[/yellow]")
        return

    client = create_client(settings)
    cache_dir = client.cache_dir
    sorted_ids = sorted(candidate_ids)
    console.print(
        f"[bold]補抓 {len(sorted_ids)} 檔候選股 {months} 個月歷史 OHLCV[/bold]"
        f"  快取目錄：{cache_dir}"
    )

    cache_hits = 0
    fetched = 0
    failures = 0
    for idx, sid in enumerate(sorted_ids, start=1):
        # 預先檢查：所有月份已快取則跳過（避免無謂 log）
        from datetime import date as _date

        from tw_screener.data.twse import _months_back

        today = _date.today()
        current_ym = today.strftime("%Y%m")
        all_cached = True
        for n in range(months):
            ym = _months_back(today, n).strftime("%Y%m")
            f = cache_dir / f"stock_day_{sid}_{ym}.parquet"
            if not f.exists():
                all_cached = False
                break
            if ym == current_ym:
                # 當月可能過期，仍要讓底層判斷
                all_cached = False
                break
        if all_cached:
            cache_hits += 1
            continue

        try:
            df = client.fetch_stock_history(sid, months=months)
            if df.is_empty():
                failures += 1
            else:
                fetched += 1
        except Exception as exc:
            failures += 1
            console.print(f"  [yellow]{sid}: {exc}[/yellow]")

        if idx % 20 == 0:
            console.print(
                f"  進度 {idx}/{len(sorted_ids)} "
                f"(cache {cache_hits}、新抓 {fetched}、失敗 {failures})"
            )

    console.print(
        f"[green]完成：cache hit {cache_hits}、新抓 {fetched}、失敗 {failures}[/green]"
    )


# ─── screen 子指令 ────────────────────────────────────────────────────────────


def _print_strategy_url(strategy: str, settings: Path) -> None:
    import yaml as _yaml

    from tw_screener.screener.goodinfo.url_builder import build_screener_url, load_strategy

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    strategy_path = Path(cfg["paths"]["strategies_dir"]) / f"{strategy}.yaml"
    if not strategy_path.exists():
        console.print(f"[red]找不到策略檔：{strategy_path}[/red]")
        raise typer.Exit(1)

    strat = load_strategy(strategy_path)
    url = build_screener_url(strat, cfg["goodinfo"]["base_url"])
    console.print(f"[bold]策略：{strat.name}[/bold]  （{strat.description}）")
    console.print(f"\n[cyan]{url}[/cyan]")


@screen_app.command("run")
def screen_run(
    strategy: str = typer.Argument(help="策略 ID，如 a_breakout"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只組 URL，不打網"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """執行篩選策略，輸出 CSV 到 reports/YYYY-Www/。--dry-run 只組 URL 不打網。"""
    if dry_run:
        _print_strategy_url(strategy, settings)
        return

    import yaml as _yaml

    from tw_screener.screener.runner import ScreenerRunner

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    strategy_path = Path(cfg["paths"]["strategies_dir"]) / f"{strategy}.yaml"
    if not strategy_path.exists():
        console.print(f"[red]找不到策略檔：{strategy_path}[/red]")
        raise typer.Exit(1)

    from tw_screener.screener.goodinfo.fetcher import GoodinfoBlockedError
    from tw_screener.screener.goodinfo.parser import GoodinfoTooManyResultsError
    from tw_screener.screener.runner import derive_week_tag

    week_tag = derive_week_tag(settings)
    runner = ScreenerRunner(settings)
    try:
        df = runner.run_strategy(strategy_path)
    except GoodinfoTooManyResultsError as e:
        console.print(f"[red]篩選結果 {e.count} 筆超過 Goodinfo 匿名上限（300 筆）[/red]")
        console.print("[yellow]請縮小篩選條件，例如調高 成交筆數 的 min 值[/yellow]")
        raise typer.Exit(1)
    except GoodinfoBlockedError:
        runner.write_blocked_log(strategy, week_tag)
        console.print(f"[red]Goodinfo 封鎖，已記錄到 reports/{week_tag}/blocked.log[/red]")
        raise typer.Exit(1)

    output = runner.export_csv(df, strategy, week_tag)

    console.print(f"[green]篩出 {len(df)} 檔[/green]，結果存於 [bold]{output}[/bold]")
    if len(df) > 100:
        console.print(f"[yellow]⚠ {len(df)} 檔 > 100，條件可能太寬鬆[/yellow]")


@screen_app.command("run-all")
def screen_run_all(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
    group: str = typer.Option(..., "--group", help="策略組：abc（A/B/C 經典三角）或 def（D/E/F ProPicks 復刻組）"),
) -> None:
    """執行指定組策略（--group abc 跑 a_*/b_*/c_*，--group def 跑 d_*/e_*/f_*）。"""
    if group not in ("abc", "def"):
        console.print(f"[red]❌ 未知 group：{group!r}，請用 abc 或 def[/red]")
        raise typer.Exit(1)

    from tw_screener.screener.runner import ScreenerRunner, derive_week_tag

    runner = ScreenerRunner(settings)
    results = runner.run_all(group=group)

    for strategy_id, df in results.items():
        line = f"  {strategy_id}: [green]{len(df)} 檔[/green]"
        if len(df) > 100:
            line += "  [yellow]⚠ 條件可能太寬鬆[/yellow]"
        console.print(line)

    week_tag = derive_week_tag(settings)
    console.print(f"\n[bold]報告目錄：reports/{week_tag}/[/bold]")


# ─── analysis 子指令 ──────────────────────────────────────────────────────────


def _load_latest_screener_results(
    settings: Path,
) -> "tuple[str, dict]":
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
        except Exception as exc:
            console.print(f"[yellow]讀取 {csv_file.name} 失敗：{exc}[/yellow]")

    return week_tag, results


@analysis_app.command("group")
def analysis_group(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """讀最新一週的篩選 CSV + TWSE 快取，產出 group_analysis.md。"""
    import yaml as _yaml

    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import find_leaders
    from tw_screener.data.twse import create_client
    from tw_screener.report.group_report import render_group_report

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    ga_cfg = cfg.get("group_analysis", {})
    weights = ga_cfg.get(
        "weights",
        {"momentum": 0.50, "entry_rate": 0.25, "institutional": 0.15, "size": 0.10},
    )
    min_group_size = int(ga_cfg.get("min_group_size", 2))
    top_groups = int(ga_cfg.get("top_groups", 10))
    top_stocks = int(ga_cfg.get("top_stocks", 10))

    week_tag, screener_results = _load_latest_screener_results(settings)
    if not screener_results:
        console.print("[red]找不到篩選 CSV，請先執行 make screen-all[/red]")
        raise typer.Exit(1)

    total_rows = sum(len(df) for df in screener_results.values())
    console.print(f"[bold]族群分析：{week_tag}，共 {total_rows} 筆篩選結果[/bold]")

    import polars as _pl

    client = create_client(settings)

    console.print("  載入產業別資料（TWSE 上市 + 上櫃）...")
    listed_df = client.fetch_listed_industry()
    otc_df = client.fetch_otc_industry()
    if not listed_df.is_empty() and not otc_df.is_empty():
        industry_df = _pl.concat([listed_df, otc_df])
    elif not listed_df.is_empty():
        industry_df = listed_df
    elif not otc_df.is_empty():
        industry_df = otc_df
    else:
        industry_df = _pl.DataFrame()
    if industry_df.is_empty():
        console.print("[yellow]  產業別資料無法取得，以「未分類」處理[/yellow]")
    else:
        console.print(f"  上市 {len(listed_df)} 檔、上櫃 {len(otc_df)} 檔")

    console.print("  合併候選股 OHLCV（stock_day + daily）...")
    candidate_ids: list[str] = []
    seen: set[str] = set()
    for df in screener_results.values():
        if df.is_empty() or "stock_id" not in df.columns:
            continue
        for sid in df["stock_id"].cast(_pl.Utf8).to_list():
            sid = str(sid).strip()
            if sid not in seen:
                seen.add(sid)
                candidate_ids.append(sid)

    price_history = client.load_candidate_history(candidate_ids, n_days=60)
    if price_history.is_empty():
        console.print(
            "[yellow]  無 stock_day / daily 快取，5 日動能將 fallback 到當日漲跌幅[/yellow]"
            "  （建議先跑 make fetch-candidates-history 補抓歷史）"
        )
    else:
        # 顯示資料覆蓋情況
        per_stock_days = price_history.group_by("stock_id").len().get_column("len")
        if len(per_stock_days) > 0:
            console.print(
                f"  候選股 {candidate_ids and len(candidate_ids) or 0} 檔，"
                f"歷史覆蓋 min={per_stock_days.min()}、median={per_stock_days.median()} 日"
            )

    institutional = client.fetch_institutional()

    console.print("  計算族群強度分數...")
    groups, enriched_stocks = group_stocks(
        screener_results,
        price_history,
        _pl.DataFrame(),  # benchmark: skip for now
        industry_df=industry_df if not industry_df.is_empty() else None,
        weights=weights,
        min_group_size=min_group_size,
    )

    if groups.is_empty():
        console.print("[yellow]無符合條件的族群（需 ≥ 2 檔同族群），產出空報告[/yellow]")

    console.print("  計算族群內排名...")
    leaders = find_leaders(enriched_stocks, price_history, institutional)

    output_path = Path(cfg["paths"]["reports_dir"]) / week_tag / "group_analysis.md"
    render_group_report(
        groups, leaders, screener_results, week_tag, output_path, top_groups, top_stocks
    )

    console.print(f"[green]報告輸出：{output_path}[/green]")
    console.print(f"  族群數：{len(groups)}，推薦分析：前 {top_stocks} 檔")


@analysis_app.command("leaders")
def analysis_leaders(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """顯示各族群本週表現第一名（族群內 rank #1）。"""
    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import rank_within_groups
    from tw_screener.data.twse import create_client

    _week_tag, screener_results = _load_latest_screener_results(settings)
    if not screener_results:
        console.print("[red]找不到篩選 CSV，請先執行 make screen-all[/red]")
        raise typer.Exit(1)

    import polars as _pl

    client = create_client(settings)
    industry_df = client.fetch_listed_industry()

    candidate_ids: list[str] = []
    seen: set[str] = set()
    for df in screener_results.values():
        if df.is_empty() or "stock_id" not in df.columns:
            continue
        for sid in df["stock_id"].cast(_pl.Utf8).to_list():
            sid = str(sid).strip()
            if sid not in seen:
                seen.add(sid)
                candidate_ids.append(sid)
    price_history = client.load_candidate_history(candidate_ids, n_days=60)
    institutional = client.fetch_institutional()

    _, enriched_stocks = group_stocks(
        screener_results,
        price_history,
        _pl.DataFrame(),
        industry_df=industry_df if not industry_df.is_empty() else None,
    )

    members = rank_within_groups(enriched_stocks, price_history, institutional)

    if not members.is_empty() and "rank_in_group" in members.columns:
        top_df = members.filter(_pl.col("rank_in_group") == 1).sort("leader_score", descending=True)
        console.print("[bold]各族群本週表現第一名：[/bold]")
        for row in top_df.iter_rows(named=True):
            mom = row.get("momentum_5d", row.get("rs", 0)) or 0
            console.print(
                f"  {row['industry_name']:15s}  {row['stock_id']} {row['name']:10s}  "
                f"5 日漲幅 {mom:+.2f}%  分數 {row.get('leader_score', 0):.2f}"
            )
    else:
        console.print("[yellow]無排名資料[/yellow]")


# ─── report 子指令 ────────────────────────────────────────────────────────────


@report_app.command("stock")
def report_stock(
    stock_id: str = typer.Argument(help="股票代號，如 2330"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
    week: str = typer.Option("", "--week", help="週別標籤，如 2026-W20（預設本週）"),
) -> None:
    """產出單檔個股深度報告（需設定 ANTHROPIC_API_KEY）。"""
    import os

    from tw_screener.report.builder import build_stock_report

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[yellow]未設定 ANTHROPIC_API_KEY，將產出資料草稿（Claude 分析段落留空）[/yellow]"
        )

    week_tag = week if week else None
    output = build_stock_report(stock_id, settings, week_tag=week_tag, api_key=api_key)
    console.print(f"[green]報告輸出：[/green][bold]{output}[/bold]")


@report_app.command("batch")
def report_batch(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
    top: int = typer.Option(5, "--top", help="取推薦清單前 N 檔"),
) -> None:
    """批次產出本週 group_analysis.md 推薦清單的個股報告。"""
    import os
    import re

    import yaml as _yaml

    from tw_screener.report.builder import build_stock_report

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[yellow]未設定 ANTHROPIC_API_KEY，將產出資料草稿[/yellow]"
        )

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    rdir = Path(cfg["paths"]["reports_dir"])
    week_dirs = sorted(
        [d for d in rdir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not week_dirs:
        console.print("[red]找不到報告目錄，請先執行 make group[/red]")
        raise typer.Exit(1)

    week_dir = week_dirs[0]
    week_tag = week_dir.name
    report_file = week_dir / "group_analysis.md"
    if not report_file.exists():
        console.print(f"[red]找不到 {report_file}，請先執行 make group[/red]")
        raise typer.Exit(1)

    # Extract stock IDs from priority stocks section
    text = report_file.read_text(encoding="utf-8")
    # Pattern: "N. **XXXX 股名**（..." from section 5
    stock_ids = re.findall(r"\*\*(\d{4,6})\s+[^*]+\*\*", text)[:top]

    if not stock_ids:
        console.print("[yellow]group_analysis.md 中找不到推薦個股[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold]批次報告：{week_tag}，共 {len(stock_ids)} 檔[/bold]")
    for sid in stock_ids:
        console.print(f"  產出 {sid}...")
        try:
            output = build_stock_report(sid, settings, week_tag=week_tag, api_key=api_key)
            console.print(f"    [green]→ {output.name}[/green]")
        except Exception as e:
            console.print(f"    [red]失敗：{e}[/red]")


if __name__ == "__main__":
    app()
