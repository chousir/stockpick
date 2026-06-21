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

sector_app = typer.Typer(
    help="次產業資金流向輪動（docs/12-sector-rotation.md）", no_args_is_help=True
)
app.add_typer(sector_app, name="sector")

cp_app = typer.Typer(
    help="CP 值補漲股研究（docs/13-cp-value-research.md，個股層）", no_args_is_help=True
)
app.add_typer(cp_app, name="cp")

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
    console.print(f"  法人（上市）：{len(df_inst)} 筆")

    # 上櫃法人/日線：TPEX 僅供最新日、缺日不可回補——必須每次 fetch-twse 都抓
    console.print("[bold]抓取上櫃法人買賣超...[/bold]")
    df_inst_otc = client.fetch_otc_institutional()
    console.print(f"  法人（上櫃）：{len(df_inst_otc)} 筆")

    console.print("[bold]抓取上櫃全市場日線...[/bold]")
    df_otc_daily = client.fetch_otc_daily_all()
    console.print(f"  上櫃日線：{len(df_otc_daily)} 檔")

    console.print("[bold]抓取月營收...[/bold]")
    df_rev = client.fetch_revenue()
    console.print(f"  月營收：{len(df_rev)} 筆")

    console.print("[bold]抓取單季基本面（毛利率/EPS）...[/bold]")
    df_fund = client.fetch_quarterly_fundamentals()
    console.print(f"  單季基本面：{len(df_fund)} 檔")

    # 官方日估值比（trailing PE/PBR/殖利率，上市 BWIBBU_d + 上櫃 peratio）；逐日累積
    console.print("[bold]抓取官方日估值比（PE/PBR/殖利率）...[/bold]")
    df_val = client.fetch_valuation_ratios()
    n_pe = df_val["pe"].drop_nulls().len() if len(df_val) else 0
    console.print(f"  估值比：{len(df_val)} 檔（有 PE {n_pe}）")

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


@data_app.command("fetch-institutional-history")
def data_fetch_institutional_history(
    days: int = typer.Option(20, "--days", help="回補的交易日數，預設 20"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """回補近 N 個交易日的三大法人買賣超（上市 T86 + 上櫃），供族群法人強度與報告使用。

    上市 T86 與上櫃（舊版 3itrade_hedge）都吃日期可回補；缺日自動補、已快取的日跳過。
    這支進 make week → 不必每天開機/跑也能補齊近 N 日法人（解上櫃「缺日不可回補」痛點）。
    """
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    console.print(f"[bold]回補近 {days} 個交易日上市法人（T86）...[/bold]")
    df = client.fetch_institutional_history(days=days)
    n_days = df["date"].n_unique() if not df.is_empty() else 0
    console.print(f"[green]  上市：{n_days} 個交易日、{len(df)} 筆[/green]")

    console.print(f"[bold]回補近 {days} 個交易日上櫃法人（3itrade_hedge）...[/bold]")
    df_otc = client.fetch_otc_institutional_history(days=days)
    n_otc = df_otc["date"].n_unique() if not df_otc.is_empty() else 0
    console.print(f"[green]  上櫃：{n_otc} 個交易日、{len(df_otc)} 筆[/green]")


@data_app.command("backfill-otc-history")
def data_backfill_otc_history(
    months: int = typer.Option(13, "--months", help="每檔回補月數（13≈年線）"),
    limit: int = typer.Option(0, "--limit", help="只跑前 N 檔（測試用；0=全部）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """一次性回補上櫃次產業成員的日線歷史（輪動籃子價格軸用，docs/12 §3 缺口）。

    對「concepts.yaml 次產業成員 ∩ 上櫃」逐檔走 TPEX tradingStock（限速 1 秒/請求）。
    過去月份永久快取——中斷重跑會自動跳過已完成的檔（fast path），天然可續跑。
    全量首跑約 2-3 小時，建議掛背景；之後 fetch_otc_daily_all 每日累積即可，不需重跑。
    """
    import yaml as _yaml

    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    with open(settings) as f:
        _cfg = _yaml.safe_load(f)
    cache_dir = Path(_cfg["paths"]["cache_dir"]) / "twse"

    members = list_subindustries()
    otc_files = sorted(cache_dir.glob("otc_industry_*.parquet"))
    if members.is_empty() or not otc_files:
        console.print("[red]缺 concepts.yaml 次產業或 otc_industry 快取[/red]")
        raise typer.Exit(1)
    import polars as _pl

    otc_ids = set(_pl.read_parquet(otc_files[-1])["stock_id"].to_list())
    targets = sorted(set(members["stock_id"].to_list()) & otc_ids)
    if limit > 0:
        targets = targets[:limit]
    console.print(f"[bold]上櫃次產業成員回補：{len(targets)} 檔 × {months} 個月[/bold]")

    done = failed = 0
    for i, sid in enumerate(targets, 1):
        try:
            df = client.fetch_stock_history(sid, months=months)
            done += 1
            if i % 25 == 0 or i == len(targets):
                console.print(f"  進度 {i}/{len(targets)}（最新：{sid} {len(df)} 日）")
        except Exception as e:  # noqa: BLE001 — 單檔失敗不該中斷整批
            failed += 1
            console.print(f"[yellow]  {sid} 失敗：{e}[/yellow]")
    console.print(f"[green]回補完成：成功 {done}、失敗 {failed}[/green]")


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


@data_app.command("build-themes")
def data_build_themes(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="寫 config/concepts.candidate.yaml、不覆蓋正式檔"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """爬 Yahoo「概念股」主題成分，merge 進 config/concepts.yaml（多標籤主題）。

    只更新概念股；**手動電子次產業原封不動**。靠檔內 concept_themes 清單分辨自動概念股、
    重跑時清舊換新。每主題 SSR 只內嵌前約 30 檔（領頭觀察、非全量），分頁 XHR 依爬蟲自律
    不爬。約 101 頁 × 3 秒、久久跑一次（建議每月，避免題材成分變動失準）。
    """
    import yaml as _yaml

    from tw_screener.screener.yahoo.fetcher import create_yahoo_fetcher
    from tw_screener.screener.yahoo.parser import (
        parse_category_members,
        parse_class_index,
    )

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)

    tb = cfg.get("themes_build", {})
    wanted_labels = set(tb.get("category_labels", ["概念股"]))
    whitelist = set(tb.get("concept_whitelist", []) or [])
    min_members = int(tb.get("concept_min_members", 3))
    fetcher = create_yahoo_fetcher(cfg, Path(cfg["paths"]["cache_dir"]))
    concepts_path = Path("config/concepts.yaml")

    # 1) 爬 Yahoo 概念股 → {主題: [股號]}
    console.print("[bold]抓 Yahoo 主題索引（/class）...[/bold]")
    all_refs = parse_class_index(fetcher.get("/class"))
    refs = [r for r in all_refs if r.kind in wanted_labels]
    if whitelist:
        missing = whitelist - {r.name for r in refs}
        if missing:
            console.print(
                f"[yellow]  白名單有 {len(missing)} 個對不到 Yahoo 主題名（忽略）："
                f"{'、'.join(sorted(missing))}[/yellow]"
            )
        refs = [r for r in refs if r.name in whitelist]
    console.print(
        f"  全部 {len(all_refs)} 主題，取 {'、'.join(sorted(wanted_labels))}"
        f"{'（白名單篩選）' if whitelist else ''} 共 {len(refs)} 個"
    )
    scraped: dict[str, list[str]] = {}
    dropped = 0
    for idx, ref in enumerate(refs, 1):
        try:
            members = parse_category_members(fetcher.get(ref.href))
        except Exception as exc:  # noqa: BLE001 — 單主題失敗不中斷整批
            console.print(f"[yellow]  [{idx}/{len(refs)}] {ref.name} 失敗：{exc}[/yellow]")
            continue
        ids = [sid for sid, _ in members]
        if len(ids) < min_members:
            dropped += 1
            continue
        scraped[ref.name] = ids
        if idx % 20 == 0:
            console.print(f"  進度 {idx}/{len(refs)}（保留 {len(scraped)}、丟棄 {dropped}）")
    new_concept_set = set(scraped)

    # 2) 讀現有 concepts.yaml：剝掉上次自動寫入的概念股標籤（保留手動次產業），再加本次概念股
    existing: dict = {}
    if concepts_path.exists():
        with open(concepts_path, encoding="utf-8") as fh:
            existing = _yaml.safe_load(fh) or {}
    old_concept_set = set(existing.get("concept_themes", []) or [])
    labels_by_id: dict[str, list[str]] = {}
    for sid, val in (existing.get("concepts", {}) or {}).items():
        lst = list(val) if isinstance(val, list) else [val]
        labels_by_id[str(sid)] = [str(x) for x in lst if str(x) not in old_concept_set]
    for theme, ids in scraped.items():
        for sid in ids:
            labels_by_id.setdefault(sid, []).append(theme)

    # 3) 收斂：去重、單一標籤收成 scalar、剝光（只剩舊概念股）者移除
    out_concepts: dict[str, object] = {}
    for sid, labels in labels_by_id.items():
        deduped = list(dict.fromkeys(labels))
        if not deduped:
            continue
        out_concepts[sid] = deduped[0] if len(deduped) == 1 else deduped

    header = (
        "# 主題對照表（次產業手動 + 概念股自動）\n"
        "# - concepts: {股號: 標籤 | [標籤...]}；一檔可多標籤。\n"
        "# - concept_themes: Yahoo 概念股主題名，由 `make build-themes` 維護（重跑只換概念股，\n"
        "#   手動電子次產業不動）；判斷標籤 kind 也靠它。每主題取 Yahoo SSR 前約 30 檔。\n\n"
    )
    doc = {"concept_themes": sorted(new_concept_set), "concepts": out_concepts}

    out_path = Path("config") / ("concepts.candidate.yaml" if dry_run else "concepts.yaml")
    if not dry_run and concepts_path.exists():
        bak = concepts_path.with_name(concepts_path.name + ".bak")
        bak.write_text(concepts_path.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"  已備份舊檔 → {bak}")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        _yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)
    console.print(
        f"[green]完成：{len(scraped)} 概念股主題 merge 進 {out_path}"
        f"（丟棄 {dropped} 個 < {min_members} 檔；手動次產業保留）[/green]"
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
    group: str = typer.Option(
        ...,
        "--group",
        help="策略組：defg（D/E/F/G 現行主流程）、def（D/E/F）、abc（A/B/C legacy）",
    ),
) -> None:
    """執行指定組策略（--group defg 跑 d/e/f/g，def 跑 d/e/f，abc 跑 a/b/c）。"""
    if group not in ("abc", "def", "defg"):
        console.print(f"[red]❌ 未知 group：{group!r}，請用 defg / def / abc[/red]")
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


def _read_watchlist_csv(path: Path) -> list[str]:
    """讀觀察清單 CSV（欄：stock_id[,note]）→ 股號清單。檔不存在回空。"""
    import polars as _pl

    if not path.exists():
        return []
    try:
        df = _pl.read_csv(str(path), infer_schema_length=0)  # 全當字串、保留前導 0
    except Exception:
        return []
    if "stock_id" not in df.columns:
        return []
    return [str(s).strip() for s in df["stock_id"].to_list() if s and str(s).strip()]


def _read_holdings_csv(path: Path) -> dict:
    """讀庫存 CSV（欄：stock_id,buy_price[,shares,note]）→ {股號: {buy_price, shares}}。"""
    import polars as _pl

    if not path.exists():
        return {}
    try:
        df = _pl.read_csv(str(path), infer_schema_length=0)
    except Exception:
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


def _enrich_named_list(
    client, stock_ids, industry_df, institutional, g_pullback, name_map=None, vol_lookback=20,
    dividends=None,
):
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
        if oh.is_empty():
            # 快取沒有 → 主動抓歷史（上櫃股自動走 TPEX），再讀一次
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
                "goodinfo_url": f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={sid}",
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
    )
    return members, {"_list": synth}


@analysis_app.command("group")
def analysis_group(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """讀最新一週的篩選 CSV + TWSE 快取，產出 group_analysis.md。"""
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    import yaml as _yaml

    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import find_leaders
    from tw_screener.data.twse import (
        create_client,
        filter_dividend_calendar,
        load_recent_dividends,
    )
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
    dividend_lookahead = int(ga_cfg.get("dividend_lookahead_days", 14))
    macro_lookahead = int(ga_cfg.get("macro_lookahead_days", 30))
    vol_lookback = int(ga_cfg.get("vol_lookback_days", 20))

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

    price_history = client.load_candidate_history(candidate_ids, n_days=90)
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

    institutional = client.load_institutional_history(n_days=20)
    if institutional.is_empty():
        console.print(
            "[yellow]  無法人快取，族群法人強度將為 0[/yellow]"
            "（建議先跑 make fetch-institutional-history）"
        )
    else:
        console.print(f"  法人快取：{institutional['date'].n_unique()} 個交易日")

    volume_history = client.load_volume_history(candidate_ids, n_days=21)
    if volume_history.is_empty():
        console.print(
            "[yellow]  無 trade_volume 快取，量比欄位將顯示 '-'[/yellow]"
        )
    else:
        console.print(f"  量比資料：{volume_history['stock_id'].n_unique()} 檔")

    # 資料日期一致性檢查（安全網）：三個來源若非同一交易日，量價/籌碼可能來自不同快照
    _src_dates: dict[str, object] = {}
    for _label, _df in (
        ("OHLCV", price_history),
        ("量", volume_history),
        ("法人", institutional),
    ):
        if not _df.is_empty() and "date" in _df.columns:
            _src_dates[_label] = _df["date"].max()
    if len(set(_src_dates.values())) > 1:
        console.print(
            "[yellow]  ⚠ 資料來源最新日期不一致："
            + "、".join(f"{k}={v}" for k, v in _src_dates.items())
            + "；量價/籌碼可能來自不同快照，建議重抓對齊[/yellow]"
        )

    dividends = filter_dividend_calendar(
        client.fetch_dividend_calendar(), _date.today(), dividend_lookahead, candidate_ids
    )
    if dividends.is_empty():
        console.print(f"  本週除權息：候選股未來 {dividend_lookahead} 天內無除權息")
    else:
        console.print(f"  本週除權息：{len(dividends)} 檔候選股（未來 {dividend_lookahead} 天）")

    # 除息還原：聯集近日除權息快照，取近 20 天 ex_date（涵蓋 5 交易日動能視窗），把視窗內
    # 現金股利加回 momentum_5d，修正 6-8 月除息季的假負與排名失真。
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    recent_dividends = load_recent_dividends(cache_dir, _date.today() - _timedelta(days=20))
    if not recent_dividends.is_empty():
        n_exdiv = recent_dividends.filter(
            _pl.col("stock_id").is_in(candidate_ids)
        )["stock_id"].n_unique()
        if n_exdiv:
            console.print(f"  除息還原：近 20 天 {n_exdiv} 檔候選股除權息，動能加回現金股利")

    from tw_screener.data.macro import filter_macro_calendar, load_macro_calendar

    macro_events = filter_macro_calendar(load_macro_calendar(), _date.today(), macro_lookahead)
    if macro_events.is_empty():
        console.print(
            f"  未來總經事件：未來 {macro_lookahead} 天內無"
            "（或 config/macro_calendar.yaml 未建/窗內無事件）"
        )
    else:
        console.print(f"  未來總經事件：{len(macro_events)} 筆（未來 {macro_lookahead} 天）")

    g_pullback = cfg.get("g_pullback")

    console.print("  計算族群強度分數...")
    groups, enriched_stocks = group_stocks(
        screener_results,
        price_history,
        _pl.DataFrame(),  # benchmark: skip for now
        industry_df=industry_df if not industry_df.is_empty() else None,
        weights=weights,
        min_group_size=min_group_size,
        institutional=institutional,
        volume_history=volume_history,
        g_pullback=g_pullback,
        vol_lookback=vol_lookback,
        dividends=recent_dividends,
    )

    if groups.is_empty():
        console.print("[yellow]無符合條件的族群（需 ≥ 2 檔同族群），產出空報告[/yellow]")

    console.print("  計算族群內排名...")
    leaders = find_leaders(enriched_stocks, price_history, institutional)

    # 主題（多標籤）：手標電子次產業 + Yahoo 概念股 → long table。不 join 進 leaders
    # （會把每檔複製成多列、炸掉逐股表），改交報表內 rank_themes / 顯示字串處理。
    from tw_screener.analysis.concepts import load_themes, unmapped_electronics

    themes_long = load_themes()
    if not themes_long.is_empty() and not leaders.is_empty():
        unmapped = unmapped_electronics(leaders, themes_long)
        if unmapped:
            console.print(
                f"[yellow]  次產業未標（電子股 {len(unmapped)} 檔，可補 config/concepts.yaml）："
                f"{', '.join(unmapped[:20])}{' …' if len(unmapped) > 20 else ''}[/yellow]"
            )
        else:
            console.print("  次產業：電子候選股全數已標")

    output_path = Path(cfg["paths"]["reports_dir"]) / week_tag / "group_analysis.md"
    render_group_report(
        groups, leaders, screener_results, week_tag, output_path, top_groups, top_stocks,
        dividend_events=dividends, themes_long=themes_long, macro_events=macro_events,
        radar_cfg=ga_cfg.get("radar"),
    )

    from tw_screener.report.group_report import write_candidates_enriched_csv

    rev_df = client.fetch_revenue()
    rev_yoy_map: dict[str, object] = {}
    name_map: dict[str, str] = {}
    if not rev_df.is_empty() and "stock_id" in rev_df.columns:
        rdf = rev_df
        if "year_month" in rev_df.columns:
            rdf = rev_df.sort("year_month", descending=True)
        for rr in rdf.iter_rows(named=True):
            sid = str(rr["stock_id"])
            if "yoy_pct" in rev_df.columns:
                rev_yoy_map.setdefault(sid, rr.get("yoy_pct"))
            if "company_name" in rev_df.columns:
                name_map.setdefault(sid, str(rr.get("company_name") or ""))

    # 單季基本面（毛利率/EPS）：純讀快取，由 make fetch-twse 累積
    fund_df = client.load_latest_fundamentals()
    fundamentals_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in fund_df.iter_rows(named=True)}
        if not fund_df.is_empty()
        else {}
    )

    # 官方日估值比（PE/PB/殖利率）：純讀快取，由 make fetch-twse 累積（BWIBBU）。candidates 估值欄
    # 以此為主、Goodinfo 兜底（官方覆蓋 ~97%、口徑一致 trailing）。再過 build_valuation 算次產業
    # 相對位階（PE 主、PB 補虧損股）→ 每檔候選 inline 帶「次位/相對便宜」，免另跑 cp_valuation。
    from tw_screener.analysis.sector_universe import build_peer_membership, list_subindustries
    from tw_screener.analysis.valuation import build_valuation

    val_df = client.load_latest_valuation_ratios()
    val_cfg = cfg.get("cp_value", {}).get("valuation", {})
    valuation = build_valuation(
        val_df,
        build_peer_membership(list_subindustries(), industry_df),
        min_peers=int(val_cfg.get("min_peers", 5)),
        cheap_pctile=float(val_cfg.get("cheap_pctile", 30.0)),
    )
    valuation_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in valuation.iter_rows(named=True)}
        if not valuation.is_empty()
        else {}
    )

    csv_path = output_path.parent / "candidates_enriched.csv"
    cand_rows = write_candidates_enriched_csv(
        leaders, themes_long, screener_results, csv_path,
        flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
        fundamentals_map=fundamentals_map, valuation_map=valuation_map,
    )
    # 重疊股重用：庫存/觀察清單同檔一律沿用 candidates 那筆，避免跨 CSV 量比/集中度/成交額分岔
    canonical_rows = {row["stock_id"]: row for row in cand_rows}
    n_cand = len(cand_rows)

    console.print(f"[green]報告輸出：{output_path}[/green]")
    console.print(f"  全候選股完整欄位 CSV：{csv_path}（{n_cand} 檔，供 ProPicks 全宇宙挑股）")

    # 庫存與觀察清單（必分析）→ enrich 成 reports 下 2 個 CSV
    from tw_screener.report.group_report import write_named_list_csv

    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_map = _read_holdings_csv(wl_dir / "holdings.csv")
    watch_ids = _read_watchlist_csv(wl_dir / "watchlist.csv")
    for label, ids, hmap in [
        ("holdings", list(holdings_map), holdings_map),
        ("watchlist", watch_ids, None),
    ]:
        if not ids:
            continue
        console.print(f"  enrich {label}（{len(ids)} 檔，無快取會抓網）...")
        wl_members, wl_synth = _enrich_named_list(
            client,
            ids,
            industry_df if not industry_df.is_empty() else None,
            institutional,
            g_pullback,
            name_map=name_map,
            vol_lookback=vol_lookback,
            dividends=recent_dividends,
        )
        out_csv = output_path.parent / f"{label}_enriched.csv"
        n = write_named_list_csv(
            wl_members, themes_long, wl_synth, out_csv,
            flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
            fundamentals_map=fundamentals_map, valuation_map=valuation_map,
            holdings_map=hmap, canonical_rows=canonical_rows,
        )
        console.print(f"[green]  {label}_enriched.csv：{n} 檔 → {out_csv}[/green]")

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
    institutional = client.load_institutional_history(n_days=20)

    _, enriched_stocks = group_stocks(
        screener_results,
        price_history,
        _pl.DataFrame(),
        industry_df=industry_df if not industry_df.is_empty() else None,
        institutional=institutional,
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
    settings: Path = typer.Option(Path("config/settings.yaml"), help="（已停用）設定檔路徑"),
    top: int = typer.Option(5, "--top", help="（已停用）"),
) -> None:
    """（已停用）原本批次產 group_analysis Section 5 機械推薦清單的個股報告。

    機械推薦清單已移除（docs/11 已 ⛔，挑股改由 Claude 在完整候選宇宙中精選）。
    新流程：make week → 把 group_analysis.md＋candidates_enriched.csv 貼給 Claude Opus →
    存成本週 picks.md → 再依 picks.md 逐檔跑 `make report STOCK_ID=XXXX`。
    """
    console.print(
        "[yellow]report batch 已停用[/yellow]："
        "機械推薦清單（舊 group_analysis Section 5）已移除。\n"
        "請依本週 [bold]picks.md[/bold]（Claude 精選）逐檔跑 "
        "[bold]make report STOCK_ID=XXXX[/bold]，流程見 docs/11-propicks-analysis.md。"
    )


# ─── sector 子指令（次產業資金流向輪動，docs/12-sector-rotation.md） ────────────


def _warn_otc_lag(lag_info: tuple[int, str | None, str | None]) -> None:
    """上櫃法人快取落後上市時印警告（TPEX 缺日不可回補，落後＝上櫃資金流被低估）。"""
    lag, listed_max, otc_max = lag_info
    if lag >= 1:
        console.print(
            f"[yellow]⚠ 上櫃法人快取落後上市 {lag} 個交易日"
            f"（上市至 {listed_max}・上櫃至 {otc_max}）——TPEX 缺日不可回補，"
            f"上櫃股近期資金流被低估；請每交易日跑 make fetch-twse[/yellow]"
        )


@sector_app.command("universe")
def sector_universe_cmd(
    list_all: bool = typer.Option(False, "--list", help="列出所有次產業與成員"),
    audit: bool = typer.Option(
        False, "--audit", help="清查：列出近日無日線收盤的次產業成員（興櫃/下市/誤標）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """次產業宇宙總覽：concepts.yaml 手標次產業 + TWSE 28 類對照覆蓋率（純讀快取）。

    --audit 另比對日線快取，列出無價成員供手動清 concepts.yaml（本指令不改檔）。
    """
    import polars as pl
    import yaml

    from tw_screener.analysis.sector_universe import (
        audit_priceless_members,
        list_subindustries,
        load_industry_mapping,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    members = list_subindustries()
    industry = load_industry_mapping(cache_dir)

    if members.is_empty():
        console.print("[red]concepts.yaml 無次產業標記[/red]")
        raise typer.Exit(1)

    n_subs = members["sub_industry"].n_unique()
    n_stocks = members["stock_id"].n_unique()
    console.print(f"[bold]次產業：{n_subs} 個・成員股：{n_stocks} 檔（去重）[/bold]")
    if not industry.is_empty():
        mapped = members.join(industry, on="stock_id", how="inner")["stock_id"].n_unique()
        console.print(
            f"TWSE 28 類對照：全市場 {industry.height} 檔・"
            f"次產業成員可對照 {mapped}/{n_stocks} 檔"
        )
    else:
        console.print("[yellow]無 industry_YYYYMM 快取（請先 make fetch-twse）[/yellow]")

    if list_all:
        counts = (
            members.group_by("sub_industry")
            .agg(pl.col("stock_id").count().alias("members"))
            .sort("members", descending=True)
        )
        for sub, n in counts.iter_rows():
            sample = members.filter(pl.col("sub_industry") == sub)["stock_id"].head(6)
            console.print(f"  {sub}（{n} 檔）：{', '.join(sample)}{' …' if n > 6 else ''}")

    if audit:
        from tw_screener.analysis.rotation import load_market_history

        rot = cfg.get("rotation", {})
        lookback = int(rot.get("audit_lookback_days", 20))
        market = load_market_history(cache_dir, n_days=lookback)
        if market.is_empty():
            console.print(
                "[yellow]無日線快取（daily_*/otc_daily_*），無法清查無價成員；"
                "請先 make fetch-twse[/yellow]"
            )
            return
        priced = set(market["stock_id"].unique().to_list())
        priceless = audit_priceless_members(members, priced)
        n_priceless = priceless["stock_id"].n_unique()
        console.print(
            f"\n[bold]── 清查：近 {lookback} 交易日無日線收盤的次產業成員 ──[/bold]"
        )
        if priceless.is_empty():
            console.print("[green]全數有價，無需清理[/green]")
            return
        console.print(
            f"[yellow]{n_priceless}/{n_stocks} 檔無價（{n_priceless / n_stocks:.0%}）"
            "——多為興櫃/下市/誤標，會讓籃子悄悄縮水[/yellow]"
        )
        # 受影響次產業（縮水家數 / 標記家數），按縮水比例排序
        impact = (
            priceless.group_by("sub_industry")
            .agg(pl.col("stock_id").n_unique().alias("priceless"))
            .join(
                members.group_by("sub_industry").agg(
                    pl.col("stock_id").n_unique().alias("tagged")
                ),
                on="sub_industry",
                how="left",
            )
            .with_columns((pl.col("priceless") / pl.col("tagged")).alias("ratio"))
            .sort("ratio", descending=True)
        )
        for r in impact.iter_rows(named=True):
            console.print(
                f"  {r['sub_industry']}：{r['priceless']}/{r['tagged']} 縮水（{r['ratio']:.0%}）"
            )
        # 逐檔（一檔多標籤併列），供手動定位 concepts.yaml
        per_stock = priceless.group_by("stock_id").agg(
            pl.col("sub_industry").sort().str.join("、").alias("labels")
        ).sort("stock_id")
        name_map = (
            dict(industry.select(["stock_id", "stock_name"]).iter_rows())
            if not industry.is_empty() and "stock_name" in industry.columns
            else {}
        )
        console.print(
            "[dim]逐檔（手動從 config/concepts.yaml 移除確認無誤者；本指令不改檔）：[/dim]"
        )
        for r in per_stock.iter_rows(named=True):
            nm = name_map.get(r["stock_id"], "")
            console.print(f"  {r['stock_id']} {nm}　[{r['labels']}]")


@sector_app.command("flows")
def sector_flows_cmd(
    week: str = typer.Option("current", "--week", help="計算週次（R1 僅支援 current）"),
    dry: bool = typer.Option(False, "--dry", help="只印結果不落檔（R1 一律 dry，落檔留 R3）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """次產業法人資金流向排名（純讀快取；前 N 流入 + 流出警訊）。"""
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        load_market_history,
        otc_institutional_lag,
        rank_flows,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.data.twse import create_client

    if week != "current":
        console.print("[red]R1 僅支援 --week current（歷史週次查詢隨 R3 落檔後提供）[/red]")
        raise typer.Exit(1)

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    top_n = int(rot.get("top_n", 10))
    rank_by = str(rot.get("rank_by", f"net_flow_{long_w}d"))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    members = list_subindustries()
    if members.is_empty():
        console.print("[red]concepts.yaml 無次產業標記[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]載入法人/價量歷史（{history_days} 交易日）...[/bold]")
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    market = load_market_history(cache_dir, n_days=history_days)
    if institutional.is_empty():
        console.print("[red]無法人快取（請先 make fetch-twse）[/red]")
        raise typer.Exit(1)
    _warn_otc_lag(otc_institutional_lag(cache_dir))

    volume = (
        market.select(["date", "stock_id", "volume"]) if not market.is_empty() else None
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=volume,
        short_window=short_w,
        long_window=long_w,
    )
    ranked = rank_flows(flows, by=rank_by, min_members=min_members)
    latest_date = flows["date"].max()
    console.print(
        f"[bold]資金流向排名（{latest_date}・排名鍵 {rank_by}・籃子 ≥{min_members} 檔）[/bold]"
    )

    def _fmt_row(r: dict) -> str:
        lots = r[rank_by] / 1000  # 股 → 張
        conc = r.get(f"flow_concentration_{long_w}d")
        conc_part = f"・力度 {conc * 100:.2f}%" if conc is not None else ""
        delta = r.get("rank_delta")
        delta_part = f"・ΔRank {delta:+d}" if delta is not None else ""
        return (
            f"  #{r['radar_rank']:>2} {r['sub_industry']}（{r['members']} 檔）"
            f" 淨{'流入' if lots >= 0 else '流出'} {abs(lots):,.0f} 張"
            f"・breadth {r[f'flow_breadth_{long_w}d']:.0%}{conc_part}{delta_part}"
        )

    console.print(f"[green]── 資金流入 前 {top_n} ──[/green]")
    for r in ranked.head(top_n).iter_rows(named=True):
        console.print(_fmt_row(r))
    console.print(f"[red]── 資金流出 末 {min(top_n, 5)} ──[/red]")
    for r in ranked.tail(min(top_n, 5)).iter_rows(named=True):
        console.print(_fmt_row(r))
    if dry:
        console.print("[dim]（dry：未落檔；落檔與 ΔRank 快照接續於 R3）[/dim]")


@sector_app.command("rotation")
def sector_rotation_cmd(
    top: int | None = typer.Option(None, help="排名顯示前 N（預設讀 settings）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """R3 生產輪動報表：產 reports/YYYY-Www/sector_rotation.md + .csv（四象限＋校準訊號）。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        compute_subindustry_baskets,
        load_market_history,
        otc_institutional_lag,
    )
    from tw_screener.analysis.sector_universe import (
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.data.twse import create_client
    from tw_screener.report.rotation_report import (
        build_participation,
        build_rotation_table,
        load_prev_rotation_snapshot,
        render_rotation_report,
    )
    from tw_screener.screener.runner import derive_week_tag

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    quad = rot.get("quadrant", {})
    cp_ceiling = float(rot.get("cp_score", {}).get("position_ceiling", 60.0))
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    top_n = top if top is not None else int(rot.get("top_n", 10))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    console.print(f"[bold]載入資料（{history_days} 交易日）...[/bold]")
    members = list_subindustries()
    market = load_market_history(cache_dir, n_days=history_days)
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    if members.is_empty() or market.is_empty() or institutional.is_empty():
        console.print("[red]缺資料：concepts.yaml / daily 快取 / 法人快取[/red]")
        raise typer.Exit(1)
    _warn_otc_lag(otc_institutional_lag(cache_dir))

    baskets = compute_subindustry_baskets(
        members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=short_w,
        long_window=long_w,
    )

    week_tag = derive_week_tag(settings)
    prev = load_prev_rotation_snapshot(reports_dir, week_tag)
    table = build_rotation_table(
        flows,
        baskets,
        short_window=short_w,
        long_window=long_w,
        entry_signal=rot.get("entry_signal", {}),
        position_window=int(quad.get("position_window", 60)),
        position_low_pct=float(quad.get("position_low_pct", 10.0)),
        cp_position_ceiling=cp_ceiling,
        rank_by=rot.get("rank_by"),
        min_members=min_members,
        prev=prev,
    )
    if table.is_empty():
        console.print("[red]輪動表為空（資料不足）[/red]")
        raise typer.Exit(1)

    # R4 疊圖：庫存 / 觀察 / 本週命中（任一未維護則略過該來源，不報錯）
    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(_read_holdings_csv(wl_dir / "holdings.csv").keys())
    watch_ids = _read_watchlist_csv(wl_dir / "watchlist.csv")
    hit_ids: list[str] = []
    for f in sorted((reports_dir / week_tag).glob("screen_result_*.csv")):
        try:
            df = pl.read_csv(f, infer_schema_length=0)
            if "stock_id" in df.columns:
                hit_ids.extend(str(s) for s in df["stock_id"].to_list() if s)
        except Exception:  # noqa: BLE001 — 壞 CSV 不擋疊圖
            continue
    industry = load_industry_mapping(cache_dir)
    names = (
        {
            sid: (nm or "").replace("股份有限公司", "").replace("(股)公司", "").strip()
            for sid, nm in industry.select(["stock_id", "stock_name"]).iter_rows()
        }
        if not industry.is_empty()
        else {}
    )
    participation = build_participation(
        [
            ("庫存（holdings.csv）", holdings_ids, False),
            ("觀察（watchlist.csv）", watch_ids, False),
            (f"本週命中（{week_tag} 篩選聯集）", hit_ids, True),
        ],
        members,
        table,
        long_window=long_w,
        names=names,
    )

    md_path = render_rotation_report(
        table,
        week_tag=week_tag,
        output_dir=reports_dir / week_tag,
        short_window=short_w,
        long_window=long_w,
        entry_signal=rot.get("entry_signal", {}),
        position_low_pct=float(quad.get("position_low_pct", 10.0)),
        cp_position_ceiling=cp_ceiling,
        top_n=top_n,
        data_date=str(flows["date"].max()),
        participation=participation,
    )
    n_next = table.filter(pl.col("quadrant") == "下一棒").height
    n_trig = table.filter(pl.col("entry_triggered")).height
    console.print(f"[green]輪動報表 → {md_path}[/green]")
    console.print(
        f"  次產業 {table.height} 個・下一棒候選 {n_next} 個・★訊號觸發 {n_trig} 個"
        f"・ΔRank {'有上週快照' if prev is not None else '首週（無快照）'}"
    )


@sector_app.command("calibrate")
def sector_calibrate_cmd(
    x_pct: float | None = typer.Option(None, help="起漲門檻 X%（預設讀 settings）"),
    n_days: int | None = typer.Option(None, help="前瞻視窗 N 交易日"),
    m_days: int | None = typer.Option(None, help="低基期回看 M 交易日"),
    out_dir: Path = typer.Option(Path("research/rotation"), help="校準報告輸出目錄"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """R2 起漲點回測校準（研究軌）：掃描資金訊號門檻，產 research/rotation/ 報告。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_fund_flows,
        compute_subindustry_baskets,
        load_market_history,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest.rotation_calib import (
        detect_breakouts,
        render_calibration_report,
        scan_signals,
    )
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rot = cfg.get("rotation", {})
    cal = rot.get("calibration", {})
    p = {
        "x_pct": x_pct if x_pct is not None else float(cal.get("breakout_x_pct", 10.0)),
        "n_days": n_days if n_days is not None else int(cal.get("breakout_n_days", 15)),
        "m_days": m_days if m_days is not None else int(cal.get("low_base_m_days", 60)),
        "low_base_tol_pct": float(cal.get("low_base_tol_pct", 3.0)),
        "cooldown_days": int(cal.get("cooldown_days", 15)),
        "lead_window": int(cal.get("lead_window", 15)),
        "z_window": int(cal.get("z_window", 60)),
        "z_min_periods": int(cal.get("z_min_periods", 30)),
    }
    short_w = int(rot.get("short_window", 5))
    long_w = int(rot.get("long_window", 20))
    history_days = int(rot.get("history_days", 250))
    min_members = int(rot.get("min_members", 5))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    console.print(f"[bold]載入資料（{history_days} 交易日）...[/bold]")
    members = list_subindustries()
    market = load_market_history(cache_dir, n_days=history_days)
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    if members.is_empty() or market.is_empty() or institutional.is_empty():
        console.print("[red]缺資料：concepts.yaml / daily 快取 / 法人快取[/red]")
        raise typer.Exit(1)

    # 只校準成員數達標的次產業（避免單檔灌水）
    big_enough = (
        members.group_by("sub_industry")
        .agg(pl.col("stock_id").count().alias("n"))
        .filter(pl.col("n") >= min_members)["sub_industry"]
        .to_list()
    )
    members = members.filter(pl.col("sub_industry").is_in(big_enough))

    baskets = compute_subindustry_baskets(
        members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
    )
    flows = compute_fund_flows(
        members,
        institutional,
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=short_w,
        long_window=long_w,
    )
    episodes = detect_breakouts(
        baskets,
        x_pct=p["x_pct"],
        n_days=p["n_days"],
        m_days=p["m_days"],
        low_base_tol_pct=p["low_base_tol_pct"],
        cooldown_days=p["cooldown_days"],
    )
    console.print(
        f"次產業 {len(big_enough)} 個・起漲點 {episodes.height} 個"
        f"（X={p['x_pct']}% N={p['n_days']} M={p['m_days']}）"
    )
    if episodes.is_empty():
        console.print("[red]無起漲點樣本——放寬 --x-pct 或 --n-days 後重跑[/red]")
        raise typer.Exit(1)

    console.print("[bold]掃描訊號 × 門檻...[/bold]")
    scan = scan_signals(
        flows,
        episodes,
        z_window=p["z_window"],
        z_min_periods=p["z_min_periods"],
        lead_window=p["lead_window"],
        occupy_days=p["cooldown_days"],
    )

    data_range = (market["date"].min(), market["date"].max())
    report = render_calibration_report(
        scan,
        episodes,
        p,
        data_range,
        min_triggers=int(cal.get("min_triggers", 8)),
        min_lift=float(cal.get("min_lift", 1.5)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    md_path = out_dir / f"calibration_{tag}.md"
    csv_path = out_dir / f"calibration_{tag}.csv"
    md_path.write_text(report, encoding="utf-8")
    scan.write_csv(csv_path)
    console.print(f"[green]報告 → {md_path}[/green]")
    console.print(f"[green]全掃描表 → {csv_path}[/green]")
    console.print("\n[bold]F1 前 8 名：[/bold]")
    for r in scan.head(8).iter_rows(named=True):
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        console.print(
            f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
            f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
        )


@cp_app.command("calibrate")
def cp_calibrate_cmd(
    out_dir: Path = typer.Option(Path("research/cp_value"), help="校準報告輸出目錄"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """B2 個股版起漲事件回測（研究軌）：三 label 各掃因子訊號，產 research/cp_value/ 報告。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import (
        compute_subindustry_baskets,
        load_market_history,
    )
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.analysis.stock_panel import build_stock_panel, compute_coverage_meta
    from tw_screener.backtest.stock_calib import (
        compute_cross_window_lead,
        detect_ambush_episodes,
        detect_breakout_episodes,
        detect_reversal_episodes,
        detect_top_episodes,
        holdout_table,
        liquidity_table,
        payoff_decay_table,
        render_cp_calibration_report,
        render_cross_window_lead,
        render_robustness_report,
        render_top_calibration_report,
        scan_stock_signals,
        scan_top_signals,
    )
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cp = cfg.get("cp_value", {})
    rot = cfg.get("rotation", {})
    history_days = int(cp.get("history_days", 250))
    z_window = int(cp.get("z_window", 60))
    z_min_periods = int(cp.get("z_min_periods", 30))
    lead_window = int(cp.get("lead_window", 15))
    z_thr = tuple(cp.get("z_thresholds", [0.5, 1.0, 1.5, 2.0]))
    vol_thr = tuple(cp.get("volume_thresholds", [1.0, 1.5, 2.0]))
    position_low_pct = float(cp.get("position_low_pct", 15.0))
    min_triggers = int(cp.get("min_triggers", 8))
    min_lift = float(cp.get("min_lift", 1.3))
    windows = tuple(int(x) for x in cp.get("windows", [1, 3, 5, 10, 20]))
    early_cfg = cp.get("early_gate", {})
    early_on = bool(early_cfg.get("enabled", True))
    early_z = float(early_cfg.get("z_threshold", 1.0))
    early_lookback = int(early_cfg.get("lead_lookback", 30))
    early_min_lead = int(early_cfg.get("min_lead_days", 2))
    labels_cfg = cp.get("labels", {})
    rb = cp.get("robustness", {})  # B-P1 穩健度四件套（docs/15 T3）
    rb_anchor = str(rb.get("anchor_label", "ambush"))
    rb_top_k = int(rb.get("top_k", 6))
    rb_horizons = tuple(int(x) for x in rb.get("horizons", [5, 10, 20, 40]))
    rb_holdout_frac = float(rb.get("holdout_frac", 0.7))
    rb_adv_window = int(rb.get("adv_window", 20))
    rb_adv_min = float(rb.get("adv_min_amount", 100))
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    console.print(f"[bold]載入上市資料（{history_days} 交易日）...[/bold]")
    # 個股層只框上市：只讀 daily_*（otc_daily_/stock_day_ 不符此 glob），docs/13 §4 B1
    market = load_market_history(cache_dir, n_days=history_days, patterns=("daily_*.parquet",))
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    members = list_subindustries()
    if market.is_empty():
        console.print("[red]缺上市日線快取（daily_*.parquet）[/red]")
        raise typer.Exit(1)

    baskets = (
        compute_subindustry_baskets(
            members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
        )
        if not members.is_empty()
        else market.head(0)
    )
    console.print(f"[bold]建個股特徵面板（窗集合 {list(windows)}）...[/bold]")
    panel = build_stock_panel(
        market,
        institutional,
        members,
        baskets,
        windows=windows,
        z_window=z_window,
        z_min_periods=z_min_periods,
    )
    if panel.is_empty():
        console.print("[red]面板為空——檢查日線/法人快取[/red]")
        raise typer.Exit(1)
    coverage = compute_coverage_meta(panel, institutional, members, universe="listed")
    console.print(
        f"面板：{coverage['n_stocks']} 檔 × {coverage['n_trading_days']} 日"
        f"・法人覆蓋 {coverage['inst_coverage_pct']}%"
        f"・次產業覆蓋 {coverage['subind_coverage_pct']}%"
    )

    detectors = {
        "ambush": (
            "L1 埋伏",
            "距 M 日低 ≤ tol% → 前瞻續漲（抓起漲前、價貼低）",
            lambda lp: detect_ambush_episodes(
                market,
                m_days=int(lp.get("m_days", 60)),
                tol_pct=float(lp.get("tol_pct", 5.0)),
                x_pct=float(lp.get("x_pct", 15.0)),
                n_days=int(lp.get("n_days", 20)),
                cooldown_days=int(lp.get("cooldown_days", 20)),
            ),
        ),
        "breakout": (
            "L2 追突破",
            "距 M 日低落在 [lo, hi]% 帶 → 前瞻續漲（抓起漲初）",
            lambda lp: detect_breakout_episodes(
                market,
                m_days=int(lp.get("m_days", 60)),
                lo_pct=float(lp.get("lo_pct", 3.0)),
                hi_pct=float(lp.get("hi_pct", 8.0)),
                x_pct=float(lp.get("x_pct", 12.0)),
                n_days=int(lp.get("n_days", 10)),
                cooldown_days=int(lp.get("cooldown_days", 15)),
            ),
        ),
        "reversal": (
            "L3 超跌反轉（選配）",
            "距 L 日高 ≤ −drawdown% → 前瞻反彈（抓 V 底）",
            lambda lp: detect_reversal_episodes(
                market,
                l_days=int(lp.get("l_days", 60)),
                drawdown_pct=float(lp.get("drawdown_pct", 20.0)),
                x_pct=float(lp.get("x_pct", 15.0)),
                n_days=int(lp.get("n_days", 15)),
                cooldown_days=int(lp.get("cooldown_days", 15)),
            ),
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _date.today().strftime("%Y%m%d")
    summary = [
        f"# 個股 CP 值起漲事件校準總表（{tag}）",
        "",
        f"- 窗集合 {list(windows)}"
        + (
            f"・M-MH 早偵測閘 開（z≥{early_z:g}・領先回看 {early_lookback} 日・"
            f"過閘需中位領先 ≥{early_min_lead} 日）"
            if early_on
            else "・M-MH 早偵測閘 關"
        ),
        "",
    ]
    anchor_scan: pl.DataFrame = pl.DataFrame()  # B-P1：捕捉錨定 label 的掃描結果供穩健度剖析
    anchor_eps: pl.DataFrame = pl.DataFrame()
    anchor_occupy = 15
    for key, (name, desc, detect) in detectors.items():
        lp = labels_cfg.get(key, {})
        episodes = detect(lp)
        report_params = {
            "fwd_n_days": int(lp.get("n_days", 0)),
            "fwd_x_pct": float(lp.get("x_pct", 0.0)),
            "cooldown_days": int(lp.get("cooldown_days", 0)),
            "lead_window": lead_window,
        }
        n_ep = episodes.height
        console.print(f"\n[bold]{name}[/bold]：事件 {n_ep} 個")
        if episodes.is_empty():
            summary.append(f"- **{name}**：事件 0 個——前瞻門檻過嚴或資料不足，無法掃描。")
            continue
        scan = scan_stock_signals(
            panel,
            episodes,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=int(lp.get("cooldown_days", 15)),
            z_min_periods=z_min_periods,
            early_gate=early_cfg if early_on else None,
        )
        if key == rb_anchor:  # B-P1：留存錨定 label 的掃描＋事件供穩健度剖析
            anchor_scan = scan
            anchor_eps = episodes
            anchor_occupy = int(lp.get("cooldown_days", 15))
        report = render_cp_calibration_report(
            scan, episodes, name, desc, report_params, coverage, min_triggers, min_lift
        )
        # M-MH Phase 2：跨窗配對領先（直接驗短窗是否早於 20d-z 達標＝GATE 核心）
        lead_df = pl.DataFrame()
        if early_on:
            lead_df = compute_cross_window_lead(
                panel, episodes, early_z, early_lookback, early_min_lead
            )
            report += "\n" + "\n".join(
                render_cross_window_lead(lead_df, early_z, early_min_lead)
            )
        (out_dir / f"calibration_{tag}_{key}.md").write_text(report, encoding="utf-8")
        scan.write_csv(out_dir / f"calibration_{tag}_{key}.csv")

        qualified = scan.filter(
            (pl.col("n_triggers") >= min_triggers)
            & (pl.col("lift").is_not_null())
            & (pl.col("lift") >= min_lift)
            & (pl.col("median_lead_days") > 0)
        ).sort("lift", descending=True)
        if qualified.is_empty():
            verdict = f"無因子過門檻（lift ≥{min_lift}・觸發 ≥{min_triggers}・領先 >0 日）"
            top = "—"
        else:
            b = qualified.row(0, named=True)
            top = (
                f"{b['signal']}（lift {b['lift']:.2f}・領先中位 {b['median_lead_days']} 日"
                f"・{b['hits']}/{b['n_triggers']} 命中）"
            )
            verdict = f"{qualified.height} 個因子過門檻"
        summary.append(f"- **{name}**：事件 {n_ep} 個・{verdict}；最佳因子 {top}")

        # M-MH Phase 2 GATE：改判「早偵測力」——短窗中位領先 20d ≥ min_lead 且早閘子集 lift>基率
        if early_on:
            lead_pass = (
                lead_df.filter(pl.col("median_lead_days") >= early_min_lead)
                .sort("median_lead_days", descending=True)
                if not lead_df.is_empty()
                else pl.DataFrame()
            )
            early_lift = scan.filter(
                pl.col("signal").str.contains(r"\+early")
                & (pl.col("n_triggers") >= min_triggers)
                & (pl.col("lift") > 1.0)
            ).sort("lift", descending=True)
            passed = not lead_pass.is_empty() and not early_lift.is_empty()
            if lead_pass.is_empty():
                ld = f"無短窗中位領先 ≥{early_min_lead} 日"
            else:
                lt = lead_pass.row(0, named=True)
                ld = (
                    f"{lt['short_signal']} 領先 {lt['median_lead_days']} 日"
                    f"（vs {lt['long_signal']}）"
                )
            if early_lift.is_empty():
                lf = "無早閘因子 lift>1"
            else:
                ft = early_lift.row(0, named=True)
                lf = f"{ft['signal']} lift {ft['lift']:.2f}（{ft['hits']}/{ft['n_triggers']}）"
            summary.append(
                f"  - M-MH GATE（領先 ≥{early_min_lead} 日 ＋ 早閘 lift>1）："
                f"{'✅ 過閘' if passed else '❌ 未過閘'}；領先：{ld}；早閘：{lf}"
            )
        for r in scan.head(5).iter_rows(named=True):
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
            console.print(
                f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
            )

    # ★ L4 頂部/出貨退潮警示校準（M-MH 精修・對稱 L1；驗證 overheat_watch 啟發式是否真有頂部預測力）
    top_lp = labels_cfg.get("top", {})
    tc = cp.get("top_calib", {})
    oh = cp.get("overheat_watch", {})  # 掃描沿用生產 overheat_watch 門檻＝直接驗生產規則
    top_eps = detect_top_episodes(
        market,
        m_days=int(top_lp.get("m_days", 60)),
        tol_pct=float(top_lp.get("tol_pct", 8.0)),
        drop_pct=float(top_lp.get("drop_pct", 10.0)),
        n_days=int(top_lp.get("n_days", 10)),
        cooldown_days=int(top_lp.get("cooldown_days", 15)),
    )
    console.print(f"\n[bold]L4 頂部/出貨[/bold]：事件 {top_eps.height} 個")
    if top_eps.is_empty():
        summary.append("- **L4 頂部/出貨**：事件 0 個——前瞻跌幅門檻過嚴或資料不足，無法掃描。")
    else:
        top_scan = scan_top_signals(
            panel,
            top_eps,
            near_high_pct=float(oh.get("near_high_pct", 8.0)),
            decel_thresholds=tuple(tc.get("decel_thresholds", [0.0])),
            div_floor=float(oh.get("div_floor", 0.0)),
            vol_floor=float(oh.get("vol_contract_floor", 0.0)),
            sell_z_thresholds=tuple(tc.get("sell_z_thresholds", [1.0, 1.5])),
            sell_prefixes=tuple(tc.get("sell_prefixes", ["foreign_flow", "net_flow"])),
            lead_window=int(top_lp.get("n_days", 10)),
            occupy_days=int(top_lp.get("cooldown_days", 15)),
            z_min_periods=z_min_periods,
        )
        top_params = {
            "fwd_n_days": int(top_lp.get("n_days", 10)),
            "fwd_x_pct": float(top_lp.get("drop_pct", 10.0)),
            "tol_pct": float(top_lp.get("tol_pct", 8.0)),
            "cooldown_days": int(top_lp.get("cooldown_days", 15)),
            "lead_window": int(top_lp.get("n_days", 10)),
        }
        top_report = render_top_calibration_report(
            top_scan, top_eps, top_params, coverage, min_triggers
        )
        (out_dir / f"calibration_{tag}_top.md").write_text(top_report, encoding="utf-8")
        top_scan.write_csv(out_dir / f"calibration_{tag}_top.csv")
        oh_row = (
            top_scan.filter(
                pl.col("signal").str.starts_with("★overheat")
                & (pl.col("n_triggers") >= min_triggers)
                & pl.col("lift").is_not_null()
            )
            .sort("lift", descending=True)
            .head(1)
        )
        if oh_row.is_empty():
            summary.append(
                f"- **L4 頂部/出貨**：事件 {top_eps.height} 個・生產啟發式觸發不足或無 lift，"
                "標『資料累積後重校』。"
            )
        else:
            b = oh_row.row(0, named=True)
            summary.append(
                f"- **L4 頂部/出貨**：事件 {top_eps.height} 個・生產啟發式 ★overheat "
                f"lift {b['lift']:.2f}（{b['hits']}/{b['n_triggers']} 命中・"
                f"領先中位 {b['median_lead_days']} 日）；對照裁決見 calibration_{tag}_top.md。"
            )
        for r in top_scan.head(5).iter_rows(named=True):
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
            console.print(
                f"  {r['signal']}：命中 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {lift}・領先中位 {r['median_lead_days']} 日（{r['n_triggers']} 觸發）"
            )

    # ★ B-P1 穩健度四件套（payoff/decay/holdout/流動性硬化；docs/15 T3）——錨定勝出因子、研究軌
    anchor_signals: list[str] = []
    if not anchor_scan.is_empty() and not anchor_eps.is_empty():
        anchor_signals = (
            anchor_scan.filter(
                (pl.col("n_triggers") >= min_triggers)
                & pl.col("lift").is_not_null()
                & (pl.col("lift") >= min_lift)
            )
            .sort("lift", descending=True)
            .head(rb_top_k)["signal"]
            .to_list()
        )
    if not anchor_signals:
        summary.append(
            f"- **穩健度剖析（docs/15 B-P1）**：錨定「{rb_anchor}」無因子過門檻"
            f"（lift≥{min_lift}・觸發≥{min_triggers}）或無事件，略過。"
        )
    else:
        sig_set = set(anchor_signals)
        gate = early_cfg if early_on else None
        payoff = payoff_decay_table(
            panel,
            rb_horizons,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            early_gate=gate,
        )
        holdout = holdout_table(
            panel,
            anchor_eps,
            split_frac=rb_holdout_frac,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
            early_gate=gate,
        )
        liquidity = liquidity_table(
            panel,
            anchor_eps,
            adv_window=rb_adv_window,
            adv_min_amount=rb_adv_min,
            signals=sig_set,
            z_thresholds=z_thr,
            volume_thresholds=vol_thr,
            position_low_pct=position_low_pct,
            lead_window=lead_window,
            occupy_days=anchor_occupy,
            z_min_periods=z_min_periods,
            early_gate=gate,
        )
        rb_params = {
            "top_k": rb_top_k,
            "horizons": list(rb_horizons),
            "holdout_frac": rb_holdout_frac,
            "adv_window": rb_adv_window,
            "adv_min_amount": rb_adv_min,
        }
        rb_report = render_robustness_report(
            payoff, holdout, liquidity, rb_anchor, anchor_signals, rb_params, coverage
        )
        (out_dir / f"calibration_{tag}_robustness.md").write_text(rb_report, encoding="utf-8")
        if not payoff.is_empty():
            payoff.write_csv(out_dir / f"calibration_{tag}_robustness_payoff.csv")
        if not holdout.is_empty():
            holdout.write_csv(out_dir / f"calibration_{tag}_robustness_holdout.csv")
        if not liquidity.is_empty():
            liquidity.write_csv(out_dir / f"calibration_{tag}_robustness_liquidity.csv")
        console.print(
            f"\n[bold]穩健度剖析[/bold]（錨定 {rb_anchor}・{len(anchor_signals)} 因子）"
            f" → calibration_{tag}_robustness.md"
        )
        summary.append(
            f"- **穩健度（docs/15 B-P1）**：錨定「{rb_anchor}」前 {len(anchor_signals)} 名因子"
            f"・payoff/decay/holdout/流動性見 calibration_{tag}_robustness.md。"
        )

    # L3 裁決（docs/13 §3：lift≥門檻＋領先 >0＋需確認非單日 spike，否則不上線）
    summary += [
        "",
        "## L3 超跌反轉裁決（docs/13 §3 三關）",
        "",
        "上表已套（1）lift ≥ 門檻、（2）中位領先 > 0 日兩關；",
        "（3）「需確認非單日 spike」屬訊號設計層，本掃描未強制——若 L3 過前兩關，",
        "上線前仍須在 B3 加「連 2 日續買 / breadth 同步轉正」確認，不接受單日資金 flip。",
        "未過門檻則依 §3 剔除、不放寬標準。",
        "",
    ]
    (out_dir / f"calibration_{tag}_summary.md").write_text("\n".join(summary), encoding="utf-8")
    console.print(f"\n[green]報告 → {out_dir}/calibration_{tag}_*.md（含 _summary）[/green]")


@cp_app.command("candidates")
def cp_candidates_cmd(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """B3 個股 CP 候選清單（生產軌）：套 B2 勝出因子於最新快照，產 reports/週次/cp_candidates.*。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.rotation import compute_subindustry_baskets, load_market_history
    from tw_screener.analysis.sector_universe import (
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.analysis.stock_panel import build_stock_panel, compute_coverage_meta
    from tw_screener.analysis.valuation import build_valuation
    from tw_screener.data.twse import create_client
    from tw_screener.report.cp_candidates import (
        attach_subind_quadrant,
        attach_valuation,
        build_cp_candidates,
        build_early_inflow_watch,
        build_overheat_watch,
        compute_early_inflow,
        compute_overheat_warning,
        latest_snapshot,
        recent_buy_confirm,
        render_cp_candidates_report,
        tag_holding_status,
    )
    from tw_screener.screener.runner import derive_week_tag

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cp = cfg.get("cp_value", {})
    cand = cp.get("candidate", {})
    rot = cfg.get("rotation", {})
    history_days = int(cp.get("history_days", 250))
    z_window = int(cp.get("z_window", 60))
    z_min_periods = int(cp.get("z_min_periods", 30))
    position_low_pct = float(cp.get("position_low_pct", 15.0))
    cp_ceiling = float(rot.get("cp_score", {}).get("position_ceiling", 60.0))
    rules = cand.get("rules", [])
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    console.print(f"[bold]載入上市資料（{history_days} 交易日）...[/bold]")
    # 個股層只框上市：只讀 daily_*（同 B2），docs/13 §4 B1
    market = load_market_history(cache_dir, n_days=history_days, patterns=("daily_*.parquet",))
    institutional = create_client(settings).load_institutional_history(n_days=history_days)
    members = list_subindustries()
    if market.is_empty():
        console.print("[red]缺上市日線快取（daily_*.parquet）[/red]")
        raise typer.Exit(1)

    baskets = (
        compute_subindustry_baskets(
            members, market, clip_daily_return_pct=float(rot.get("clip_daily_return_pct", 10.0))
        )
        if not members.is_empty()
        else market.head(0)
    )
    console.print("[bold]建個股特徵面板 + 取最新快照...[/bold]")
    panel = build_stock_panel(
        market, institutional, members, baskets, z_window=z_window, z_min_periods=z_min_periods
    )
    if panel.is_empty():
        console.print("[red]面板為空——檢查日線/法人快取[/red]")
        raise typer.Exit(1)
    coverage = compute_coverage_meta(panel, institutional, members, universe="listed")

    snapshot = latest_snapshot(panel)
    confirm_days = int(cand.get("confirm_days", 2))
    confirm = recent_buy_confirm(panel, institutional, confirm_days=confirm_days)
    candidates = build_cp_candidates(
        snapshot,
        confirm,
        rules,
        position_low_pct=position_low_pct,
        drawdown_pct=float(cand.get("drawdown_pct", 20.0)),
        cp_ceiling=cp_ceiling,
        max_candidates=int(cand.get("max_candidates", 40)),
    )

    week_tag = derive_week_tag(settings)
    out_dir = reports_dir / week_tag
    rotation_csv = out_dir / "sector_rotation.csv"
    candidates = attach_subind_quadrant(candidates, members, rotation_csv)

    # 疊庫存 / 觀察 / 本週命中（任一未維護則該來源空，不報錯）
    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(_read_holdings_csv(wl_dir / "holdings.csv").keys())
    watch_ids = _read_watchlist_csv(wl_dir / "watchlist.csv")
    hit_ids: list[str] = []
    for fp in sorted(out_dir.glob("screen_result_*.csv")):
        try:
            df = pl.read_csv(fp, infer_schema_length=0)
            if "stock_id" in df.columns:
                hit_ids.extend(str(s) for s in df["stock_id"].to_list() if s)
        except Exception:  # noqa: BLE001 — 壞 CSV 不擋疊圖
            continue
    candidates = tag_holding_status(candidates, holdings_ids, watch_ids, hit_ids)

    # M-MH Phase 3：短窗早訊號加值欄（庫存/觀察的 20d 未確認低信心早訊；補 20d 漏掉的覆蓋）
    early_cfg = cp.get("early_gate", {})
    ew_cfg = cp.get("early_watch", {})
    early_watch = None
    if bool(ew_cfg.get("enabled", True)):
        early = compute_early_inflow(
            snapshot,
            prefixes=tuple(ew_cfg.get("prefixes", ["foreign_flow", "net_flow"])),
            z_threshold=float(early_cfg.get("z_threshold", 1.0)),
            long_z_ceiling=float(early_cfg.get("long_z_ceiling", 0.5)),
            decel_floor=float(early_cfg.get("decel_floor", 0.0)),
        )
        cand_ids = set(candidates["stock_id"].to_list()) if not candidates.is_empty() else set()
        early_watch = build_early_inflow_watch(early, holdings_ids, watch_ids, cand_ids)

    # 精修・點 5：過熱-退潮警示（庫存/觀察已漲到高位但短窗退潮＝停利提醒；未校準啟發式）
    oh_cfg = cp.get("overheat_watch", {})
    overheat_watch = None
    if bool(oh_cfg.get("enabled", True)):
        overheat = compute_overheat_warning(
            snapshot,
            near_high_pct=float(oh_cfg.get("near_high_pct", 8.0)),
            div_floor=float(oh_cfg.get("div_floor", 0.0)),
            vol_contract_floor=float(oh_cfg.get("vol_contract_floor", 0.0)),
        )
        overheat_watch = build_overheat_watch(overheat, holdings_ids, watch_ids)

    # C2 三重濾網：疊 C1 次產業相對估值（官方 trailing PE 主 / PB 補虧損股，橫斷面取最新一份）
    industry = load_industry_mapping(cache_dir)
    val_cfg = cp.get("valuation", {})
    cheap_pctile = float(val_cfg.get("cheap_pctile", 30.0))
    ratios = create_client(settings).load_latest_valuation_ratios()
    # 估值同儕：手標次產業優先、未標上市股以 TWSE 產業別兜底（members 仍純手標供型態/籃子用）
    peer_members = build_peer_membership(members, industry)
    valuation = build_valuation(
        ratios,
        peer_members,
        min_peers=int(val_cfg.get("min_peers", 5)),
        cheap_pctile=cheap_pctile,
    )
    candidates = attach_valuation(candidates, valuation, cheap_pctile=cheap_pctile)

    names = (
        {
            sid: (nm or "").replace("股份有限公司", "").replace("(股)公司", "").strip()
            for sid, nm in industry.select(["stock_id", "stock_name"]).iter_rows()
        }
        if not industry.is_empty()
        else {}
    )
    md_path = render_cp_candidates_report(
        candidates,
        week_tag=week_tag,
        output_dir=out_dir,
        params={"drawdown_pct": float(cand.get("drawdown_pct", 20.0)),
                "confirm_days": int(cand.get("confirm_days", 2))},
        coverage=coverage,
        rules=rules,
        names=names,
        data_date=str(panel["date"].max()),
        early_watch=early_watch,
        overheat_watch=overheat_watch,
    )
    n_triple = (
        candidates.filter(pl.col("triple_filter") == "✓三重").height
        if "triple_filter" in candidates.columns
        else 0
    )
    console.print(f"[green]CP 候選清單 → {md_path}[/green]")
    n_ew = early_watch.height if early_watch is not None else 0
    n_oh = overheat_watch.height if overheat_watch is not None else 0
    console.print(
        f"  候選 {candidates.height} 檔（三重濾網全過 {n_triple}）・"
        f"短窗早訊（庫存/觀察）{n_ew} 檔・過熱-退潮（庫存/觀察）{n_oh} 檔・"
        f"面板 {coverage['n_stocks']} 檔 × {coverage['n_trading_days']} 日・"
        f"法人覆蓋 {coverage['inst_coverage_pct']}%"
    )


@cp_app.command("valuation")
def cp_valuation_cmd(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """C1 個股相對估值表：官方 trailing PE/PB vs 次產業中位數，產 cp_valuation.*。"""
    import yaml

    from tw_screener.analysis.sector_universe import (
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.analysis.valuation import build_valuation, compute_valuation_meta
    from tw_screener.data.twse import create_client
    from tw_screener.report.cp_valuation import render_valuation_report
    from tw_screener.screener.runner import derive_week_tag

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    val = cfg.get("cp_value", {}).get("valuation", {})
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    reports_dir = Path(cfg["paths"]["reports_dir"])

    # 官方日估值比（上市+上櫃），取最新一份做橫斷面比較
    ratios = create_client(settings).load_latest_valuation_ratios()
    if ratios.is_empty():
        console.print("[red]缺官方估值比快取（valuation_ratios_*.parquet）——先跑 fetch-twse[/red]")
        raise typer.Exit(1)
    latest = ratios["date"].max()

    industry = load_industry_mapping(cache_dir)
    # 估值同儕：手標次產業優先，未標上市股以 TWSE 產業別兜底（覆蓋率 46%→~99%）
    peer_members = build_peer_membership(list_subindustries(), industry)

    valuation = build_valuation(
        ratios,
        peer_members,
        min_peers=int(val.get("min_peers", 5)),
        cheap_pctile=float(val.get("cheap_pctile", 30.0)),
    )
    meta = compute_valuation_meta(valuation, data_date=str(latest), universe="上市+上櫃")

    week_tag = derive_week_tag(settings)
    out_dir = reports_dir / week_tag
    names = (
        {
            sid: (nm or "").replace("股份有限公司", "").replace("(股)公司", "").strip()
            for sid, nm in industry.select(["stock_id", "stock_name"]).iter_rows()
        }
        if not industry.is_empty()
        else {}
    )
    md_path = render_valuation_report(
        valuation, week_tag, out_dir, meta, names=names, max_rows=int(val.get("max_rows", 40))
    )
    console.print(f"[green]相對 PE 估值表 → {md_path}[/green]")
    console.print(
        f"  有 PE {meta['n_with_pe']}/{meta['n_stocks']} 檔・"
        f"有相對位階 {meta['n_with_relative']} 檔・相對便宜 {meta['n_cheap']} 檔"
    )


if __name__ == "__main__":
    app()
