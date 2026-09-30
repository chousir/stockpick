"""tests/backtest/test_panel_tr_runner.py — D6 成員稠密總報酬面板編排（離線合成快取）。"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import typer
import yaml

from tw_screener.backtest.panel import build_price_panel
from tw_screener.backtest.panel_tr_runner import run_panel_tr
from tw_screener.data.finmind import _DIVIDEND_RESULT_SCHEMA, _STOCK_PRICE_SCHEMA

SECTORS = {"甲": ["1101", "1102", "1103"], "乙": ["2201", "2202", "2203"]}
EX_SID, EX_DATE, EX_RATIO = "1101", date(2022, 9, 15), 1.05
UNCOVERED = "2203"      # 沒有 dividend_ 檔 → target 全 null
ABSENT_OLD = "1103"     # 舊面板整檔缺席（對應真實的 129 檔）
SPARSE_FROM = date(2023, 3, 1)


def _weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2021, 6, 1), date(2023, 6, 30))


def _closes(sid: str) -> list[float]:
    rng = random.Random(int(sid))
    px, out = 100.0, []
    for d in DAYS:
        px *= 1 + rng.uniform(-0.02, 0.021)
        if sid == EX_SID and d == EX_DATE:
            px /= EX_RATIO
        out.append(round(px, 2))
    return out


def _price_frame(sid: str) -> pl.DataFrame:
    closes = _closes(sid)
    return pl.DataFrame(
        {
            "date": DAYS, "stock_id": [sid] * len(DAYS), "open": closes, "high": closes,
            "low": closes, "close": closes, "volume": [3_000_000] * len(DAYS),
            "amount": [c * 3_000_000 for c in closes], "transactions": [1000] * len(DAYS),
        },
        schema=_STOCK_PRICE_SCHEMA,
    )


def _write_world(tmp: Path) -> tuple[Path, dict[str, Any]]:
    fm = tmp / "cache" / "finmind"
    fm.mkdir(parents=True)
    all_prices = []
    for sids in SECTORS.values():
        for sid in sids:
            frame = _price_frame(sid)
            frame.write_parquet(fm / f"price_{sid}.parquet")
            all_prices.append(frame)
            if sid != UNCOVERED:
                rows = []
                if sid == EX_SID:
                    before = _closes(sid)[DAYS.index(EX_DATE) - 1]
                    rows.append({
                        "ex_date": EX_DATE, "stock_id": sid, "before_price": before,
                        "after_price": before / EX_RATIO, "reference_price": before / EX_RATIO,
                        "dividend_value": before - before / EX_RATIO, "event_type": "息",
                    })
                pl.DataFrame(rows, schema=_DIVIDEND_RESULT_SCHEMA).write_parquet(
                    fm / f"dividend_{sid}.parquet"
                )
    # 舊面板：1103 整檔缺席；SPARSE_FROM 起隨機漏 40% 列；r{h} 為純價差（未還原）
    rng = random.Random(3)
    full = pl.concat(all_prices).select("date", "stock_id", "close", "volume").filter(
        pl.col("stock_id") != ABSENT_OLD
    )
    keep = [
        not (d >= SPARSE_FROM and rng.random() < 0.4)
        for d in full["date"].to_list()
    ]
    sparse = full.filter(pl.Series(keep))
    old = build_price_panel(sparse, horizons=(10, 20, 40), ma_windows=(60,)).filter(
        pl.col("date") >= date(2022, 1, 3)
    )
    (tmp / "research").mkdir()
    old.write_parquet(tmp / "research" / "panel.parquet")

    with open("config/settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["paths"]["cache_dir"] = str(tmp / "cache")
    cfg["backtest"]["panel_tr"] = {
        "start_date": "2022-01-03", "horizons_td": [10, 20, 40], "main_horizon_td": 20,
        "old_panel_path": str(tmp / "research" / "panel.parquet"), "old_panel_end": "2023-05-31",
        "mpick2_stockweeks_glob": str(tmp / "nomatch" / "*.parquet"),
        "calendar_min_names": 2, "span_baseline_year": 2022, "span_floor_margin_pp": 1.0,
        "span_check_since": "2023-03-01", "before_price_tol_pct": 0.5,
        "before_price_min_rate": 0.98,
        "spot_checks": [[EX_SID, "2022-09-01"]],
        "output_dir": str(tmp / "out"), "output_name": "panel_tr_members.parquet",
    }
    conf = tmp / "config"
    conf.mkdir()
    path = conf / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (conf / "concepts.yaml").write_text(
        yaml.safe_dump(
            {"concept_themes": [], "concepts": {s: sec for sec, ss in SECTORS.items() for s in ss}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path, cfg


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any], Path]:
    def _no_net(*a: object, **k: object) -> object:
        raise AssertionError("D6 重建不應打網")

    monkeypatch.setattr(httpx, "get", _no_net)
    path, cfg = _write_world(tmp_path)
    return path, cfg, tmp_path


def test_run_panel_tr_outputs_report_and_acceptance(
    world: tuple[Path, dict[str, Any], Path],
) -> None:
    path, _, tmp = world
    run_panel_tr(path, None)
    out = tmp / "out"
    panel = pl.read_parquet(out / "panel_tr_members.parquet")
    assert panel.columns == [
        "date", "stock_id", "close", "ma60_dist_pct", "r10", "r20", "r40",
        "bad_evt_10", "bad_evt_20", "bad_evt_40", "div_covered",
    ]
    assert panel["date"].min() == date(2022, 1, 3)
    expect_ids = sorted(s for ss in SECTORS.values() for s in ss)
    assert sorted(panel["stock_id"].unique().to_list()) == expect_ids
    # 沒抓過除權息的 2203：r20 全 null；其餘股票在期內到期的窗皆有值（稠密＋還原）
    assert panel.filter(pl.col("stock_id") == UNCOVERED)["r20"].null_count() == panel.filter(
        pl.col("stock_id") == UNCOVERED
    ).height
    # 1101 除息窗手算：d＝除息前一交易日往前 5 列，窗含 EX_DATE → r20 = (exit/entry × 1.05 − 1)×100
    closes = _closes(EX_SID)
    t = DAYS.index(EX_DATE) - 5
    e, x = t + 1, t + 1 + 20
    expect = (closes[x] / closes[e] * EX_RATIO - 1) * 100
    got = panel.filter((pl.col("stock_id") == EX_SID) & (pl.col("date") == DAYS[t]))["r20"].item()
    assert got == pytest.approx(expect)

    report = next(out.glob("panel_tr_rebuild_*.md")).read_text(encoding="utf-8")
    assert "### 1.1 列吻合率" in report and "### 1.3" in report
    # 年列只含門檻月（2023-03）之前；門檻月起逐月列出
    assert "| 2023（全年，至 2023-03 前） |" in report and "| 2023-03 |" in report
    assert "A1 窗跨度不退化" in report and "**PASS**" in report        # 稠密新面板 → A1、A2 皆 PASS
    assert report.count("**PASS**") == 2 and "FAIL" not in report
    assert "舊面板缺席的成員（整檔不在舊面板）1 檔，其中新面板有價者 1 檔" in report
    assert f"| {EX_SID} | 2022-09-01 |" in report                        # 抽查窗有列
    assert "找不到" in report                                           # 無 M-Pick2 檔 → 略過說明


def test_run_panel_tr_a1_fails_when_new_panel_is_sparse(
    world: tuple[Path, dict[str, Any], Path],
) -> None:
    path, cfg, tmp = world
    # 把門檻墊高到不可能（基線 −1pp 後仍 > 1）→ A1 必 FAIL，且報告如實寫出
    cfg["backtest"]["panel_tr"]["span_floor_margin_pp"] = -200.0
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    run_panel_tr(path, None)
    report = next((tmp / "out").glob("panel_tr_rebuild_*.md")).read_text(encoding="utf-8")
    assert "FAIL（" in report


def test_run_panel_tr_mpick2_section_when_file_present(
    world: tuple[Path, dict[str, Any], Path],
) -> None:
    path, cfg, tmp = world
    d = date(2023, 4, 3)
    (tmp / "mp2").mkdir()
    pl.DataFrame({
        "date": [d, d], "stock_id": ["1101", "1102"], "in_main": [True, False], "r20": [1.0, 2.0],
    }).write_parquet(tmp / "mp2" / "intra_pick_stockweeks_20260928.parquet")
    cfg["backtest"]["panel_tr"]["mpick2_stockweeks_glob"] = str(
        tmp / "mp2" / "intra_pick_stockweeks_*.parquet"
    )
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    run_panel_tr(path, None)
    report = next((tmp / "out").glob("panel_tr_rebuild_*.md")).read_text(encoding="utf-8")
    assert "intra_pick_stockweeks_20260928.parquet" in report
    assert "快照日 ≥ 2026-05-01" not in report
    assert "快照日 < 2026-05-01" in report      # 2023-04 的列落在「<」期間


def test_run_panel_tr_missing_inputs_exit(world: tuple[Path, dict[str, Any], Path]) -> None:
    path, cfg, tmp = world
    cfg["backtest"]["panel_tr"]["old_panel_path"] = str(tmp / "no_such_panel.parquet")
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(typer.Exit):
        run_panel_tr(path, None)
