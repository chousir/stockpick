"""tests/screener/test_build_themes.py — build-themes --dry-run 端到端（全離線）。"""

from pathlib import Path

import polars as pl
from typer.testing import CliRunner

import tw_screener.screener.yahoo.fetcher as yf
from tw_screener.analysis.concepts import CONCEPT_KIND, load_themes
from tw_screener.cli import app

_SETTINGS = """\
paths:
  cache_dir: data/cache
yahoo:
  base_url: "https://x"
  user_agent: "ua"
  request_interval_sec: 0
  request_interval_jitter_sec: 0
  cache_ttl_hours: 24
  max_retries: 1
  backoff_base: 1
  concurrency: 1
themes_build:
  category_labels: ["概念股"]
  concept_min_members: 3
  min_members: 2
"""


def test_build_themes_dry_run_e2e(tmp_path: Path, monkeypatch):
    # 先在 chdir 前讀好 fixtures（相對 repo root）
    idx = Path("tests/fixtures/yahoo/class_index.html").read_text(encoding="utf-8")
    sat = Path("tests/fixtures/yahoo/category_satellite.html").read_text(encoding="utf-8")

    class FakeFetcher:
        def get(self, path_or_url: str, *, force: bool = False) -> str:
            return idx if path_or_url == "/class" else sat

    monkeypatch.setattr(yf, "create_yahoo_fetcher", lambda *a, **k: FakeFetcher())

    (tmp_path / "config").mkdir()
    settings = tmp_path / "config" / "settings.yaml"
    settings.write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app, ["data", "build-themes", "--dry-run", "--settings", str(settings)]
    )
    assert result.exit_code == 0, result.output

    cand = tmp_path / "config" / "themes.candidate.yaml"
    assert cand.exists()
    assert not (tmp_path / "config" / "themes.yaml").exists()  # dry-run 不寫正式檔

    df = load_themes(concepts_path=tmp_path / "nope.yaml", themes_path=cand)
    assert not df.is_empty()
    assert (df["kind"] == CONCEPT_KIND).all()  # 只收概念股
    assert "衛星/低軌衛星" in set(df["theme"].to_list())
    sat_ids = set(df.filter(pl.col("theme") == "衛星/低軌衛星")["stock_id"].to_list())
    assert "2317" in sat_ids  # 鴻海，股號乾淨
