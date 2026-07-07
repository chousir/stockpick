"""picks record／sync 編排（規劃書 05 F1-PO1；自 cli.py 薄殼呼叫）。

把 pick／剔除紀錄寫進 reports/<week>/picks.csv 或 excluded.csv：
- record：逐檔手記（單檔補記 fallback）
- sync：解析 pick.md 尾端機器可讀區塊（docs/11「第三層」），全列驗證通過才整批 upsert

data_date 自動取該週 screen_result 的 screened_at；name／ext_ma60_pct 自動從
candidates_enriched.csv 補、查無再兜 watchlist／holdings_enriched.csv（觀察清單股
未命中策略時不在 candidates，兜底讓 F2 位階紀律仍可查核；皆可覆寫）。純本地檔案、不打網。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    import polars as pl

console = Console()

PICKS_BLOCK_BEGIN = "<!-- picks:begin -->"
PICKS_BLOCK_END = "<!-- picks:end -->"

_PICK_KEYS = {"stock", "layer", "sub", "entry", "stop", "thesis", "name", "ext_ma60"}
_EXCLUDED_KEYS = {"stock", "reason", "detail", "name"}


def _load_week_dir(settings: Path, week: str) -> tuple[Path, dict[str, Any]]:
    """讀 settings 並驗週次目錄存在（不存在 → exit 1），回 (week_dir, cfg)。"""
    import yaml

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    week_dir = Path(cfg["paths"]["reports_dir"]) / week
    if not week_dir.is_dir():
        console.print(f"[red]{week_dir} 不存在——week 應為 reports/ 下的週次目錄名[/red]")
        raise typer.Exit(1)
    return week_dir, cfg


def _resolve_data_date(week_dir: Path) -> date | None:
    """該週 screen_result_*.csv 的 screened_at（全找不到 → None）。"""
    import polars as pl

    for csv in sorted(week_dir.glob("screen_result_*.csv")):
        try:
            col = pl.read_csv(csv, columns=["screened_at"])["screened_at"]
        except Exception:  # noqa: BLE001 — 換下一個檔找 screened_at
            continue
        if col.len():
            return date.fromisoformat(str(col[0]))
    return None


# name/ext 查找順序：candidates 為主（挑股主宇宙），watchlist/holdings 兜底——
# 觀察清單股與策略命中同權入 picks，未命中策略時只存在後兩檔（欄位同口徑）
_ENRICHED_FILENAMES = (
    "candidates_enriched.csv",
    "watchlist_enriched.csv",
    "holdings_enriched.csv",
)


def _load_enriched(week_dir: Path) -> pl.DataFrame | None:
    """讀 enriched CSV 供 name/ext 自動補（全缺或全壞 → None）；同股以先讀到者為準。"""
    import polars as pl

    frames = []
    for fname in _ENRICHED_FILENAMES:
        path = week_dir / fname
        if not path.exists():
            continue
        try:
            df = pl.read_csv(path, schema_overrides={"stock_id": pl.Utf8})
        except Exception as e:  # noqa: BLE001 — 單檔壞掉不擋記錄，換下一個來源
            console.print(f"[yellow]讀 {path} 失敗（{e}），改查其他 enriched／需手動給[/yellow]")
            continue
        if "stock_id" not in df.columns:
            continue
        frames.append(
            df.select(
                pl.col("stock_id"),
                (
                    pl.col("name").cast(pl.Utf8, strict=False)
                    if "name" in df.columns
                    else pl.lit(None, dtype=pl.Utf8).alias("name")
                ),
                (
                    pl.col("ma60_dist_pct").cast(pl.Float64, strict=False)
                    if "ma60_dist_pct" in df.columns
                    else pl.lit(None, dtype=pl.Float64).alias("ma60_dist_pct")
                ),
            )
        )
    if not frames:
        return None
    return pl.concat(frames).unique(subset=["stock_id"], keep="first", maintain_order=True)


def _lookup_enriched(
    enriched: pl.DataFrame | None, stock_id: str
) -> tuple[str | None, float | None]:
    """從 enriched 撈 (name, ma60_dist_pct)；查無 → (None, None)。"""
    import polars as pl

    if enriched is None:
        return None, None
    hit = enriched.filter(pl.col("stock_id") == stock_id)
    if hit.is_empty():
        return None, None
    row = hit.row(0, named=True)
    ext = float(row["ma60_dist_pct"]) if row.get("ma60_dist_pct") is not None else None
    return row.get("name"), ext


def _opt_str(value: Any) -> str | None:
    """YAML 純量 → 字串（None 保持 None）；防未加引號的數字價位寫進 Utf8 欄位炸掉。"""
    return None if value is None else str(value)


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
    from tw_screener.report.pick_store import (
        core_extension_violation,
        upsert_excluded,
        upsert_pick,
    )

    week_dir, cfg = _load_week_dir(settings, week)

    resolved_date: date | None = date.fromisoformat(data_date) if data_date else None
    if resolved_date is None:
        resolved_date = _resolve_data_date(week_dir)
    if resolved_date is None:
        console.print("[red]找不到資料日：該週無 screen_result_*.csv，請給 --data-date[/red]")
        raise typer.Exit(1)

    resolved_name, resolved_ext = name, ext_ma60
    if resolved_name is None or resolved_ext is None:
        found_name, found_ext = _lookup_enriched(_load_enriched(week_dir), stock_id)
        if resolved_name is None:
            resolved_name = found_name
        if resolved_ext is None:
            resolved_ext = found_ext

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
            # F2 位階紀律：核心層距季線乖離硬上限（settings.picks.core_ext_ma60_max_pct）
            max_ext = cfg.get("picks", {}).get("core_ext_ma60_max_pct")
            violation = core_extension_violation(
                layer, resolved_ext, float(max_ext) if max_ext is not None else None
            )
            if violation:
                console.print(f"[red]❌ {stock_id} {resolved_name or ''}：{violation}[/red]")
                raise typer.Exit(1)
            if layer == "core" and resolved_ext is None and max_ext is not None:
                console.print(
                    "[yellow]⚠️ 距季線乖離未知（candidates/watchlist/holdings_enriched "
                    "皆無此檔且未給 --ext-ma60）——F2 位階紀律無法查核，如實記錄[/yellow]"
                )
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


def _parse_picks_block(text: str, path: Path) -> dict[str, Any]:
    """抽出 pick.md 的機器可讀區塊並解析 YAML；語法錯誤報「檔案第 N 行」。"""
    import yaml

    lines = text.splitlines()
    begins = [i for i, ln in enumerate(lines) if ln.strip() == PICKS_BLOCK_BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == PICKS_BLOCK_END]
    if not begins or not ends:
        console.print(
            f"[red]{path} 找不到 {PICKS_BLOCK_BEGIN} … {PICKS_BLOCK_END} 區塊"
            f"（pick.md 第三層，格式見 docs/11）[/red]"
        )
        raise typer.Exit(1)
    if len(begins) > 1 or len(ends) > 1 or ends[0] <= begins[0]:
        console.print(
            f"[red]{path} 的 picks 區塊標記重複或順序錯誤"
            f"（begin 第 {begins[0] + 1} 行／end 第 {ends[0] + 1} 行）[/red]"
        )
        raise typer.Exit(1)
    body = lines[begins[0] + 1 : ends[0]]
    offset = begins[0] + 1  # body[0] 在檔案中的 0-based 行號
    while body and not body[0].strip():
        body.pop(0)
        offset += 1
    if body and body[0].lstrip().startswith("```"):  # 可選的 ```yaml 圍欄
        body.pop(0)
        offset += 1
        for j in range(len(body) - 1, -1, -1):
            if body[j].lstrip().startswith("```"):
                del body[j:]
                break
    try:
        data = yaml.safe_load("\n".join(body))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        at = f"{path} 第 {offset + mark.line + 1} 行" if mark is not None else str(path)
        console.print(f"[red]picks 區塊 YAML 解析失敗（{at}）：{e}[/red]")
        raise typer.Exit(1) from e
    if not isinstance(data, dict):
        console.print(
            f"[red]{path} picks 區塊必須是 YAML 映射（picks:／excluded:），"
            f"收到 {type(data).__name__}[/red]"
        )
        raise typer.Exit(1)
    return data


def run_picks_sync(settings: Path, week: str, file: Path | None = None) -> None:
    """解析該週 pick.md 尾端機器可讀區塊，整批寫入底帳（全列驗證過才寫、冪等）。"""
    from tw_screener.report.pick_store import (
        LAYERS,
        core_extension_violation,
        upsert_excluded,
        upsert_pick,
    )

    week_dir, cfg = _load_week_dir(settings, week)
    path = file if file is not None else week_dir / "pick.md"
    if not path.is_file():
        console.print(f"[red]{path} 不存在——先依 docs/11 產出 pick.md（含第三層區塊）[/red]")
        raise typer.Exit(1)

    data = _parse_picks_block(path.read_text(encoding="utf-8"), path)

    unknown_top = set(data) - {"picks", "excluded", "data_date"}
    if unknown_top:
        console.print(
            f"[red]picks 區塊頂層未知欄位 {sorted(unknown_top)}"
            f"（可用 picks／excluded／data_date）[/red]"
        )
        raise typer.Exit(1)
    picks_rows = data.get("picks") or []
    excluded_rows = data.get("excluded") or []
    if not isinstance(picks_rows, list) or not isinstance(excluded_rows, list):
        console.print("[red]picks／excluded 必須是清單[/red]")
        raise typer.Exit(1)
    if not picks_rows and not excluded_rows:
        console.print("[red]picks 區塊為空——picks 與 excluded 至少要有一筆[/red]")
        raise typer.Exit(1)

    raw_date = data.get("data_date")
    try:
        resolved_date = (
            raw_date
            if isinstance(raw_date, date)
            else date.fromisoformat(str(raw_date))
            if raw_date is not None
            else _resolve_data_date(week_dir)
        )
    except ValueError:
        console.print(f"[red]data_date 格式錯誤：{raw_date!r}（應為 YYYY-MM-DD）[/red]")
        raise typer.Exit(1) from None
    if resolved_date is None:
        console.print(
            "[red]找不到資料日：該週無 screen_result_*.csv，"
            "請在區塊頂層給 data_date（YYYY-MM-DD）[/red]"
        )
        raise typer.Exit(1)

    enriched = _load_enriched(week_dir)
    max_ext = cfg.get("picks", {}).get("core_ext_ma60_max_pct")

    errors: list[str] = []
    warnings: list[str] = []
    prepared_picks: list[dict[str, Any]] = []
    prepared_excluded: list[dict[str, Any]] = []
    seen: dict[str, str] = {}  # stock_id → 首見位置（區塊內重複＝手誤，報錯）

    for i, raw in enumerate(picks_rows, start=1):
        where = f"picks 第 {i} 筆"
        if not isinstance(raw, dict):
            errors.append(f"{where}：應為欄位映射（stock/layer/…），收到 {raw!r}")
            continue
        unknown = set(raw) - _PICK_KEYS
        if unknown:
            errors.append(f"{where}：未知欄位 {sorted(unknown)}（可用 {sorted(_PICK_KEYS)}）")
            continue
        stock_id = str(raw.get("stock") or "").strip()
        if not stock_id:
            errors.append(f"{where}：缺 stock（股號請加引號，防前導零丟失）")
            continue
        where = f"{where}（{stock_id}）"
        if stock_id in seen:
            errors.append(f"{where}：與 {seen[stock_id]} 重複")
            continue
        seen[stock_id] = where
        layer = raw.get("layer")
        if layer not in LAYERS:
            errors.append(f"{where}：layer 必須是 {LAYERS}，收到 {layer!r}")
            continue
        found_name, found_ext = _lookup_enriched(enriched, stock_id)
        row_name = _opt_str(raw.get("name")) or found_name
        try:
            row_ext = float(raw["ext_ma60"]) if raw.get("ext_ma60") is not None else found_ext
        except (TypeError, ValueError):
            errors.append(f"{where}：ext_ma60 必須是數字，收到 {raw.get('ext_ma60')!r}")
            continue
        # F2 位階紀律：核心層距季線乖離硬上限（settings.picks.core_ext_ma60_max_pct）
        violation = core_extension_violation(
            layer, row_ext, float(max_ext) if max_ext is not None else None
        )
        if violation:
            errors.append(f"{where}：{violation}")
            continue
        if layer == "core" and row_ext is None and max_ext is not None:
            warnings.append(
                f"⚠️ {stock_id} {row_name or ''}：距季線乖離未知（candidates/watchlist/"
                f"holdings_enriched 皆無此檔且未給 ext_ma60）——F2 位階紀律無法查核，如實記錄"
            )
        prepared_picks.append(
            {
                "week": week,
                "data_date": resolved_date,
                "stock_id": stock_id,
                "name": row_name,
                "layer": layer,
                "sub_industry": _opt_str(raw.get("sub")),
                "entry_zone": _opt_str(raw.get("entry")),
                "stop": _opt_str(raw.get("stop")),
                "ext_ma60_pct": row_ext,
                "thesis_tag": _opt_str(raw.get("thesis")),
            }
        )

    for i, raw in enumerate(excluded_rows, start=1):
        where = f"excluded 第 {i} 筆"
        if not isinstance(raw, dict):
            errors.append(f"{where}：應為欄位映射（stock/reason/…），收到 {raw!r}")
            continue
        unknown = set(raw) - _EXCLUDED_KEYS
        if unknown:
            errors.append(f"{where}：未知欄位 {sorted(unknown)}（可用 {sorted(_EXCLUDED_KEYS)}）")
            continue
        stock_id = str(raw.get("stock") or "").strip()
        if not stock_id:
            errors.append(f"{where}：缺 stock（股號請加引號，防前導零丟失）")
            continue
        where = f"{where}（{stock_id}）"
        if stock_id in seen:
            errors.append(f"{where}：與 {seen[stock_id]} 重複")
            continue
        seen[stock_id] = where
        reason = _opt_str(raw.get("reason"))
        if not (reason or "").strip():
            errors.append(f"{where}：缺 reason（剔除旗標類別，如 過熱／土洋對作）")
            continue
        found_name, _ = _lookup_enriched(enriched, stock_id)
        prepared_excluded.append(
            {
                "week": week,
                "data_date": resolved_date,
                "stock_id": stock_id,
                "name": _opt_str(raw.get("name")) or found_name,
                "reason": reason,
                "detail": _opt_str(raw.get("detail")),
            }
        )

    if errors:
        for msg in errors:
            console.print(f"[red]{msg}[/red]")
        console.print(f"[red]共 {len(errors)} 個錯誤——全列驗證過才寫，本次未寫入任何列[/red]")
        raise typer.Exit(1)

    for msg in warnings:
        console.print(f"[yellow]{msg}[/yellow]")
    for row in prepared_picks:
        upsert_pick(week_dir, row)
        ext_txt = f"{row['ext_ma60_pct']:+.1f}%" if row["ext_ma60_pct"] is not None else "—"
        console.print(
            f"[green]picks.csv ← {week} {row['stock_id']} {row['name'] or ''} "
            f"[{row['layer']}] 距季線 {ext_txt}[/green]"
        )
    for row in prepared_excluded:
        upsert_excluded(week_dir, row)
        console.print(
            f"[green]excluded.csv ← {week} {row['stock_id']} {row['name'] or ''}"
            f"（{row['reason']}）[/green]"
        )
    n = {layer: sum(1 for r in prepared_picks if r["layer"] == layer) for layer in LAYERS}
    console.print(
        f"[bold green]sync 完成：picks {len(prepared_picks)} 檔"
        f"（core {n['core']}／opportunity {n['opportunity']}／pool {n['pool']}）"
        f"＋excluded {len(prepared_excluded)} 檔 → {week_dir}[/bold green]"
    )
