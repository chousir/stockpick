"""docs/31 §22.5/§22.7 編排——panel-only候選排列組合研究維度1（族群輪動）
＋維度1×維度2組合（族群輪動×法人流向）。

自 cli.py 薄殼呼叫。stock_rows組建方式跟`official_sector_grid_runner.py`／
`flow_trigger_grid_runner.py`（group_source="hand"）完全相同——重用同一組
membership/basket建構函式，唯一差異是cell定義（本節用§22.3.2統一的「前20%」
動態門檻，非official_sector_top5的固定top5）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import typer
import yaml
from rich.console import Console

console = Console()

_DIMENSIONS = ("rotation", "rotation_flow_combo")


def run_redesign_dimension_grid(settings: Path, out_dir: Path | None, dimension: str) -> None:
    """docs/31 §22.5/§22.7：panel-only候選排列組合研究，維度1（族群輪動）與
    維度1×維度2組合（族群輪動×法人流向）。

    Args:
        dimension: "rotation"（維度1，§22.5）｜"rotation_flow_combo"（維度1×維度2
            組合，§22.7）。維度3-5依§22.3.4排序，各自開工前才寫pre-registration＋
            加對應dimension值，本輪不預先搭好尚未存在的分支。
    """
    if dimension not in _DIMENSIONS:
        console.print(
            f"[red]--dimension 目前只支援 {_DIMENSIONS}，收到 {dimension!r}——"
            "維度3-5尚未pre-registration，見docs/31 §22.3.4[/red]"
        )
        raise typer.Exit(1)

    from tw_screener.analysis.sector_universe import list_subindustries, load_industry_mapping
    from tw_screener.backtest import official_sector_grid as osg
    from tw_screener.backtest.rotation_efficacy import trend_score_series, weekly_snapshot_dates
    from tw_screener.data.twse import create_client

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    rc = cfg.get("backtest", {}).get("redesign_dimension_grid", {})
    fc = cfg.get("backtest", {}).get("flow_trigger_grid", {})
    horizons = tuple(int(h) for h in rc.get("horizons_td", [10, 20, 40]))
    top_quantile = float(rc.get("top_quantile", 0.2))
    min_purity = float(rc.get("hand_min_purity", 0.5))
    snapshot_gap_td = int(rc.get("snapshot_gap_td", 5))
    n_boot = int(rc.get("n_boot", 1000))
    n_splits = int(rc.get("n_splits", 4))
    min_train_frac = float(rc.get("min_train_frac", 0.4))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(rc.get("output_dir", "research/redesign_dimension_grid"))

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)

    client = create_client(settings)
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"
    industry = load_industry_mapping(cache_dir)
    if industry.is_empty():
        console.print("[red]缺官方產業分類（industry_*.parquet/otc_industry_*.parquet）[/red]")
        raise typer.Exit(1)
    sector_index = client.load_sector_index_history()
    if sector_index.is_empty():
        console.print(
            "[red]無官方族群指數快取——先跑 "
            "`tw-screener data backfill-sector-index --start ... --end ...`[/red]"
        )
        raise typer.Exit(1)

    hand = list_subindustries()
    purity = osg.compute_subindustry_purity(hand, industry)
    membership = osg.build_hand_sector_membership(hand, purity, min_purity=min_purity)
    baskets = osg.build_hand_sector_baskets(sector_index, purity, min_purity=min_purity)
    if baskets.is_empty() or membership.is_empty():
        console.print("[red]映射後族群/籃子為空，無法計算[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建手標次產業 trend_score（逐週）...[/bold]")
    price = panel.select("date", "stock_id", "close")
    trend = trend_score_series(price, membership, baskets)
    weekly = set(weekly_snapshot_dates(panel["date"].unique().to_list()))
    trend_weekly = trend.filter(pl.col("date").is_in(list(weekly)))
    has_regime = "regime" in panel.columns
    stock_rows = membership.join(trend_weekly, on="sub_industry", how="inner").join(
        panel.select(
            "date", "stock_id",
            *[f"alpha{h}" for h in horizons],
            *(["regime"] if has_regime else []),
        ),
        on=["date", "stock_id"],
        how="left",
    )

    out.mkdir(parents=True, exist_ok=True)
    common: dict[str, Any] = dict(
        horizons=horizons, snapshot_gap_td=snapshot_gap_td, n_boot=n_boot, n_splits=n_splits,
        min_train_frac=min_train_frac, has_regime=has_regime, out=out, min_purity=min_purity,
    )
    if dimension == "rotation":
        _run_rotation(stock_rows, top_quantile=top_quantile, **common)
    else:
        institutional_daily = panel.select(
            "date", "stock_id", "foreign_net", "trust_net", "dealer_net"
        )
        console.print("[bold]重建★投信流校準歷史觸發序列（沿用production校準值）...[/bold]")
        triggers = osg.build_flow_triggers(
            institutional_daily, membership,
            short_window=int(fc.get("short_window", 5)),
            long_window=int(fc.get("long_window", 20)),
            z_window=int(fc.get("z_window", 60)),
            z_min_periods=int(fc.get("z_min_periods", 30)),
            signal_col=str(fc.get("signal_col", "trust_flow_20d")),
            threshold=float(fc.get("threshold", 1.0)),
            require_momentum=bool(fc.get("require_momentum", True)),
        )
        daily_dates = sorted(panel["date"].unique().to_list())
        lookback_window = int(fc.get("lookback_window", 15))
        _run_rotation_flow_combo(
            stock_rows, triggers, daily_dates, top_quantile=top_quantile,
            lookback_window=lookback_window, **common,
        )


def _c(v: object, suf: str = "", nd: int = 2) -> str:
    return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"


def _decision_lines(
    cells: pl.DataFrame,
    target_cell: str,
    wf: pl.DataFrame,
    horizons: tuple[int, ...],
    n_splits: int,
    min_train_frac: float,
    n_boot: int,
    snapshot_gap_td: int,
    has_regime: bool,
    min_purity: float,
) -> tuple[list[str], dict[int, str]]:
    """docs/31 §22.5/§22.7共用的四步裁決（CI95→regime→前後半段→walk-forward保留
    驗證窗複核），對`target_cell`逐horizon跑。回傳(markdown行, {horizon: 裁決字串})。
    """
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest import redesign_dimension_grid as rdg

    lines = [f"## {target_cell}格 regime切片＋裁決（四步門檻）", ""]
    if not has_regime or "regime" not in cells.columns or cells["regime"].drop_nulls().is_empty():
        lines += [
            "> regime 標籤未產（面板 regime 欄缺席或全 null）——本段誠實跳過；"
            "先跑 make regime-history 再 make build-panel。",
            "",
        ]
        console.print("[yellow]regime 標籤未產，regime 切片段跳過[/yellow]")
        return lines, {}

    all_dates = sorted(cells["date"].unique().to_list())
    verdicts: dict[int, str] = {}
    for h in horizons:
        emb = h + 1
        splits = lab.walk_forward_splits(
            all_dates, n_splits=n_splits, min_train_frac=min_train_frac, embargo_td=emb
        )
        if not splits:
            lines.append(f"- r+{h}：可用週數不足以切出walk-forward段，裁決跳過。")
            continue
        confirm_split = splits[-1]
        search_cells = cells.filter(pl.col("date") < confirm_split.test_start)

        search_grid = rdg.evaluate_signal_cells(
            search_cells, (target_cell,), horizons=(h,), n_boot=n_boot, seed=42,
            snapshot_gap_td=snapshot_gap_td,
        )
        if search_grid.is_empty():
            lines.append(f"- r+{h}：搜尋階段{target_cell}格無資料——裁決跳過（樣本不足）。")
            verdicts[h] = "資料不足"
            continue
        hit = search_grid.row(0, named=True)
        ci_lo, ci_hi = hit["ci_lo"], hit["ci_hi"]

        search_by_regime = rdg.evaluate_signal_cells_by_regime(
            search_cells, (target_cell,), horizons=(h,), n_boot=n_boot,
            snapshot_gap_td=snapshot_gap_td,
        ).filter(pl.col("cell") == target_cell)

        def _regime_n(reg: str, df: pl.DataFrame = search_by_regime) -> int:
            return int(df.filter(pl.col("regime") == reg)["n"].sum())

        lines += [
            f"### r+{h}",
            "",
            "| regime | n | n_dates | mean | bs_CI95 | 樣本 |",
            "|---|---|---|---|---|---|",
        ]
        for r in search_by_regime.iter_rows(named=True):
            rci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            flag = "樣本不足" if r["thin"] else "可判"
            lines.append(
                f"| {r['regime']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                f"| {rci} | {flag} |"
            )
        lines.append("")

        h1, h2 = hit["mean_h1"], hit["mean_h2"]
        fh_same = (
            isinstance(h1, (int, float)) and isinstance(h2, (int, float))
            and (h1 >= 0) == (h2 >= 0)
        )
        fh_desc = "同向" if fh_same else "不同向或缺資料"

        same: list[str] = []
        if ci_lo is None:
            verdict = "資料不足"
        elif ci_hi is not None and ci_hi < 0:
            verdict = "已否證"
        elif ci_lo is not None and ci_lo > 0:
            regime_verdict, same, _present = lab.regime_alignment_verdict(
                search_by_regime, 1, value_col="mean"
            )
            if regime_verdict == "跨 regime 穩健" and fh_same:
                confirm_row = wf.filter(
                    (pl.col("horizon") == h) & (pl.col("cell") == target_cell)
                    & (pl.col("split_id") == confirm_split.split_id)
                )
                confirm_delta = (
                    confirm_row.row(0, named=True)["test_delta_mean"]
                    if not confirm_row.is_empty() else None
                )
                if isinstance(confirm_delta, (int, float)) and confirm_delta >= 0:
                    verdict = "候選"
                else:
                    verdict = (
                        f"觀察結果（未過保留驗證窗，第{confirm_split.split_id}段"
                        f"delta_mean={_c(confirm_delta, '%')}）"
                    )
            else:
                verdict = f"未過關（regime裁決={regime_verdict}、前後半段{fh_desc}）"
        else:
            verdict = "未過關（CI跨0）"

        verdicts[h] = verdict
        lines.append(
            f"- **r+{h}裁決：{verdict}**（搜尋階段delta_mean {_c(hit['delta_mean'], '%')}、"
            f"CI[{_c(ci_lo)}, {_c(ci_hi)}]、跨regime同向 {len(same)}、"
            f"前後半段{fh_desc}；保留驗證窗＝第{confirm_split.split_id}段"
            f"{confirm_split.test_start}~{confirm_split.test_end}）"
        )
        console.print(f"  {target_cell} r+{h}：{verdict}")
        lines += [
            "",
            *lab.inference_footer(
                sample_span=f"{search_cells['date'].min()!s}~{search_cells['date'].max()!s}"
                f"（搜尋階段，保留驗證窗{confirm_split.test_start}起另計）",
                regime_dist="、".join(f"{reg} n={_regime_n(reg)}" for reg in lab.REGIME_LABELS),
                method_desc=(
                    f"moving-block bootstrap（block長度以快照步數換算horizon={h}td・"
                    f"B={n_boot}・seed=42）對{target_cell}格delta（cell當日均值−當日全樣本"
                    "均值）per-date序列算CI95"
                ),
                membership_desc=f"手標46細分類（purity≥{min_purity:.0%}，同§10.9/§12/§14/§17）",
            ),
            "",
        ]
    return lines, verdicts


def _full_sample_table(grid: pl.DataFrame, title: str) -> list[str]:
    lines = [title, ""]
    for h in sorted(set(grid["horizon"].to_list())):
        sub = grid.filter(pl.col("horizon") == h)
        lines += [
            f"### r+{h}",
            "",
            "| cell | n | n_dates | mean | median | win | delta_mean | CI95(delta) "
            "| 前半 | 後半 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sub.iter_rows(named=True):
            ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            nd = r["n_dates"] if r["n_dates"] is not None else "—"
            lines.append(
                f"| {r['cell']} | {r['n']} | {nd} | {_c(r['mean'], '%')} "
                f"| {_c(r['median'], '%')} | {win} | {_c(r['delta_mean'], '%')} | {ci} "
                f"| {_c(r['mean_h1'], '%')} | {_c(r['mean_h2'], '%')} |"
            )
        lines.append("")
    return lines


def _wf_table(wf: pl.DataFrame, cell_name: str, title: str) -> list[str]:
    lines = [title, ""]
    if wf.is_empty():
        return lines + ["> walk-forward資料不足（可用週數過少），該段跳過。", ""]
    for h in sorted(set(wf["horizon"].to_list())):
        subh = wf.filter((pl.col("horizon") == h) & (pl.col("cell") == cell_name))
        lines += [
            f"### r+{h}（{cell_name}格）",
            "",
            "| split | test期間 | n | n_dates | delta_mean | CI95 |",
            "|---|---|---|---|---|---|",
        ]
        for r in subh.iter_rows(named=True):
            ci = (
                f"[{_c(r['test_ci_lo'])}, {_c(r['test_ci_hi'])}]"
                if r["test_ci_lo"] is not None else "—"
            )
            lines.append(
                f"| {r['split_id']} | {r['test_start']}~{r['test_end']} | {r['test_n']} "
                f"| {r['test_n_dates']} | {_c(r['test_delta_mean'], '%')} | {ci} |"
            )
        lines.append("")
    return lines


def _run_rotation(
    stock_rows: pl.DataFrame,
    top_quantile: float,
    horizons: tuple[int, ...],
    snapshot_gap_td: int,
    n_boot: int,
    n_splits: int,
    min_train_frac: float,
    has_regime: bool,
    out: Path,
    min_purity: float,
) -> None:
    from tw_screener.backtest import redesign_dimension_grid as rdg

    grid = rdg.rotation_grid(
        stock_rows, horizons=horizons, top_quantile=top_quantile, n_boot=n_boot,
        snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)
    wf = rdg.walk_forward_rotation(
        stock_rows, horizons=horizons, top_quantile=top_quantile, n_splits=n_splits,
        min_train_frac=min_train_frac, n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )
    cells = rdg.build_rotation_cells(stock_rows, top_quantile=top_quantile)

    base_tag = date.today().strftime("%Y%m%d")
    grid.write_csv(out / f"redesign_dim1_rotation_{base_tag}.csv")
    if not wf.is_empty():
        wf.write_csv(out / f"redesign_dim1_rotation_wf_{base_tag}.csv")

    n_weeks = stock_rows["date"].n_unique()
    n_groups = stock_rows["sub_industry"].n_unique()
    lines = [
        "# docs/31 §22.5：維度1（族群輪動）——panel-only候選排列組合研究第1個維度",
        "",
        "> 累積測試數：1/14。門檻已於 docs/31 §22.5 執行前預先登記，本報告只呈現結果，"
        "裁決依登記門檻套用。**明確不是重跑official_sector_top5**——那是固定top5門檻的"
        "既有研究、已生產化；本節依§22.3.2統一規則改用「前20%」動態門檻，是獨立測試。",
        "",
        f"- 產出日：{date.today()}；手標次產業（purity≥{min_purity:.0%}）映射後 {n_groups} "
        f"個群組、{n_weeks} 週快照；top_quantile={top_quantile:.0%}。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}，供量級參考；`ci_lo`/`ci_hi`是對"
        "delta（cell當日均值−當日全樣本均值）做moving-block bootstrap CI。",
        "",
    ]
    lines += _full_sample_table(grid, "## 全樣本讀值（hit=當日trend_score排名前20%、miss=其餘）")
    lines += _wf_table(wf, "hit", "## walk-forward（第4段＝§22.5保留驗證窗，不用於搜尋）")
    dec_lines, _ = _decision_lines(
        cells, "hit", wf, horizons, n_splits, min_train_frac, n_boot, snapshot_gap_td,
        has_regime, min_purity,
    )
    lines += dec_lines

    md = out / f"redesign_dim1_rotation_{base_tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]完成，報告見 {md}[/green]")


def _run_rotation_flow_combo(
    stock_rows: pl.DataFrame,
    triggers: pl.DataFrame,
    daily_dates: list,
    top_quantile: float,
    lookback_window: float,
    horizons: tuple[int, ...],
    snapshot_gap_td: int,
    n_boot: int,
    n_splits: int,
    min_train_frac: float,
    has_regime: bool,
    out: Path,
    min_purity: float,
) -> None:
    from tw_screener.backtest import redesign_dimension_grid as rdg

    lookback = int(lookback_window)
    grid = rdg.rotation_flow_grid(
        stock_rows, triggers, daily_dates, horizons=horizons, top_quantile=top_quantile,
        lookback_window=lookback, n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )
    if grid.is_empty():
        console.print("[red]格為空——輸入資料異常[/red]")
        raise typer.Exit(1)
    wf = rdg.walk_forward_rotation_flow(
        stock_rows, triggers, daily_dates, horizons=horizons, top_quantile=top_quantile,
        lookback_window=lookback, n_splits=n_splits, min_train_frac=min_train_frac,
        n_boot=n_boot, snapshot_gap_td=snapshot_gap_td,
    )
    cells = rdg.build_rotation_flow_cells(
        stock_rows, triggers, daily_dates, top_quantile=top_quantile, lookback_window=lookback
    )
    # 維度1單獨讀值（§22.5既有數字）當比對基準，非重算新結論。
    rotation_only_grid = rdg.rotation_grid(
        stock_rows, horizons=horizons, top_quantile=top_quantile, n_boot=n_boot,
        snapshot_gap_td=snapshot_gap_td,
    )

    base_tag = date.today().strftime("%Y%m%d")
    grid.write_csv(out / f"redesign_dim1x2_rotation_flow_{base_tag}.csv")
    if not wf.is_empty():
        wf.write_csv(out / f"redesign_dim1x2_rotation_flow_wf_{base_tag}.csv")

    n_weeks = stock_rows["date"].n_unique()
    n_groups = stock_rows["sub_industry"].n_unique()
    n_trig_groups = triggers["sub_industry"].n_unique() if not triggers.is_empty() else 0
    lines = [
        "# docs/31 §22.7：維度1×維度2組合（族群輪動×法人流向）",
        "",
        "> 累積測試數：2/14（正式裁決只算`hit_triggered`格；`miss_triggered`/"
        "`miss_untriggered`純描述性，預期重現§17已否證方向，不算新測試）。門檻已於"
        "docs/31 §22.7 執行前預先登記，本報告只呈現結果，裁決依登記門檻套用。",
        "",
        f"- 產出日：{date.today()}；手標次產業（purity≥{min_purity:.0%}）映射後 {n_groups} "
        f"個群組、{n_weeks} 週快照；top_quantile={top_quantile:.0%}；★投信流觸發事件"
        f"{triggers.height}個（{n_trig_groups}個群組曾觸發，lookback_window="
        f"{lookback}個交易日）。",
        "- `mean`/`median`/`win_rate`＝原始個股alpha{h}，供量級參考；`ci_lo`/`ci_hi`是對"
        "delta（cell當日均值−當日全樣本均值）做moving-block bootstrap CI。",
        "",
    ]
    lines += _full_sample_table(
        grid, "## 全樣本讀值（4格：rotation hit/miss × flow triggered/untriggered）"
    )
    lines += _wf_table(
        wf, "hit_triggered", "## walk-forward（hit_triggered格，第4段＝保留驗證窗）"
    )
    dec_lines, verdicts = _decision_lines(
        cells, "hit_triggered", wf, horizons, n_splits, min_train_frac, n_boot,
        snapshot_gap_td, has_regime, min_purity,
    )
    lines += dec_lines

    lines += ["## 解讀：flow是否對rotation-hit有額外加值（§22.7比對）", ""]
    for h in horizons:
        hit_row = rotation_only_grid.filter(
            (pl.col("horizon") == h) & (pl.col("cell") == "hit")
        )
        combo_row = grid.filter(
            (pl.col("horizon") == h) & (pl.col("cell") == "hit_triggered")
        )
        if hit_row.is_empty() or combo_row.is_empty():
            lines.append(f"- r+{h}：資料不足，無法比對。")
            continue
        hit_delta = hit_row.row(0, named=True)["delta_mean"]
        combo_delta = combo_row.row(0, named=True)["delta_mean"]
        verdict = verdicts.get(h, "（見上方裁決）")
        lines.append(
            f"- r+{h}：維度1單獨（hit）delta_mean {_c(hit_delta, '%')} vs "
            f"hit_triggered delta_mean {_c(combo_delta, '%')}（四步裁決：{verdict}）"
        )
    lines.append("")

    md = out / f"redesign_dim1x2_rotation_flow_{base_tag}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]完成，報告見 {md}[/green]")
