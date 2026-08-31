"""族群分析（analysis group）的編排層（自 cli.py 下沉）。

CLI 只保留參數解析＋呼叫 run_group_analysis；資料載入、enrich、組合體檢段與
報表/CSV 產出都在這裡，行為與搬出前一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
from rich.console import Console

from tw_screener.analysis.watchlist import (
    enrich_named_list,
    load_latest_screener_results,
    read_holdings_csv,
    read_watchlist_csv,
)

if TYPE_CHECKING:
    import polars as pl

console = Console()


def run_group_analysis(settings: Path) -> None:
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

    week_tag, screener_results = load_latest_screener_results(settings)
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
            cov_min, cov_med = str(per_stock_days.min()), str(per_stock_days.median())
            console.print(
                f"  候選股 {len(candidate_ids)} 檔，歷史覆蓋 min={cov_min}、median={cov_med} 日"
            )

    # as_of 對齊價格錨點（latest_trading_date）：TPEX 上櫃法人更新較快，不封頂會讓上櫃股
    # 法人多窗領先日線一天、與上市股不對稱（見 load_institutional_history docstring）。
    institutional = client.load_institutional_history(
        n_days=20, as_of=client.latest_trading_date()
    )
    if institutional.is_empty():
        console.print(
            "[yellow]  無法人快取，族群法人強度將為 0[/yellow]"
            "（建議先跑 make fetch-institutional-history）"
        )
    else:
        console.print(f"  法人快取：{institutional['date'].n_unique()} 個交易日")

    # 量窗：量比需 vol_lookback+1；F5 軌跡量比需 回踩窗+前段窗+1（取大；量比 tail 不受多載影響）
    _traj_cfg = cfg.get("trajectory", {})
    _vol_days = max(
        vol_lookback + 1,
        int(_traj_cfg.get("pullback_vol_window", 5))
        + int(_traj_cfg.get("base_vol_window", 20))
        + 1,
    )
    volume_history = client.load_volume_history(candidate_ids, n_days=_vol_days)
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
        trajectory_cfg=cfg.get("trajectory", {}),  # F5 軌跡欄（沿舊 07 TR1）
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
    # 大盤 regime 總控（規劃書 03 V2）：全市場日線＋已載法人快取 → 進攻/中性/防禦姿態
    from tw_screener.analysis.regime import compute_market_regime, describe_regime
    from tw_screener.report.density import data_density_note

    console.print("  計算大盤 regime（趨勢/廣度/資金）...")
    regime_result = compute_market_regime(cfg, settings, institutional=institutional)
    regime = describe_regime(regime_result)
    console.print(f"  {regime['line']}")

    # M2 投降洗盤 flag（委託書 M2）：**反向訊號**，獨立於 regime 分數、不改燈色/排序。
    # 容錯：任何一步壞掉都只是這段不渲染，不擋週報主流程（同總經燈號的處理）。
    try:
        from tw_screener.analysis.washout import (
            append_washout_history,
            compute_market_washout,
            render_washout_block,
        )

        # 不傳 price_history：本函式的 price_history 是**候選股**日線（load_candidate_history，
        # ~百檔），不是全市場。等權指數必須用 load_market_history 的全市場宇宙，
        # 口徑才與 regime 一致；讓 compute_market_washout 自己載。
        washout_result = compute_market_washout(cfg, settings, regime_result)
        regime["washout_lines"] = render_washout_block(washout_result)
        regime["washout_triggered"] = washout_result.triggered
        if washout_result.triggered:
            regime["washout_posture"] = washout_result.posture_note
        console.print(
            f"  投降洗盤 flag：{'觸發' if washout_result.triggered else '未觸發'}"
            f"（已求值 {washout_result.n_evaluable} 項中觸發 {washout_result.n_hit} 項）"
        )
        if washout_result.as_of is not None:
            append_washout_history(
                Path(cfg.get("washout", {}).get(
                    "history_path", "data/washout/washout_history.parquet"
                )),
                washout_result,
                washout_result.as_of,
            )
    except Exception as exc:  # noqa: BLE001 — 投降偵測非主流程關鍵路徑，壞了不擋報告
        console.print(f"[yellow]  投降洗盤 flag 計算失敗，Section 0 略過該段：{exc}[/yellow]")

    # 總經燈號（docs/25 v2）：讀 make macro 最新一列 history.parquet；讀不到/過期 → 不渲染該段。
    macro_light: dict | None = None
    _macro_result = None  # MacroLight｜None；渲染完摘要後重用於逐指標明細 CSV，避免重算
    _macro_history_path = Path(cfg["paths"]["data_dir"]) / "macro_regime" / "history.parquet"
    if _macro_history_path.exists():
        from tw_screener.analysis.macro_regime import (
            compute_market_macro,
            describe_macro_light,
            load_panel_deltas,
            resolve_panel_history_path,
        )

        _mr_cfg = cfg.get("macro_regime", {})
        _mr_df = _pl.read_parquet(_macro_history_path)
        _mr_stale_days = int(_mr_cfg.get("stale_days", 10))
        if not _mr_df.is_empty():
            _latest_row = _mr_df.sort("as_of").tail(1)
            _latest_as_of = _latest_row["as_of"].item()
            if _latest_as_of is not None and (_date.today() - _latest_as_of).days <= _mr_stale_days:
                # 重跑 compute_market_macro 拿完整 MacroLight（history.parquet 只存摘要欄位，
                # 揭露面板明細需重算；FRED 24h 快取命中，不會真的打網）
                try:
                    _macro_result = compute_market_macro(
                        cfg, settings, history_path=_macro_history_path
                    )
                    # docs/26 A案：面板變化欄（讀 panel_history，只讀不寫——append 是
                    # `make macro` 的職責，報告渲染不該產生新的歷史列）
                    _macro_deltas = load_panel_deltas(
                        resolve_panel_history_path(cfg), _macro_result, cfg
                    )
                    macro_light = describe_macro_light(_macro_result, _macro_deltas)
                    console.print(f"  {macro_light['line']}")
                except Exception as exc:  # noqa: BLE001 — 總經燈號非主流程關鍵路徑，壞了不擋報告
                    console.print(f"[yellow]  總經燈號讀取失敗，Section 0 略過該段：{exc}[/yellow]")
                    macro_light = None
                    _macro_result = None

    # 組合層風控（規劃書 03 V3）：持股標籤集中度＋因子簇曝險（價格無關、render 期即可得）。
    # 報酬相關簇需全市場日線，留給 `portfolio check` CLI；報告段只揭露集中度/因子簇。
    portfolio = _portfolio_section_for_report(cfg, industry_df, themes_long)

    _hist_days = price_history["date"].n_unique() if not price_history.is_empty() else 0
    render_group_report(
        groups, leaders, screener_results, week_tag, output_path, top_groups, top_stocks,
        dividend_events=dividends, themes_long=themes_long, macro_events=macro_events,
        radar_cfg=ga_cfg.get("radar"),
        density_note=data_density_note(_hist_days),
        regime=regime, portfolio=portfolio, macro_light=macro_light,
    )

    # 總經燈號逐指標明細落地（docs/25 v2 §4.2）：週報正文只放摘要，明細供互動深挖。
    if _macro_result is not None:
        from tw_screener.analysis.macro_regime import to_detail_frame

        try:
            to_detail_frame(_macro_result).write_csv(output_path.parent / "macro_regime.csv")
        except OSError as exc:  # 明細落地失敗不擋主報告（已渲染完成）
            console.print(
                f"[yellow]  macro_regime.csv 落地失敗（不影響已產出的報告）：{exc}[/yellow]"
            )

    from tw_screener.report.group_report import write_candidates_enriched_csv

    rev_df = client.fetch_revenue()
    rev_yoy_map: dict[str, object] = {}
    cum_rev_yoy_map: dict[str, object] = {}
    name_map: dict[str, str] = {}
    if not rev_df.is_empty() and "stock_id" in rev_df.columns:
        rdf = rev_df
        if "year_month" in rev_df.columns:
            rdf = rev_df.sort("year_month", descending=True)
        for rr in rdf.iter_rows(named=True):
            sid = str(rr["stock_id"])
            if "yoy_pct" in rev_df.columns:
                rev_yoy_map.setdefault(sid, rr.get("yoy_pct"))
            if "cum_yoy_pct" in rev_df.columns:
                cum_rev_yoy_map.setdefault(sid, rr.get("cum_yoy_pct"))
            if "company_name" in rev_df.columns:
                name_map.setdefault(sid, str(rr.get("company_name") or ""))

    # 市值（億元）＝已發行股數×收盤價/1e8：上市+上櫃股數合併，純讀快取（make fetch-twse 累積）。
    listed_shares_df = client.fetch_listed_shares()
    otc_shares_df = client.fetch_otc_shares()
    shares_map: dict[str, object] = {}
    for shares_df in (listed_shares_df, otc_shares_df):
        if shares_df.is_empty() or "stock_id" not in shares_df.columns:
            continue
        for rr in shares_df.iter_rows(named=True):
            shares_map[str(rr["stock_id"])] = rr.get("shares_outstanding")

    # M-BR1：月營收 YoY 二階導（本月 YoY − 上月 YoY）；純讀既有快取，不多打一次網。
    # 餵 fundamental_health 揭露欄——把「YoY 水準」與「加速度」拆開（sell-the-news 真因）。
    rev_yoy_delta_map = client.load_revenue_yoy_deltas()

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
    from tw_screener.analysis.sector_universe import (
        build_broad_industry_membership,
        build_peer_membership,
        list_subindustries,
    )
    from tw_screener.analysis.valuation import (
        build_valuation,
        compute_self_history_median,
        compute_self_history_median_pb,
        compute_self_history_median_yield,
        compute_self_history_pctile,
        compute_valuation_legs,
    )

    val_df = client.load_latest_valuation_ratios()
    val_cfg = cfg.get("cp_value", {}).get("valuation", {})
    val_membership = build_peer_membership(list_subindustries(), industry_df)
    val_broad_membership = build_broad_industry_membership(industry_df)
    val_min_peers = int(val_cfg.get("min_peers", 5))
    valuation = build_valuation(
        val_df,
        val_membership,
        min_peers=val_min_peers,
        cheap_pctile=float(val_cfg.get("cheap_pctile", 30.0)),
        # 手標次產業樣本 <min_peers（如晶圓代工全市場僅4檔）退用TWSE粗產業別，
        # 讓這類股票不再永遠估值缺（2026-08-29，docs/31 §20.8）
        broad_membership=val_broad_membership,
    )
    # docs/31 §14：自身估值歷史百分位粗版代理（跟val_pctile的同儕橫斷面互補，不取代）——
    # 純揭露欄，任一步驟壞掉不擋主流程（同官方族群前5段的容錯慣例）。
    try:
        val_history = client.load_valuation_ratios_history()
        self_history_min_snapshots = int(val_cfg.get("self_history_min_snapshots", 8))
        self_history = compute_self_history_pctile(
            val_history, min_snapshots=self_history_min_snapshots
        )
        if not self_history.is_empty():
            valuation = valuation.join(self_history, on="stock_id", how="left")
        else:
            valuation = valuation.with_columns(
                _pl.lit(None, dtype=_pl.Float64).alias("pe_self_pctile"),
                _pl.lit(None, dtype=_pl.Int64).alias("pe_self_n"),
            )
    except Exception as e:  # noqa: BLE001 — 純揭露段，任何一步壞掉不擋 group 報告主流程
        console.print(f"[yellow]  自身估值歷史百分位計算失敗，該段留空：{e}[/yellow]")
        valuation = valuation.with_columns(
            _pl.lit(None, dtype=_pl.Float64).alias("pe_self_pctile"),
            _pl.lit(None, dtype=_pl.Int64).alias("pe_self_n"),
        )
    # docs/31 §20.7：估值回歸參考價「自身回歸」腿的錨點（自身歷史PE中位數，跟上面的
    # pe_self_pctile百分位是互補不同用途，同一份val_history重用不重抓）。只取
    # pe_self_median（pe_self_n跟pctile那次join算出來的同一份，避免重複欄位衝突）。
    try:
        self_history_median = compute_self_history_median(
            val_history, min_snapshots=self_history_min_snapshots
        )
        if not self_history_median.is_empty():
            valuation = valuation.join(
                self_history_median.select("stock_id", "pe_self_median"),
                on="stock_id", how="left",
            )
        else:
            valuation = valuation.with_columns(
                _pl.lit(None, dtype=_pl.Float64).alias("pe_self_median")
            )
    except Exception as e:  # noqa: BLE001 — 純揭露段，任何一步壞掉不擋 group 報告主流程
        console.print(f"[yellow]  自身估值歷史中位數計算失敗，該段留空：{e}[/yellow]")
        valuation = valuation.with_columns(
            _pl.lit(None, dtype=_pl.Float64).alias("pe_self_median")
        )
    # docs/31 §20.9：估值回歸參考價（綜合版）額外4條線索——同儕PB/同儕殖利率（不論
    # PE是否可用都額外算，跟build_valuation()的val_metric主鏡頭選擇平行、互不影響）
    # ＋自身PB歷史/自身殖利率歷史中位數。純揭露段，任一步驟壞掉不擋主流程。
    _composite_leg_cols = (
        "pb_peer_median", "yield_peer_median", "pb_self_median", "pb_self_n",
        "yield_self_median", "yield_self_n",
    )
    try:
        legs = compute_valuation_legs(
            val_df, val_membership, min_peers=val_min_peers,
            broad_membership=val_broad_membership,
        )
        if not legs.is_empty():
            valuation = valuation.join(legs, on="stock_id", how="left")
        else:
            valuation = valuation.with_columns(
                _pl.lit(None, dtype=_pl.Float64).alias("pb_peer_median"),
                _pl.lit(None, dtype=_pl.Float64).alias("yield_peer_median"),
            )
        pb_self = compute_self_history_median_pb(
            val_history, min_snapshots=self_history_min_snapshots
        )
        if not pb_self.is_empty():
            valuation = valuation.join(pb_self, on="stock_id", how="left")
        else:
            valuation = valuation.with_columns(
                _pl.lit(None, dtype=_pl.Float64).alias("pb_self_median"),
                _pl.lit(None, dtype=_pl.Int64).alias("pb_self_n"),
            )
        yield_self = compute_self_history_median_yield(
            val_history, min_snapshots=self_history_min_snapshots
        )
        if not yield_self.is_empty():
            valuation = valuation.join(yield_self, on="stock_id", how="left")
        else:
            valuation = valuation.with_columns(
                _pl.lit(None, dtype=_pl.Float64).alias("yield_self_median"),
                _pl.lit(None, dtype=_pl.Int64).alias("yield_self_n"),
            )
    except Exception as e:  # noqa: BLE001 — 純揭露段，任何一步壞掉不擋 group 報告主流程
        console.print(f"[yellow]  估值回歸參考價（綜合版）額外線索計算失敗，該段留空：{e}[/yellow]")
        valuation = valuation.with_columns(
            *[_pl.lit(None, dtype=_pl.Int64 if c.endswith("_n") else _pl.Float64).alias(c)
              for c in _composite_leg_cols if c not in valuation.columns]
        )
    valuation_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in valuation.iter_rows(named=True)}
        if not valuation.is_empty()
        else {}
    )

    # D3 集保大戶持股比（≥400張 / ≥1000張＋WoW）：純讀快取（make week 的 fetch-tdcc 累積）。
    # TDCC 異常時回空表 → 大戶欄誠實 null，不擋報告。
    from tw_screener.data.tdcc import create_tdcc_client

    bh_df = create_tdcc_client(settings).load_big_holders()
    big_holder_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in bh_df.iter_rows(named=True)}
        if not bh_df.is_empty()
        else {}
    )

    # 上市融資融券（D4）：純讀快取（make week 的 fetch-twse 累積）。上櫃缺→該股 margin 欄 null。
    margin_df = client.load_margin_signals()
    margin_map: dict[str, dict] = (
        {str(r["stock_id"]): r for r in margin_df.iter_rows(named=True)}
        if not margin_df.is_empty()
        else {}
    )

    # docs/31 §12/§13：官方族群前5揭露欄——重用官方族群指數重測（§10）已驗證的純函式，
    # 不重新設計。純揭露非gate/非排序輸入；任一步驟資料缺席就整段跳過（誠實留白，
    # 不擋 group 報告主流程，同 washout/macro 等既有揭露段的容錯慣例）。
    official_sector_map: dict[str, dict] = {}
    try:
        from tw_screener.analysis.rotation import load_market_history
        from tw_screener.backtest import official_sector_grid as osg
        from tw_screener.backtest import official_sector_watch as osw
        from tw_screener.backtest.rotation_efficacy import trend_score_series

        osc_cfg = cfg.get("backtest", {}).get("official_sector_watch", {})
        os_min_purity = float(osc_cfg.get("min_purity", 0.5))
        os_top_n = int(osc_cfg.get("top_n_groups", 5))
        os_history_days = int(osc_cfg.get("market_history_days", 90))
        os_cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

        os_data_date = client.latest_trading_date()
        if os_data_date is not None and not industry_df.is_empty():
            client.fetch_sector_index_historical(os_data_date)
            os_sector_index = client.load_sector_index_history()
            os_hand = list_subindustries()
            if not os_sector_index.is_empty() and not os_hand.is_empty():
                os_purity = osg.compute_subindustry_purity(os_hand, industry_df)
                os_membership = osg.build_hand_sector_membership(
                    os_hand, os_purity, min_purity=os_min_purity
                )
                os_baskets = osg.build_hand_sector_baskets(
                    os_sector_index, os_purity, min_purity=os_min_purity
                )
                os_market_hist = load_market_history(os_cache_dir, n_days=os_history_days)
                if (
                    not os_membership.is_empty() and not os_baskets.is_empty()
                    and not os_market_hist.is_empty()
                ):
                    os_price = os_market_hist.select("date", "stock_id", "close")
                    os_trend = trend_score_series(os_price, os_membership, os_baskets)
                    os_names = {
                        str(rr["stock_id"]): rr.get("stock_name")
                        for rr in industry_df.iter_rows(named=True)
                    }
                    os_snapshot = osw.latest_top5_snapshot(
                        os_membership, os_trend, os_names, week_tag, os_data_date,
                        os_min_purity, top_n_groups=os_top_n,
                    )
                    if not os_snapshot.is_empty():
                        os_best = os_snapshot.sort(["stock_id", "group_rank"]).unique(
                            subset=["stock_id"], keep="first"
                        )
                        official_sector_map = {
                            str(rr["stock_id"]): rr for rr in os_best.iter_rows(named=True)
                        }
                        # 讓research/底帳跟著make week自動累積（docs/31 §13.3）——
                        # 不必再手動額外跑 official-sector-watch 指令。
                        osw_out = Path(
                            osc_cfg.get("output_path", "research/official_sector_watch/ledger.csv")
                        )
                        osw.upsert_ledger(osw_out, os_snapshot)
        console.print(f"  官方族群前5：{len(official_sector_map)} 檔命中（purity≥{os_min_purity}）")
    except Exception as e:  # noqa: BLE001 — 純揭露段，任何一步壞掉不擋 group 報告主流程
        console.print(f"[yellow]  官方族群前5揭露欄計算失敗，該段留空：{e}[/yellow]")
        official_sector_map = {}

    # docs/31 §4/§9/§11（2026-08-24 使用者要求）：G1/G2/G4/G5/L6/F2' 新設計候選揭露欄。
    # 這五式此前只累積在 gitignored 的 research/ 底帳（`g1-g2-g5-watch`／`l6-g4-watch`
    # 手動指令），使用者從未在週報實際看到。這裡把底帳計算搬進 group（比照上面
    # official_sector 段的整合模式）、順便自動 upsert 兩份底帳，並把命中旗標揭露成
    # candidates_enriched.csv 的單一欄位。純觀察，不進篩選/排序/pick.md 核心層；
    # G3 已驗證未過關（docs/31 §9），不放進這欄——放進去會把被否證的訊號包裝成觀察名單。
    # 只加一欄（逗號分隔命中旗標），不加五個布林欄，避免重蹈 §18.1 已反省過的欄位膨脹。
    redesign_watch_map: dict[str, str] = {}
    try:
        from tw_screener.backtest.g1_g2_g5_watch import build_g1_g2_g5_inputs
        from tw_screener.backtest.g1_g2_g5_watch import build_g1_g2_g5_snapshot as _g1g2g5_snap
        from tw_screener.backtest.g1_g2_g5_watch import upsert_ledger as upsert_g1g2g5_ledger
        from tw_screener.backtest.l6_g4_watch import build_l6_g4_inputs, upsert_l6_g4_ledger
        from tw_screener.backtest.l6_g4_watch import build_l6_g4_snapshot as _l6g4_snap
        from tw_screener.screener.local.universe import build_local_universe

        rw_data_date = client.latest_trading_date()
        rw_universe = build_local_universe(client)
        if rw_data_date is not None and not rw_universe.is_empty():
            g1g2g5_cfg = cfg.get("backtest", {}).get("g1_g2_g5_watch", {})
            l6g4_cfg = cfg.get("backtest", {}).get("l6_g4_watch", {})

            g1g2g5_inputs = build_g1_g2_g5_inputs(client, cfg, rw_universe)
            g1g2g5_snapshot = _g1g2g5_snap(
                rw_universe, g1g2g5_inputs.fundamentals, g1g2g5_inputs.gross_margin_peer,
                g1g2g5_inputs.valuation, g1g2g5_inputs.ma60_map, g1g2g5_inputs.amount_map,
                week_tag, rw_data_date,
                g1_delta_net_margin_min=float(g1g2g5_cfg.get("g1_delta_net_margin_min", 1.5)),
                g1_ma60_max_pct=float(g1g2g5_cfg.get("g1_ma60_max_pct", 15.0)),
                g2_roe_min=float(g1g2g5_cfg.get("g2_roe_min", 3.5)),
                g2_debt_max_pct=float(g1g2g5_cfg.get("g2_debt_max_pct", 60.0)),
                g2_current_min=float(g1g2g5_cfg.get("g2_current_min", 1.2)),
                g2_mktcap_min_billion=float(g1g2g5_cfg.get("g2_mktcap_min_billion", 300.0)),
                g5_val_pctile_max=float(g1g2g5_cfg.get("g5_val_pctile_max", 40.0)),
                g5_amount_min_million=float(g1g2g5_cfg.get("g5_amount_min_million", 300.0)),
                f2_pe_min=float(g1g2g5_cfg.get("f2_pe_min", 15.0)),
                f2_pe_max=float(g1g2g5_cfg.get("f2_pe_max", 30.0)),
                f2_mktcap_min_billion=float(g1g2g5_cfg.get("f2_mktcap_min_billion", 300.0)),
            )
            upsert_g1g2g5_ledger(
                Path(g1g2g5_cfg.get("output_path", "research/g1_g2_g5_watch/ledger.csv")),
                g1g2g5_snapshot,
            )

            l6g4_inputs = build_l6_g4_inputs(client, cfg, rw_data_date)
            l6g4_snapshot = _l6g4_snap(
                rw_universe, l6g4_inputs.revenue, l6g4_inputs.yoy_deltas,
                l6g4_inputs.trust_net_5d, week_tag, rw_data_date,
                l6_yoy_min=float(l6g4_cfg.get("l6_yoy_min", 20.0)),
                l6_pe_max=float(l6g4_cfg.get("l6_pe_max", 25.0)),
                l6_mktcap_min_billion=float(l6g4_cfg.get("l6_mktcap_min_billion", 100.0)),
            )
            upsert_l6_g4_ledger(
                Path(l6g4_cfg.get("output_path", "research/l6_g4_watch/ledger.csv")),
                l6g4_snapshot,
            )

            for snap, tag_cols in (
                (g1g2g5_snapshot, ("g1", "g2", "g5", "f2")),
                (l6g4_snapshot, ("l6_2cond", "l6_4cond", "g4")),
            ):
                for row in snap.iter_rows(named=True):
                    hits = [c for c in tag_cols if row.get(c)]
                    if not hits:
                        continue
                    sid = str(row["stock_id"])
                    prior = redesign_watch_map.get(sid, "")
                    redesign_watch_map[sid] = ",".join(
                        [t for t in prior.split(",") if t] + hits
                    )
        console.print(f"  docs/31新設計候選觀察（未驗證）：{len(redesign_watch_map)} 檔命中")
    except Exception as e:  # noqa: BLE001 — 純揭露段，任何一步壞掉不擋 group 報告主流程
        console.print(f"[yellow]  G1/G2/G4/G5/L6/F2'揭露欄計算失敗，該段留空：{e}[/yellow]")
        redesign_watch_map = {}

    csv_path = output_path.parent / "candidates_enriched.csv"
    cand_rows = write_candidates_enriched_csv(
        leaders, themes_long, screener_results, csv_path,
        flags_cfg=cfg.get("propicks_flags"), rev_yoy_map=rev_yoy_map,
        fundamentals_map=fundamentals_map, valuation_map=valuation_map,
        big_holder_map=big_holder_map, margin_map=margin_map,
        near_flow_cfg=cfg.get("near_flow", {}),  # F5 近端籌碼揭露欄（沿舊 06 NF1）
        contrarian_cfg=cfg.get("contrarian_base", {}),  # M-BR1 底部左側欄（規劃書 24／委託書 M1）
        inflection_cfg=cfg.get("inflection", {}),      # M4.1 轉折早段欄（委託書 M4）
        deep_value_cfg=cfg.get("deep_value", {}),      # M5 深值成長 tag（委託書 M5）
        rev_yoy_delta_map=rev_yoy_delta_map,
        cum_rev_yoy_map=cum_rev_yoy_map,
        shares_map=shares_map,
        official_sector_map=official_sector_map,
        official_sector_regime=cast("str | None", regime.get("regime")),
        redesign_watch_map=redesign_watch_map,
    )
    # 重疊股重用：庫存/觀察清單同檔一律沿用 candidates 那筆，避免跨 CSV 量比/集中度/成交額分岔
    canonical_rows = {row["stock_id"]: row for row in cand_rows}
    n_cand = len(cand_rows)

    console.print(f"[green]報告輸出：{output_path}[/green]")
    console.print(f"  全候選股完整欄位 CSV：{csv_path}（{n_cand} 檔，供 ProPicks 全宇宙挑股）")

    # M4.2 轉折埋伏候選源 E（委託書 M4.2）：法人剛開始買、價格還在底部。
    # 必須在 enriched 之後跑（四個條件全依賴 enriched 欄）——這也是它沒能塞進
    # cp_candidates.md 的原因（那步在 ⑧、早於本步 ⑨），見 report/inflection_ambush.py docstring。
    try:
        from tw_screener.report.inflection_ambush import (
            build_inflection_ambush,
            render_inflection_ambush,
        )

        icfg = cfg.get("inflection", {}) or {}
        near_low = float(icfg.get("ambush_near_low_pct", 10.0))
        days_rng = tuple(int(x) for x in icfg.get("ambush_inflection_days", [1, 5]))[:2]
        small_pos = float(icfg.get("ambush_foreign_20d_small_positive_lots", 5000))
        allow_bz = bool(icfg.get("ambush_allow_base_zone_branch", True))
        nm_limit = int(icfg.get("ambush_near_miss_md_limit", 15))
        # infer_schema_length=None：cand_rows 常 >100 列，預設只看前 100 列猜型別，
        # 晚出現的字串值（如次產業「主機板」）會撞錯，2026-08-29 實跑觸發
        # （同 report/inflection_ambush.py 內部已修過的成因，那邊修的是另一個
        # 更晚的 DataFrame 建構點，這裡是更早、真正先炸的那個）
        qualified, near_miss = build_inflection_ambush(
            _pl.DataFrame(cand_rows, infer_schema_length=None) if cand_rows else _pl.DataFrame(),
            near_low_pct=near_low,
            inflection_days_range=(days_rng[0], days_rng[1]),
            small_positive_lots=small_pos,
            allow_base_zone_branch=allow_bz,
        )
        amb_path = output_path.parent / "inflection_ambush.md"
        amb_path.write_text(
            render_inflection_ambush(
                qualified, near_miss, week_tag, near_low,
                (days_rng[0], days_rng[1]), small_pos,
                near_miss_limit=nm_limit,
            ),
            encoding="utf-8",
        )
        if not qualified.is_empty():
            qualified.write_csv(output_path.parent / "inflection_ambush.csv")
        # md 截斷了 near_miss，完整名單只在 csv——不落檔就等於資料消失
        if not near_miss.is_empty():
            near_miss.write_csv(
                output_path.parent / "inflection_ambush_near_miss.csv"
            )
        console.print(
            f"  轉折埋伏候選（M4.2）：{amb_path}（合格 {qualified.height} 檔、"
            f"只差一條 {near_miss.height} 檔）"
        )
    except Exception as exc:  # noqa: BLE001 — 候選源 E 非主流程關鍵路徑，壞了不擋週報
        console.print(f"[yellow]  轉折埋伏清單產出失敗（不影響其他產物）：{exc}[/yellow]")

    # 庫存與觀察清單（必分析）→ enrich 成 reports 下 2 個 CSV
    from tw_screener.report.group_report import write_named_list_csv

    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_map = read_holdings_csv(wl_dir / "holdings.csv")
    watch_ids = read_watchlist_csv(wl_dir / "watchlist.csv")
    for label, ids, hmap in [
        ("holdings", list(holdings_map), holdings_map),
        ("watchlist", watch_ids, None),
    ]:
        if not ids:
            continue
        console.print(f"  enrich {label}（{len(ids)} 檔，無快取會抓網）...")
        wl_members, wl_synth = enrich_named_list(
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
            big_holder_map=big_holder_map, margin_map=margin_map,
            holdings_map=hmap, canonical_rows=canonical_rows,
            near_flow_cfg=cfg.get("near_flow", {}),
            contrarian_cfg=cfg.get("contrarian_base", {}),  # M-BR1（規劃書 24／委託書 M1）
            inflection_cfg=cfg.get("inflection", {}),      # M4.1（委託書 M4）
            deep_value_cfg=cfg.get("deep_value", {}),      # M5（委託書 M5）
            rev_yoy_delta_map=rev_yoy_delta_map,
            cum_rev_yoy_map=cum_rev_yoy_map,
            shares_map=shares_map,
        )
        console.print(f"[green]  {label}_enriched.csv：{n} 檔 → {out_csv}[/green]")

    console.print(f"  族群數：{len(groups)}，推薦分析：前 {top_stocks} 檔")


def _portfolio_section_for_report(
    cfg: dict, industry_df: pl.DataFrame | None, themes_long: pl.DataFrame | None
) -> dict | None:
    """group_analysis.md 組合體檢段：持股 ids ＋ industry_df ＋ themes_long → 標籤集中度/因子簇。

    純本地、不抓網、不需價格（相關簇留給 `portfolio check` CLI）。無持股回 None（不渲染該段）。
    """
    import polars as _pl

    from tw_screener.analysis.portfolio import (
        compute_portfolio_check,
        describe_portfolio_check,
    )

    wl_dir = Path(cfg["paths"].get("watchlist_dir", "watchlist"))
    holdings_ids = list(read_holdings_csv(wl_dir / "holdings.csv"))
    if not holdings_ids:
        return None
    base = _pl.DataFrame({"stock_id": [str(s) for s in holdings_ids]})
    if (
        industry_df is not None
        and not industry_df.is_empty()
        and {"stock_id", "industry_name"}.issubset(industry_df.columns)
    ):
        ind = industry_df.select(
            _pl.col("stock_id").cast(_pl.Utf8),
            _pl.col("industry_name").alias("industry"),
        ).unique(subset=["stock_id"])
        base = base.join(ind, on="stock_id", how="left")
    if themes_long is not None and not themes_long.is_empty():
        th = (
            themes_long.with_columns(_pl.col("stock_id").cast(_pl.Utf8))
            .group_by("stock_id")
            .agg(_pl.col("theme"))
            .with_columns(_pl.col("theme").list.join("、"))
        )
        base = base.join(th, on="stock_id", how="left")
    # 價格史傳空：報告段只取 label/factor（價格無關）；相關簇由 CLI 提供
    result = compute_portfolio_check(base, _pl.DataFrame(), cfg.get("portfolio", {}))
    return describe_portfolio_check(result)
