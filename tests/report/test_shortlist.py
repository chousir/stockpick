"""M-Pick1 機器排序（report/shortlist.py＋shortlist_runner）測試。

小 Polars DataFrame 驗 gate 各原因／排序鍵優先序／上限／來源優先序／承接停損／說明欄紅線；
runner 在 tmp_path 上驗缺輸入不寫檔、停用跳過、happy path 落檔。不碰真 reports/。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from tw_screener.backtest.picks_outcome import parse_stop_price
from tw_screener.report.shortlist import (
    SHORTLIST_COLUMNS,
    STOP_BASIS_BELOW,
    STOP_BASIS_LOW60,
    STOP_BASIS_MA60,
    ShortlistConfig,
    build_pool,
    build_shortlist,
    order_members_by_concepts,
    sector_trend_table,
)
from tw_screener.report.shortlist_runner import run_shortlist

WEEK = "2026-W39"
DATA_DATE = date(2026, 9, 24)


def _row(stock_id: str, **kw: Any) -> dict[str, Any]:
    """一列 enriched（預設：過全部 gate、MA60 停損、距季線 +7%＝偏好帶內）。"""
    base: dict[str, Any] = {
        "stock_id": stock_id,
        "name": f"股{stock_id}",
        "asset_type": "stock",
        "industry": "電子零組件業",
        "theme": "",
        "strategy": "G4",
        "flags": "",
        "fundamental_health": "穩健",
        "close": 107.0,
        "ma20_price": 104.0,
        "ma60_price": 100.0,
        "ma60_dist_pct": 7.0,
        "low_20d": 98.0,
        "low_60d": 90.0,
        "amount_million": 500.0,
        "pe_ratio": 15.0,
        "val_gap_pct_composite": None,
        "deep_value_growth": False,
        "contrarian_ready": False,
    }
    base.update(kw)
    return base


def _df(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None)


def _members(pairs: list[tuple[str, str]]) -> pl.DataFrame:
    """[(stock_id, sub_industry)] → universe long table（列序＝優先序）。"""
    return pl.DataFrame(
        {"sub_industry": [s for _, s in pairs], "stock_id": [sid for sid, _ in pairs]}
    )


def _rotation(scores: dict[str, float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "sub_industry": list(scores),
            "trend_score": list(scores.values()),
            "date": ["2026-09-24"] * len(scores),
        },
        schema_overrides={"trend_score": pl.Float64},
    )


# 5 個次產業、k=5 → bucket＝名次（S1..S2 可入選，S3 起 bucket gate）
ROT5 = {"S1": 90.0, "S2": 80.0, "S3": 70.0, "S4": 60.0, "S5": 50.0}


def _run(
    candidates: list[dict[str, Any]],
    members: list[tuple[str, str]],
    rotation: dict[str, float | None] = ROT5,
    cfg: ShortlistConfig | None = None,
    watchlist: list[dict[str, Any]] | None = None,
    holdings: list[dict[str, Any]] | None = None,
) -> pl.DataFrame:
    cfg = cfg or ShortlistConfig()
    pool = build_pool(
        _df(candidates),
        _df(watchlist) if watchlist else None,
        _df(holdings) if holdings else None,
        cfg,
    )
    return build_shortlist(
        pool, _members(members), _rotation(rotation), cfg, week=WEEK, data_date=DATA_DATE
    )


def _by_id(out: pl.DataFrame) -> dict[str, dict[str, Any]]:
    return {r["stock_id"]: r for r in out.iter_rows(named=True)}


# ── gate ──────────────────────────────────────────────────────────────────────


def test_each_gate_reason():
    cands = [
        _row("OK1"),
        _row("E1", asset_type="ETF"),                       # 大小寫不拘
        _row("P1", price_discontinuity=True),
        _row("P3", flags="價格不連續(2026-09-24 +19.2%)"),   # flags 內標記也算
        _row("F1", flags="高PE;強漲法人賣"),
        _row("F2", flags="低流動"),
        _row("L1", amount_million=50.0),                    # flags 未標但成交額 < 100 百萬
        _row("X0", ma60_dist_pct=None),
        _row("X1", ma60_dist_pct=-1.0),
        _row("X2", ma60_dist_pct=16.0),
        _row("N1"),                                          # 無次產業標籤
        _row("N2"),                                          # 次產業不在輪動表
        _row("B3"),                                          # 第 3 桶
        _row("TY", flags="土洋對作"),                        # 刻意不列的旗標 → 照常入選
    ]
    special = {"N1", "N2", "B3", "TY"}
    members = [(r["stock_id"], "S1") for r in cands if r["stock_id"] not in special]
    members += [("N2", "不在表"), ("B3", "S3"), ("TY", "S2")]
    out = _by_id(
        _run(cands, members, cfg=ShortlistConfig(max_per_sub_industry=0, max_per_cluster=0))
    )
    expected = {
        "E1": "etf",
        "P1": "price_discontinuity",
        "P3": "price_discontinuity",
        "F1": "flag:強漲法人賣",
        "F2": "flag:低流動",
        "L1": "flag:低流動",
        "X0": "ext_unknown",
        "X1": "ext_below",
        "X2": "ext_above",
        "N1": "no_trend_score",
        "N2": "no_trend_score",
        "B3": "bucket",
    }
    for sid, reason in expected.items():
        assert out[sid]["gate_reason"] == reason, sid
        assert out[sid]["tier"] == "gated" and out[sid]["rank"] is None, sid
    assert out["OK1"]["tier"] == "top" and out["OK1"]["gate_reason"] is None
    assert out["TY"]["tier"] == "top"


def test_price_discontinuity_read_as_string_column():
    """同欄在某些週被 CSV 推斷成字串（"True"/"true"）——一樣要擋。"""
    cands = [
        _row("A", price_discontinuity="True"),
        _row("B", price_discontinuity="true"),
        _row("C", price_discontinuity=None),
    ]
    out = _by_id(_run(cands, [("A", "S1"), ("B", "S2"), ("C", "S1")]))
    assert out["A"]["gate_reason"] == "price_discontinuity"
    assert out["B"]["gate_reason"] == "price_discontinuity"
    assert out["C"]["tier"] == "top"


def test_gate_order_first_hit_wins():
    """ETF 又低流動又無趨勢分 → 取順序最前的 etf。"""
    out = _by_id(_run([_row("E", asset_type="etf", amount_million=1.0)], []))
    assert out["E"]["gate_reason"] == "etf"


# ── 族群趨勢 ──────────────────────────────────────────────────────────────────


def test_bucket_formula_ceil_rank_k_over_n():
    rot = _rotation({f"T{i:02d}": 100.0 - i for i in range(1, 42)})  # n=41，名次＝i
    table = _by_id_sub(sector_trend_table(rot, 5))
    assert table["T08"]["trend_bucket"] == 1   # ceil(40/41)
    assert table["T09"]["trend_bucket"] == 2   # ceil(45/41)
    assert table["T16"]["trend_bucket"] == 2   # ceil(80/41)
    assert table["T17"]["trend_bucket"] == 3   # ceil(85/41)
    assert table["T41"]["trend_bucket"] == 5
    assert {r["trend_n"] for r in table.values()} == {41}


def _by_id_sub(df: pl.DataFrame) -> dict[str, dict[str, Any]]:
    return {r["sub_industry"]: r for r in df.iter_rows(named=True)}


def test_trend_null_subindustry_not_ranked_and_not_counted():
    rot = {"S1": 90.0, "S2": None, "S3": 70.0, "S4": 60.0, "S5": 50.0, "S6": 40.0}
    table = _by_id_sub(sector_trend_table(_rotation(rot), 5))
    assert "S2" not in table and table["S1"]["trend_n"] == 5
    out = _by_id(_run([_row("A"), _row("B")], [("A", "S1"), ("B", "S2")], rotation=rot))
    assert out["A"]["tier"] == "top"
    assert out["B"]["gate_reason"] == "no_trend_score" and out["B"]["trend_rank"] is None


def test_tied_trend_scores_share_rank_and_bucket():
    rot = _rotation({"A": 90.0, "B": 81.1, "C": 81.1, "D": 70.0})
    table = _by_id_sub(sector_trend_table(rot, 5))
    assert table["B"]["trend_rank"] == table["C"]["trend_rank"] == 2
    assert table["B"]["trend_bucket"] == table["C"]["trend_bucket"]
    assert table["D"]["trend_rank"] == 4


def test_multi_label_takes_first_member_row():
    out = _by_id(_run([_row("M")], [("M", "S2"), ("M", "S1")]))
    assert out["M"]["sub_industry"] == "S2"


def test_member_order_follows_concepts_yaml_not_alphabet():
    """universe.csv 已按字母序排 → 用 concepts.yaml 手標順序重排後，第一個＝主身分。"""
    members = _members([("3189", "IC生產製造"), ("3189", "PCB"), ("2330", "晶圓代工")])
    themes = pl.DataFrame(
        {
            "stock_id": ["3189", "3189", "3189", "2330"],
            "theme": ["PCB", "5G", "IC生產製造", "晶圓代工"],
            "kind": ["次產業", "概念股", "次產業", "次產業"],
        }
    )
    ordered = order_members_by_concepts(members, themes)
    first = ordered.unique(subset=["stock_id"], keep="first", maintain_order=True)
    assert dict(zip(first["stock_id"], first["sub_industry"], strict=True)) == {
        "3189": "PCB",
        "2330": "晶圓代工",
    }


# ── 排序與上限 ────────────────────────────────────────────────────────────────


def test_bucket_beats_band_distance():
    cands = [
        _row("A", ma60_dist_pct=7.0),    # S2（第 2 桶）、帶內
        _row("B", ma60_dist_pct=12.0),   # S1（第 1 桶）、帶外 2pp
    ]
    out = _by_id(_run(cands, [("A", "S2"), ("B", "S1")]))
    assert out["B"]["rank"] == 1 and out["A"]["rank"] == 2


def test_band_beats_trend_score_within_bucket():
    rot = {f"T{i}": 100.0 - 5 * i for i in range(10)}  # n=10、k=5：名次 1–2 同為第 1 桶
    cands = [
        _row("C", ma60_dist_pct=12.0),   # T0（100 分）、帶外
        _row("D", ma60_dist_pct=7.0),    # T1（95 分）、帶內
        _row("E", ma60_dist_pct=7.0),    # T2（90 分、第 2 桶）
    ]
    out = _by_id(_run(cands, [("C", "T0"), ("D", "T1"), ("E", "T2")], rotation=rot))
    assert (out["D"]["rank"], out["C"]["rank"], out["E"]["rank"]) == (1, 2, 3)


def test_trend_score_then_amount_then_stock_id_tiebreak_deterministic():
    rot = {"S1": 90.0, "S2": 90.0, "S3": 85.0, "S4": 90.0, **{f"F{i}": 50.0 - i for i in range(6)}}
    cands = [
        _row("Z1", ma60_dist_pct=6.0, amount_million=300.0),   # S1
        _row("Y2", ma60_dist_pct=8.0, amount_million=900.0),   # S2：同分同桶、成交額大
        _row("X3", ma60_dist_pct=9.0, amount_million=5000.0),  # S3：分數低
        _row("B4", ma60_dist_pct=6.0, amount_million=300.0),   # S4：與 Z1 全同 → stock_id
    ]
    members = [("Z1", "S1"), ("Y2", "S2"), ("X3", "S3"), ("B4", "S4")]
    first = _run(cands, members, rotation=rot)
    ranks = {r["stock_id"]: r["rank"] for r in first.iter_rows(named=True)}
    assert ranks == {"Y2": 1, "B4": 2, "Z1": 3, "X3": 4}
    # 輸入順序打亂 → 輸出逐位元組相同
    second = _run(list(reversed(cands)), list(reversed(members)), rotation=rot)
    assert first.equals(second)


def test_sub_industry_cap_one():
    cands = [_row("A", ma60_dist_pct=7.0), _row("B", ma60_dist_pct=8.0), _row("C")]
    out = _by_id(_run(cands, [("A", "S1"), ("B", "S1"), ("C", "S2")]))
    assert out["A"]["rank"] == 1 and out["C"]["rank"] == 2
    assert out["B"]["tier"] == "capped" and out["B"]["gate_reason"] == "cap_sub_industry"


def test_factor_cluster_cap():
    clusters = ({"name": "利率敏感", "labels": ["銀行", "壽險", "建材營造"], "max_count": 2},)
    cfg = ShortlistConfig(factor_clusters=clusters, max_per_cluster=2)
    rot = {"銀行": 90.0, "壽險": 89.0, "建材營造": 88.0, **{f"F{i}": 50.0 - i for i in range(7)}}
    cands = [
        _row("BK", theme="銀行"),
        _row("LF", theme="壽險"),
        _row("CN", theme="", industry="建材營造"),   # industry 標籤也算入簇
    ]
    members = [("BK", "銀行"), ("LF", "壽險"), ("CN", "建材營造")]
    out = _by_id(_run(cands, members, rotation=rot, cfg=cfg))
    assert out["BK"]["rank"] == 1 and out["LF"]["rank"] == 2
    assert out["CN"]["tier"] == "capped" and out["CN"]["gate_reason"] == "cap_cluster"


def test_top_alt_split_and_beyond_n():
    rot = {f"S{i}": 100.0 - i for i in range(12)}
    cfg = ShortlistConfig(top_n=2, alt_n=1, max_bucket_for_top=5)
    cands = [_row(f"A{i}", ma60_dist_pct=5.0 + 0.1 * i) for i in range(4)]
    out = _by_id(_run(cands, [(f"A{i}", f"S{i}") for i in range(4)], rotation=rot, cfg=cfg))
    assert [out[f"A{i}"]["tier"] for i in range(4)] == ["top", "top", "alt", "capped"]
    assert out["A3"]["gate_reason"] == "beyond_n" and out["A3"]["rank"] is None


def test_fewer_eligible_than_n_lists_fewer_without_backfilling_weak_buckets():
    cands = [_row("A"), _row("B"), _row("W1"), _row("W2")]
    members = [("A", "S1"), ("B", "S2"), ("W1", "S3"), ("W2", "S4")]
    out = _run(cands, members)
    ranked = out.filter(pl.col("tier").is_in(["top", "alt"]))
    assert ranked["stock_id"].to_list() == ["A", "B"]
    assert ranked["rank"].to_list() == [1, 2]
    assert out.filter(pl.col("tier") == "alt").is_empty()


# ── 來源 ──────────────────────────────────────────────────────────────────────


def test_holding_is_held_not_ranked_but_gets_stop():
    out = _by_id(
        _run([_row("C")], [("C", "S1"), ("H", "S2")], holdings=[_row("H", name="持股")])
    )
    assert out["H"]["source"] == "holding"
    assert out["H"]["tier"] == "held" and out["H"]["gate_reason"] == "held"
    assert out["H"]["rank"] is None
    assert parse_stop_price(out["H"]["stop_text"]) == out["H"]["stop_price"]
    # include_holdings=true → 持股也進排序
    out2 = _by_id(
        _run(
            [_row("C")], [("C", "S1"), ("H", "S2")],
            cfg=ShortlistConfig(include_holdings=True), holdings=[_row("H")],
        )
    )
    assert out2["H"]["tier"] == "top"


def test_watchlist_stock_can_be_selected():
    out = _by_id(_run([_row("C")], [("C", "S1"), ("W", "S2")], watchlist=[_row("W")]))
    assert out["W"]["source"] == "watchlist" and out["W"]["tier"] == "top"
    out_off = _by_id(
        _run(
            [_row("C")], [("C", "S1"), ("W", "S2")],
            cfg=ShortlistConfig(include_watchlist=False), watchlist=[_row("W")],
        )
    )
    assert "W" not in out_off


def test_duplicate_stock_candidate_takes_precedence():
    out = _run(
        [_row("D", name="候選版")],
        [("D", "S1")],
        watchlist=[_row("D", name="觀察版")],
        holdings=[_row("D", name="持股版")],
    )
    assert out.height == 1
    row = out.row(0, named=True)
    assert (row["source"], row["name"], row["tier"]) == ("candidate", "候選版", "top")


# ── 承接區／停損 ──────────────────────────────────────────────────────────────


def test_entry_zone_bounds_and_labels():
    cands = [
        _row("A", close=107.0, ma20_price=104.0, ma60_price=100.0),                 # MA60<MA20<收盤
        _row("B", close=107.0, ma20_price=98.0, ma60_price=100.0, ma60_dist_pct=7.0),  # MA20<MA60
        _row("C", close=103.0, ma20_price=105.0, ma60_price=97.0, ma60_dist_pct=6.2),  # 收盤<MA20
    ]
    out = _by_id(_run(cands, [("A", "S1"), ("B", "S2"), ("C", "S1")]))
    assert (out["A"]["entry_low"], out["A"]["entry_high"]) == (100.0, 104.0)
    assert out["A"]["entry_text"] == "100.00–104.00（MA60–MA20）"
    assert out["B"]["entry_text"] == "98.00–100.00（MA20–MA60）"
    assert (out["C"]["entry_low"], out["C"]["entry_high"]) == (97.0, 103.0)
    assert out["C"]["entry_text"] == "97.00–103.00（MA60–收盤）"


def test_stop_ma60_default_and_low60_when_tangled_or_hugging():
    cands = [
        _row("N", ma20_price=104.0, ma60_price=100.0, ma60_dist_pct=7.0, low_60d=90.0),
        _row("T", ma20_price=101.5, ma60_price=100.0, ma60_dist_pct=7.0, low_60d=88.8),  # 糾結 1.5%
        _row("H", close=101.5, ma20_price=106.0, ma60_price=100.0, ma60_dist_pct=1.5,
             low_60d=87.25),                                                            # 貼 MA60
    ]
    out = _by_id(
        _run(cands, [("N", "S1"), ("T", "S2"), ("H", "S1")],
             cfg=ShortlistConfig(ext_min_pct=0.0, max_per_sub_industry=0))
    )
    assert (out["N"]["stop_price"], out["N"]["stop_basis"]) == (100.0, STOP_BASIS_MA60)
    assert out["N"]["stop_text"] == "收盤跌破 100.00（MA60）、隔日未收復出場"
    assert (out["T"]["stop_price"], out["T"]["stop_basis"]) == (88.8, STOP_BASIS_LOW60)
    assert (out["H"]["stop_price"], out["H"]["stop_basis"]) == (87.25, STOP_BASIS_LOW60)


def test_stop_basis_says_below_ma60_not_tangled_when_price_under_ma60():
    """已跌破 MA60 的列（持股/gated）停損改 low_60d，依據寫「已跌破 MA60」而非「均線糾結」
    （docs/11 持股條件價）。"""
    held = _row("B", close=95.0, ma20_price=110.0, ma60_price=100.0, ma60_dist_pct=-5.0,
                low_60d=90.0)
    out = _by_id(_run([_row("C")], [("C", "S1"), ("B", "S2")], holdings=[held]))
    assert (out["B"]["stop_price"], out["B"]["stop_basis"]) == (90.0, STOP_BASIS_BELOW)
    assert out["B"]["stop_text"] == "收盤跌破 90.00（low_60d（已跌破 MA60））、隔日未收復出場"
    assert parse_stop_price(out["B"]["stop_text"]) == 90.0


def test_stop_text_parses_back_to_stop_price():
    cands = [
        _row("A", ma60_price=1234.5678, ma20_price=1300.0, close=1330.0, ma60_dist_pct=7.7),
        _row("B", ma20_price=100.9, ma60_price=100.0, low_60d=82.5),
        _row("C", ma60_price=24.975, ma20_price=27.29, close=27.75, ma60_dist_pct=11.1),
    ]
    out = _run(cands, [("A", "S1"), ("B", "S2"), ("C", "S2")])
    rows = out.filter(pl.col("stop_price").is_not_null())
    assert rows.height == 3
    for r in rows.iter_rows(named=True):
        assert parse_stop_price(r["stop_text"]) == r["stop_price"], r["stop_text"]


# ── 說明欄 ────────────────────────────────────────────────────────────────────


def test_evidence_bear_hints_and_unvalidated_notes():
    cands = [
        _row(
            "A", ma60_dist_pct=12.0, pe_ratio=45.0, fundamental_health="減速",
            strategy="G1+G4", val_gap_pct_composite=18.26, deep_value_growth=True,
            contrarian_ready=True, low_60d=80.0, ma20_price=101.0, close=112.0,
        ),
    ]
    row = _run(cands, [("A", "S1")]).row(0, named=True)
    assert row["evidence"] == "族群趨勢分 90.0（#1/5・第1桶）；距季線 +12.0%"
    hints = row["bear_hints"].split("；")
    assert hints[0] == "族群層訊號、個股層未驗證"
    assert "高PE 45.0（>30）" in hints
    assert "月營收減速（fundamental_health）" in hints
    assert "距季線 +12.0% 在偏好帶 5–10% 外" in hints
    assert any(h.startswith("停損距現價 28.6%") for h in hints)  # low_60d 80 vs 收盤 112
    assert row["unvalidated_notes"] == (
        "策略命中 G1+G4；估值缺口 +18.3%（綜合）；deep_value_growth；contrarian_ready（M-BR1 左側）"
    )
    in_band = _run([_row("B")], [("B", "S1")]).row(0, named=True)
    assert in_band["evidence"].endswith("距季線 +7.0%（偏好帶內）")
    assert in_band["bear_hints"] == "族群層訊號、個股層未驗證"


def test_notes_never_mention_institutional_flow():
    """個股層法人流否證（docs/19/20/22 §4）→ 說明欄不得出現外資/投信/自營/法人字樣或數字。"""
    flow_cols = {
        "foreign_net_5d_lots": 5000.0, "trust_net_5d_lots": -3000.0,
        "inst_net_lots": 1234.0, "flow_state": "外資加速", "near_share_5d_pct": 40.0,
        "foreign_flow_inflection": "轉買",
    }
    cands = [
        _row("A", flags="土洋對作", **flow_cols),
        _row("B", flags="強漲法人賣", **flow_cols),
        _row("C", ma60_dist_pct=-3.0, contrarian_ready=True, **flow_cols),
    ]
    out = _run(cands, [("A", "S1"), ("B", "S2"), ("C", "S2")])
    text_cols = ["evidence", "bear_hints", "unvalidated_notes", "entry_text", "stop_text"]
    for r in out.select(text_cols).iter_rows(named=True):
        for col, text in r.items():
            for word in ("外資", "投信", "自營", "法人", "5000", "3000", "1234"):
                assert word not in (text or ""), (col, text)


def test_output_columns_and_row_order():
    cands = [_row("A"), _row("B", asset_type="etf"), _row("C", ma60_dist_pct=8.0)]
    out = _run(cands, [("A", "S1"), ("B", "S1"), ("C", "S2")], holdings=[_row("H")])
    assert tuple(out.columns) == SHORTLIST_COLUMNS
    assert out["tier"].to_list() == ["top", "top", "gated", "held"]
    row = out.row(0, named=True)
    assert (row["week"], row["data_date"], row["rotation_date"]) == (
        WEEK, DATA_DATE, date(2026, 9, 24)
    )


def test_config_from_settings_reads_external_thresholds():
    cfg = ShortlistConfig.from_settings(
        {
            "picks": {"core_ext_ma60_max_pct": 12.0, "shortlist": {"top_n": 3, "enabled": False}},
            "propicks_flags": {"low_liquidity_amount": 250},
            "portfolio": {"factor_clusters": [{"name": "X", "labels": ["銀行"]}]},
        }
    )
    assert (cfg.top_n, cfg.enabled, cfg.ext_max_pct, cfg.low_liquidity_amount) == (
        3, False, 12.0, 250.0
    )
    assert cfg.factor_clusters[0]["name"] == "X" and cfg.alt_n == 5


# ── runner（tmp_path IO）──────────────────────────────────────────────────────


def _setup_runner(tmp_path: Path, shortlist_cfg: dict[str, Any] | None = None) -> tuple[Path, Path]:
    reports = tmp_path / "reports"
    week_dir = reports / WEEK
    week_dir.mkdir(parents=True)
    snaps = tmp_path / "snapshots"
    (snaps / WEEK).mkdir(parents=True)
    _members([("A", "S1"), ("B", "S2")]).write_csv(snaps / WEEK / "universe.csv")
    pl.DataFrame({"stock_id": ["A"], "screened_at": ["2026-09-24"]}).write_csv(
        week_dir / "screen_result_g4_yoy_divergence.csv"
    )
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        yaml.safe_dump(
            {
                "paths": {"reports_dir": str(reports)},
                "snapshots": {"dir": str(snaps)},
                "picks": {"core_ext_ma60_max_pct": 15.0, "shortlist": shortlist_cfg or {}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return settings, week_dir


def test_runner_missing_required_input_writes_nothing(tmp_path):
    settings, week_dir = _setup_runner(tmp_path)
    _df([_row("A")]).write_csv(week_dir / "candidates_enriched.csv")  # 缺 sector_rotation.csv
    assert run_shortlist(settings, WEEK) == 1
    assert not (week_dir / "shortlist.csv").exists()


def test_runner_disabled_skips(tmp_path):
    settings, week_dir = _setup_runner(tmp_path, {"enabled": False})
    _df([_row("A")]).write_csv(week_dir / "candidates_enriched.csv")
    _rotation(ROT5).write_csv(week_dir / "sector_rotation.csv")
    assert run_shortlist(settings, WEEK) == 0
    assert not (week_dir / "shortlist.csv").exists()


def test_runner_happy_path_latest_week_default(tmp_path):
    settings, week_dir = _setup_runner(tmp_path)
    (week_dir.parent / "2026-W38").mkdir()  # 較舊的週 → 預設取最新（W39）
    _df([_row("A"), _row("B", ma60_dist_pct=8.0)]).write_csv(week_dir / "candidates_enriched.csv")
    _rotation(ROT5).write_csv(week_dir / "sector_rotation.csv")
    assert run_shortlist(settings, None) == 0
    out = pl.read_csv(week_dir / "shortlist.csv", schema_overrides={"stock_id": pl.Utf8})
    assert tuple(out.columns) == SHORTLIST_COLUMNS
    assert out["stock_id"].to_list() == ["A", "B"] and out["rank"].to_list() == [1, 2]
    assert out["data_date"][0] == "2026-09-24"
