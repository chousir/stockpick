"""picks record 編排（規劃書 05 F1-PO1；自 cli.py 薄殼呼叫）。

把單檔 pick／剔除紀錄寫進 reports/<week>/picks.csv 或 excluded.csv：
data_date 自動取該週 screen_result 的 screened_at；name／ext_ma60_pct 自動從
candidates_enriched.csv 補（皆可用參數覆寫）。純本地檔案、不打網。
"""

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def run_pick_record(
    settings: Path,
    week: str,
    stock_id: str,
    layer: str | None,
    sub_industry: str | None,
    entry_zone: str | None,
    stop: str | None,
    thesis_tag: str | None,
    ext_ma60: float | None,
    excluded: bool,
    reason: str | None,
    detail: str | None,
    name: str | None,
    data_date: str | None,
) -> None:
    """記錄一列 pick（core/opportunity/pool）或剔除紀錄（--excluded --reason）。"""
    from datetime import date as _date

    import polars as pl
    import yaml

    from tw_screener.report.pick_store import upsert_excluded, upsert_pick

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    week_dir = Path(cfg["paths"]["reports_dir"]) / week
    if not week_dir.is_dir():
        console.print(f"[red]{week_dir} 不存在——week 應為 reports/ 下的週次目錄名[/red]")
        raise typer.Exit(1)

    resolved_date: _date | None = _date.fromisoformat(data_date) if data_date else None
    if resolved_date is None:
        for csv in sorted(week_dir.glob("screen_result_*.csv")):
            try:
                col = pl.read_csv(csv, columns=["screened_at"])["screened_at"]
            except Exception:  # noqa: BLE001 — 換下一個檔找 screened_at
                continue
            if col.len():
                resolved_date = _date.fromisoformat(str(col[0]))
                break
    if resolved_date is None:
        console.print("[red]找不到資料日：該週無 screen_result_*.csv，請給 --data-date[/red]")
        raise typer.Exit(1)

    resolved_name, resolved_ext = name, ext_ma60
    enriched_path = week_dir / "candidates_enriched.csv"
    if (resolved_name is None or resolved_ext is None) and enriched_path.exists():
        try:
            enriched = pl.read_csv(enriched_path, schema_overrides={"stock_id": pl.Utf8})
            hit = enriched.filter(pl.col("stock_id") == stock_id)
            if not hit.is_empty():
                row = hit.row(0, named=True)
                if resolved_name is None:
                    resolved_name = row.get("name")
                if resolved_ext is None and row.get("ma60_dist_pct") is not None:
                    resolved_ext = float(row["ma60_dist_pct"])
        except Exception as e:  # noqa: BLE001 — enriched 壞掉不擋記錄，欄位留空
            console.print(f"[yellow]讀 {enriched_path} 失敗（{e}），name/ext 需手動給[/yellow]")

    try:
        if excluded:
            upsert_excluded(
                week_dir,
                {
                    "week": week,
                    "data_date": resolved_date,
                    "stock_id": stock_id,
                    "name": resolved_name,
                    "reason": reason,
                    "detail": detail,
                },
            )
            console.print(
                f"[green]excluded.csv ← {week} {stock_id} {resolved_name or ''}"
                f"（{reason}）[/green]"
            )
        else:
            if layer is None:
                console.print("[red]非 --excluded 時必須給 --layer core|opportunity|pool[/red]")
                raise typer.Exit(1)
            upsert_pick(
                week_dir,
                {
                    "week": week,
                    "data_date": resolved_date,
                    "stock_id": stock_id,
                    "name": resolved_name,
                    "layer": layer,
                    "sub_industry": sub_industry,
                    "entry_zone": entry_zone,
                    "stop": stop,
                    "ext_ma60_pct": resolved_ext,
                    "thesis_tag": thesis_tag,
                },
            )
            ext_txt = f"{resolved_ext:+.1f}%" if resolved_ext is not None else "—"
            console.print(
                f"[green]picks.csv ← {week} {stock_id} {resolved_name or ''} "
                f"[{layer}] 距季線 {ext_txt}[/green]"
            )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
