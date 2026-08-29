"""個股相對估值測試（C1）：官方 PE 主、PB 補虧損股、次產業相對位階、多標籤平均、覆蓋 meta。

全離線合成資料、手算對拍。守 docs/13 §C1：缺值不補零、不給負值；虧損股退用官方 PBR。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from tw_screener.analysis.valuation import (
    build_valuation,
    compute_self_history_median,
    compute_self_history_pctile,
    compute_subind_relative,
    compute_valuation_meta,
    implied_price_from_ratio_median,
    implied_price_gap_pct,
    market_cap_billion,
    peg_like_ratio,
)


def _ratios(rows: list[dict]) -> pl.DataFrame:
    """合成官方估值比表；未給的欄填合理預設（market=上市、pbr=1.0、殖利率 None）。"""
    schema = {
        "stock_id": pl.Utf8,
        "market": pl.Utf8,
        "pe": pl.Float64,
        "pbr": pl.Float64,
        "dividend_yield": pl.Float64,
    }
    full = [
        {
            "stock_id": r["stock_id"],
            "market": r.get("market", "上市"),
            "pe": r.get("pe"),
            "pbr": r.get("pbr", 1.0),
            "dividend_yield": r.get("dividend_yield"),
        }
        for r in rows
    ]
    return pl.DataFrame(full, schema=schema)


def _membership(pairs: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"sub_industry": [p[0] for p in pairs], "stock_id": [p[1] for p in pairs]}
    )


def test_pe_primary_relative_ranking() -> None:
    # 次產業 A 五檔全獲利（過 min_peers=5）：PE 10,5,2.5,2,1.25 → 中位 2.5
    rows = [{"stock_id": s, "pe": pe} for s, pe in
            (("a1", 10.0), ("a2", 5.0), ("a3", 2.5), ("a4", 2.0), ("a5", 1.25))]
    membership = _membership([("A", s) for s in ("a1", "a2", "a3", "a4", "a5")])
    out = build_valuation(_ratios(rows), membership, min_peers=5, cheap_pctile=30.0)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}

    assert by["a1"]["val_metric"] == "PE"
    assert by["a1"]["val_median"] == 2.5
    # a5 最便宜（PE 1.25）→ 升冪百分位 0；a1 最貴 → 100
    assert by["a5"]["val_pctile"] == 0.0
    assert by["a1"]["val_pctile"] == 100.0
    # 相對%：a5 (1.25/2.5−1)*100 = −50
    assert by["a5"]["val_vs_subind_pct"] == -50.0
    # 百分位 ≤30 標相對便宜
    assert by["a5"]["cheap_flag"] == "相對便宜"
    assert by["a1"]["cheap_flag"] == ""


def test_pb_fills_loss_maker() -> None:
    # 次產業 B 六檔：b1 虧損（無 PE）、b2..b6 獲利 PE=20；PB：b1=0.6 其餘 1..5
    rows = [{"stock_id": "b1", "pe": None, "pbr": 0.6}]
    rows += [{"stock_id": s, "pe": 20.0, "pbr": p}
             for s, p in (("b2", 1.0), ("b3", 2.0), ("b4", 3.0), ("b5", 4.0), ("b6", 5.0))]
    membership = _membership([("B", f"b{i}") for i in range(1, 7)])
    out = build_valuation(_ratios(rows), membership, min_peers=5, cheap_pctile=30.0)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}

    # b1 無正 PE → pe_status 無本益比、退用 PB（val_metric=PB）
    assert by["b1"]["pe_status"] == "無本益比"
    assert by["b1"]["pe"] is None
    assert by["b1"]["val_metric"] == "PB"
    # PB 中位 = median(0.6,1,2,3,4,5)=2.5；b1 PB 0.6 最低 → 百分位 0 → 相對便宜(PB)
    assert by["b1"]["val_pctile"] == 0.0
    assert by["b1"]["cheap_flag"] == "相對便宜(PB)"
    # b2..b6 有正 PE（5 檔過門檻）→ 仍用 PE（PE 主），不退 PB
    assert by["b2"]["pe_status"] == "ok"
    assert by["b2"]["val_metric"] == "PE"


def test_min_peers_filters_small_subindustry() -> None:
    rows = [{"stock_id": "x1", "pe": 10.0, "pbr": 1.0},
            {"stock_id": "x2", "pe": 20.0, "pbr": 2.0}]
    membership = _membership([("Small", "x1"), ("Small", "x2")])
    out = build_valuation(_ratios(rows), membership, min_peers=5)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    # 只有 2 檔 < min_peers=5 → 無相對位階（PE、PB 同儕皆不足）
    assert by["x1"]["val_pctile"] is None
    assert by["x1"]["val_metric"] is None
    assert by["x1"]["cheap_flag"] == ""


def test_multi_tag_averages_across_subindustries() -> None:
    # m 同屬 A、B 兩次產業，各自 PE 中位不同 → 相對值取平均（守 stock_panel 慣例）
    rows = [{"stock_id": "m", "pe": 5.0}]
    rows += [{"stock_id": s, "pe": 15.0} for s in ("a1", "a2", "a3", "a4")]  # A 同儕 PE 15
    rows += [{"stock_id": s, "pe": 3.0} for s in ("b1", "b2", "b3", "b4")]   # B 同儕 PE 3
    membership = _membership(
        [("A", "m"), ("B", "m")]
        + [("A", s) for s in ("a1", "a2", "a3", "a4")]
        + [("B", s) for s in ("b1", "b2", "b3", "b4")]
    )
    out = build_valuation(_ratios(rows), membership, min_peers=5)
    m = out.filter(pl.col("stock_id") == "m").row(0, named=True)
    # A 中位含 m：median(5,15,15,15,15)=15；B 中位含 m：median(5,3,3,3,3)=3 → 平均 9
    assert m["val_median"] == 9.0
    # A 內 PE5 最便宜→pctile 0；B 內 PE5 最貴→pctile 100 → 平均 50
    assert m["val_pctile"] == 50.0


def test_peer_source_marks_fine_vs_fallback() -> None:
    # 兩組同儕：手標「A」與 TWSE 產業別兜底「產業別:鋼鐵」各 5 檔
    rows = [{"stock_id": s, "pe": float(i % 5 + 1)} for i, s in enumerate(
        ("a1", "a2", "a3", "a4", "a5", "f1", "f2", "f3", "f4", "f5"))]
    membership = _membership(
        [("A", s) for s in ("a1", "a2", "a3", "a4", "a5")]
        + [("產業別:鋼鐵", s) for s in ("f1", "f2", "f3", "f4", "f5")]
    )
    out = build_valuation(_ratios(rows), membership, min_peers=5)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    assert by["a1"]["peer_source"] == "次產業"
    assert by["f1"]["peer_source"] == "產業別"


def test_meta_counts_and_notes() -> None:
    rows = [{"stock_id": s, "pe": pe} for s, pe in
            (("a1", 10.0), ("a2", 5.0), ("a3", 2.5), ("a4", 2.0), ("a5", 1.25))]
    rows.append({"stock_id": "loss", "pe": None, "pbr": 0.5})  # 虧損股、無同儕（不計相對）
    membership = _membership([("A", s) for s in ("a1", "a2", "a3", "a4", "a5")])
    out = build_valuation(_ratios(rows), membership, min_peers=5, cheap_pctile=30.0)
    meta = compute_valuation_meta(out, data_date="2026-06-12")

    assert meta["n_stocks"] == 6
    assert meta["n_with_pe"] == 5
    assert meta["n_no_pe"] == 1
    # a5(0)、a4(25) 便宜 → 2
    assert meta["n_cheap"] == 2
    # 誠實但書必含「官方」「前瞻」「PB」
    joined = " ".join(meta["notes"])
    assert "官方" in joined and "前瞻" in joined and "PB" in joined


def test_compute_subind_relative_generic_value_col() -> None:
    # 直接測通用相對函式吃任意欄（PB）
    df = pl.DataFrame({"stock_id": [f"s{i}" for i in range(5)],
                       "pbr": [1.0, 2.0, 3.0, 4.0, 5.0]})
    membership = _membership([("G", f"s{i}") for i in range(5)])
    rel = compute_subind_relative(df, membership, value_col="pbr", min_peers=5)
    by = {r["stock_id"]: r for r in rel.iter_rows(named=True)}
    assert by["s0"]["subind_median"] == 3.0
    assert by["s0"]["subind_pctile"] == 0.0   # PB 1.0 最低
    assert by["s4"]["subind_pctile"] == 100.0


def test_empty_inputs() -> None:
    empty = pl.DataFrame(
        schema={"stock_id": pl.Utf8, "market": pl.Utf8, "pe": pl.Float64,
                "pbr": pl.Float64, "dividend_yield": pl.Float64}
    )
    assert build_valuation(empty, _membership([("A", "a")])).is_empty()
    meta = compute_valuation_meta(empty)
    assert meta["n_stocks"] == 0 and meta["notes"]


def test_market_cap_billion_basic() -> None:
    # 台泥 1101：已發行股數 7,523,181,742、收盤價（假設）50 元 → 市值(億元) = 股數×價/1e8
    got = market_cap_billion(7_523_181_742, 50.0)
    assert got == 7_523_181_742 * 50.0 / 1e8


def test_market_cap_billion_none_propagates() -> None:
    assert market_cap_billion(None, 50.0) is None
    assert market_cap_billion(1_000_000, None) is None
    assert market_cap_billion(None, None) is None


# ─── compute_self_history_pctile（docs/31 §14：自身估值歷史百分位粗版代理） ──────────


def _history_rows(stock_id: str, pes: list[float]) -> list[dict]:
    return [
        {
            "date": date(2026, 1, 1 + i), "stock_id": stock_id, "market": "上市",
            "pe": pe, "pbr": 1.0, "dividend_yield": 1.0,
        }
        for i, pe in enumerate(pes)
    ]


def test_self_history_pctile_latest_is_all_time_high() -> None:
    history = pl.DataFrame(_history_rows("A", [float(v) for v in range(10, 20)]))
    out = compute_self_history_pctile(history, min_snapshots=8)
    row = out.filter(pl.col("stock_id") == "A").row(0, named=True)
    assert row["pe_self_pctile"] == 100.0
    assert row["pe_self_n"] == 10


def test_self_history_pctile_latest_is_all_time_low() -> None:
    history = pl.DataFrame(_history_rows("A", [float(v) for v in range(20, 10, -1)]))
    out = compute_self_history_pctile(history, min_snapshots=8)
    assert out.filter(pl.col("stock_id") == "A").row(0, named=True)["pe_self_pctile"] == 0.0


def test_self_history_pctile_excludes_insufficient_history() -> None:
    """筆數 < min_snapshots → 不出現，不假裝精確（誠實留白，非0填）。"""
    history = pl.DataFrame(_history_rows("B", [20.0, 20.0, 20.0]))
    out = compute_self_history_pctile(history, min_snapshots=8)
    assert out.is_empty()


def test_self_history_pctile_ignores_non_positive_pe() -> None:
    """虧損股（無正PE）該筆不計入分母——沿用 build_valuation 同一慣例。"""
    rows = _history_rows("A", [10.0] * 5) + [
        {"date": date(2026, 1, 6 + i), "stock_id": "A", "market": "上市",
         "pe": None, "pbr": 1.0, "dividend_yield": 1.0}
        for i in range(5)
    ]
    history = pl.DataFrame(rows, schema={
        "date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
        "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64,
    })
    out = compute_self_history_pctile(history, min_snapshots=8)
    assert out.is_empty()  # 只有5筆有效PE，未達門檻


def test_self_history_pctile_empty_input() -> None:
    empty = pl.DataFrame(
        schema={"date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
                "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64}
    )
    assert compute_self_history_pctile(empty).is_empty()


# ─── peg_like_ratio（docs/31 §18：PE對營收YoY成長比，非EPS-based PEG） ──────────


def test_peg_like_ratio_basic() -> None:
    assert peg_like_ratio(pe=20.0, rev_yoy_pct=40.0) == 0.5


def test_peg_like_ratio_none_when_pe_missing_or_non_positive() -> None:
    assert peg_like_ratio(pe=None, rev_yoy_pct=40.0) is None
    assert peg_like_ratio(pe=0.0, rev_yoy_pct=40.0) is None
    assert peg_like_ratio(pe=-5.0, rev_yoy_pct=40.0) is None


def test_peg_like_ratio_none_when_growth_missing_or_non_positive() -> None:
    """成長為負/零時比值方向會反轉、不可比照PEG直覺讀，寧可留null不硬算。"""
    assert peg_like_ratio(pe=20.0, rev_yoy_pct=None) is None
    assert peg_like_ratio(pe=20.0, rev_yoy_pct=0.0) is None
    assert peg_like_ratio(pe=20.0, rev_yoy_pct=-10.0) is None


# ─── compute_self_history_median（docs/31 §20.7：估值回歸參考價自身腿的錨點） ─────


def test_self_history_median_basic() -> None:
    history = pl.DataFrame(_history_rows("A", [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]))
    out = compute_self_history_median(history, min_snapshots=8)
    row = out.filter(pl.col("stock_id") == "A").row(0, named=True)
    assert row["pe_self_median"] == pytest.approx(45.0)
    assert row["pe_self_n"] == 8


def test_self_history_median_excludes_insufficient_history() -> None:
    history = pl.DataFrame(_history_rows("B", [20.0, 20.0, 20.0]))
    out = compute_self_history_median(history, min_snapshots=8)
    assert out.is_empty()


def test_self_history_median_ignores_non_positive_pe() -> None:
    rows = _history_rows("A", [10.0] * 5) + [
        {"date": date(2026, 1, 6 + i), "stock_id": "A", "market": "上市",
         "pe": None, "pbr": 1.0, "dividend_yield": 1.0}
        for i in range(5)
    ]
    history = pl.DataFrame(rows, schema={
        "date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
        "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64,
    })
    out = compute_self_history_median(history, min_snapshots=8)
    assert out.is_empty()  # 只有5筆有效PE，未達門檻


def test_self_history_median_empty_input() -> None:
    empty = pl.DataFrame(
        schema={"date": pl.Date, "stock_id": pl.Utf8, "market": pl.Utf8,
                "pe": pl.Float64, "pbr": pl.Float64, "dividend_yield": pl.Float64}
    )
    assert compute_self_history_median(empty).is_empty()


# ─── implied_price_from_ratio_median / implied_price_gap_pct（docs/31 §20.7） ──


def test_implied_price_from_ratio_median_basic() -> None:
    """現價100、現行PE20、同儕中位PE30 → 若回歸中位數，implied_price=100*(30/20)=150。"""
    assert implied_price_from_ratio_median(
        current_price=100.0, current_ratio=20.0, reference_median=30.0
    ) == pytest.approx(150.0)


def test_implied_price_from_ratio_median_none_when_ratio_non_positive() -> None:
    assert implied_price_from_ratio_median(100.0, 0.0, 30.0) is None
    assert implied_price_from_ratio_median(100.0, -5.0, 30.0) is None


def test_implied_price_from_ratio_median_none_when_missing_input() -> None:
    assert implied_price_from_ratio_median(None, 20.0, 30.0) is None
    assert implied_price_from_ratio_median(100.0, None, 30.0) is None
    assert implied_price_from_ratio_median(100.0, 20.0, None) is None


def test_implied_price_gap_pct_positive_means_undervalued() -> None:
    """implied_price(150) > current_price(100) → gap為正＝估值高於現價＝相對便宜/進場訊號。"""
    assert implied_price_gap_pct(implied_price=150.0, current_price=100.0) == pytest.approx(50.0)


def test_implied_price_gap_pct_negative_means_price_ran_ahead() -> None:
    """implied_price(80) < current_price(100) → gap為負＝基本面看好但價格已衝高、等回檔。"""
    assert implied_price_gap_pct(implied_price=80.0, current_price=100.0) == pytest.approx(-20.0)


def test_implied_price_gap_pct_none_when_missing_or_non_positive_price() -> None:
    assert implied_price_gap_pct(None, 100.0) is None
    assert implied_price_gap_pct(150.0, None) is None
    assert implied_price_gap_pct(150.0, 0.0) is None


# ─── broad_membership 小樣本兜底（2026-08-29，docs/31 §20.8） ──────────────────


def test_broad_membership_rescues_undersized_hand_tagged_group() -> None:
    """手標次產業僅4檔（<min_peers=5，如晶圓代工實例）——不傳broad維持null，傳了才救回。"""
    rows = [{"stock_id": s, "pe": pe} for s, pe in
            (("w1", 28.1), ("w2", 15.0), ("w3", 20.0), ("w4", 22.0),
             ("o1", 30.0), ("o2", 31.0), ("o3", 29.0), ("o4", 28.0), ("o5", 27.0))]
    membership = _membership(
        [("晶圓代工", s) for s in ("w1", "w2", "w3", "w4")]
        + [("其他半導體", s) for s in ("o1", "o2", "o3", "o4", "o5")]
    )
    broad = _membership([("產業別:半導體業", s) for s in
                          ("w1", "w2", "w3", "w4", "o1", "o2", "o3", "o4", "o5")])

    without = build_valuation(_ratios(rows), membership, min_peers=5)
    by_without = {r["stock_id"]: r for r in without.iter_rows(named=True)}
    for s in ("w1", "w2", "w3", "w4"):
        assert by_without[s]["val_pctile"] is None
        assert by_without[s]["peer_source"] is None

    with_broad = build_valuation(_ratios(rows), membership, min_peers=5, broad_membership=broad)
    by_with = {r["stock_id"]: r for r in with_broad.iter_rows(named=True)}
    for s in ("w1", "w2", "w3", "w4"):
        assert by_with[s]["val_pctile"] is not None
        assert by_with[s]["peer_source"] == "產業別(次產業樣本不足)"
    # 已經有夠樣本的次產業（其他半導體，5檔）不受影響——值與不傳broad時完全一致
    for s in ("o1", "o2", "o3", "o4", "o5"):
        assert by_with[s]["val_pctile"] == by_without[s]["val_pctile"]
        assert by_with[s]["val_median"] == by_without[s]["val_median"]
        assert by_with[s]["peer_source"] == "次產業"


def test_broad_membership_none_or_empty_keeps_old_behavior() -> None:
    """broad_membership 不傳或傳空表——行為與修改前完全一致（向後相容）。"""
    rows = [{"stock_id": s, "pe": pe} for s, pe in
            (("w1", 28.1), ("w2", 15.0), ("w3", 20.0), ("w4", 22.0))]
    membership = _membership([("晶圓代工", s) for s in ("w1", "w2", "w3", "w4")])
    empty_broad = pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8})

    out_none = build_valuation(_ratios(rows), membership, min_peers=5)
    out_empty = build_valuation(
        _ratios(rows), membership, min_peers=5, broad_membership=empty_broad
    )
    for s in ("w1", "w2", "w3", "w4"):
        assert out_none.filter(pl.col("stock_id") == s)["val_pctile"].item() is None
        assert out_empty.filter(pl.col("stock_id") == s)["val_pctile"].item() is None
