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
