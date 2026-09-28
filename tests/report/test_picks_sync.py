"""picks sync 測試（pick.md 機器可讀區塊 → 底帳整批 upsert；規劃書 05 F1-PO1）。

tmp_path 上驗 happy path／冪等／F2 硬擋全不寫／解析錯誤／未知欄位，不碰真 reports/。
"""

from __future__ import annotations

import re
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


def test_appendix_g_target_price_does_not_affect_sync(tmp_path):
    """docs/31 §20.13：pick.md「附錄 G 實驗性目標價」（含「目標價」字眼）在 picks
    區塊之外 → picks sync 完全不受影響（parser 只認 picks:begin/end）。"""
    settings, week_dir = _setup_week(tmp_path)
    appendix_g = (
        "\n## 附錄 G — 實驗性目標價\n\n"
        "| 股號 | 前瞻EPS | 目標PE | search-augmented 目標價 | vs 現價 |\n"
        "|---|---|---|---|---|\n"
        "| 3006 | 12.5 | 20.8 | 260.0 | +6.1% |\n"
        "> 無歷史驗證、可信度低；目標PE=自身歷史中位（pe_self_n=26）\n\n"
    )
    # 一份不含附錄 G、一份含（附錄 G 插在 picks 區塊之前）
    _write_pick_md(week_dir, GOOD_BLOCK)
    run_picks_sync(settings, WEEK)
    picks_without = load_week_picks(week_dir).sort("stock_id")
    excl_without = load_week_excluded(week_dir).sort("stock_id")

    (week_dir / "pick.md").write_text(
        f"# ProPicks {WEEK}\n\n一頁決策卡……\n{appendix_g}\n{GOOD_BLOCK}",
        encoding="utf-8",
    )
    run_picks_sync(settings, WEEK)
    picks_with = load_week_picks(week_dir).sort("stock_id")
    excl_with = load_week_excluded(week_dir).sort("stock_id")

    assert picks_with.equals(picks_without)
    assert excl_with.equals(excl_without)


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


# ── M-Pick1：rank ＋ shortlist.csv 對帳 ─────────────────────────────────────


def _write_shortlist(week_dir: Path, rows: list[tuple[str, str, int | None]]) -> None:
    """[(stock_id, tier, rank)] → reports/<週>/shortlist.csv（picks sync 只讀這三欄＋name）。"""
    pl.DataFrame(
        {
            "stock_id": [r[0] for r in rows],
            "name": [f"股{r[0]}" for r in rows],
            "tier": [r[1] for r in rows],
            "rank": [r[2] for r in rows],
        },
        schema_overrides={"rank": pl.Int64},
    ).write_csv(week_dir / "shortlist.csv")


_STOP = "收盤跌破 100.00（MA60）、隔日未收復出場"


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    """rich 會依終端寬度折行、FORCE_COLOR 下會插 ANSI 碼——兩者都去掉再比對訊息。"""
    return re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).replace("\n", "")


def test_sync_without_rank_stores_null_machine_rank(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(week_dir, GOOD_BLOCK)
    run_picks_sync(settings, WEEK)
    picks = load_week_picks(week_dir)
    assert picks.schema["machine_rank"] == pl.Int64
    assert picks["machine_rank"].to_list() == [None, None]


def test_sync_rank_matching_shortlist_is_written(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_shortlist(week_dir, [("3006", "top", 1), ("6271", "alt", 6)])
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        f'  - {{stock: "3006", layer: core, rank: 1, stop: "{_STOP}"}}\n'
        '  - {stock: "6271", layer: pool, rank: 6}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    ranks = dict(load_week_picks(week_dir).select("stock_id", "machine_rank").iter_rows())
    assert ranks == {"3006": 1, "6271": 6}


def test_sync_rank_mismatch_blocks_entire_write(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_shortlist(week_dir, [("3006", "top", 1), ("6271", "top", 2)])
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        f'  - {{stock: "3006", layer: core, rank: 2, stop: "{_STOP}"}}\n'
        f'  - {{stock: "6271", layer: core, rank: 2, stop: "{_STOP}"}}\n'
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert "不符" in _out(capsys)
    assert not (week_dir / "picks.csv").exists()  # 合法的 6271 也不落帳


def test_sync_rank_for_stock_outside_shortlist_top_alt_errors(tmp_path):
    settings, week_dir = _setup_week(tmp_path)
    _write_shortlist(week_dir, [("3006", "top", 1), ("2344", "gated", None)])
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "2344", layer: pool, rank: 3}\n'
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert not (week_dir / "picks.csv").exists()


@pytest.mark.parametrize("bad", ['"1"', "1.5", "true", "0", "-2", "第一"])
def test_sync_non_integer_rank_errors(tmp_path, capsys, bad):
    settings, week_dir = _setup_week(tmp_path)
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        f'  - {{stock: "3006", layer: core, rank: {bad}}}\n'
        "<!-- picks:end -->\n",
    )
    with pytest.raises(typer.Exit):
        run_picks_sync(settings, WEEK)
    assert "rank 必須是 ≥1 的整數" in _out(capsys)
    assert not (week_dir / "picks.csv").exists()


def test_sync_substitute_alt_promoted_to_core_keeps_own_rank(tmp_path, capsys):
    """否決 top #5 → alt #6 遞補：YAML 帶它自己在 shortlist 的名次 6、layer 改 core＝合法。"""
    settings, week_dir = _setup_week(tmp_path)
    top = [("1101", "top", 1), ("1102", "top", 2), ("1103", "top", 3), ("1104", "top", 4),
           ("1105", "top", 5), ("1106", "alt", 6)]
    _write_shortlist(week_dir, top)
    core_rows = "".join(
        f'  - {{stock: "{sid}", layer: core, rank: {rank}, stop: "{_STOP}"}}\n'
        for sid, _, rank in top if sid != "1105"
    )
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        f"picks:\n{core_rows}"
        "excluded:\n"
        '  - {stock: "1105", reason: 機器排序否決, detail: "處置股（查詢 2026-09-27）"}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    out = _out(capsys)
    assert "既不在 picks 也不在 excluded" not in out
    picks = load_week_picks(week_dir)
    sub = picks.filter(pl.col("stock_id") == "1106").row(0, named=True)
    assert (sub["layer"], sub["machine_rank"]) == ("core", 6)
    veto = load_week_excluded(week_dir).row(0, named=True)
    assert (veto["stock_id"], veto["reason"]) == ("1105", "機器排序否決")


def test_sync_missing_veto_record_warns_but_writes(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_shortlist(week_dir, [("3006", "top", 1), ("6271", "top", 2)])
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        f'  - {{stock: "3006", layer: core, rank: 1, stop: "{_STOP}"}}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    out = _out(capsys)
    assert "6271" in out and "既不在 picks 也不在 excluded" in out
    assert load_week_picks(week_dir).height == 1


def test_sync_vetoes_over_max_warns(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    ids = ["1101", "1102", "1103"]
    _write_shortlist(week_dir, [(sid, "top", i) for i, sid in enumerate(ids, start=1)])
    vetoes = "".join(f'  - {{stock: "{sid}", reason: 機器排序否決}}\n' for sid in ids)
    _write_pick_md(
        week_dir, f"<!-- picks:begin -->\nexcluded:\n{vetoes}<!-- picks:end -->\n"
    )
    run_picks_sync(settings, WEEK)  # 預設 max_vetoes=2
    assert "機器排序否決 3 檔 > 上限 2" in _out(capsys)
    assert load_week_excluded(week_dir).height == 3


def test_sync_core_stop_unparseable_warns_when_shortlist_exists(tmp_path, capsys):
    settings, week_dir = _setup_week(tmp_path)
    _write_shortlist(week_dir, [("3006", "top", 1)])
    _write_pick_md(
        week_dir,
        "<!-- picks:begin -->\n"
        "picks:\n"
        '  - {stock: "3006", layer: core, rank: 1, stop: "跌破季線停損"}\n'
        "<!-- picks:end -->\n",
    )
    run_picks_sync(settings, WEEK)
    assert "抽不到絕對停損價" in _out(capsys)
    assert load_week_picks(week_dir).row(0, named=True)["machine_rank"] == 1
