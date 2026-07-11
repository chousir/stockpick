"""CLI 入口：tw-screener 命令。"""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
from rich.console import Console

from tw_screener import __version__
from tw_screener.analysis.watchlist import (
    load_latest_screener_results,
)
from tw_screener.data.cache import find_latest

if TYPE_CHECKING:
    import polars as pl


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

backtest_app = typer.Typer(
    help="策略回測閉環（規劃書 03 量化驗證）", no_args_is_help=True
)
app.add_typer(backtest_app, name="backtest")

market_app = typer.Typer(
    help="大盤 regime 總控閘門（規劃書 03 V2）", no_args_is_help=True
)
app.add_typer(market_app, name="market")

portfolio_app = typer.Typer(
    help="組合層風控：集中度/相關簇/因子簇（規劃書 03 V3）", no_args_is_help=True
)
app.add_typer(portfolio_app, name="portfolio")

picks_app = typer.Typer(
    help="每週 pick 閉環：底帳記錄與命中率×α×反事實（規劃書 05 F1）", no_args_is_help=True
)
app.add_typer(picks_app, name="picks")

# ─── 頂層指令 ─────────────────────────────────────────────────────────────────


@app.command()
def hello() -> None:
    """確認安裝正常。"""
    console.print("Hello from tw-stock-screener")


@app.command()
def version() -> None:
    """顯示版本。"""
    console.print(f"tw-stock-screener {__version__}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="綁定位址（預設本機，勿綁 0.0.0.0）"),
    port: int = typer.Option(8000, help="埠"),
    reload: bool = typer.Option(False, "--reload", help="dev 自動重載"),
) -> None:
    """啟動投資戰情室 Dashboard（FastAPI 服務 frontend/dist ＋ /api；docs/17）。"""
    from tw_screener.webapp.server import run

    run(host=host, port=port, reload=reload)


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

    # 上市融資融券（MI_MARGN）；逐日累積供 margin_chg_5d（規劃書 02 D4）。上櫃為缺口（D6 backlog）。
    console.print("[bold]抓取上市融資融券（MI_MARGN）...[/bold]")
    df_margin = client.fetch_margin()
    console.print(f"  融資融券：{len(df_margin)} 檔（上市）")

    console.print("[green]fetch-twse 完成[/green]")


@data_app.command("fetch-tdcc")
def data_fetch_tdcc(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """抓 TDCC 集保戶股權分散表（每週大戶持股比），逐週累積快取（規劃書 02 D3）。"""
    import polars as pl

    from tw_screener.data.tdcc import create_tdcc_client

    client = create_tdcc_client(settings)
    console.print("[bold]抓取 TDCC 集保戶股權分散表...[/bold]")
    df = client.fetch_distribution()
    if df.is_empty():
        console.print("[yellow]TDCC 集保分散表未取得；大戶欄本週退化為 null[/yellow]")
        return
    n_stocks = df.select(pl.col("stock_id").n_unique()).item()
    data_date = df.select(pl.col("data_date").max()).item()
    console.print(f"  集保分散表：{n_stocks} 檔個股（資料日 {data_date}）")
    console.print("[green]fetch-tdcc 完成[/green]")


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
    otc_latest = find_latest(cache_dir, "otc_industry_*.parquet", by="name")
    if members.is_empty() or otc_latest is None:
        console.print("[red]缺 concepts.yaml 次產業或 otc_industry 快取[/red]")
        raise typer.Exit(1)
    import polars as _pl

    otc_ids = set(_pl.read_parquet(otc_latest)["stock_id"].to_list())
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


@data_app.command("backfill-universe-history")
def data_backfill_universe_history(
    months: int = typer.Option(13, "--months", help="每檔回補月數（13≈年線）"),
    limit: int = typer.Option(0, "--limit", help="只跑前 N 檔（測試用；0=全部）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """一次性回補「全部次產業成員（上市＋上櫃）」的日線歷史（規劃書 02 D2 冷啟動）。

    為何需要：STOCK_DAY_ALL / otc_daily_all 都只能往未來累積、過去補不回（docs/02），
    rotation z 需 60+ 日、calibration 需 ~250 日。本指令對 concepts.yaml 全部次產業成員
    逐檔走 `fetch_stock_history`（自動分派 TWSE STOCK_DAY / TPEX tradingStock，限速 1 秒/請求），
    把輪動籃子的歷史密度從「snapshot 累積」補成「~1 年」。

    優先補成員多的次產業（成員數由多到少排序、跨次產業去重）。過去月份永久快取——中斷重跑
    會自動跳過已完成的檔（fast path），天然可續跑。全量首跑約 8-12 小時，建議掛背景；
    之後每日 fetch-twse 累積即可，不需重跑。涵蓋上櫃成員，故為 backfill-otc-history 的超集。
    """
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    members = list_subindustries()
    if members.is_empty():
        console.print("[red]缺 concepts.yaml 次產業成員[/red]")
        raise typer.Exit(1)

    # 依次產業成員數由多到少排序、跨次產業去重（成員多的次產業先補，密度優先見效）
    counts = members.group_by("sub_industry").len()
    ordered = (
        members.join(counts, on="sub_industry")
        .sort("len", descending=True)["stock_id"]
        .to_list()
    )
    seen: set[str] = set()
    targets: list[str] = []
    for sid in ordered:
        if sid not in seen:
            seen.add(sid)
            targets.append(sid)
    if limit > 0:
        targets = targets[:limit]
    n_sub = members["sub_industry"].n_unique()
    console.print(
        f"[bold]全次產業成員回補：{len(targets)} 檔（{n_sub} 個次產業）× {months} 個月[/bold]"
    )

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
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不該中斷整批
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
        except Exception as exc:  # noqa: BLE001 — 單檔失敗計入 failures、不中斷整批
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


@data_app.command("prune-cache")
def data_prune_cache(
    dry: bool = typer.Option(False, "--dry", help="只列出超窗檔、不實際刪除"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """依 settings.cache.retention 保留窗刪除超窗的本地快取檔（規劃書 01 P2）。

    預設「真刪」；加 --dry 只列出不刪。**不自動跑**——由使用者手動或排程呼叫。
    個股月檔 stock_day_<sid>_* 預設全留（settings.cache.retention.stock_day_keep_all）。
    """
    import yaml as _yaml

    from tw_screener.data.cache import select_prune_candidates

    with open(settings, encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh)
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    retention = cfg.get("cache", {}).get("retention", {})

    candidates = select_prune_candidates(cache_dir, retention)
    if not candidates:
        console.print("[green]無超窗快取檔，無需清理。[/green]")
        return

    total_mb = sum(f.stat().st_size for f in candidates) / 1024 / 1024
    tag = "[dim]\\[would delete][/dim]" if dry else "[red]\\[deleted][/red]"
    console.print(
        f"[bold]{'(--dry) ' if dry else ''}超窗快取檔：{len(candidates)} 個"
        f"（約 {total_mb:.1f} MB）[/bold]"
    )
    for f in candidates:
        console.print(f"  {tag} {f.name}")
    if dry:
        console.print("[yellow]--dry：未刪除任何檔。[/yellow]")
        return
    for f in candidates:
        f.unlink()
    console.print(f"[green]已刪除 {len(candidates)} 個超窗快取檔。[/green]")


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
    strategy: str = typer.Argument(help="策略 ID，如 d_quality_leader"),
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
        help="策略組：defg（D/E/F/G 現行唯一主流程）",
    ),
) -> None:
    """執行指定組策略（--group defg 跑 d/e/f/g）。abc/def 已退役（規劃書 04 A4）。"""
    if group == "abc":
        console.print(
            "[red]❌ group=abc 已退役（規劃書 04 A4）[/red]："
            "A/B/C 經典三角 YAML 已移至 config/strategies/archive/，不再可跑。"
            "請改用 [bold]--group defg[/bold]（現行主流程）。"
        )
        raise typer.Exit(1)
    if group == "def":
        console.print(
            "[red]❌ group=def 已退役（規劃書 04 A4）[/red]："
            "D/E/F 已併入主流程，請改用 [bold]--group defg[/bold]（含 G 成長拉回）。"
        )
        raise typer.Exit(1)
    if group != "defg":
        console.print(f"[red]❌ 未知 group：{group!r}，請用 defg（現行主流程）[/red]")
        raise typer.Exit(1)

    from tw_screener.screener.runner import ScreenerRunner, derive_week_tag

    runner = ScreenerRunner(settings)
    results = runner.run_all(group=group)

    for strategy_id, df in results.items():
        line = f"  {strategy_id}: [green]{len(df)} 檔[/green]"
        if len(df) > 100:
            line += "  [yellow]⚠ 條件可能太寬鬆[/yellow]"
        console.print(line)

    for strategy_id, reason in runner.failures.items():
        console.print(f"  {strategy_id}: [red]本週未取得[/red]（{reason}）")

    week_tag = derive_week_tag(settings)
    console.print(f"\n[bold]報告目錄：reports/{week_tag}/[/bold]")


@screen_app.command("doctor")
def screen_doctor(
    replay: bool = typer.Option(
        False, "--replay", help="離線：用 committed fixture 驗 parser 沒退化（不打網）"
    ),
    save_fixture: bool = typer.Option(
        False, "--save-fixture", help="live 且結果 OK 時，把抓到的 HTML 落地供手動刷新 fixture"
    ),
    force: bool = typer.Option(False, "--force", help="略過快取強制打網（預設沿用快取）"),
    fixture: Path = typer.Option(None, "--fixture", help="replay fixture 路徑（預設讀 settings）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """Goodinfo 健康檢查：判斷正常／被擋／改版／欄位改名並回傳診斷碼。OK→exit 0，其餘→exit 1。"""
    from tw_screener.screener.goodinfo.doctor import (
        DoctorStatus,
        replay_doctor,
        run_doctor,
    )

    if replay:
        result = replay_doctor(settings, fixture=fixture)
    else:
        result = run_doctor(settings, force=force, save_fixture=save_fixture)

    mode = "replay" if replay else "live"
    if result.status is DoctorStatus.OK:
        console.print(f"[green]✓ ({mode}) {result.status}[/green] — {result.message}")
        raise typer.Exit(0)

    console.print(f"[red]✗ ({mode}) {result.status}[/red] — {result.message}")
    raise typer.Exit(1)


# ─── analysis 子指令 ──────────────────────────────────────────────────────────


@analysis_app.command("group")
def analysis_group(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """讀最新一週的篩選 CSV + TWSE 快取，產出 group_analysis.md。"""
    from tw_screener.report.group_runner import run_group_analysis

    run_group_analysis(settings)


@analysis_app.command("leaders")
def analysis_leaders(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """顯示各族群本週表現第一名（族群內 rank #1）。"""
    from tw_screener.analysis.grouping import group_stocks
    from tw_screener.analysis.leader import rank_within_groups
    from tw_screener.data.twse import create_client

    _week_tag, screener_results = load_latest_screener_results(settings)
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
    存成本週 pick.md → 再依 pick.md 逐檔跑 `make report STOCK_ID=XXXX`。
    """
    console.print(
        "[yellow]report batch 已停用[/yellow]："
        "機械推薦清單（舊 group_analysis Section 5）已移除。\n"
        "請依本週 [bold]pick.md[/bold]（Claude 精選）逐檔跑 "
        "[bold]make report STOCK_ID=XXXX[/bold]，流程見 docs/11-propicks-analysis.md。"
    )


# ─── sector 子指令（次產業資金流向輪動，docs/12-sector-rotation.md） ────────────


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
    from tw_screener.report.sector_runner import run_sector_universe

    run_sector_universe(list_all, audit, settings)


@sector_app.command("flows")
def sector_flows_cmd(
    week: str = typer.Option("current", "--week", help="計算週次（R1 僅支援 current）"),
    dry: bool = typer.Option(False, "--dry", help="只印結果不落檔（R1 一律 dry，落檔留 R3）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """次產業法人資金流向排名（純讀快取；前 N 流入 + 流出警訊）。"""
    from tw_screener.report.sector_runner import run_sector_flows

    run_sector_flows(week, dry, settings)


@sector_app.command("rotation")
def sector_rotation_cmd(
    top: int | None = typer.Option(None, help="排名顯示前 N（預設讀 settings）"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """R3 生產輪動報表：產 reports/YYYY-Www/sector_rotation.md + .csv（四象限＋校準訊號）。"""
    from tw_screener.report.sector_runner import run_sector_rotation

    run_sector_rotation(top, settings)


@sector_app.command("calibrate")
def sector_calibrate_cmd(
    x_pct: float | None = typer.Option(None, help="起漲門檻 X%（預設讀 settings）"),
    n_days: int | None = typer.Option(None, help="前瞻視窗 N 交易日"),
    m_days: int | None = typer.Option(None, help="低基期回看 M 交易日"),
    out_dir: Path = typer.Option(Path("research/rotation"), help="校準報告輸出目錄"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """R2 起漲點回測校準（研究軌）：掃描資金訊號門檻，產 research/rotation/ 報告。"""
    from tw_screener.report.sector_runner import run_sector_calibrate

    run_sector_calibrate(x_pct, n_days, m_days, out_dir, settings)


@backtest_app.command("strategies")
def backtest_strategies_cmd(
    hold_weeks: str | None = typer.Option(
        None, help="持有週數清單（逗號分隔，預設讀 settings，如 2,4,8,12）"
    ),
    out_dir: Path = typer.Option(
        Path("research/strategy_backtest"), help="回測報告輸出目錄"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """V1 個股策略回測閉環：量化 D/E/F/G 入選後各持有窗的勝率/報酬/回撤 vs 大盤。"""
    from tw_screener.backtest.strategies_runner import run_backtest_strategies

    run_backtest_strategies(hold_weeks, out_dir, settings)


@backtest_app.command("build-panel")
def backtest_build_panel_cmd(
    out_dir: Path | None = typer.Option(
        None, help="面板輸出目錄（預設讀 settings，research/panel）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """WS-A2 ground-truth 面板：date×stock_id 前瞻報酬/位階/法人/量比 parquet＋核價抽查。"""
    from tw_screener.backtest.panel_runner import run_build_panel

    run_build_panel(settings, out_dir)


@backtest_app.command("factor-lab")
def backtest_factor_lab_cmd(
    out_dir: Path | None = typer.Option(
        None, help="輸出目錄（預設讀 settings，research/factor_lab）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """WS-B 因子實驗台驗收：機器等價＋docs/19 基準對表＋面板全宇宙首驗。"""
    from tw_screener.backtest.factor_lab_runner import run_factor_lab

    run_factor_lab(settings, out_dir)


@backtest_app.command("rotation-efficacy")
def backtest_rotation_efficacy_cmd(
    out_dir: Path | None = typer.Option(
        None, help="輸出目錄（預設讀 settings，research/rotation_efficacy）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """WS-C 輪動欄效度：歷史重建逐週訊號→生產對表→forward basket IC/lift＋榜外機會成本。"""
    from tw_screener.backtest.rotation_efficacy_runner import run_rotation_efficacy

    run_rotation_efficacy(settings, out_dir)


@backtest_app.command("laggard-grid")
def backtest_laggard_grid_cmd(
    out_dir: Path | None = typer.Option(
        None, help="輸出目錄（預設讀 settings，research/laggard_grid）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """WS-D 族群內強弱：族群強弱×個股領先落後×位階 forward 報酬格（WS5-② 正式驗證）。"""
    from tw_screener.backtest.laggard_grid_runner import run_laggard_grid

    run_laggard_grid(settings, out_dir)


@backtest_app.command("flow-inflection")
def backtest_flow_inflection_cmd(
    out_dir: Path | None = typer.Option(
        None, help="輸出目錄（預設讀 settings，research/flow_inflection）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """WS-E 資金流 inflection 因子族首驗（差分/加速度/z-of-z/主體拆分，走 factor_lab）。"""
    from tw_screener.backtest.flow_inflection_runner import run_flow_inflection

    run_flow_inflection(settings, out_dir)


@backtest_app.command("diagnose")
def backtest_diagnose_cmd(
    out_dir: Path | None = typer.Option(
        None, help="診斷報告輸出目錄（預設讀 settings，research/diagnostic）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """M-Diag1「抓太晚」診斷：進場延伸度分桶曲線＋排序訊號 IC＋組內名次 skill（研究軌）。"""
    from tw_screener.backtest.diagnostic_runner import run_diagnostic

    run_diagnostic(settings, out_dir)


@report_app.command("check")
def report_check_cmd(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """F4：產物完整性檢查——比對本週應產出清單＋歷週 pick 底帳，缺者 WARNING（不擋流程）。"""
    from tw_screener.report.artifact_check import run_artifact_check

    run_artifact_check(settings)


@picks_app.command("record")
def picks_record_cmd(
    week: str = typer.Option(..., help="週次目錄名（如 2026-W27）"),
    stock: str = typer.Option(..., help="股號"),
    layer: str | None = typer.Option(None, help="core|opportunity|pool（非 --excluded 必填）"),
    sub: str | None = typer.Option(None, help="所屬次產業（族群超額基準，用 concepts.yaml 名稱）"),
    entry: str | None = typer.Option(None, help="進場條件（自由文字）"),
    stop: str | None = typer.Option(None, help="停損條件（自由文字）"),
    thesis: str | None = typer.Option(None, help="入選論點短標（如「F 主升續勢」）"),
    ext_ma60: float | None = typer.Option(
        None, help="入選時距季線乖離%（預設自動讀該週 candidates_enriched）"
    ),
    excluded: bool = typer.Option(False, "--excluded", help="記錄為被旗標剔除（寫 excluded.csv）"),
    reason: str | None = typer.Option(None, help="剔除旗標類別（--excluded 必填，如 過熱）"),
    detail: str | None = typer.Option(None, help="剔除補充說明"),
    name: str | None = typer.Option(None, help="股名（預設自動讀該週 candidates_enriched）"),
    data_date: str | None = typer.Option(
        None, help="資料日 YYYY-MM-DD（預設自動讀該週 screen_result 的 screened_at）"
    ),
    late_entry: bool = typer.Option(
        False, "--late-entry",
        help="明示覆寫同週 data_date 一致性檢查（快取缺資料致 entry 順延等已知情形）",
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """F1-PO1：把一檔 pick／剔除紀錄寫進 reports/<week>/picks.csv 或 excluded.csv（冪等）。"""
    from tw_screener.report.picks_runner import run_pick_record

    run_pick_record(
        settings, week, stock, layer, sub, entry, stop, thesis, ext_ma60,
        excluded, reason, detail, name, data_date, late_entry,
    )


@picks_app.command("sync")
def picks_sync_cmd(
    week: str = typer.Option(..., help="週次目錄名（如 2026-W27）"),
    file: Path | None = typer.Option(
        None, help="pick.md 路徑（預設 reports/<week>/pick.md）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """F1-PO1：解析 pick.md 尾端機器可讀區塊，整批 upsert 進底帳（全列驗證過才寫、冪等）。"""
    from tw_screener.report.picks_runner import run_picks_sync

    run_picks_sync(settings, week, file)


@picks_app.command("outcome")
def picks_outcome_cmd(
    exit_date: str | None = typer.Option(
        None, help="到期快照截止日 YYYY-MM-DD（預設快取最新交易日）"
    ),
    diff: bool = typer.Option(False, "--diff", help="附 PO3 翻轉解剖（週對週降級＋翻轉前訊號）"),
    hold_weeks: str | None = typer.Option(
        None, help="固定持有窗清單（逗號分隔，預設讀 settings，如 2,4,8,12）"
    ),
    brief: bool = typer.Option(
        False, "--brief", help="WS-A3：只產上週 picks r+5 一頁 brief 進最新週報目錄（輸入包）"
    ),
    week: str | None = typer.Option(
        None,
        "--week",
        help="僅 --brief：指定評估週次（如 2026-W27），輸出改寫該週目錄的 pick_outcome_brief.md"
        "（預設＝底帳中 r+5 已到期的最近一週，寫最新週報目錄）",
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """F1 PO2–PO4：分層命中率×α（vs 大盤＋vs 族群）＋偽陰性帳，產 research/pick_outcome/。"""
    from tw_screener.backtest.picks_outcome_runner import run_picks_brief, run_picks_outcome

    if brief:
        run_picks_brief(settings, week)
        return
    run_picks_outcome(settings, exit_date, diff, hold_weeks)


@market_app.command("regime")
def market_regime_cmd(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """V2 大盤 regime 總控：印當前 進攻/中性/防禦 與 趨勢/廣度/資金 分項依據。"""
    import yaml

    from tw_screener.analysis.regime import compute_market_regime, describe_regime

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    result = compute_market_regime(cfg, settings)
    if result.as_of is None:
        console.print("[red]無日線快取（daily_*.parquet）——先跑 data fetch-twse[/red]")
        raise typer.Exit(1)

    desc = describe_regime(result)
    color = {"進攻": "green", "中性": "yellow", "防禦": "red"}.get(result.regime, "white")
    console.print(
        f"\n[bold {color}]{desc['line']}[/bold {color}]  （截至 {desc['as_of']}）"
    )
    console.print(f"  姿態建議：{desc['advice']}\n")
    ev = result.evidence
    t = cast("dict", ev.get("trend", {}))
    b = cast("dict", ev.get("breadth", {}))
    fl = cast("dict", ev.get("flow", {}))
    if "index" in t:
        mas = "、".join(f"{k.upper()} {v}" for k, v in t.get("ma", {}).items())
        console.print(f"  趨勢：指數 {t['index']}　vs　{mas}")
    if "frac_above_ma" in b:
        console.print(
            f"  廣度：站上均線比例 {b['frac_above_ma']:.0%}（{b['n_priced']} 檔）"
            f"・指數位階 {b.get('index_position')}"
        )
    if fl:
        flows = "、".join(
            f"{k.replace('avg_daily_net_', '').replace('d', '日')} {v:+,} 股"
            for k, v in fl.items()
        )
        console.print(f"  資金：全市場法人日均淨流 {flows}")
    console.print(
        "\n[dim]定位＝輔助姿態揭露，非硬性 gate；最終由人決策（CLAUDE.md Part 3）。[/dim]"
    )


def _resolve_week_dir(cfg: dict, week: str) -> "Path | None":
    """解析 --week：空＝最新一週目錄；否則取 reports/<week>。找不到回 None。"""
    rdir = Path(cfg["paths"]["reports_dir"])
    if not rdir.exists():
        return None
    if week:
        d = rdir / week
        return d if d.is_dir() else None
    week_dirs = sorted(
        (d for d in rdir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True
    )
    return week_dirs[0] if week_dirs else None


def _load_portfolio_members(week_dir: "Path", include_candidates: bool) -> "pl.DataFrame":
    """讀 holdings_enriched.csv（持股為主）；--include-candidates 時併入候選（去重 keep 持股）。"""
    import polars as _pl

    cols = ["stock_id", "name", "industry", "theme"]

    def _read(path: Path) -> "pl.DataFrame":
        if not path.exists():
            return _pl.DataFrame()
        df = _pl.read_csv(str(path), infer_schema_length=2000)
        keep = [c for c in cols if c in df.columns]
        return df.select(keep).with_columns(_pl.col("stock_id").cast(_pl.Utf8))

    holdings = _read(week_dir / "holdings_enriched.csv")
    if not include_candidates:
        return holdings
    candidates = _read(week_dir / "candidates_enriched.csv")
    if candidates.is_empty():
        return holdings
    if holdings.is_empty():
        return candidates
    held = set(holdings["stock_id"].to_list())
    extra = candidates.filter(~_pl.col("stock_id").is_in(list(held)))
    return _pl.concat([holdings, extra], how="diagonal")


@portfolio_app.command("check")
def portfolio_check_cmd(
    week: str = typer.Option("", "--week", help="週別（如 2026-W26）；空＝最新一週"),
    include_candidates: bool = typer.Option(
        False, "--include-candidates", help="併入本週候選股（預設只看 holdings 持股）"
    ),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """V3 組合層風控：對持股算 標籤集中度／報酬相關簇／因子簇曝險（風險揭露，非硬約束）。"""
    import yaml

    from tw_screener.analysis.portfolio import (
        compute_portfolio_check,
        describe_portfolio_check,
    )
    from tw_screener.analysis.rotation import load_market_history

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    week_dir = _resolve_week_dir(cfg, week)
    if week_dir is None:
        console.print("[red]找不到 reports 週別目錄——先跑 make week 產出 enriched CSV[/red]")
        raise typer.Exit(1)

    members = _load_portfolio_members(week_dir, include_candidates)
    if members.is_empty():
        console.print(f"[red]{week_dir.name} 無 holdings_enriched.csv（或欄位缺）[/red]")
        raise typer.Exit(1)

    pcfg = cfg.get("portfolio", {})
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    price_history = load_market_history(cache_dir, n_days=int(pcfg.get("history_days", 90)))
    result = compute_portfolio_check(members, price_history, pcfg)
    desc = describe_portfolio_check(result)

    src = "持股＋候選" if include_candidates else "持股"
    console.print(f"\n[bold]{desc['line']}[/bold]（{week_dir.name}・{src}）")
    if desc.get("as_of"):
        console.print(f"  價格史截至 {desc['as_of']}")

    # 1. 標籤集中度
    flagged_labels = cast("list", desc["flagged_labels"])
    if flagged_labels:
        console.print("\n[yellow]標籤集中（次產業/主題）：[/yellow]")
        for d in flagged_labels:
            console.print(
                f"  {d['label']}：{d['count']} 檔（{float(d['share']):.0%}）"
                f"　{', '.join(cast('list', d['stock_ids']))}"
            )
    else:
        console.print("\n  標籤集中：無達門檻")

    # 2. 報酬相關簇
    corr_clusters = cast("list", desc["corr_clusters"])
    if corr_clusters:
        console.print("\n[yellow]高相關簇（近報酬共動）：[/yellow]")
        for cl in corr_clusters:
            top = cast("list", cl["pairs"])[:3]
            pairs_s = "、".join(f"{p['a']}~{p['b']} ρ{float(p['rho']):+.2f}" for p in top)
            console.print(
                f"  {cl['size']} 檔：{', '.join(cast('list', cl['stock_ids']))}　[{pairs_s}]"
            )
    else:
        console.print("\n  高相關簇：無")

    # 3. 因子簇曝險
    console.print("\n  因子簇曝險：")
    for d in cast("list", desc["factor_clusters"]):
        mark = "[red]⚠ 超限[/red]" if d.get("flagged") else "ok"
        cap = []
        if d.get("max_count") is not None:
            cap.append(f"≤{d['max_count']}檔")
        if d.get("max_share") is not None:
            cap.append(f"≤{float(d['max_share']):.0%}")
        console.print(
            f"  {d['name']}：{d['count']} 檔（{float(d['share']):.0%}，上限 {'/'.join(cap)}）"
            f" {mark}　{', '.join(cast('list', d['stock_ids']))}"
        )

    for note in cast("list", desc["notes"]):
        console.print(f"  [dim]註：{note}[/dim]")
    console.print(
        "\n[dim]定位＝風險揭露，非硬約束；「合計%」為等權檔數佔比近似（無部位大小）；"
        "相關隨市況變、由人決策（規劃書 03 V3）。[/dim]"
    )


@cp_app.command("calibrate")
def cp_calibrate_cmd(
    out_dir: Path = typer.Option(Path("research/cp_value"), help="校準報告輸出目錄"),
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """B2 個股版起漲事件回測（研究軌）：三 label 各掃因子訊號，產 research/cp_value/ 報告。"""
    from tw_screener.backtest.cp_calib_runner import run_cp_calibration

    run_cp_calibration(out_dir, settings)


@cp_app.command("candidates")
def cp_candidates_cmd(
    settings: Path = typer.Option(Path("config/settings.yaml"), help="設定檔路徑"),
) -> None:
    """B3 個股 CP 候選清單（生產軌）：套 B2 勝出因子於最新快照，產 reports/週次/cp_candidates.*。"""
    from tw_screener.report.cp_candidates_runner import run_cp_candidates

    run_cp_candidates(settings)


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
