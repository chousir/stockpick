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


# ─── E2E: candidates_enriched.csv ─────────────────────────────────────────────


def test_e2e_candidates_enriched_csv(tmp_path: Path):
    """全候選股完整欄位 CSV：每檔一列、含技術/籌碼欄、檔數＝候選數。"""
    from tw_screener.report.group_report import write_candidates_enriched_csv

    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    out = tmp_path / "candidates_enriched.csv"
    rows = write_candidates_enriched_csv(ranked, pl.DataFrame(), results, out)
    assert out.exists()
    assert len(rows) == len(ranked)  # 現回傳已建立的列（供重疊股重用）
    df = pl.read_csv(out)
    assert df.height == len(ranked)
    for col in [
        "stock_id", "name", "industry", "theme", "strategy", "rank_in_group",
        "momentum_5d_pct", "close", "ma60_dist_pct", "ma20_price", "ma60_price",
        "amount_million", "pe_ratio", "pb_ratio", "rev_yoy_pct",
        "volume_lots_today", "inst_net_lots", "inst_pct20d", "flags", "goodinfo_url",
    ]:
        assert col in df.columns


def test_e2e_named_list_csv_holdings(tmp_path: Path):
    """庫存清單 CSV：加 buy_price/return_pct/market_value_k；觀察清單無 buy_price。"""
    from tw_screener.report.group_report import write_named_list_csv

    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    sid = str(ranked["stock_id"].to_list()[0])

    out_h = tmp_path / "holdings_enriched.csv"
    n = write_named_list_csv(
        ranked, pl.DataFrame(), results, out_h,
        holdings_map={sid: {"buy_price": 100.0, "shares": 5.0}},
    )
    assert out_h.exists() and n == len(ranked)
    dfh = pl.read_csv(out_h)
    for col in ["buy_price", "return_pct", "market_value_k"]:
        assert col in dfh.columns

    out_w = tmp_path / "watchlist_enriched.csv"
    write_named_list_csv(ranked, pl.DataFrame(), results, out_w)  # 無 holdings_map
    assert "buy_price" not in pl.read_csv(out_w).columns


def _fake_ohlcv(sid: str) -> pl.DataFrame:
    """造 12 個交易日的最小 OHLCV（_enrich_named_list / group_stocks 需要的欄）。"""
    from datetime import date, timedelta

    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(12)]
    closes = [100.0 + i for i in range(12)]
    return pl.DataFrame(
        {
            "stock_id": [sid] * 12,
            "date": days,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "change": [1.0] * 12,
            "trade_volume": [1_000_000] * 12,
            "trade_value": [100_000_000] * 12,
            "transaction": [500] * 12,
        }
    )


class _FakeClient:
    """模擬 TWSE client：cached_id 直接有 OHLCV；otc_id 要等 fetch_stock_history 才有。"""

    def __init__(self, cached_id: str, otc_id: str):
        self._cached, self._otc = cached_id, otc_id
        self._otc_ready = False
        self.history_calls: list[str] = []

    def fetch_stock_ohlcv(self, sid: str, n_days: int = 60) -> pl.DataFrame:
        if sid == self._cached or (sid == self._otc and self._otc_ready):
            return _fake_ohlcv(sid)
        return pl.DataFrame()

    def fetch_stock_history(self, sid: str, months: int = 3) -> pl.DataFrame:
        self.history_calls.append(sid)
        if sid == self._otc:
            self._otc_ready = True  # 模擬 TPEX 抓回後寫入快取
        return _fake_ohlcv(sid)


def test_enrich_named_list_fetches_uncached_and_names_from_industry():
    """回歸：上櫃股無快取時主動 fetch_stock_history 不被丟；name 由 industry_df 補。"""
    from tw_screener.cli import _enrich_named_list

    cached, otc = "2330", "6231"
    industry_df = pl.DataFrame(
        {
            "stock_id": [cached, otc],
            "stock_name": ["台積電", "系微"],
            "industry_code": ["24", "27"],
            "industry_name": ["半導體業", "資訊服務業"],
        }
    )
    client = _FakeClient(cached, otc)
    # name_map 只有上市股、缺上櫃 6231 → 6231 的名字必須走 industry_df fallback
    members, _ = _enrich_named_list(
        client, [cached, otc], industry_df, pl.DataFrame(), None, name_map={cached: "台積電"}
    )

    got = set(members["stock_id"].cast(pl.Utf8).to_list())
    assert {cached, otc} <= got, "無快取的上櫃股不應被丟掉"
    assert client.history_calls == [otc], "只有無快取的股應觸發 fetch_stock_history"
    name_by_id = dict(zip(members["stock_id"].cast(pl.Utf8), members["name"], strict=False))
    assert name_by_id[otc] == "系微", "name_map 缺名時應由 industry_df.stock_name 補"
