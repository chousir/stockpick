"""report/snapshot.py — WS-J.1 週快照：point-in-time 凍結族群/持股/觀察清單狀態。

動機：族群 membership（config/concepts.yaml）與持股/觀察清單（watchlist/）每週都在變，
研究軌回頭重建歷史時無法還原「當週看到的狀態」。本模組把當週狀態複製/展開進
data/snapshots/<週次>/，供之後回測/校準需要「當時看到什麼」時讀取。純落檔，不改動
任何來源檔案。同週重跑＝整目錄覆寫（快照代表「該週最終狀態」，非逐次疊加）。
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

_UNIVERSE_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
}


def _resolve_week_and_date(settings_path: Path) -> tuple[str, date]:
    """週次 tag ＋ 對應交易日。

    鏡像自 src/tw_screener/screener/runner.py:30-43（derive_week_tag）——該函式是
    trading_date 錨點的公開唯一入口，但只回傳字串；這裡另需原始 date 物件寫進
    meta.yaml 的 data_date，為避免重複建立 TWSE client／重打一次快取，鏡像其邏輯
    （同一 fallback：取交易日失敗 → 今日）而非另外呼叫一次 derive_week_tag。
    """
    from tw_screener.data.twse import create_client

    try:
        client = create_client(settings_path)
        td = client.latest_trading_date()
    except Exception as exc:  # noqa: BLE001 — 取交易日失敗 fallback 今日（同 derive_week_tag）
        logger.warning("snapshot：取交易日失敗，fallback 今日：{}", exc)
        td = None
    resolved = td or date.today()
    return resolved.strftime("%Y-W%V"), resolved


def _copy_or_skip(src: Path, dest: Path, label: str) -> bool:
    """複製 src → dest；缺檔 warning＋跳過（回 False），不讓整個快照流程失敗。"""
    if not src.exists():
        logger.warning("snapshot：{} 不存在（{}），跳過", label, src)
        return False
    shutil.copy2(src, dest)
    return True


def run_week_snapshot(settings: Path) -> Path:
    """把當週 concepts/holdings/watchlist/次產業宇宙成員凍結到 data/snapshots/<週次>/。

    內容四件：concepts.yaml（原樣複製）、holdings.csv／watchlist.csv（watchlist/ 下同名
    檔複製，缺檔跳過不失敗）、universe.csv（當週次產業宇宙成員展開，name 欄不可得留空）、
    meta.yaml（week/created_at/data_date/檔案清單）。同週重跑＝整目錄覆寫。

    回傳快照目錄路徑。
    """
    from tw_screener.analysis.sector_universe import list_subindustries

    with open(settings, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    week_tag, trading_date = _resolve_week_and_date(settings)

    snap_root = Path(cfg.get("snapshots", {}).get("dir", "data/snapshots"))
    watchlist_dir = Path(cfg["paths"]["watchlist_dir"])
    concepts_path = settings.parent / "concepts.yaml"

    out_dir = snap_root / week_tag
    if out_dir.exists():
        logger.info("snapshot：{} 已存在，整目錄覆寫（快照＝該週最終狀態）", out_dir)
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    included: list[str] = []
    if _copy_or_skip(concepts_path, out_dir / "concepts.yaml", "concepts.yaml"):
        included.append("concepts.yaml")
    if _copy_or_skip(watchlist_dir / "holdings.csv", out_dir / "holdings.csv", "holdings.csv"):
        included.append("holdings.csv")
    if _copy_or_skip(
        watchlist_dir / "watchlist.csv", out_dir / "watchlist.csv", "watchlist.csv"
    ):
        included.append("watchlist.csv")

    universe = list_subindustries(concepts_path=concepts_path)
    if universe.is_empty():
        universe = pl.DataFrame(schema=_UNIVERSE_SCHEMA)
    else:
        universe = universe.with_columns(pl.lit(None, dtype=pl.Utf8).alias("name")).select(
            ["sub_industry", "stock_id", "name"]
        )
    universe.write_csv(out_dir / "universe.csv")
    included.append("universe.csv")

    meta = {
        "week": week_tag,
        "created_at": datetime.now(UTC).isoformat(),
        "data_date": trading_date.isoformat(),
        "files": included,
    }
    with open(out_dir / "meta.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(meta, fh, allow_unicode=True, sort_keys=False)

    logger.info("snapshot：{} 完成（{}）", out_dir, "、".join(included))
    return out_dir
