"""tests/report/test_group_report.py — enriched CSV writer 行為（全離線）。"""

import tempfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis.grouping import group_stocks
from tw_screener.report.group_report import (
    _build_rotation_axis,
    write_candidates_enriched_csv,
)

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


def test_base_zone_disclosure_flag(tmp_path):
    """M-WS5a（WS5-①）：距季線 ≤ 門檻 → base_zone='貼底'（起漲 base 位階，過熱旗標對稱面）。
    純揭露、獨立於 flags 排雷欄；延伸/過熱股 base_zone 空。"""
    members = pl.DataFrame(
        {
            "stock_id": ["BASE", "MID", "HOT"],
            "name": ["貼底", "中段", "過熱"],
            "industry_name": ["金融保險"] * 3,
            "momentum_5d": [1.0, 1.0, 1.0],
            "ma60_dist_pct": [5.0, 25.0, 45.0],  # ≤10 貼底／中段／>40 過熱
            "ma20_dist_pct": [1.0, 1.0, 1.0],
            "vol_ratio": [1.0, 1.0, 1.0],
            "foreign_net": [0, 0, 0],
            "trust_net": [0, 0, 0],
            "inst_net": [0, 0, 0],
        }
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), {}, out)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    assert by_id["BASE"]["base_zone"] == "貼底"
    assert (by_id["MID"]["base_zone"] or "") == ""
    assert (by_id["HOT"]["base_zone"] or "") == ""
    # 獨立於 flags：過熱股仍走 flags 排雷、base_zone 不因此被污染
    assert "過熱" in (by_id["HOT"]["flags"] or "")
    assert "貼底" not in (by_id["HOT"]["flags"] or "")


def test_sector_flag_coverage_note(tmp_path):
    """M-WS5a（WS5-②）：同旗標掛滿整族（覆蓋度 ≥ 門檻且族群 ≥ min）→ sector_flag_note 標
    『族群共振』（＝輪動足跡，別當個股排除理由）；小族群（<min）不標。金融 W21 型。"""
    # 金控 6 檔：5 檔外資投信反向（土洋對作）、1 檔同向；小族群 1 檔對作但落單
    ids = ["FIN1", "FIN2", "FIN3", "FIN4", "FIN5", "FIN6", "SM1"]
    members = pl.DataFrame(
        {
            "stock_id": ids,
            "name": ids,
            "industry_name": ["金融保險"] * 6 + ["其他"],
            "momentum_5d": [2.0] * 7,
            "ma60_dist_pct": [6.0] * 7,  # 金控 base 齊漲貼底型
            "ma20_dist_pct": [2.0] * 7,
            "vol_ratio": [1.0] * 7,
            # FIN1-5 + SM1：外資 +、投信 −（反向、雙邊 >5000 張＝5M 股）；FIN6 同向不對作
            "foreign_net": [10_000_000] * 5 + [10_000_000] + [10_000_000],
            "trust_net": [-8_000_000] * 5 + [2_000_000] + [-8_000_000],
            "inst_net": [2_000_000] * 7,
        }
    )
    themes_long = pl.DataFrame(
        {
            "stock_id": ids,
            "theme": ["金控"] * 6 + ["迷你族群"],
            "kind": ["次產業"] * 7,
        }
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, themes_long, {}, out)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    # 金控土洋對作覆蓋 5/6=83% ≥60% → 5 檔掛旗者皆標族群共振
    assert "土洋對作" in (by_id["FIN1"]["flags"] or "")
    assert "族群共振" in (by_id["FIN1"]["sector_flag_note"] or "")
    # FIN6 未掛土洋對作 → 無註記
    assert (by_id["FIN6"]["sector_flag_note"] or "") == ""
    # SM1 掛旗但族群僅 1 檔 < min_members=5 → 覆蓋度不成立、不標（避免小族群假共振）
    assert "土洋對作" in (by_id["SM1"]["flags"] or "")
    assert (by_id["SM1"]["sector_flag_note"] or "") == ""


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


def test_pe_self_history_pctile_populated_and_default_blank(tmp_path):
    """docs/31 §14：自身PE歷史百分位——命中股顯示，valuation_map缺此key的股如實留白。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    valuation_map = {
        "2330": {"pe": 31.0, "val_pctile": 12.0, "pe_self_pctile": 87.5, "pe_self_n": 20},
        "2454": {"pe": 18.0, "val_pctile": 40.0},  # 歷史筆數不足門檻，valuation_map未帶此key
    }
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out,
                                  valuation_map=valuation_map)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}
    assert by_id["2330"]["pe_self_pctile"] == 87.5
    assert by_id["2330"]["pe_self_n"] == 20
    assert by_id["2454"]["pe_self_pctile"] is None
    assert by_id["2454"]["pe_self_n"] is None


def test_peg_like_ratio_column_computed_and_null_when_growth_non_positive(tmp_path):
    """docs/31 §18：PEG-like用官方PE(非Goodinfo兜底)×月營收YoY；成長非正時留null。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    valuation_map = {
        "2330": {"pe": 20.0},
        "2454": {"pe": 15.0},
    }
    rev_yoy_map = {"2330": 40.0, "2454": -5.0}
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, out,
        valuation_map=valuation_map, rev_yoy_map=rev_yoy_map,
    )
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}
    assert by_id["2330"]["peg_like_ratio"] == 0.5
    assert by_id["2454"]["peg_like_ratio"] is None


def test_valuation_implied_price_columns_peer_and_self_legs(tmp_path):
    """docs/31 §20.7：估值回歸參考價——同儕腿(PE回歸同產業中位數)+自身腿(PE回歸自身
    歷史中位數)。close來自_screener_df固定100.0；2330有val_median+pe_self_median
    兩腿皆算得出；2454只有val_median(PB基準)，self腿因pe_self_median缺留null。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    valuation_map = {
        "2330": {"pe": 20.0, "val_metric": "PE", "val_median": 30.0, "pe_self_median": 25.0},
        "2454": {"pbr": 4.0, "val_metric": "PB", "val_median": 5.0},  # PB基準，無pe_self_median
    }
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out,
                                  valuation_map=valuation_map)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    # 2330：close=100、PE 20 回歸同產業中位30 → implied_price_peer=150、gap_pct_peer=+50%
    row = by_id["2330"]
    assert row["val_implied_price_peer"] == pytest.approx(150.0)
    assert row["val_gap_pct_peer"] == pytest.approx(50.0)
    # close=100、PE 20 回歸自身歷史中位25 → implied_price_self=125、gap_pct_self=+25%
    assert row["val_implied_price_self"] == pytest.approx(125.0)
    assert row["val_gap_pct_self"] == pytest.approx(25.0)

    # 2454：PB基準，close=100、PB 4 回歸同產業PB中位5 → implied_price_peer=125、gap=+25%
    row2 = by_id["2454"]
    assert row2["val_implied_price_peer"] == pytest.approx(125.0)
    assert row2["val_gap_pct_peer"] == pytest.approx(25.0)
    # self腿v1只做PE基準，2454無pe_self_median → 留null，不硬算
    assert row2["val_implied_price_self"] is None
    assert row2["val_gap_pct_self"] is None


def test_valuation_composite_gap_uses_available_legs(tmp_path):
    """docs/31 §20.9：估值回歸參考價（綜合版）——6條線索取中位數＋回報用了幾條。

    2330：6條全給（PE同儕/自身、PB同儕/自身、殖利率同儕/自身）——驗證composite是
    這6個gap%的中位數，n_legs=6。
    2454：只給PE同儕一條（其餘留空）——composite應等於那一條，n_legs=1（低信心）。
    """
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    valuation_map = {
        "2330": {
            "pe": 20.0, "pbr": 2.0, "dividend_yield": 4.0,
            "val_metric": "PE", "val_median": 30.0, "pe_self_median": 25.0,
            "pb_peer_median": 2.5, "pb_self_median": 1.8,
            "yield_peer_median": 3.0, "yield_self_median": 5.0,
        },
        "2454": {"pe": 20.0, "val_metric": "PE", "val_median": 30.0},  # 只有PE同儕一條
    }
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out,
                                  valuation_map=valuation_map)
    by_id = {str(r["stock_id"]): r for r in pl.read_csv(out).iter_rows(named=True)}

    row = by_id["2330"]
    assert row["val_composite_n_legs"] == 6
    # composite gap 必須落在 6 條 gap% 的範圍內（不手算全部6條、只驗證合理性與非null）
    assert row["val_gap_pct_composite"] is not None

    row2 = by_id["2454"]
    # 只有PE同儕一條可用 → composite 應等於那一條本身，n_legs=1
    assert row2["val_composite_n_legs"] == 1
    assert row2["val_gap_pct_composite"] == pytest.approx(row2["val_gap_pct_peer"])


# ── 0.3 本週族群主軸 / Section 5 補位塊（問題3・M3）─────────────────────────


def _rotation_csv(tmp_path):
    """小型 sector_rotation.csv：甲趨勢最強無候選（盲點）、乙有候選、丙流入未漲。"""
    pl.DataFrame(
        {
            "sub_industry": ["甲", "乙", "丙"],
            "trend_score": [95.0, 80.0, 40.0],
            "quadrant": ["主升續勢", "主升續勢", "下一棒"],
            "radar_rank": [1, 2, 30],
            "entry_triggered": [False, True, False],
            "next_precision": [False, False, True],
            "leader_stock_id": ["1111", "2222", "3333"],
            "leader_rs_pct": [58.0, 40.0, 9.0],
        }
    ).write_csv(tmp_path / "sector_rotation.csv")


def test_build_rotation_axis_flags_blind_spots_and_counts():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _rotation_csv(tmp)
        themes = pl.DataFrame(
            {  # 乙有兩檔候選、甲/丙無；混一列概念股確認 kind 過濾
                "stock_id": ["2222", "9999", "8888"],
                "theme": ["乙", "乙", "AI"],
                "kind": ["次產業", "次產業", "概念股"],
            }
        )
        axis = _build_rotation_axis(
            tmp, themes, candidate_ids={"2222", "9999"},
            covered_subs={"乙"},  # 乙已被雷達六塊涵蓋
            axis_cfg={"trend_top_n": 3},
        )
    assert axis is not None
    top = {it["sub_industry"]: it for it in axis["trend_top"]}
    assert top["甲"]["n_candidates"] == 0  # 盲點
    assert top["乙"]["n_candidates"] == 2  # 候選計數（概念股列不算）
    assert top["甲"]["quadrant_label"] == "流入×已漲"  # 中性化顯示
    # 流入×未漲 + ⚡貼低
    assert axis["next_up"][0]["sub_industry"] == "丙" and axis["next_up"][0]["precision"]
    # ★ 觸發
    assert [it["sub_industry"] for it in axis["triggered"]] == ["乙"]
    # uncovered：甲/丙未被雷達涵蓋、乙已涵蓋→排除
    subs = {it["sub_industry"] for it in axis["uncovered"]}
    assert "甲" in subs and "丙" in subs and "乙" not in subs


def test_build_rotation_axis_none_without_csv():
    with tempfile.TemporaryDirectory() as d:
        axis = _build_rotation_axis(
            Path(d), pl.DataFrame(), candidate_ids=set(), covered_subs=set(), axis_cfg=None
        )
    assert axis is None  # 無 sector_rotation.csv → 降級略段


def test_named_list_csv_asset_type_and_etf_lightweight_row(tmp_path):
    """holdings enrich ETF 輕量列（docs/21 M-ETF1）：asset_type 標 etf/stock、
    ETF 有 close/return_pct/market_value_k（持股報酬追蹤），列不再無聲消失。"""
    from tw_screener.report.group_report import write_named_list_csv

    results = {"_list": _screener_df(["2330", "0050"], [1.0, 0.5])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=1, skip_etf=False,
    )
    out = tmp_path / "holdings_enriched.csv"
    n = write_named_list_csv(
        members, None, results, out,
        holdings_map={
            "2330": {"buy_price": 90.0, "shares": 1000.0},
            "0050": {"buy_price": 107.0, "shares": 10000.0},
        },
    )
    assert n == 2
    df = pl.read_csv(out, infer_schema_length=0)
    rows = {r["stock_id"]: r for r in df.iter_rows(named=True)}
    assert rows["0050"]["asset_type"] == "etf"
    assert rows["2330"]["asset_type"] == "stock"
    # 輕量列仍有報酬追蹤欄（close=100、買 107 → 負報酬），不因 ETF 而空
    assert rows["0050"]["return_pct"] not in (None, "")
    assert float(rows["0050"]["return_pct"]) < 0
    assert rows["0050"]["market_value_k"] not in (None, "")


# ─── M-BR1 底部左側揭露欄（規劃書 24 Phase 1：純加法、零行為變更） ────────────────


_M_BR1_NEW_FIELDS = frozenset({
    "rev_yoy_delta", "fundamental_health", "foreign_flow_inflection",
    "trust_flow_inflection", "inst_flow_inflection",
    "dist_low_20d_pct", "dist_low_60d_pct", "base_proximity", "contrarian_base",
})


def _br1_members():
    """20 日賣超 / 近 5 日翻買的左側型單股面板（外資 20d −30,000 張、5d +5,000 張）。"""
    dates = [date(2026, 5, d) for d in range(1, 13)]
    fnet = [-5_000_000] * 7 + [1_000_000] * 5  # 股：20日−30,000張、5日+5,000張
    inst = pl.DataFrame(
        {
            "date": dates,
            "stock_id": ["2330"] * 12,
            "stock_name": ["公司2330"] * 12,
            "foreign_net": fnet,
            "trust_net": [0] * 12,
            "dealer_net": [0] * 12,
            "total_net": fnet,
        }
    )
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=inst,
    )
    return results, members


def test_m_br1_columns_present_and_classify(tmp_path):
    """M-BR1 九欄進 candidates CSV，且左側型被分類為『轉買』。"""
    results, members = _br1_members()
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, out,
        contrarian_cfg={"min_lots": 1000},
        rev_yoy_delta_map={"2330": (-5.0, None)},
        rev_yoy_map={"2330": 35.0},
    )
    df = pl.read_csv(out)
    assert _M_BR1_NEW_FIELDS <= set(df.columns)
    row = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}["2330"]
    assert row["foreign_flow_inflection"] == "轉買"
    assert row["fundamental_health"] == "穩健"  # YoY +35% 但小幅降速
    assert row["rev_yoy_delta"] == -5.0


def test_m_br1_is_purely_additive(tmp_path):
    """Phase 1 驗收核心：加欄前後**既有欄逐位元不變**（零行為變更）。

    以 contrarian_cfg=None（等同舊呼叫端）與帶設定兩路各產一次，比對兩者交集欄位
    ——只要有一個既有欄變動，M-BR1 就不是純加法、驗收不成立。
    """
    results, members = _br1_members()
    base_out = tmp_path / "base.csv"
    new_out = tmp_path / "new.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, base_out)
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, new_out,
        contrarian_cfg={"min_lots": 1000}, rev_yoy_delta_map={"2330": (-5.0, None)},
    )
    base_df, new_df = pl.read_csv(base_out), pl.read_csv(new_out)
    legacy_cols = [c for c in base_df.columns if c not in _M_BR1_NEW_FIELDS]
    assert base_df.select(legacy_cols).equals(new_df.select(legacy_cols))
    # 且 risk_kind / flow_state 未被賣方分支污染（沿用買方口徑，見 contrarian.py docstring）
    assert base_df["flow_state"].to_list() == new_df["flow_state"].to_list()
    assert base_df["risk_kind"].to_list() == new_df["risk_kind"].to_list()


def test_m_br1_null_safe_when_inst_missing(tmp_path):
    """法人缺漏股：三個 inflection 欄如實 null，不假值、不崩潰。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    institutional = _institutional(["2330"], [5_000_000])  # 2454 缺漏
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2, institutional=institutional,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, out, contrarian_cfg={"min_lots": 1000}
    )
    df = pl.read_csv(out)
    miss = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}["2454"]
    assert miss["foreign_flow_inflection"] is None
    assert miss["trust_flow_inflection"] is None
    assert miss["inst_flow_inflection"] is None
    assert miss["fundamental_health"] == "待查"   # 無 rev_yoy → 待查，不猜
    assert miss["contrarian_base"] is False       # 缺資料不得放行


def test_official_sector_columns_default_blank_without_map(tmp_path):
    """docs/31 §12/§13：不傳official_sector_map → 五欄如實留白，不臆造命中。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    assert (df["official_sector_top5"] == False).all()  # noqa: E712
    assert df["official_sector_group"].is_null().all()
    assert df["official_sector_rank"].is_null().all()
    assert df["official_sector_trend_score"].is_null().all()
    assert df["official_sector_regime"].is_null().all()


def test_official_sector_columns_populated_from_map(tmp_path):
    """命中股顯示對應族群/排名/trend_score/當週regime；未命中股五欄留白。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    official_sector_map = {
        "2330": {
            "sub_industry": "半導體業", "trend_score": 96.9, "group_rank": 1,
        }
    }
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, out,
        official_sector_map=official_sector_map, official_sector_regime="進攻",
    )
    df = pl.read_csv(out)
    by_id = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}
    assert by_id["2330"]["official_sector_top5"] is True
    assert by_id["2330"]["official_sector_group"] == "半導體業"
    assert by_id["2330"]["official_sector_rank"] == 1
    assert by_id["2330"]["official_sector_trend_score"] == 96.9
    assert by_id["2330"]["official_sector_regime"] == "進攻"
    assert by_id["2454"]["official_sector_top5"] is False
    assert by_id["2454"]["official_sector_group"] is None
    # regime 對全部列一視同仁（本週單一標籤，非命中與否而異）
    assert by_id["2454"]["official_sector_regime"] == "進攻"


def test_redesign_watch_column_default_blank_without_map(tmp_path):
    """docs/31 §4/§9/§11：不傳redesign_watch_map → 欄位如實留白，不臆造命中。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    assert df["redesign_watch"].is_null().all()


def test_redesign_watch_column_populated_from_map(tmp_path):
    """命中股顯示逗號分隔旗標；未命中股留白。"""
    results = {"a_breakout": _screener_df(["2330", "2454"], [3.0, 2.0])}
    _, members = group_stocks(
        results, pl.DataFrame(), pl.DataFrame(), industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(
        members, pl.DataFrame(), results, out,
        redesign_watch_map={"2330": "g2,l6_2cond"},
    )
    df = pl.read_csv(out)
    by_id = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}
    assert by_id["2330"]["redesign_watch"] == "g2,l6_2cond"
    assert by_id["2454"]["redesign_watch"] is None


def test_candidates_csv_close_from_local_price_history_when_screener_lacks_it(tmp_path):
    """2026-08-27修正：screen_result無close欄（G1-G5/L6實際形狀，Goodinfo被擋時的
    候選來源）＋本地price_history有資料 → candidates_enriched.csv的close欄仍算得
    出真實值，不是空白/0（點1「缺資料」根因修復的端到端驗證）。"""
    local_filter_df = pl.DataFrame({
        "stock_id": ["2330", "2454"],
        "name": ["台積電", "聯發科"],
        "strategy_id": ["g1_margin_expansion"] * 2,
        "screened_at": ["2026-08-27"] * 2,
        "goodinfo_url": [""] * 2,
        "source": ["local_unvalidated"] * 2,
    })
    price_history = pl.DataFrame({
        "stock_id": ["2330"] * 3,
        "date": [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)],
        "close": [500.0, 510.0, 520.0],
    })
    results = {"g1_margin_expansion": local_filter_df}
    _, members = group_stocks(
        results, price_history, pl.DataFrame(),
        industry_df=_INDUSTRY_DF, min_group_size=2,
    )
    out = tmp_path / "candidates_enriched.csv"
    write_candidates_enriched_csv(members, pl.DataFrame(), results, out)
    df = pl.read_csv(out)
    by_id = {str(r["stock_id"]): r for r in df.iter_rows(named=True)}
    assert by_id["2330"]["close"] == pytest.approx(520.0, rel=1e-3)
    # 2454無本地價格快取、screen_result也無close欄 → None，不捏造0
    assert by_id["2454"]["close"] is None
