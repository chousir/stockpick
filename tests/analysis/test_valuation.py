"""個股相對 PE 估值測試（C1）：PE 計算/狀態、次產業相對位階、多標籤平均、覆蓋 meta。

全離線合成資料、手算對拍。守 docs/13 §C1：缺 EPS／EPS≤0 不補零、不給負 PE。
"""

from __future__ import annotations

import polars as pl

from tw_screener.analysis.valuation import (
    build_valuation,
    compute_pe,
    compute_subind_relative,
    compute_valuation_meta,
)


def _prices(d: dict[str, float]) -> pl.DataFrame:
    return pl.DataFrame({"stock_id": list(d), "close": [float(v) for v in d.values()]})


def _fund(d: dict[str, float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {"stock_id": list(d), "eps": list(d.values())},
        schema={"stock_id": pl.Utf8, "eps": pl.Float64},
    )


def _membership(pairs: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"sub_industry": [p[0] for p in pairs], "stock_id": [p[1] for p in pairs]}
    )


def test_compute_pe_status_and_values() -> None:
    prices = _prices({"1111": 100.0, "2222": 50.0, "3333": 20.0, "4444": 10.0})
    # 1111 EPS=2/季 → 年化 8 → PE 12.5；3333 EPS=0 虧損打平；4444 無 EPS 未取得
    fund = _fund({"1111": 2.0, "2222": 1.0, "3333": 0.0})
    out = compute_pe(prices, fund, annualize_factor=4.0)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}

    assert by["1111"]["pe"] == 100.0 / 8.0
    assert by["1111"]["pe_status"] == "ok"
    assert by["2222"]["pe"] == 50.0 / 4.0
    # EPS≤0 → pe null + 虧損打平（不給負 PE）
    assert by["3333"]["pe"] is None
    assert by["3333"]["pe_status"] == "虧損打平"
    # 無 EPS → pe null + 未取得（不補零）
    assert by["4444"]["pe"] is None
    assert by["4444"]["eps_q"] is None
    assert by["4444"]["pe_status"] == "未取得"


def test_relative_ranking_within_subindustry() -> None:
    # 次產業 A 五檔（過 min_peers=5），PE 由 EPS×4 與 close 反推；同 close 不同 EPS
    prices = _prices({s: 40.0 for s in ("a1", "a2", "a3", "a4", "a5")})
    # EPS：1,2,4,5,8 → 年化 4,8,16,20,32 → PE 10,5,2.5,2,1.25
    fund = _fund({"a1": 1.0, "a2": 2.0, "a3": 4.0, "a4": 5.0, "a5": 8.0})
    membership = _membership([("A", s) for s in ("a1", "a2", "a3", "a4", "a5")])
    rel = compute_subind_relative(compute_pe(prices, fund), membership, min_peers=5)
    by = {r["stock_id"]: r for r in rel.iter_rows(named=True)}

    # 中位 PE = median(10,5,2.5,2,1.25) = 2.5
    assert by["a1"]["subind_median_pe"] == 2.5
    # a5 最便宜（PE 1.25）→ 升冪百分位 0；a1 最貴（PE 10）→ 100
    assert by["a5"]["pe_subind_pctile"] == 0.0
    assert by["a1"]["pe_subind_pctile"] == 100.0
    # 相對%：a1 (10/2.5−1)*100 = +300；a5 (1.25/2.5−1)*100 = −50
    assert by["a1"]["pe_vs_subind_pct"] == 300.0
    assert by["a5"]["pe_vs_subind_pct"] == -50.0


def test_min_peers_filters_small_subindustry() -> None:
    prices = _prices({"x1": 30.0, "x2": 30.0})
    fund = _fund({"x1": 1.0, "x2": 3.0})
    membership = _membership([("Small", "x1"), ("Small", "x2")])
    # 只有 2 檔 < min_peers=5 → 無相對位階
    rel = compute_subind_relative(compute_pe(prices, fund), membership, min_peers=5)
    assert rel.is_empty()


def test_multi_tag_averages_across_subindustries() -> None:
    # m 同屬 A、B 兩次產業，各自中位數不同 → 相對值取平均（守 stock_panel 慣例）
    prices = _prices({s: 60.0 for s in ("m", "a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4")})
    fund = _fund(
        {
            "m": 3.0,  # PE = 60/12 = 5
            "a1": 1.0, "a2": 1.0, "a3": 1.0, "a4": 1.0,  # A 同儕 PE 15
            "b1": 5.0, "b2": 5.0, "b3": 5.0, "b4": 5.0,  # B 同儕 PE 3
        }
    )
    membership = _membership(
        [("A", "m"), ("B", "m")]
        + [("A", s) for s in ("a1", "a2", "a3", "a4")]
        + [("B", s) for s in ("b1", "b2", "b3", "b4")]
    )
    rel = compute_subind_relative(compute_pe(prices, fund), membership, min_peers=5)
    m = rel.filter(pl.col("stock_id") == "m").row(0, named=True)
    # A 中位含 m：median(5,15,15,15,15)=15；B 中位含 m：median(5,3,3,3,3)=3 → 平均 9
    assert m["subind_median_pe"] == 9.0
    assert m["n_subind"] == 2
    # A 內 PE5 最便宜→pctile 0；B 內 PE5 最貴→pctile 100 → 平均 50
    assert m["pe_subind_pctile"] == 50.0


def test_build_valuation_cheap_flag_and_meta() -> None:
    prices = _prices({s: 40.0 for s in ("a1", "a2", "a3", "a4", "a5")} | {"z": 10.0})
    fund = _fund({"a1": 1.0, "a2": 2.0, "a3": 4.0, "a4": 5.0, "a5": 8.0})  # z 無 EPS
    membership = _membership([("A", s) for s in ("a1", "a2", "a3", "a4", "a5")])
    out = build_valuation(prices, fund, membership, min_peers=5, cheap_pctile=30.0)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}

    # 百分位 ≤30 標相對便宜：a5(0)、a4(25) 是；a3(50)、a1(100) 否
    assert by["a5"]["cheap_flag"] == "相對便宜"
    assert by["a4"]["cheap_flag"] == "相對便宜"
    assert by["a3"]["cheap_flag"] == ""
    # 無相對位階者（z 缺 EPS）不標便宜
    assert by["z"]["cheap_flag"] == ""
    assert by["z"]["pe_subind_pctile"] is None

    meta = compute_valuation_meta(out, data_date="2026-06-09")
    assert meta["n_stocks"] == 6
    assert meta["n_with_pe"] == 5
    assert meta["n_no_eps"] == 1
    assert meta["n_cheap"] == 2
    # 誠實但書必含「未取得」歷史百分位與「非前瞻」
    joined = " ".join(meta["notes"])
    assert "未取得" in joined and "前瞻" in joined


def test_peer_source_marks_fine_vs_fallback() -> None:
    # 兩個同儕組：手標「A」與 TWSE 產業別兜底「產業別:鋼鐵」，各 5 檔
    prices = _prices(
        {s: 40.0 for s in ("a1", "a2", "a3", "a4", "a5", "f1", "f2", "f3", "f4", "f5")}
    )
    fund = _fund({s: float(i % 5 + 1) for i, s in enumerate(
        ("a1", "a2", "a3", "a4", "a5", "f1", "f2", "f3", "f4", "f5"))})
    membership = _membership(
        [("A", s) for s in ("a1", "a2", "a3", "a4", "a5")]
        + [("產業別:鋼鐵", s) for s in ("f1", "f2", "f3", "f4", "f5")]
    )
    out = build_valuation(prices, fund, membership, min_peers=5)
    by = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    assert by["a1"]["peer_source"] == "次產業"
    assert by["f1"]["peer_source"] == "產業別"


def test_empty_inputs() -> None:
    empty_price = pl.DataFrame(schema={"stock_id": pl.Utf8, "close": pl.Float64})
    assert compute_pe(empty_price, _fund({"a": 1.0})).is_empty()
    assert build_valuation(empty_price, _fund({"a": 1.0}), _membership([("A", "a")])).is_empty()
