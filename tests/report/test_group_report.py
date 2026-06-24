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
    assert miss["foreign_net_5d_lots"] is None  # 修法6：缺漏時近端窗也空白
    assert miss["foreign_net_10d_lots"] is None
    assert miss["trust_net_lots"] is None
    assert miss["inst_net_5d_lots"] is None     # 分析層補窗：缺漏時三大法人/投信近端窗也空白
    assert miss["inst_net_10d_lots"] is None
    assert miss["trust_net_5d_lots"] is None
    assert miss["trust_net_10d_lots"] is None
    assert miss["inst_pct20d"] is None
    assert "法人缺漏" in (miss["flags"] or "")

    ok = by_id["2330"]
    assert ok["inst_net_lots"] == 5000  # 5,000,000 股 / 1000 = 5000 張
    assert "法人缺漏" not in (ok["flags"] or "")


def test_candidates_csv_foreign_multiwindow_columns(tmp_path):
    """修法6：candidates CSV 應有 foreign_net_5d/10d_lots 與 ret_10d_pct 三欄。"""
    dates = [date(2026, 5, d) for d in range(1, 13)]
    fnet = [10_000_000] * 7 + [-4_000_000] * 5  # 股：20日+50000張、5日−20000張
    inst = pl.DataFrame(
        {
            "date": dates,
            "stock_id": ["2330"] * 12,
            "stock_name": ["台積電"] * 12,
            "foreign_net": fnet,
            "trust_net": [0] * 12,
            "dealer_net": [0] * 12,
            "total_net": fnet,
        }
    )
    closes = [170.0, 176.0, 173.0, 168.0, 165.0, 160.0, 158.0, 156.0, 159.0, 158.0, 163.0, 162.0]
    history = pl.DataFrame({"stock_id": ["2330"] * 12, "date": dates, "close": closes})
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=inst,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    for col in ("foreign_net_5d_lots", "foreign_net_10d_lots", "ret_10d_pct"):
        assert col in df.columns
    row = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}["2330"]
    assert row["foreign_net_lots"] == 50000      # 20 日累計（正）
    assert row["foreign_net_5d_lots"] == -20000  # 近 5 日轉賣（近端真相）
    assert row["ret_10d_pct"] < 0                # 近 10 日下跌


def test_candidates_csv_inst_trust_multiwindow_columns(tmp_path):
    """分析層補窗：candidates CSV 應有 inst/trust 的 5d/10d_lots 欄（張），與外資同口徑。"""
    dates = [date(2026, 5, d) for d in range(1, 13)]
    fnet = [10_000_000] * 7 + [-4_000_000] * 5   # 外資 20日+50000 / 5日−20000 張
    tnet = [1_000_000] * 12                        # 投信 5日+5000 / 10日+10000 張
    total = [f + t for f, t in zip(fnet, tnet)]   # 三大法人(自營=0) 5日−15000 / 10日+40000 張
    inst = pl.DataFrame(
        {
            "date": dates,
            "stock_id": ["2330"] * 12,
            "stock_name": ["台積電"] * 12,
            "foreign_net": fnet,
            "trust_net": tnet,
            "dealer_net": [0] * 12,
            "total_net": total,
        }
    )
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=inst,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    for col in ("inst_net_5d_lots", "inst_net_10d_lots", "trust_net_5d_lots", "trust_net_10d_lots"):
        assert col in df.columns
    row = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}["2330"]
    assert row["trust_net_5d_lots"] == 5000      # 投信近 5 日（張）
    assert row["trust_net_10d_lots"] == 10000
    assert row["inst_net_5d_lots"] == -15000     # 三大法人近 5 日轉賣（張）
    assert row["inst_net_10d_lots"] == 40000


def test_candidates_csv_range_extrema_columns(tmp_path):
    """M-修法7（7a）：candidates CSV 應有 low/high_20d/60d 四欄（近 20/60 日收盤 min/max）。"""
    n = 25
    dates = [date(2026, 5, 1 + i) for i in range(n)]
    head = [50.0, 200.0, 60.0, 190.0, 70.0]  # 全域低 50 / 高 200（僅落在 60 日窗）
    tail = [120.0, 130.0, 110.0, 140.0, 150.0, 100.0, 160.0, 170.0, 115.0, 125.0,
            135.0, 145.0, 155.0, 165.0, 175.0, 180.0, 118.0, 128.0, 138.0, 148.0]
    closes = head + tail
    history = pl.DataFrame({"stock_id": ["2330"] * n, "date": dates, "close": closes})
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    for col in ("low_20d", "high_20d", "low_60d", "high_60d"):
        assert col in df.columns
    row = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}["2330"]
    assert row["low_20d"] == 100.0
    assert row["high_20d"] == 180.0
    assert row["low_60d"] == 50.0
    assert row["high_60d"] == 200.0


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


def test_cross_trade_relative_liquidity_gate(tmp_path):
    """修法4：土洋對作加相對流通量門檻——弱邊張數須達近 20 日總量 ≥4% 才算對作。
    權值股小量反向（台積電型）不再誤標；小型股對作仍標；量資料缺則退回絕對判定。"""
    # 三檔皆外資投信反向且雙邊 >5000 張（過絕對門檻）；差別在弱邊相對 20 日量
    members = pl.DataFrame(
        {
            "stock_id": ["TSMC", "SMALL", "NOVOL"],
            "name": ["權值", "小型", "缺量"],
            "industry_name": ["半導體業"] * 3,
            "momentum_5d": [3.0, 3.0, 3.0],
            "ma60_dist_pct": [11.0, 8.0, 8.0],  # 皆 <40，避開過熱旗標干擾
            "ma20_dist_pct": [3.0, 2.0, 2.0],
            "vol_ratio": [1.21, 1.0, 1.0],
            "foreign_net": [-41_697_000, 10_000_000, 10_000_000],  # 股
            "trust_net": [7_826_000, -8_000_000, -8_000_000],
            "inst_net": [-33_871_000, 2_000_000, 2_000_000],
        }
    )
    # 今日量（張）走 screener 的 volume_lots；NOVOL 不在 screener → 無量 → 退回絕對判定
    sc = pl.DataFrame(
        {
            "stock_id": ["TSMC", "SMALL"],
            "name": ["權值", "小型"],
            "close": [100.0, 100.0],
            "change_pct": [1.0, 1.0],
            "volume_lots": [49983, 5829],
            "goodinfo_url": ["http://g/TSMC", "http://g/SMALL"],
            "strategy_id": ["a_breakout", "a_breakout"],
        }
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), {"a_breakout": sc}, out)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    # TSMC：弱邊投信 7,826 張 / 20日總量 ≈0.95% < 4% → 不標（誤殺解除）
    assert "土洋對作" not in (by_id["TSMC"]["flags"] or "")
    # SMALL：弱邊 8,000 張 / 20日總量 ≈6.9% ≥ 4% → 仍標
    assert "土洋對作" in (by_id["SMALL"]["flags"] or "")
    # NOVOL：無量資料 → 退回絕對判定（雙邊過 5000 張）→ 仍標，不因缺資料漏標
    assert "土洋對作" in (by_id["NOVOL"]["flags"] or "")


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
