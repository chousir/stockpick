"""tests/report/test_group_report.py — enriched CSV writer 行為（全離線）。"""

from datetime import date

import polars as pl

from tw_screener.analysis.grouping import group_stocks
from tw_screener.report.group_report import write_candidates_enriched_csv

_INDUSTRY_DF = pl.DataFrame(
    {
        "stock_id": ["2330", "2454"],
        "industry_code": ["24", "24"],
        "industry_name": ["半導體業", "半導體業"],
    }
)


def _screener_df(stock_ids: list[str], change_pcts: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": stock_ids,
            "name": [f"公司{s}" for s in stock_ids],
            "close": [100.0] * len(stock_ids),
            "change_pct": change_pcts,
            "amount_million": [1000.0] * len(stock_ids),
            "goodinfo_url": [f"http://goodinfo/{s}" for s in stock_ids],
            "strategy_id": ["a_breakout"] * len(stock_ids),
        }
    )


def _institutional(stock_ids: list[str], total_nets: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2026, 5, 19)] * len(stock_ids),
            "stock_id": stock_ids,
            "stock_name": [f"公司{s}" for s in stock_ids],
            "foreign_net": total_nets,
            "trust_net": [0] * len(stock_ids),
            "dealer_net": [0] * len(stock_ids),
            "total_net": total_nets,
        }
    )


def test_candidates_csv_blanks_and_flags_inst_missing(tmp_path):
    """法人快取缺漏的股票：inst 四欄空白 + flags 含『法人缺漏』；有資料的股票正常。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    institutional = _institutional(["2330"], [5_000_000])  # 只有 2330（5000 張），2454 缺漏
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=institutional,
    )
    out = tmp_path / "candidates_enriched.csv"
    rows = write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    assert rows  # 回傳已建立的列（供重疊股重用）

    df = pl.read_csv(out)
    by_id = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}

    miss = by_id["2454"]
    assert miss["inst_net_lots"] is None
    assert miss["foreign_net_lots"] is None
    assert miss["trust_net_lots"] is None
    assert miss["inst_pct20d"] is None
    assert "法人缺漏" in (miss["flags"] or "")

    ok = by_id["2330"]
    assert ok["inst_net_lots"] == 5000  # 5,000,000 股 / 1000 = 5000 張
    assert "法人缺漏" not in (ok["flags"] or "")


def test_strong_leader_vs_overheated_flag(tmp_path):
    """距季線 >40%：外資投信同向買 + 營收 YoY 達標 → 『強勢領頭』（非過熱）；
    否則（缺 YoY / 法人非同向）→ 『過熱』。修法1：過熱由硬否決改為需確認的脈絡。"""
    # 直接建 members（注入 ma60_dist_pct，省去 OHLCV 歷史）；張數同 _institutional 以股為單位
    members = pl.DataFrame(
        {
            "stock_id": ["AAA", "BBB", "CCC"],
            "name": ["強領", "缺營收", "非同向"],
            "industry_name": ["半導體業"] * 3,
            "momentum_5d": [39.0, 35.0, 20.0],
            "ma60_dist_pct": [78.0, 62.0, 50.0],  # 三檔皆 >40（原本一律過熱）
            "ma20_dist_pct": [30.0, 25.0, 15.0],
            "foreign_net": [173_128_000, 147_877_000, 10_000_000],
            "trust_net": [75_549_000, 24_858_000, -8_000_000],  # CCC 投信賣＝非同向
            "inst_net": [248_677_000, 172_735_000, 2_000_000],
        }
    )
    rev_yoy_map = {"AAA": 182.0, "CCC": 50.0}  # BBB 無 YoY；CCC YoY 夠但法人非同向
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), {}, out, rev_yoy_map=rev_yoy_map)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    # AAA：同向買 + YoY 達標 → 強勢領頭（不再被打成過熱）
    assert "強勢領頭" in (by_id["AAA"]["flags"] or "")
    assert "過熱" not in (by_id["AAA"]["flags"] or "")
    # BBB：同向買但無 YoY → 過熱（非強勢領頭）
    assert "過熱" in (by_id["BBB"]["flags"] or "")
    assert "強勢領頭" not in (by_id["BBB"]["flags"] or "")
    # CCC：YoY 夠但外資投信非同向（投信賣）→ 過熱
    assert "過熱" in (by_id["CCC"]["flags"] or "")
    assert "強勢領頭" not in (by_id["CCC"]["flags"] or "")


def test_valuation_map_official_primary_goodinfo_fallback(tmp_path):
    """估值欄：官方 BWIBBU 為主、Goodinfo 兜底；殖利率僅官方有。"""
    # Goodinfo screener 帶 pe_ratio/pb_ratio（兩檔都有）
    sc = _screener_df(["2330", "2454"], [3.0, 2.0]).with_columns(
        pl.Series("pe_ratio", [20.0, 18.0]),  # Goodinfo 值
        pl.Series("pb_ratio", [4.0, 3.0]),
    )
    results = {"a_breakout": sc}
    institutional = _institutional(["2330", "2454"], [5_000_000, 4_000_000])
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=institutional,
    )
    # 官方只有 2330（build_valuation 列：pe/pbr/殖利率＋次產業相對位階）；2454 官方缺
    valuation_map = {
        "2330": {"pe": 31.0, "pbr": 10.0, "dividend_yield": 1.5,
                 "val_metric": "PE", "val_pctile": 12.0, "cheap_flag": "相對便宜"},
    }
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out,
                                  valuation_map=valuation_map)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    # 2330：用官方 PE/PB（非 Goodinfo 20/4），殖利率＋次產業相對位階 inline
    assert by_id["2330"]["pe_ratio"] == 31.0
    assert by_id["2330"]["pb_ratio"] == 10.0
    assert by_id["2330"]["dividend_yield_pct"] == 1.5
    assert by_id["2330"]["val_metric"] == "PE"
    assert by_id["2330"]["val_pctile"] == 12.0
    assert by_id["2330"]["cheap_flag"] == "相對便宜"
    # 2454：官方缺 → 兜底 Goodinfo PE/PB（18/3）、殖利率＋相對位階空白
    assert by_id["2454"]["pe_ratio"] == 18.0
    assert by_id["2454"]["pb_ratio"] == 3.0
    assert by_id["2454"]["dividend_yield_pct"] is None
    assert by_id["2454"]["val_pctile"] is None
    assert by_id["2454"]["cheap_flag"] in (None, "")
