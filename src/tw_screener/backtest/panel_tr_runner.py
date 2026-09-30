"""D6 成員稠密總報酬面板編排（docs/33 §7；自 cli.py 薄殼呼叫）。

FinMind 稠密日線＋除權息（M-Pick3a 已回補）→ 今日次產業成員 → 2022 起的 ma60／r{h}
（除權息還原總報酬）→ research/panel/panel_tr_members.parquet；另出「新舊面板差異」報告
（列吻合率、r{h} 窗跨度、target 差、M-Pick2 主宇宙 target 差、抽查窗）與兩條事前寫定的驗收
（A1 窗跨度不退化、A2 前收自洽）。
**不覆蓋原面板、不計任何因子×target 統計**（M-Pick2 面板不得重測，docs/33 §4「明確不做」）。
"""

from __future__ import annotations

import glob
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import typer
from rich.console import Console

from tw_screener.backtest import intra_pick_oos as oos
from tw_screener.backtest import panel_tr as pt
from tw_screener.backtest.intra_pick_oos_runner import _member_price, _sha256

console = Console()


def _pct(v: float | None, nd: int = 1) -> str:
    return f"{v * 100:.{nd}f}%" if v is not None else "—"


def _pp(v: float | None) -> str:
    return f"{v:+.2f}" if v is not None else "—"


def _yearly_share(share: pl.DataFrame) -> pl.DataFrame:
    """月表 → 年表（依窗數加權平均 share_eq／share_gt）。"""
    return (
        share.with_columns(pl.col("ym").str.slice(0, 4).alias("year"))
        .group_by("year")
        .agg(
            pl.col("n_windows").sum(),
            (pl.col("share_eq") * pl.col("n_windows")).sum() / pl.col("n_windows").sum(),
            (pl.col("share_gt") * pl.col("n_windows")).sum() / pl.col("n_windows").sum(),
        )
        .sort("year")
    )


def _match_lines(match: pl.DataFrame, recent_from: str) -> list[str]:
    yearly = (
        match.with_columns(pl.col("ym").str.slice(0, 4).alias("year"))
        .filter(pl.col("ym") < recent_from)
        .group_by("year")
        .agg(pl.col("n_new").sum(), pl.col("n_matched").sum())
        .with_columns((pl.col("n_matched") / pl.col("n_new")).alias("match_rate"))
        .sort("year")
    )
    lines = ["| 期間 | 新面板列數 | 舊面板有同鍵列 | 吻合率 |", "|---|---|---|---|"]
    for r in yearly.iter_rows(named=True):
        lines.append(f"| {r['year']}（全年） | {r['n_new']:,} | {r['n_matched']:,} | "
                     f"{_pct(r['match_rate'])} |")
    for r in match.filter(pl.col("ym") >= recent_from).iter_rows(named=True):
        lines.append(
            f"| {r['ym']} | {r['n_new']:,} | {r['n_matched']:,} | {_pct(r['match_rate'])} |"
        )
    return lines


def _span_lines(new: pl.DataFrame, old: pl.DataFrame, h: int, recent_from: str) -> list[str]:
    ny = _yearly_share(new.filter(pl.col("ym") < recent_from))
    oy = _yearly_share(old.filter(pl.col("ym") < recent_from))
    lines = [
        f"| 期間 | 新：恰跨 {h} 日 | 新：多於 {h} 日 | 舊：恰跨 {h} 日 | 舊：多於 {h} 日 |",
        "|---|---|---|---|---|",
    ]
    old_y = {r["year"]: r for r in oy.iter_rows(named=True)}
    for r in ny.iter_rows(named=True):
        o = old_y.get(r["year"])
        lines.append(
            f"| {r['year']}（全年，至 {recent_from} 前） | {_pct(r['share_eq'])} | "
            f"{_pct(r['share_gt'])} | {_pct(o['share_eq']) if o else '—'} | "
            f"{_pct(o['share_gt']) if o else '—'} |"
        )
    old_m = {r["ym"]: r for r in old.iter_rows(named=True)}
    for r in new.filter(pl.col("ym") >= recent_from).iter_rows(named=True):
        o = old_m.get(r["ym"])
        lines.append(
            f"| {r['ym']} | {_pct(r['share_eq'])} | {_pct(r['share_gt'])} | "
            f"{_pct(o['share_eq']) if o else '—'} | {_pct(o['share_gt']) if o else '—'} |"
        )
    return lines


def _diff_lines(diff: pl.DataFrame) -> list[str]:
    lines = [
        "| 期間 | 兩邊皆有 | 僅新有（補回） | 僅舊有 | 差中位 | \\|差\\| p90 | \\|差\\| p99 | "
        "\\|差\\| max | \\|差\\|>1pp | \\|差\\|>5pp |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in diff.iter_rows(named=True):
        lines.append(
            f"| {r['period']} | {r['n_both']:,} | {r['n_new_only']:,} | {r['n_old_only']:,} | "
            f"{_pp(r['diff_median'])} | {_pp(r['abs_p90'])} | {_pp(r['abs_p99'])} | "
            f"{_pp(r['abs_max'])} | {r['n_gt_1pp']:,} | {r['n_gt_5pp']:,} |"
        )
    return lines


def render_report(res: dict[str, Any], pcfg: pt.PanelTrConfig) -> list[str]:
    h = pcfg.main_horizon
    recent = pcfg.span_check_since.strftime("%Y-%m")
    a1, a1_bad, floor = res["a1"], res["a1_bad"], res["floor"]
    a2 = res["a2"]
    a1_txt = "PASS" if a1 else (f"FAIL（{'、'.join(a1_bad)}）" if a1 is False else "無法判定")
    lines = [
        "# D6 成員稠密總報酬面板（docs/33 §7）",
        "",
        f"- 產出日：{res['today']}；輸出 `{res['out_path']}`，SHA-256 `{res['sha256']}`。",
        f"- 範圍：今日次產業成員（{res['n_members']:,} 檔）中在 FinMind 有成交價者 "
        f"{res['n_stocks']:,} 檔；{pcfg.start}～{res['last_date']}，{res['n_rows']:,} 列；"
        f"欄位 {', '.join(res['columns'])}。",
        "- 口徑：位階與宇宙過濾＝`build_price_panel`（4 位數字且非 00 開頭）；"
        "r{h}＝除權息還原總報酬（`total_return_targets`：entry＝次一交易列收盤、"
        "exit＝其後第 h 列、現金與股票股利一併還原；"
        f"窗內無效事件或除權息沒抓過 → null），h∈{list(pcfg.horizons)}，主窗 r{h}。同 M-Pick3a。",
        f"- 舊面板 `{pcfg.old_panel_path}` 只用來量化新舊差異，**不當參照**"
        f"（2026-06 起列稀疏，docs/33 §6.6）；比較僅限日期 ≤ {pcfg.old_panel_end} 的今日成員列。",
        f"- 舊面板缺席的成員（整檔不在舊面板）{res['n_old_absent']} 檔，其中新面板有價者 "
        f"{res['n_old_absent_priced']} 檔。",
        "",
        "## 1. 舊面板的問題量化（新舊同鍵比較；描述性）",
        "",
        "### 1.1 列吻合率（新面板每列在舊面板是否有同鍵列）",
        "",
        *_match_lines(res["match"], recent),
        "",
        f"### 1.2 r{h} 窗實際跨了幾個市場交易日（entry＝該股次一列、exit＝其後第 {h} 列）",
        "",
        f"完整（無漏列）＝恰跨 {h} 日；漏列（該股無成交日，或舊面板快取稀疏）→ 多於 {h} 日。",
        "",
        *_span_lines(res["span_new"], res["span_old"], h, recent),
        "",
        f"### 1.3 r{h} 新−舊（百分點；只看兩邊皆有的窗）",
        "",
        "**依年**：",
        "",
        *_diff_lines(res["diff_year"]),
        "",
        f"**依月（{res['diff_month_from']} 起）**：",
        "",
        *_diff_lines(res["diff_month"]),
        "",
        "## 2. 驗收（事前寫定）",
        "",
        f"- **A1 窗跨度不退化**：新面板 r{h} 窗「恰跨 {h} 日」比例，"
        f"{pcfg.span_check_since} 所在月起各月 ≥ 門檻 {_pct(floor, 2)}"
        f"（＝{pcfg.span_baseline_year} 年各月最低比例 − {pcfg.span_floor_margin_pp:g}pp）："
        f"**{a1_txt}**。",
        f"- **A2 前收自洽（{pcfg.start} 起的除權息事件）**：FinMind「除息前收盤價」與價格資料"
        f"前一交易列收盤差 ≤ {pcfg.before_price_tol_pct:g}%：{a2['n_ok']:,}/{a2['n_checked']:,}"
        f"（{_pct(a2['rate'], 2)}）；通過線 {_pct(pcfg.before_price_min_rate, 0)}："
        f"**{'PASS' if a2['ok'] else 'FAIL'}**。",
        "",
        "## 3. M-Pick2 主宇宙（`in_main`）的 target 差（只比 target，不算任何因子×target）",
        "",
    ]
    if res["mp2_note"]:
        lines += [res["mp2_note"], ""]
    else:
        lines += [
            f"M-Pick2 stockweeks（{res['mp2_file']}）的 `in_main` 股週：其 r{h}"
            f"（舊面板；已依 fwd_disc 作廢）對新面板 r{h}。"
            "「僅新有」含 M-Pick2 因價格不連續（>15%）"
            "作廢、但新面板以還原比值處理的窗。",
            "",
            *_diff_lines(res["mp2_diff"]),
            "",
        ]
    lines += ["## 4. 抽查窗（可手算拆解）", ""]
    if res["spots"]:
        lines += [
            "| 股票 | 日期 | entry（日／收） | exit（日／收） | 純價差% | "
            f"窗內除權息（對齊日／還原比值） | 總報酬% | 新 r{h} | 舊 r{h} |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for sp in res["spots"]:
            evs = "；".join(f"{e['aligned']}／{e['adj_factor']:.5f}" for e in sp["events"]) or "無"
            lines.append(
                f"| {sp['stock_id']} | {sp['date']} | {sp['entry_date']}／{sp['entry_close']:g} | "
                f"{sp['exit_date']}／{sp['exit_close']:g} | {sp['price_ret_pct']:+.2f} | {evs} | "
                f"{sp['total_ret_pct']:+.2f} | {_pp(sp['new'])} | {_pp(sp['old'])} |"
            )
    else:
        lines.append("（未設抽查窗或抽查窗無法計算）")
    lines += [
        "",
        "## 5. 已知限制",
        "",
        "- 只含今日次產業成員（FinMind 抓取範圍）；全市場基準（mkt_ew／alpha）、法人、融資、"
        "TDCC 等欄不在內；全市場面板本身的稀疏與除息問題不在本 milestone 範圍"
        "（需補快取或全市場 FinMind）。",
        "- 成員為今日 concepts.yaml（非 point-in-time）；期間內下市者缺席。",
        f"- 無成交日不成列（比照 TWSE 慣例）：流動性差的股票 r{h} 窗仍會跨多於 {h} 個市場交易日"
        "（§1.2 的「多於」部分是自然基線，不是缺資料）。",
        f"- 重建後的 r{h} 與 M-Pick2 用的舊面板 r{h} 是**不同 target**：任何引用 M-Pick2 數字"
        "（docs/32）的地方都仍是舊 target 的讀數；是否用新 target 重驗"
        "（屬同一面板上的第二次檢視、不是樣本外）留給使用者裁決。",
        "",
    ]
    return lines


def run_panel_tr(settings: Path, out_dir: Path | None) -> None:
    """D6：重建成員稠密總報酬面板＋新舊差異報告＋驗收（docs/33 §7）。"""
    import yaml

    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.data.finmind import load_dividend_result_history, load_stock_price_history

    with open(settings, encoding="utf-8") as f:
        cfg_all = yaml.safe_load(f)
    pcfg = pt.PanelTrConfig.from_settings(cfg_all)
    out = out_dir or Path(pcfg.output_dir)
    h = pcfg.main_horizon

    finmind_dir = Path(cfg_all["paths"]["cache_dir"]) / "finmind"
    members = list_subindustries(concepts_path=settings.parent / "concepts.yaml")
    if members.is_empty():
        console.print("[red]缺 concepts.yaml 次產業成員[/red]")
        raise typer.Exit(1)
    member_ids = set(members["stock_id"].cast(pl.Utf8).to_list())
    price_raw = load_stock_price_history(finmind_dir)
    if price_raw.is_empty():
        console.print("[red]無 FinMind 日線快取——先跑 make backfill-finmind-price[/red]")
        raise typer.Exit(1)
    old_path = Path(pcfg.old_panel_path)
    if not old_path.exists():
        console.print(f"[red]無舊面板 {old_path}——先跑 make build-panel（僅作差異量化用）[/red]")
        raise typer.Exit(1)

    price, _ = _member_price(price_raw, member_ids)
    divs = load_dividend_result_history(finmind_dir)
    covered = {f.stem.removeprefix("dividend_") for f in finmind_dir.glob("dividend_*.parquet")}
    events = oos.adjustment_events(divs)

    console.print("[bold]重建成員稠密總報酬面板...[/bold]")
    panel = pt.build_member_tr_panel(price, events, pcfg.horizons, covered, pcfg.start)
    out.mkdir(parents=True, exist_ok=True)
    out_path = out / pcfg.output_name
    panel.write_parquet(out_path)

    console.print("[bold]新舊差異與驗收...[/bold]")
    rcols = [f"r{x}" for x in pcfg.horizons]
    old = pl.read_parquet(old_path, columns=["date", "stock_id", *rcols]).filter(
        pl.col("stock_id").is_in(sorted(member_ids)) & (pl.col("date") <= pcfg.old_panel_end)
    )
    new_cmp = panel.filter(pl.col("date") <= pcfg.old_panel_end)
    cal = pt.market_calendar(price, pcfg.calendar_min_names)
    span_new = pt.span_share_by_month(pt.window_spans(panel.select("date", "stock_id"), cal, h), h)
    span_old = pt.span_share_by_month(pt.window_spans(old.select("date", "stock_id"), cal, h), h)
    floor = pt.span_floor(span_new, pcfg.span_baseline_year, pcfg.span_floor_margin_pp)
    a1, a1_bad = pt.span_check(span_new, floor, pcfg.span_check_since)
    chk = oos.before_price_consistency(
        divs.filter(pl.col("ex_date") >= pcfg.start), price, pcfg.before_price_tol_pct
    )
    rate = chk["n_ok"] / chk["n_checked"] if chk["n_checked"] else None
    a2 = {**chk, "rate": rate, "ok": rate is not None and rate >= pcfg.before_price_min_rate}

    diff_from = "2025-07"
    diff_month = pt.target_diff_by_period(new_cmp, old, h).filter(pl.col("period") >= diff_from)
    diff_year = pt.target_diff_by_period(
        new_cmp.filter(pl.col("date") < date(2025, 7, 1)), old, h,
        period=pl.col("date").dt.year().cast(pl.Utf8),
    )

    mp2_note, mp2_diff, mp2_file = "", pl.DataFrame(), ""
    files = [Path(x) for x in sorted(glob.glob(pcfg.mpick2_stockweeks_glob))]
    if files:
        mp2_file = files[-1].name
        mp2 = pl.read_parquet(files[-1], columns=["date", "stock_id", "in_main", f"r{h}"]).filter(
            pl.col("in_main")
        )
        cut = pl.col("date") >= date(2026, 5, 1)
        label = (
            pl.when(cut)
            .then(pl.lit("快照日 ≥ 2026-05-01"))
            .otherwise(pl.lit("快照日 < 2026-05-01"))
        )
        # 只比 M-Pick2 主宇宙的同一批 (date, stock_id)：新面板其餘成員日不在比較範圍，
        # 否則「僅新有」會被全部成員日灌水
        keys = mp2.select("date", "stock_id")
        mp2_diff = pt.target_diff_by_period(
            new_cmp.join(keys, on=["date", "stock_id"], how="semi"),
            mp2.select("date", "stock_id", f"r{h}"),
            h,
            period=label,
        )
    else:
        mp2_note = f"（找不到 `{pcfg.mpick2_stockweeks_glob}`，略過。）"

    spots: list[dict[str, Any]] = []
    for sid, d in pcfg.spot_checks:
        sp = pt.spot_check(price, events, sid, d, h)
        if sp is None:
            continue
        key = (pl.col("stock_id") == sid) & (pl.col("date") == d)
        n_v = panel.filter(key)[f"r{h}"]
        o_v = old.filter(key)[f"r{h}"]
        spots.append(
            {**sp, "new": n_v[0] if n_v.len() else None, "old": o_v[0] if o_v.len() else None}
        )

    old_ids = set(old["stock_id"].unique().to_list())
    absent = sorted(member_ids - old_ids)
    res: dict[str, Any] = {
        "today": date.today(), "out_path": str(out_path), "sha256": _sha256(out_path),
        "n_members": len(member_ids), "n_stocks": panel["stock_id"].n_unique(),
        "last_date": panel["date"].max(), "n_rows": panel.height, "columns": panel.columns,
        "n_old_absent": len(absent),
        "n_old_absent_priced": panel.filter(pl.col("stock_id").is_in(absent))[
            "stock_id"
        ].n_unique(),
        "match": pt.row_match_by_month(new_cmp, old), "span_new": span_new, "span_old": span_old,
        "floor": floor, "a1": a1, "a1_bad": a1_bad, "a2": a2,
        "diff_year": diff_year, "diff_month": diff_month, "diff_month_from": diff_from,
        "mp2_note": mp2_note, "mp2_diff": mp2_diff, "mp2_file": mp2_file, "spots": spots,
    }
    tag = date.today().strftime("%Y%m%d")
    report = out / f"panel_tr_rebuild_{tag}.md"
    report.write_text("\n".join(render_report(res, pcfg)), encoding="utf-8")

    console.print(f"[green]面板：{out_path}（{panel.height:,} 列、{res['n_stocks']:,} 檔）[/green]")
    console.print(f"[green]報告：{report}[/green]")
    console.print(
        f"  A1 窗跨度不退化：{a1}（門檻 {floor}）；A2 前收自洽：{a2['n_ok']}/{a2['n_checked']}"
        f"（{'PASS' if a2['ok'] else 'FAIL'}）"
    )
