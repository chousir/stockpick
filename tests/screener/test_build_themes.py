"""build-themes --dry-run：Yahoo 概念股 merge 進 concepts.yaml（全離線）。"""

from pathlib import Path

from typer.testing import CliRunner

import tw_screener.screener.yahoo.fetcher as yf
from tw_screener.analysis.concepts import CONCEPT_KIND, SUB_INDUSTRY_KIND, load_themes
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
  concept_whitelist: ["衛星/低軌衛星"]
  concept_min_members: 3
  min_members: 2
"""

# 既有手動 concepts.yaml：含手動次產業 + 一個「上次自動寫入」的概念股（應被清掉換新）
_EXISTING_CONCEPTS = """\
concept_themes: [舊概念A]
concepts:
  "2330": [IC生產製造, 舊概念A]
  "9999": 某次產業
"""


def test_build_themes_dry_run_merges_into_concepts(tmp_path: Path, monkeypatch):
    idx = Path("tests/fixtures/yahoo/class_index.html").read_text(encoding="utf-8")
    sat = Path("tests/fixtures/yahoo/category_satellite.html").read_text(encoding="utf-8")

    class FakeFetcher:
        def get(self, path_or_url: str, *, force: bool = False) -> str:
            return idx if path_or_url == "/class" else sat

    monkeypatch.setattr(yf, "create_yahoo_fetcher", lambda *a, **k: FakeFetcher())

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(_SETTINGS, encoding="utf-8")
    concepts = tmp_path / "config" / "concepts.yaml"
    concepts.write_text(_EXISTING_CONCEPTS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app, ["data", "build-themes", "--dry-run", "--settings", "config/settings.yaml"]
    )
    assert result.exit_code == 0, result.output

    cand = tmp_path / "config" / "concepts.candidate.yaml"
    assert cand.exists()
    assert concepts.read_text(encoding="utf-8") == _EXISTING_CONCEPTS  # dry-run 不動正式檔

    df = load_themes(cand)
    rec = {(r["stock_id"], r["theme"]): r["kind"] for r in df.iter_rows(named=True)}
    # 手動次產業保留、kind 正確
    assert rec[("2330", "IC生產製造")] == SUB_INDUSTRY_KIND
    assert rec[("9999", "某次產業")] == SUB_INDUSTRY_KIND
    # 上次自動寫入的「舊概念A」已被清掉
    assert ("2330", "舊概念A") not in rec
    # 2317（鴻海，在衛星成分）拿到新概念股、kind=概念股
    assert rec[("2317", "衛星/低軌衛星")] == CONCEPT_KIND
    # 白名單只留衛星 → 概念股題材只有這一個
    concept_themes = {r["theme"] for r in df.iter_rows(named=True) if r["kind"] == CONCEPT_KIND}
    assert concept_themes == {"衛星/低軌衛星"}
