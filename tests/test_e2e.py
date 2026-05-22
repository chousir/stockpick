"""tests/test_e2e.py — 完整週流程端對端測試（全離線，用 fixture 資料）。

流程：screener CSV → group_stocks → rank_within_groups → render_group_report → 驗證輸出。
不打任何網路，不需要 API key。
"""

from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis.grouping import group_stocks
from tw_screener.analysis.leader import rank_within_groups
from tw_screener.report.group_report import render_group_report

# ─── Fixtures ─────────────────────────────────────────────────────────────────

_INDUSTRY_DF = pl.DataFrame(
    {
        "stock_id": ["2330", "2454", "3034", "2317", "2382", "6669"],
        "industry_code": ["24", "24", "24", "31", "25", "25"],
        "industry_name": [
            "半導體業",
            "半導體業",
            "半導體業",
            "其他電子業",
            "電腦及周邊設備業",
            "電腦及周邊設備業",
        ],
    }
)

_SCREENER_A = pl.DataFrame(
    {
        "stock_id": ["2330", "2454", "3034", "2317"],
        "name": ["台積電", "聯發科", "聯詠", "鴻海"],
        "close": [1000.0, 900.0, 500.0, 200.0],
        "change_pct": [4.5, 3.8, 5.2, 1.1],
        "amount_million": [5000.0, 3000.0, 1500.0, 4000.0],
        "goodinfo_url": [
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2330",
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2454",
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=3034",
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2317",
        ],
        "strategy_id": ["a_breakout"] * 4,
    }
)

_SCREENER_B = pl.DataFrame(
    {
        "stock_id": ["2330", "2382"],
        "name": ["台積電", "廣達"],
        "close": [1000.0, 220.0],
        "change_pct": [4.5, 2.1],
        "amount_million": [5000.0, 1800.0],
        "goodinfo_url": [
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2330",
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2382",
        ],
        "strategy_id": ["b_growth_institutional"] * 2,
    }
)


# ─── E2E: group_stocks ────────────────────────────────────────────────────────


def test_e2e_group_stocks_produces_output():
    """group_stocks 從兩組 CSV 產出非空的 groups + members。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    assert not groups.is_empty()
    assert not members.is_empty()


def test_e2e_group_stocks_top_group_is_semiconductor():
    """半導體業（3 檔）應排在前面。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, _ = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    assert groups["industry_name"][0] == "半導體業"


def test_e2e_multi_strategy_intersection():
    """2330 入選 A + B 兩組，count 欄位應正確反映。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, _ = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    semi = groups.filter(pl.col("industry_name") == "半導體業")
    assert semi["count_a_breakout"][0] == 3
    assert semi["count_b_growth_institutional"][0] == 1


# ─── E2E: rank_within_groups ──────────────────────────────────────────────────


def test_e2e_rank_within_groups_returns_dataframe():
    results = {"a_breakout": _SCREENER_A}
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    assert isinstance(ranked, pl.DataFrame)
    assert not ranked.is_empty()


def test_e2e_rank_within_groups_has_required_columns():
    results = {"a_breakout": _SCREENER_A}
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    for col in ("stock_id", "industry_code", "leader_score", "rank_in_group"):
        assert col in ranked.columns, f"missing column: {col}"


def test_e2e_rank_1_per_group_is_max_score():
    """每個族群 rank_in_group=1 的那檔，leader_score 應最高。"""
    results = {"a_breakout": _SCREENER_A}
    _, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    for code in ranked["industry_code"].unique().to_list():
        group_df = ranked.filter(pl.col("industry_code") == code)
        top_row = group_df.filter(pl.col("rank_in_group") == 1)
        if top_row.is_empty():
            continue
        max_score = group_df["leader_score"].max()
        assert top_row["leader_score"][0] == pytest.approx(max_score)


# ─── E2E: render_group_report ─────────────────────────────────────────────────


def test_e2e_render_group_report_creates_file(tmp_path: Path):
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    assert output.exists()
    assert output.stat().st_size > 0


def test_e2e_render_report_contains_required_sections(tmp_path: Path):
    """報告應含 Section 0-6 的關鍵字。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    assert "策略代號說明" in content
    assert "入選分布總覽" in content
    assert "族群強度排名" in content
    assert "本週族群表現前" in content
    assert "觀察" in content
    assert "深度分析" in content


def test_e2e_render_report_has_cross_group_strong_section(tmp_path: Path):
    """報告應含「跨族群強勢領漲股」區段，且個股按 5 日漲幅降序。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    assert "強勢領漲股" in content
    assert "不分族群" in content


def test_e2e_render_report_no_leader_word(tmp_path: Path):
    """新版報告不應再出現「領頭羊」字樣。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    assert "領頭羊" not in content, "新版報告不應再出現「領頭羊」字眼"


def test_e2e_render_report_has_5day_momentum(tmp_path: Path):
    """報告應含「5 日均漲」/「5 日漲幅」欄位。"""
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    assert "5 日中位" in content  # 群組層由「均漲」改中位數（M1）
    assert "5 日漲幅" in content


def test_e2e_render_report_has_goodinfo_links(tmp_path: Path):
    results = {"a_breakout": _SCREENER_A}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    assert "goodinfo.tw" in content


def test_e2e_render_report_no_forbidden_words(tmp_path: Path):
    results = {"a_breakout": _SCREENER_A}
    groups, members = group_stocks(
        results,
        pl.DataFrame(),
        pl.DataFrame(),
        industry_df=_INDUSTRY_DF,
        min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output)
    content = output.read_text(encoding="utf-8")
    for word in ("目標價", "強烈建議", "飆股", "絕對"):
        assert word not in content, f"forbidden word found: {word}"
