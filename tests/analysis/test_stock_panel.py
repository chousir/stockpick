"""個股特徵面板測試（B1）：資金/位階/RS/量能/coverage，全離線合成資料、手算對拍。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.analysis.rotation import compute_subindustry_baskets
from tw_screener.analysis.stock_panel import build_stock_panel, compute_coverage_meta

D0 = date(2026, 6, 1)
# 小視窗便於手算（生產值由 settings 傳入）
PARAMS = dict(
    short_window=2,
    long_window=4,
    ma_windows=(2, 3),
    position_window=4,
    rs_window=2,
    z_window=4,
    z_min_periods=2,
)


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _price(series: dict[str, list[float]]) -> pl.DataFrame:
    ds = _dates(max(len(v) for v in series.values()))
    rows = []
    for sid, closes in series.items():
        for d, c in zip(ds, closes):
            rows.append({"date": d, "stock_id": sid, "close": float(c), "volume": 1000})
    return pl.DataFrame(rows)


def _membership() -> pl.DataFrame:
    # A={1111,2222}、B={2222,3333}（2222 多標籤）；4444 無標籤
    return pl.DataFrame(
        {
            "sub_industry": ["A", "A", "B", "B"],
            "stock_id": ["1111", "2222", "2222", "3333"],
        }
    )


def _institutional(total_by_day: dict[str, list[float]]) -> pl.DataFrame:
    """只給 total_net（foreign/trust 設為其半，dealer 補差），缺日 = 該股當日不列。"""
    ds = _dates(6)
    rows = []
    for sid, totals in total_by_day.items():
        for d, t in zip(ds, totals):
            if t is None:  # 模擬「當日無進出」未列入 T86
                continue
            f = int(t * 0.5)
            tr = int(t * 0.3)
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "foreign_net": f,
                    "trust_net": tr,
                    "dealer_net": int(t) - f - tr,
                    "total_net": int(t),
                }
            )
    return pl.DataFrame(rows)


def _panel():
    prices = _price(
        {
            "1111": [100, 102, 104, 103, 105, 110],
            "2222": [50, 50, 51, 50, 52, 55],
            "3333": [200, 198, 202, 205, 210, 215],
            "4444": [30, 31, 30, 32, 33, 34],
        }
    )
    inst = _institutional({"1111": [100, 200, None, -50, 80, 120]})
    membership = _membership()
    baskets = compute_subindustry_baskets(membership, prices)
    return build_stock_panel(prices, inst, membership, baskets, **PARAMS)


def _row(panel: pl.DataFrame, sid: str, day: int) -> pl.DataFrame:
    return panel.filter((pl.col("stock_id") == sid) & (pl.col("date") == D0 + timedelta(days=day)))


# ---- 結構 ----

def test_one_row_per_stock_day_and_columns():
    panel = _panel()
    assert panel.height == 4 * 6
    assert panel.select(["date", "stock_id"]).n_unique() == panel.height
    for col in (
        "net_flow_2d", "net_flow_4d", "net_flow_4d_z", "flow_momentum",
        "above_low_4d_pct", "above_ma2_pct", "above_ma3_pct",
        "ret_2d", "rs_market_2d", "rs_subind_2d", "volume_z_2d",
    ):
        assert col in panel.columns, col


# ---- 資金：滾動加總 + 加速度（手算 1111 D5）----

def test_flow_rolling_and_momentum():
    row = _row(_panel(), "1111", 5)
    # total_net 1111 = [100,200,0(缺),-50,80,120]
    # net_flow_2d(D5)=80+120=200；net_flow_4d(D5)=0-50+80+120=150
    assert row["net_flow_2d"][0] == 200
    assert row["net_flow_4d"][0] == 150
    # momentum(D5)=rs2[D5]-rs2[D3]=200-(-50)=250
    assert row["flow_momentum"][0] == 250


def test_missing_institutional_filled_zero():
    panel = _panel()
    # 2222 完全無法人紀錄 → net 欄全 0（補 0 慣例）
    s2 = panel.filter(pl.col("stock_id") == "2222")
    assert s2["net_flow_4d"].sum() == 0
    assert s2["flow_momentum"].drop_nulls().abs().sum() == 0


# ---- 價格位階（手算 1111 D5）----

def test_price_position():
    row = _row(_panel(), "1111", 5)
    # 1111 closes=[100,102,104,103,105,110]
    # above_low_4d: 距 min(104,103,105,110)=103 → (110/103-1)*100
    assert row["above_low_4d_pct"][0] == pytest.approx((110 / 103 - 1) * 100, abs=1e-6)
    # above_ma2: mean(105,110)=107.5 → (110/107.5-1)*100
    assert row["above_ma2_pct"][0] == pytest.approx((110 / 107.5 - 1) * 100, abs=1e-6)
    # above_ma3: mean(103,105,110)=106 → (110/106-1)*100
    assert row["above_ma3_pct"][0] == pytest.approx((110 / 106 - 1) * 100, abs=1e-6)


def test_ma_warmup_null():
    # ma3 需 3 根 → D0、D1 為 null（min_samples=w，不假造）
    panel = _panel().filter(pl.col("stock_id") == "1111").sort("date")
    assert panel["above_ma3_pct"].head(2).null_count() == 2


# ---- 相對強度 ----

def test_ret_and_rs_zero_when_market_uniform():
    # 所有股每日同步 +5% → 個股報酬＝市場報酬 → rs_market≈0
    prices = _price(
        {
            "1111": [100, 105, 110.25, 115.7625],
            "2222": [10, 10.5, 11.025, 11.57625],
            "3333": [50, 52.5, 55.125, 57.88125],
        }
    )
    membership = _membership()
    baskets = compute_subindustry_baskets(membership, prices)
    panel = build_stock_panel(prices, pl.DataFrame(), membership, baskets, **PARAMS)
    rs = panel.filter(pl.col("rs_market_2d").is_not_null())["rs_market_2d"]
    assert rs.abs().max() == pytest.approx(0.0, abs=1e-6)
    # ret_2d 手算：D2 vs D0 = (110.25/100-1)*100 = 10.25%
    r = panel.filter((pl.col("stock_id") == "1111") & (pl.col("date") == D0 + timedelta(days=2)))
    assert r["ret_2d"][0] == pytest.approx(10.25, abs=1e-6)


def test_multitag_subind_is_basket_average():
    prices = _price(
        {
            "1111": [100, 102, 104, 103, 105, 110],
            "2222": [50, 50, 51, 50, 52, 55],
            "3333": [200, 198, 202, 205, 210, 215],
        }
    )
    membership = _membership()
    baskets = compute_subindustry_baskets(membership, prices)
    panel = build_stock_panel(prices, pl.DataFrame(), membership, baskets, **PARAMS)

    # 2222 屬 A、B → subind_ret 應＝兩籃子 rs_window 報酬平均（用 basket_index 直接算）
    def basket_ret(sub: str, d: date) -> float:
        bs = baskets.filter(pl.col("sub_industry") == sub).sort("date")
        idx = bs["basket_index"].to_list()
        i = bs["date"].to_list().index(d)
        return (idx[i] / idx[i - PARAMS["rs_window"]] - 1) * 100

    target = D0 + timedelta(days=5)
    expected_subind = (basket_ret("A", target) + basket_ret("B", target)) / 2
    row = _row(panel, "2222", 5)
    # rs_subind = 個股報酬 − 籃子均報酬 → 反推籃子均報酬應＝兩籃子平均
    implied_subind = row["ret_2d"][0] - row["rs_subind_2d"][0]
    assert implied_subind == pytest.approx(expected_subind, abs=1e-6)


def test_unmembered_stock_has_null_subind():
    panel = _panel()
    s4 = panel.filter(pl.col("stock_id") == "4444")
    assert s4["rs_subind_2d"].null_count() == s4.height


# ---- 量能 z ----

def test_volume_z_warmup_then_value():
    # 1111 量能放大：z_min_periods=2 → 前 1 根 null，之後有值；放大日 z > 0
    prices = _price(
        {"1111": [100, 101, 102, 103, 104, 105]}
    ).with_columns(
        pl.Series("volume", [1000, 1000, 1000, 1000, 1000, 9000])
    )
    panel = build_stock_panel(prices, pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), **PARAMS)
    vz = panel.sort("date")["volume_z_2d"].to_list()
    assert vz[0] is None  # 暖機不足
    assert vz[-1] is not None and vz[-1] > 0  # 末日爆量 → 正 z


# ---- coverage meta ----

def test_coverage_meta():
    prices = _price(
        {
            "1111": [100, 102, 104, 103, 105, 110],
            "2222": [50, 50, 51, 50, 52, 55],
            "3333": [200, 198, 202, 205, 210, 215],
            "4444": [30, 31, 30, 32, 33, 34],
        }
    )
    inst = _institutional({"1111": [100, 200, None, -50, 80, 120]})
    membership = _membership()
    baskets = compute_subindustry_baskets(membership, prices)
    panel = build_stock_panel(prices, inst, membership, baskets, **PARAMS)
    meta = compute_coverage_meta(panel, inst, membership)
    assert meta["universe"] == "listed"
    assert meta["n_stocks"] == 4
    assert meta["n_stock_days"] == 24
    # 法人僅 1111 的 5 天有列（D2 缺）→ 5/24
    assert meta["inst_coverage_pct"] == pytest.approx(100 * 5 / 24, abs=0.01)
    # 4444 無標籤 → 3/4 有標
    assert meta["subind_coverage_pct"] == pytest.approx(75.0, abs=0.01)
    assert any("周轉率" in n for n in meta["notes"])


def test_empty_inputs():
    empty = pl.DataFrame()
    assert build_stock_panel(empty, empty, empty, empty).is_empty()
    assert compute_coverage_meta(empty, empty, empty)["n_stocks"] == 0
