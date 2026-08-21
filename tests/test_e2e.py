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


def test_e2e_render_report_section26_rotation_disclosure(tmp_path: Path):
    """M-WS5b（WS5-③）：次產業表末兩欄並列揭露 rotation 趨勢分＋輪動Rank（同 sub_industry key）。

    純揭露不重排——強度分數排序不變、只多兩欄。有 sector_rotation.csv 對照者顯示趨勢分/位階。
    """
    results = {"a_breakout": _SCREENER_A, "b_growth_institutional": _SCREENER_B}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    # 兩檔候選標同一次產業（≥ min_members 2 才入次產業表）
    themes_long = pl.DataFrame(
        {
            "stock_id": ["2330", "2454"],
            "theme": ["晶圓代工", "晶圓代工"],
            "kind": ["次產業", "次產業"],
        }
    )
    # 全次產業無偏輪動快照：晶圓代工趨勢分 88、輪動位階 #3
    pl.DataFrame(
        {
            "sub_industry": ["晶圓代工"],
            "trend_score": [88.0],
            "quadrant": ["主升續勢"],
            "radar_rank": [3],
            "entry_triggered": [False],
        }
    ).write_csv(tmp_path / "sector_rotation.csv")
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output, themes_long=themes_long)
    content = output.read_text(encoding="utf-8")
    assert "| 趨勢分 | 輪動Rank |" in content  # 次產業表新增兩欄表頭
    # 晶圓代工列並列揭露趨勢分 88、輪動位階 #3（Section 2.6 表列＝以 | 起首＋含趨勢分 88 定位；
    # 2.8 雷達雖亦有 #3 但無趨勢分欄，且 2.6 在前，next 命中 2.6 列）
    row = next(
        ln for ln in content.splitlines()
        if ln.startswith("| ") and "晶圓代工" in ln and "| 88 |" in ln
    )
    assert "| #3 |" in row


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


def test_e2e_render_report_macro_light_none_degrades_gracefully(tmp_path: Path):
    """macro_light=None（docs/25 v2）→ 總經燈號段整段不渲染，其餘報告照常產出。"""
    results = {"a_breakout": _SCREENER_A}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    render_group_report(groups, ranked, results, "2026-W21", output, macro_light=None)
    content = output.read_text(encoding="utf-8")
    assert "總經燈號" not in content


def test_e2e_render_report_macro_light_renders_when_present(tmp_path: Path):
    results = {"a_breakout": _SCREENER_A}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    macro_light = {
        "color": "黃",
        "risk_score": 68.0,
        "as_of": "2026-07-30",
        "primary": {
            "series_id": "BAA10Y", "transform": "level_pct", "as_of": "2026-07-30",
            "raw_value": 2.14, "score_pct": 68.0, "stale": False,
        },
        "disclosure": [
            {
                "series_id": "DGS20", "transform": "level_pct", "as_of": "2026-07-30",
                "raw_value": 4.71, "score_pct": 61.0, "stale": False,
            },
            {
                "series_id": "DGS10", "transform": "raw", "as_of": None,
                "raw_value": None, "score_pct": None, "stale": True,
            },
        ],
        "prev_color": "綠",
        "change_line": "綠 → 黃",
        "advice": "風險水位中等，維持觀察，無需立即調整。",
        "line": "總經燈號：黃 68/100",
    }
    render_group_report(groups, ranked, results, "2026-W21", output, macro_light=macro_light)
    content = output.read_text(encoding="utf-8")
    assert "總經燈號" in content
    assert "BAA10Y" in content
    assert "綠 → 黃" in content
    assert "較上次" in content  # docs/26 A案：欄位常在，無 delta 資料時格內顯示「—」
    for word in ("目標價", "強烈建議", "飆股", "絕對"):
        assert word not in content, f"forbidden word found in macro section: {word}"


def test_e2e_render_report_macro_panel_delta_arrows(tmp_path: Path):
    """docs/26 A案：面板變化欄——有前次印箭頭＋Δ＋前次日期，無前次印「—」不硬算。"""
    results = {"a_breakout": _SCREENER_A}
    groups, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2
    )
    ranked = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    output = tmp_path / "group_analysis.md"
    macro_light = {
        "color": "綠",
        "risk_score": 44.0,
        "as_of": "2026-08-06",
        "primary": {
            "series_id": "BAA10Y", "transform": "level_pct", "as_of": "2026-08-06",
            "raw_value": 1.61, "score_pct": 39.3, "stale": False,
            "delta": {
                "arrow": "↓", "prev_as_of": "2026-07-30", "score_pct": -5.0, "raw_value": -0.03,
            },
        },
        "disclosure": [
            {
                "series_id": "DGS20", "transform": "level_pct", "as_of": "2026-08-06",
                "raw_value": 5.22, "score_pct": 99.3, "stale": False,
                "delta": {
                    "arrow": "↑", "prev_as_of": "2026-07-30", "score_pct": 12.0,
                    "raw_value": 0.14,
                },
            },
            {
                "series_id": "DEXJPUS", "transform": "raw", "as_of": "2026-07-31",
                "raw_value": 159.16, "score_pct": None, "stale": False,
                "delta": {
                    "arrow": "—", "prev_as_of": None, "score_pct": None, "raw_value": None,
                },
            },
        ],
        "prev_color": "綠",
        "change_line": None,
        "advice": "風險水位偏低，維持標準流程。",
        "line": "總經燈號：綠 44/100",
    }
    render_group_report(groups, ranked, results, "2026-W21", output, macro_light=macro_light)
    content = output.read_text(encoding="utf-8")
    assert "| 指標 | 轉換 | 現況 | 較上次 | as-of |" in content
    assert "↑ +12.0p〔前次 2026-07-30〕" in content  # 有前次＝印變化與比較基準
    assert "↓ -5.0p〔前次 2026-07-30〕" in content  # 主訊號行同樣帶變化
    assert "| — |" in content  # 無前次＝老實留白
    # 變化欄是揭露，不得暗示它會改燈色或影響排序
    assert "純揭露，不進計分、不改燈色" in content
    # raw 列的箭頭是數值方向不是風險方向（DEXJPUS ↓ 才是警戒向）——必須明標，否則會被反讀
    assert "箭頭語意分兩種" in content
    assert "DEXJPUS ↓（日圓走強）" in content


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
    # 2330 close=1000.0（見 _SCREENER_A）；shares_outstanding 25,930,380,458 → 市值(億元)
    rows = write_candidates_enriched_csv(
        ranked, pl.DataFrame(), results, out,
        cum_rev_yoy_map={"2330": 19.67},
        shares_map={"2330": 25_930_380_458},
    )
    assert out.exists()
    assert len(rows) == len(ranked)  # 現回傳已建立的列（供重疊股重用）
    df = pl.read_csv(out)
    assert df.height == len(ranked)
    for col in [
        "stock_id", "name", "industry", "theme", "strategy", "rank_in_group",
        "momentum_5d_pct", "close", "ma60_dist_pct", "ma20_price", "ma60_price",
        "amount_million", "pe_ratio", "pb_ratio", "rev_yoy_pct",
        "volume_lots_today", "inst_net_lots", "inst_pct20d", "flags", "goodinfo_url",
        "cum_rev_yoy_pct", "market_cap_billion",
    ]:
        assert col in df.columns
    row_2330 = df.filter(pl.col("stock_id") == 2330).to_dicts()[0]
    assert row_2330["cum_rev_yoy_pct"] == pytest.approx(19.7)  # _num 四捨五入至 1 位小數
    assert row_2330["market_cap_billion"] == pytest.approx(25_930_380_458 * 1000.0 / 1e8, rel=1e-6)
    # 沒進 shares_map 的股票：市值誠實 None，不補值
    row_2317 = df.filter(pl.col("stock_id") == 2317).to_dicts()[0]
    assert row_2317["market_cap_billion"] is None


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


def _fake_ohlcv(sid: str, n_days: int = 60) -> pl.DataFrame:
    """造 n_days 個交易日的最小 OHLCV（_enrich_named_list / group_stocks 需要的欄）。

    預設 60 根＝enrich 回補門檻（快取 < 60 根會觸發 fetch_stock_history）——
    「已有足夠快取」的股要給滿 60，才不會被回補邏輯誤觸發。
    """
    from datetime import date, timedelta

    days = [date(2026, 3, 1) + timedelta(days=i) for i in range(n_days)]
    closes = [100.0 + i for i in range(n_days)]
    return pl.DataFrame(
        {
            "stock_id": [sid] * n_days,
            "date": days,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "change": [1.0] * n_days,
            "trade_volume": [1_000_000] * n_days,
            "trade_value": [100_000_000] * n_days,
            "transaction": [500] * n_days,
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
    from tw_screener.analysis.watchlist import enrich_named_list

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
    members, _ = enrich_named_list(
        client, [cached, otc], industry_df, pl.DataFrame(), None, name_map={cached: "台積電"}
    )

    got = set(members["stock_id"].cast(pl.Utf8).to_list())
    assert {cached, otc} <= got, "無快取的上櫃股不應被丟掉"
    assert client.history_calls == [otc], "只有快取不足 MA60 視窗的股應觸發 fetch_stock_history"
    name_by_id = dict(zip(members["stock_id"].cast(pl.Utf8), members["name"], strict=False))
    assert name_by_id[otc] == "系微", "name_map 缺名時應由 industry_df.stock_name 補"
