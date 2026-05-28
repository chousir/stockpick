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
