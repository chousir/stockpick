"""picks shortlist 編排（M-Pick1；自 cli.py 薄殼呼叫）。

讀 reports/<週>/candidates_enriched.csv（＋watchlist／holdings_enriched）與 sector_rotation.csv，
次產業宇宙優先取 data/snapshots/<週>/universe.csv（point-in-time），呼叫 report/shortlist.py
純函式排序，寫 reports/<週>/shortlist.csv 並印 top/alt 摘要與各 gate 計數。純本地檔案、不打網。
make week 在 snapshot-week 之後容錯呼叫（失敗不擋主流程，week-check 會點名缺檔）。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from loguru import logger
from rich.console import Console
from rich.table import Table

from tw_screener.report.shortlist import (
    SHORTLIST_FILENAME,
    TIER_ALT,
    TIER_HELD,
    TIER_TOP,
    ShortlistConfig,
    build_pool,
    build_shortlist,
    order_members_by_concepts,
)

console = Console()

_REQUIRED_INPUTS = ("candidates_enriched.csv", "sector_rotation.csv")


def _read_enriched(path: Path) -> pl.DataFrame:
    """讀 enriched CSV（全檔推斷型別；stock_id 固定字串防前導零丟失）。"""
    return pl.read_csv(path, infer_schema_length=None, schema_overrides={"stock_id": pl.Utf8})


def _read_optional(path: Path) -> pl.DataFrame | None:
    """選用輸入（watchlist／holdings enriched）：缺檔或讀壞 → None（warning，不擋排序）。"""
    if not path.is_file():
        return None
    try:
        return _read_enriched(path)
    except (OSError, pl.exceptions.PolarsError) as e:
        logger.warning("shortlist：讀 {} 失敗（{}），略過該來源", path, e)
        return None


def _load_members(cfg: dict[str, Any], settings: Path, week: str) -> tuple[pl.DataFrame, str]:
    """次產業宇宙成員 (sub_industry, stock_id)，列序＝concepts.yaml 手標順序（多標籤取第一個）。

    成員優先取週快照 data/snapshots/<週>/universe.csv（point-in-time），無快照才退回
    list_subindustries()（現行 concepts.yaml）。列序對齊用同週快照的 concepts.yaml
    （無則現行檔），沿用 group_report 以 themes_long 原序取主次產業的慣例。
    """
    from tw_screener.analysis.concepts import load_themes
    from tw_screener.analysis.sector_universe import list_subindustries

    snap_dir = Path((cfg.get("snapshots") or {}).get("dir", "data/snapshots")) / week
    concepts_path = settings.parent / "concepts.yaml"
    universe_path = snap_dir / "universe.csv"
    if universe_path.is_file():
        members = pl.read_csv(
            universe_path, schema_overrides={"stock_id": pl.Utf8, "sub_industry": pl.Utf8}
        ).select("sub_industry", "stock_id")
        origin = str(universe_path)
    else:
        members = list_subindustries(concepts_path=concepts_path)
        origin = f"{concepts_path}（無 {universe_path}，退回現行 concepts.yaml）"
        logger.warning("shortlist：無週快照 {}，次產業改用現行 {}", universe_path, concepts_path)
    snap_concepts = snap_dir / "concepts.yaml"
    themes = load_themes(snap_concepts if snap_concepts.is_file() else concepts_path)
    return order_members_by_concepts(members, themes), origin


def _resolve_week_dir(reports_dir: Path, week: str | None) -> Path | None:
    """--week 給了就用該週目錄；沒給＝reports/ 下最新週次目錄（同 picks outcome --brief 慣例）。"""
    from tw_screener.report.pick_store import week_dirs

    if week is not None:
        week_dir = reports_dir / week
        if not week_dir.is_dir():
            logger.warning("shortlist：{} 不存在", week_dir)
            console.print(f"[red]{week_dir} 不存在——week 應為 reports/ 下的週次目錄名[/red]")
            return None
        return week_dir
    dirs = week_dirs(reports_dir)
    if not dirs:
        logger.warning("shortlist：{} 下無週次目錄", reports_dir)
        console.print("[red]reports/ 下無週次目錄——先跑 make week[/red]")
        return None
    return dirs[-1]


def _print_summary(table: pl.DataFrame, cfg: ShortlistConfig, out: Path, origin: str) -> None:
    """印 top/alt 摘要、各 gate 計數與落檔位置。"""
    ranked = table.filter(pl.col("tier").is_in([TIER_TOP, TIER_ALT]))
    week = table["week"][0] if table.height else out.parent.name
    grid = Table(title=f"{week} 機器排序（shortlist）", show_lines=False)
    for col in ("#", "tier", "股票", "次產業（趨勢分・#R/N・桶）", "距季線", "承接區", "停損"):
        grid.add_column(col)
    for r in ranked.iter_rows(named=True):
        grid.add_row(
            str(r["rank"]),
            r["tier"],
            f"{r['stock_id']} {r['name'] or ''}",
            f"{r['sub_industry']}（{r['trend_score']:.1f}・#{r['trend_rank']}/{r['trend_n']}"
            f"・{r['trend_bucket']}）",
            f"{r['ma60_dist_pct']:+.1f}%",
            r["entry_text"],
            r["stop_text"],
        )
    console.print(grid)

    n_top = ranked.filter(pl.col("tier") == TIER_TOP).height
    if n_top < cfg.top_n:
        console.print(
            f"[yellow]⚠️ 合格僅 {n_top} 檔 < top_n {cfg.top_n}——不從弱桶補，Top 表就少列[/yellow]"
        )
    no_stop = ranked.filter(pl.col("stop_price").is_null())
    if not no_stop.is_empty():
        console.print(
            f"[yellow]⚠️ 停損價未取得：{'、'.join(no_stop['stock_id'].to_list())}[/yellow]"
        )
    alt_ids = ranked.filter(pl.col("tier") == TIER_ALT)
    alt_txt = "、".join(
        f"#{r['rank']} {r['stock_id']} {r['name'] or ''}".strip()
        for r in alt_ids.iter_rows(named=True)
    )
    console.print(f"候補（alt）：{alt_txt or '無'}")

    counts = Counter(
        r for r in table.filter(~pl.col("tier").is_in([TIER_TOP, TIER_ALT]))["gate_reason"]
    )
    order = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    console.print(
        "gate／上限計數："
        + ("・".join(f"{k} {v}" for k, v in order) if order else "無")
        + f"（共 {table.height} 檔；held {table.filter(pl.col('tier') == TIER_HELD).height}）"
    )
    console.print(f"[dim]次產業宇宙：{origin}[/dim]")
    console.print(f"[green]shortlist → {out}[/green]")


def run_shortlist(settings: Path, week: str | None = None) -> int:
    """M-Pick1：機器排序 Top N → reports/<週>/shortlist.csv；回傳 exit code（0＝成功或設定停用）。

    缺必要輸入（candidates_enriched.csv／sector_rotation.csv）→ warning＋回 1、不寫檔
    （不留半套產物；舊檔不動）。picks.shortlist.enabled=false → 印跳過訊息、回 0。
    """
    from tw_screener.report.picks_runner import _resolve_data_date

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    sl_cfg = ShortlistConfig.from_settings(cfg)
    if not sl_cfg.enabled:
        console.print(
            "[yellow]picks.shortlist.enabled=false——機器排序跳過（回退模式，不產 shortlist.csv）"
            "[/yellow]"
        )
        return 0

    week_dir = _resolve_week_dir(Path(cfg["paths"]["reports_dir"]), week)
    if week_dir is None:
        return 1
    missing = [f for f in _REQUIRED_INPUTS if not (week_dir / f).is_file()]
    if missing:
        logger.warning("shortlist：{} 缺必要輸入 {}，不產 shortlist.csv", week_dir, missing)
        console.print(
            f"[red]{week_dir} 缺必要輸入 {'、'.join(missing)}——先跑 make group／make rotation；"
            f"本次未寫 {SHORTLIST_FILENAME}[/red]"
        )
        return 1

    try:
        candidates = _read_enriched(week_dir / "candidates_enriched.csv")
        rotation = pl.read_csv(week_dir / "sector_rotation.csv", infer_schema_length=None)
    except (OSError, pl.exceptions.PolarsError) as e:
        logger.warning("shortlist：讀必要輸入失敗：{}", e)
        console.print(f"[red]讀必要輸入失敗（{e}）——本次未寫 {SHORTLIST_FILENAME}[/red]")
        return 1
    if "stock_id" not in candidates.columns or not {"sub_industry", "trend_score"}.issubset(
        rotation.columns
    ):
        logger.warning("shortlist：必要欄位缺（stock_id／sub_industry／trend_score）")
        console.print(
            "[red]必要欄位缺：candidates_enriched 需 stock_id、sector_rotation 需 "
            f"sub_industry／trend_score——本次未寫 {SHORTLIST_FILENAME}[/red]"
        )
        return 1

    week_tag = week_dir.name
    members, origin = _load_members(cfg, settings, week_tag)
    pool = build_pool(
        candidates,
        _read_optional(week_dir / "watchlist_enriched.csv"),
        _read_optional(week_dir / "holdings_enriched.csv"),
        sl_cfg,
    )
    table = build_shortlist(
        pool, members, rotation, sl_cfg, week=week_tag, data_date=_resolve_data_date(week_dir)
    )

    out = week_dir / SHORTLIST_FILENAME
    tmp = out.with_name(out.name + ".tmp")
    table.write_csv(tmp)
    tmp.replace(out)  # 先寫暫存再換名：不留半套檔
    _print_summary(table, sl_cfg, out, origin)
    return 0
