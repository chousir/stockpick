"""snapshot 測試（WS-J.1 週快照）：happy path 四件齊／holdings 缺檔跳過不炸／同週重跑覆寫。

tmp_path 上驗證，不碰真 config/concepts.yaml、真 watchlist/、真 data/snapshots/。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import yaml

from tw_screener.data.cache import save_parquet
from tw_screener.report.snapshot import run_week_snapshot

CONCEPTS_YAML = """concept_themes: []
concepts:
  '2330':
  - 半導體
  '2454':
  - 半導體
  - IC設計
"""

TRADING_DATE = date(2026, 7, 10)  # 週五 → 2026-W28


def _stash_daily(twse_cache: Path, d: date) -> None:
    save_parquet(
        pl.DataFrame(
            {
                "date": [d],
                "stock_id": ["2330"],
                "name": ["台積電"],
                "trade_volume": [1],
                "trade_value": [1],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "change": [0.0],
                "transaction": [1],
            }
        ),
        twse_cache / f"daily_{d.strftime('%Y%m%d')}.parquet",
    )


def _make_settings(tmp_path: Path) -> tuple[Path, Path, Path]:
    """建 config/settings.yaml（同目錄放 concepts.yaml）＋ watchlist/＋快照目的地。

    回傳 (settings_path, watchlist_dir, snapshots_root)。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "concepts.yaml").write_text(CONCEPTS_YAML, encoding="utf-8")

    watchlist_dir = tmp_path / "watchlist"
    watchlist_dir.mkdir()

    cache_dir = tmp_path / "cache"
    twse_cache = cache_dir / "twse"
    twse_cache.mkdir(parents=True)
    _stash_daily(twse_cache, TRADING_DATE)

    snap_root = tmp_path / "data" / "snapshots"

    settings = config_dir / "settings.yaml"
    settings.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "reports_dir": str(tmp_path / "reports"),
                    "cache_dir": str(cache_dir),
                    "watchlist_dir": str(watchlist_dir),
                },
                "snapshots": {"dir": str(snap_root)},
                "twse": {
                    "base_url": "https://test.invalid/v1",
                    "user_agent": "test",
                    "request_interval_sec": 0,
                    "cache_ttl_hours": 6,
                },
            }
        ),
        encoding="utf-8",
    )
    return settings, watchlist_dir, snap_root


def test_happy_path_writes_all_four_files_plus_meta(tmp_path: Path) -> None:
    settings, watchlist_dir, snap_root = _make_settings(tmp_path)
    (watchlist_dir / "holdings.csv").write_text(
        "stock_id,buy_price\n2330,600\n", encoding="utf-8"
    )
    (watchlist_dir / "watchlist.csv").write_text(
        "stock_id,note\n2454,聯發科\n", encoding="utf-8"
    )

    out_dir = run_week_snapshot(settings)

    assert out_dir == snap_root / "2026-W28"
    assert (out_dir / "concepts.yaml").read_text(encoding="utf-8") == CONCEPTS_YAML
    assert "2330" in (out_dir / "holdings.csv").read_text(encoding="utf-8")
    assert "2454" in (out_dir / "watchlist.csv").read_text(encoding="utf-8")

    universe = pl.read_csv(out_dir / "universe.csv", infer_schema_length=0)
    assert set(universe.columns) == {"sub_industry", "stock_id", "name"}
    assert set(universe["stock_id"].to_list()) == {"2330", "2454"}
    assert universe["name"].is_null().all()

    meta = yaml.safe_load((out_dir / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["week"] == "2026-W28"
    assert meta["data_date"] == "2026-07-10"
    assert meta["files"] == ["concepts.yaml", "holdings.csv", "watchlist.csv", "universe.csv"]
    assert isinstance(meta["created_at"], str) and meta["created_at"]


def test_missing_holdings_skips_not_fails(tmp_path: Path) -> None:
    settings, watchlist_dir, snap_root = _make_settings(tmp_path)
    (watchlist_dir / "watchlist.csv").write_text(
        "stock_id,note\n2454,聯發科\n", encoding="utf-8"
    )
    # holdings.csv 故意不建立

    out_dir = run_week_snapshot(settings)

    assert not (out_dir / "holdings.csv").exists()
    assert (out_dir / "watchlist.csv").exists()
    assert (out_dir / "concepts.yaml").exists()
    assert (out_dir / "universe.csv").exists()

    meta = yaml.safe_load((out_dir / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["files"] == ["concepts.yaml", "watchlist.csv", "universe.csv"]


def test_rerun_same_week_overwrites_directory(tmp_path: Path) -> None:
    settings, watchlist_dir, snap_root = _make_settings(tmp_path)
    (watchlist_dir / "holdings.csv").write_text(
        "stock_id,buy_price\n2330,600\n", encoding="utf-8"
    )

    first = run_week_snapshot(settings)
    assert (first / "holdings.csv").exists()
    assert not (first / "watchlist.csv").exists()

    # 第二次跑前新增 watchlist.csv → 重跑後快照該反映最終狀態，不是疊加殘留
    (watchlist_dir / "watchlist.csv").write_text(
        "stock_id,note\n2454,聯發科\n", encoding="utf-8"
    )
    second = run_week_snapshot(settings)

    assert second == first
    assert (second / "watchlist.csv").exists()
    assert (second / "holdings.csv").exists()
