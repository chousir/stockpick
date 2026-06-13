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
    """回補近 N 個交易日的三大法人買賣超（T86），供族群法人強度與報告使用。"""
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    console.print(f"[bold]回補近 {days} 個交易日法人資料（T86）...[/bold]")
    df = client.fetch_institutional_history(days=days)
    n_days = df["date"].n_unique() if not df.is_empty() else 0
    console.print(f"[green]完成：{n_days} 個交易日、{len(df)} 筆[/green]")


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
    client, stock_ids, industry_df, institutional, g_pullback, name_map=None, vol_lookback=20
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
    )
    return members, {"_list": synth}


@analysis_app.command("group")
def analysis_group(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """讀最新一週的篩選 CSV + TWSE 快取，產出 group_analysis.md。"""
    from datetime import date as _date

    import yaml as _yaml

    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import find_leaders
    from tw_screener.data.twse import create_client, filter_dividend_calendar
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

    csv_path = output_path.parent / "candidates_enriched.csv"
    cand_rows = write_candidates_enriched_csv(
        leaders, themes_long, screener_results, csv_path,
        flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
        fundamentals_map=fundamentals_map,
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
        )
        out_csv = output_path.parent / f"{label}_enriched.csv"
        n = write_named_list_csv(
            wl_members, themes_long, wl_synth, out_csv,
            flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
            fundamentals_map=fundamentals_map, holdings_map=hmap,
            canonical_rows=canonical_rows,
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
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """次產業宇宙總覽：concepts.yaml 手標次產業 + TWSE 28 類對照覆蓋率（純讀快取）。"""
    import polars as pl
    import yaml

    from tw_screener.analysis.sector_universe import (
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


if __name__ == "__main__":
    app()
