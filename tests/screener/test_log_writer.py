"""tests/screener/test_log_writer.py — screen_log.md 產出測試。"""

from pathlib import Path

import polars as pl

from tw_screener.screener.log_writer import write_screen_log


def _df(rows: list[tuple[str, str]]) -> pl.DataFrame:
    """rows = [(stock_id, name), ...]"""
    return pl.DataFrame(
        {"stock_id": [r[0] for r in rows], "name": [r[1] for r in rows]}
    )


def test_write_screen_log_creates_file(tmp_path: Path):
    results = {"a_breakout": _df([("2330", "台積電")])}
    names = {"a_breakout": "波段啟動"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    assert out.exists()
    assert out.name == "screen_log.md"


def test_write_screen_log_contains_counts(tmp_path: Path):
    results = {
        "a_breakout": _df([("2330", "台積電"), ("2454", "聯發科")]),
        "b_growth_institutional": _df([("2330", "台積電")]),
        "c_dividend_steady": _df([]),
    }
    names = {
        "a_breakout": "波段啟動",
        "b_growth_institutional": "法人成長",
        "c_dividend_steady": "穩健存股",
    }
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    content = out.read_text(encoding="utf-8")
    assert "| a_breakout | 波段啟動 | 2 |" in content
    assert "| b_growth_institutional | 法人成長 | 1 |" in content
    assert "| c_dividend_steady | 穩健存股 | 0 |" in content


def test_write_screen_log_pairwise_intersection(tmp_path: Path):
    results = {
        "a_breakout": _df([("2330", "台積電"), ("2454", "聯發科")]),
        "b_growth_institutional": _df([("2330", "台積電"), ("2317", "鴻海")]),
    }
    names = {"a_breakout": "A", "b_growth_institutional": "B"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    content = out.read_text(encoding="utf-8")
    assert "a_breakout ∩ b_growth_institutional — 1 檔" in content
    assert "2330 台積電" in content


def test_write_screen_log_triple_intersection(tmp_path: Path):
    results = {
        "a": _df([("2330", "台積電"), ("2454", "聯發科")]),
        "b": _df([("2330", "台積電"), ("2317", "鴻海")]),
        "c": _df([("2330", "台積電"), ("2412", "中華電")]),
    }
    names = {"a": "A", "b": "B", "c": "C"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    content = out.read_text(encoding="utf-8")
    assert "a ∩ b ∩ c — 1 檔" in content
    assert "2330 台積電" in content


def test_write_screen_log_empty_intersection_shows_none(tmp_path: Path):
    results = {
        "a": _df([("2330", "台積電")]),
        "b": _df([("2317", "鴻海")]),
    }
    names = {"a": "A", "b": "B"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    content = out.read_text(encoding="utf-8")
    assert "a ∩ b — 0 檔" in content
    assert "（無）" in content


def test_write_screen_log_all_empty_strategies(tmp_path: Path):
    results = {"a": _df([]), "b": _df([])}
    names = {"a": "A", "b": "B"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    content = out.read_text(encoding="utf-8")
    assert "| a | A | 0 |" in content
    assert "a ∩ b — 0 檔" in content


def test_write_screen_log_week_dir(tmp_path: Path):
    results = {"a": _df([("2330", "台積電")])}
    names = {"a": "A"}
    out = write_screen_log(results, names, "2026-W20", tmp_path / "reports")
    assert "2026-W20" in str(out)


def test_run_all_writes_screen_log(tmp_path: Path):
    """run_all 跑完後 reports/{week}/screen_log.md 應存在。"""
    from tw_screener.screener.runner import ScreenerRunner

    fixture = (
        Path(__file__).parent / "fixtures" / "goodinfo" / "screener_result.html"
    )
    if not fixture.exists():
        # fallback path
        fixture = Path("tests/fixtures/goodinfo/screener_result.html")
    html = fixture.read_text(encoding="utf-8")

    class _Mock:
        def get(self, url: str, *, force: bool = False) -> str:
            return html

    runner = ScreenerRunner(Path("config/settings.yaml"))
    runner._reports_dir = tmp_path / "reports"
    runner._fetcher = _Mock()
    runner.run_all(week_tag="2026-W20")

    log_path = tmp_path / "reports" / "2026-W20" / "screen_log.md"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "本週篩選紀錄" in content
    assert "a_breakout" in content
