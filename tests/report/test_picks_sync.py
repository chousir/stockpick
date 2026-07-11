"""picks sync 測試（pick.md 機器可讀區塊 → 底帳整批 upsert；規劃書 05 F1-PO1）。

tmp_path 上驗 happy path／冪等／F2 硬擋全不寫／解析錯誤／未知欄位，不碰真 reports/。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
import typer
import yaml

from tw_screener.report.pick_store import load_week_excluded, load_week_picks
from tw_screener.report.picks_runner import run_picks_sync

WEEK = "2026-W27"


def _setup_week(tmp_path: Path) -> tuple[Path, Path]:
    """建 tmp 週目錄（screen_result＋enriched）與 settings，回 (settings, week_dir)。"""
    reports = tmp_path / "reports"
    week_dir = reports / WEEK
    week_dir.mkdir(parents=True)
    pl.DataFrame({"stock_id": ["3006"], "screened_at": ["2026-06-30"]}).write_csv(
        week_dir / "screen_result_d_quality_leader.csv"
    )
    pl.DataFrame(
        {
            "stock_id": ["3006", "2344"],
            "name": ["晶豪科", "華邦電"],
            "ma60_dist_pct": [8.4, 69.0],
        }
    ).write_csv(week_dir / "candidates_enriched.csv")
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        yaml.safe_dump(
            {
                "paths": {"reports_dir": str(reports)},
                "picks": {"core_ext_ma60_max_pct": 15.0},
            }
        ),
        encoding="utf-8",
    )
    return settings, week_dir


def _write_pick_md(week_dir: Path, block: str) -> None:
    (week_dir / "pick.md").write_text(
        f"# ProPicks {WEEK}\n\n一頁決策卡……\n\n{block}", encoding="utf-8"
    )


GOOD_BLOCK = """<!-- picks:begin -->
```yaml
picks:
  - stock: "3006"
    layer: core
    sub: 記憶體
    entry: "T1 232(MA20·50%)"
    stop: "收盤跌破199.2"
    thesis: "D+E 外資三窗同買"
  - {stock: 9999, layer: pool, thesis: 乾淨補充池}
excluded:
  - {stock: "2344", reason: 過熱, detail: "外資近5日爆量倒貨"}
```
<!-- picks:end -->
"""


def test_sync_happy_path_autofills_and_writes_both_ledgers(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(week_dir, GOOD_BLOCK)
    run_picks_sync(settings, WEEK)

    picks = load_week_picks(week_dir)
    assert picks.height == 2
    core = picks.filter(pl.col("stock_id") == "3006").row(0, named=True)
    assert core["name"] == "晶豪科"  # 自動從 enriched 補
    assert abs(core["ext_ma60_pct"] - 8.4) < 1e-9
    assert core["data_date"] == date(2026, 6, 30)  # 自動取 screened_at
    pool = picks.filter(pl.col("stock_id") == "9999").row(0, named=True)  # 未加引號的股號被轉字串
    assert pool["layer"] == "pool" and pool["name"] is None

    excluded = load_week_excluded(week_dir)
    assert excluded.height == 1
    row = excluded.row(0, named=True)
    assert row["stock_id"] == "2344" and row["name"] == "華邦電" and row["reason"] == "過熱"


def test_sync_rerun_is_idempotent(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(week_dir, GOOD_BLOCK)
    run_picks_sync(settings, WEEK)
    run_picks_sync(settings, WEEK)
    assert load_week_picks(week_dir).height == 2
    assert load_week_excluded(week_dir).height == 1


def test_sync_f2_core_extension_blocks_entire_write(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "3006", layer: core}\n'
        '  - {stock: "2344", layer: core}\n'  # ext 69.0 > 15 → F2 硬擋
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert not (week_dir / "picks.csv").exists()  # 全過才寫：合法的 3006 也不落帳


def test_sync_yaml_error_points_to_file_line(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - stock: "3006"\n'
        "   layer: core\n"  # 縮排錯 → YAML 語法錯誤
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    out = capsys.readouterr().out
    assert "解析失敗" in out and "行" in out
    assert not (week_dir / "picks.csv").exists()


def test_sync_missing_block_errors(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(week_dir, "（本週忘了附區塊）\n")
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert "找不到" in capsys.readouterr().out


def test_sync_unknown_field_rejected_nothing_written(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "3006", layer: core, entyr: "手滑欄位"}\n'
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert "未知欄位" in capsys.readouterr().out
    assert not (week_dir / "picks.csv").exists()


def test_sync_watchlist_stock_autofills_from_watchlist_enriched(tmp_path):
    """觀察清單股未命中策略（不在 candidates）→ name/ext 兜底自 watchlist_enriched。"""
    settings, week_dir = _setup_week(tmp_path)
    pl.DataFrame(
        {"stock_id": ["8299"], "name": ["群聯"], "ma60_dist_pct": [3.2]}
    ).write_csv(week_dir / "watchlist_enriched.csv")
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "8299", layer: core, thesis: 觀察清單升格}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    row = load_week_picks(week_dir).row(0, named=True)
    assert row["name"] == "群聯"
    assert abs(row["ext_ma60_pct"] - 3.2) < 1e-9


def test_sync_watchlist_stock_f2_still_enforced(tmp_path, capsys):
    """觀察清單股的距季線乖離兜底可查後，F2 位階紀律照擋（不再放行未知）。"""
    settings, week_dir = _setup_week(tmp_path)
    pl.DataFrame(
        {"stock_id": ["2327"], "name": ["國巨"], "ma60_dist_pct": [42.0]}
    ).write_csv(week_dir / "watchlist_enriched.csv")
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "2327", layer: core}\n'
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert "位階紀律" in capsys.readouterr().out
    assert not (week_dir / "picks.csv").exists()


def test_sync_candidates_takes_precedence_over_watchlist(tmp_path):
    """同股同時在 candidates 與 watchlist enriched → 以 candidates（主宇宙）為準。"""
    settings, week_dir = _setup_week(tmp_path)
    pl.DataFrame(
        {"stock_id": ["3006"], "name": ["晶豪科(舊)"], "ma60_dist_pct": [99.0]}
    ).write_csv(week_dir / "watchlist_enriched.csv")
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "3006", layer: core}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    row = load_week_picks(week_dir).row(0, named=True)
    assert row["name"] == "晶豪科"  # candidates 那筆，非 watchlist
    assert abs(row["ext_ma60_pct"] - 8.4) < 1e-9


def test_sync_late_entry_key_accepted_and_persisted(tmp_path):
    """WS-L：pick.md 區塊每筆可選 late_entry 鍵（白名單同步），值落進底帳。"""
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "3006", layer: core, late_entry: true}\n'
        "excluded:\n"
        '  - {stock: "2344", reason: 過熱, late_entry: true}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    pick_row = load_week_picks(week_dir).row(0, named=True)
    assert pick_row["late_entry"] is True
    excl_row = load_week_excluded(week_dir).row(0, named=True)
    assert excl_row["late_entry"] is True


def test_sync_late_entry_defaults_false_when_omitted(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(week_dir, GOOD_BLOCK)
    run_picks_sync(settings, WEEK)
    row = load_week_picks(week_dir).row(0, named=True)
    assert row["late_entry"] is False


def test_sync_core_without_ext_warns_but_records(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "8888", layer: core, name: 無快取股}\n'  # 不在 enriched → ext 未知
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    assert "如實記錄" in capsys.readouterr().out
    row = load_week_picks(week_dir).row(0, named=True)
    assert row["stock_id"] == "8888" and row["ext_ma60_pct"] is None
