"""FinMind 回補宇宙擴充（--include-uncovered）的 helper：只讀本地產業別快取、不打網。"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from tw_screener.cli import _uncovered_market_ids


def _write(cache: Path, name: str, ids: list[str]) -> None:
    pl.DataFrame(
        {"stock_id": ids, "stock_name": ids, "industry_code": ["01"] * len(ids),
         "industry_name": ["x"] * len(ids)}
    ).write_parquet(cache / name)


def test_uncovered_ids_exclude_members_etf_and_warrants(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "twse").mkdir(parents=True)
    _write(cache / "twse", "industry_202610.parquet", ["1101", "2330", "0050", "2330Y"])
    _write(cache / "twse", "otc_industry_202610.parquet", ["3293", "6669"])
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"paths:\n  cache_dir: {cache}\n", encoding="utf-8")

    out = _uncovered_market_ids(settings, covered=["2330", "6669"])

    assert out == ["1101", "3293"]  # 成員（2330/6669）、ETF（0050）、權證（2330Y）皆不在


def _settings_with_universe(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    (cache / "twse").mkdir(parents=True)
    _write(cache / "twse", "industry_202610.parquet", ["2330", "3293"])
    _write(cache / "twse", "otc_industry_202610.parquet", ["6669"])
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"paths:\n  cache_dir: {cache}\n", encoding="utf-8")
    return settings


def test_with_uncovered_three_modes(tmp_path: Path) -> None:
    from tw_screener.cli import _with_uncovered

    settings = _settings_with_universe(tmp_path)
    members = ["2330"]
    assert _with_uncovered(members, settings, False, False) == ["2330"]
    assert _with_uncovered(members, settings, True, False) == ["2330", "3293", "6669"]
    assert _with_uncovered(members, settings, False, True) == ["3293", "6669"]
    # only 優先於 include（兩旗標並用＝只補未涵蓋）
    assert _with_uncovered(members, settings, True, True) == ["3293", "6669"]


def _make_dry_run(target: str, *args: str) -> str:
    import subprocess

    out = subprocess.run(
        ["make", "-n", target, *args], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_makefile_uncovered_flag_mapping() -> None:
    """UNCOVERED 變數 → CLI 旗標（make -n 只印指令、不執行）。"""
    for target in ("backfill-finmind-per", "backfill-finmind-financials"):
        base = _make_dry_run(target)
        assert "--only-uncovered" not in base and "--include-uncovered" not in base
        assert "--only-uncovered" in _make_dry_run(target, "UNCOVERED=only")
        inc = _make_dry_run(target, "UNCOVERED=1")
        assert "--include-uncovered" in inc and "--only-uncovered" not in inc
