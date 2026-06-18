"""rotation 計算層測試（R1）：籃子報酬 / 資金流向 / 排名 ΔRank，全離線合成資料。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis.rotation import (
    compute_fund_flows,
    compute_subindustry_baskets,
    load_market_history,
    rank_flows,
)

D0 = date(2026, 6, 1)


def _dates(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _membership() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "sub_industry": ["記憶體", "記憶體", "IC設計", "IC設計"],
            "stock_id": ["2337", "8299", "2454", "8299"],  # 8299 多標籤：兩籃子都計
        }
    )


def _price_history() -> pl.DataFrame:
    # 三天收盤：2337 100→110→121（+10%/+10%）、8299 50→50→55（0%/+10%）、
    # 2454 200→180→180（−10%/0%）
    ds = _dates(3)
    rows = []
    series = [("2337", [100, 110, 121]), ("8299", [50, 50, 55]), ("2454", [200, 180, 180])]
    for sid, closes in series:
        for d, c in zip(ds, closes):
            rows.append({"date": d, "stock_id": sid, "close": float(c)})
    return pl.DataFrame(rows)


# ─── compute_subindustry_baskets ──────────────────────────────────────────────


def test_baskets_equal_weight_returns():
    df = compute_subindustry_baskets(_membership(), _price_history())
    mem = df.filter(pl.col("sub_industry") == "記憶體").sort("date")
    # day2：mean(+10%, 0%) = +5%；day3：mean(+10%, +10%) = +10%
    assert mem["basket_return_pct"].to_list() == pytest.approx([5.0, 10.0])
    assert mem["members_priced"].to_list() == [2, 2]
    # basket_index：105 → 115.5
    assert mem["basket_index"].to_list() == pytest.approx([105.0, 115.5])


def test_baskets_multilabel_counted_in_both():
    df = compute_subindustry_baskets(_membership(), _price_history())
    ic = df.filter(pl.col("sub_industry") == "IC設計").sort("date")
    # day2：mean(8299 0%, 2454 −10%) = −5%
    assert ic["basket_return_pct"][0] == pytest.approx(-5.0)
    assert ic["members_priced"][0] == 2


def test_baskets_empty_inputs():
    assert compute_subindustry_baskets(pl.DataFrame(), _price_history()).is_empty()
    assert compute_subindustry_baskets(_membership(), pl.DataFrame()).is_empty()


# ─── compute_fund_flows ───────────────────────────────────────────────────────


def _institutional(n_days: int = 6) -> pl.DataFrame:
    """2337 每天外資 +100；2454 每天投信 −50；8299 只有第一天有紀錄（+10）。"""
    ds = _dates(n_days)
    rows = []
    for d in ds:
        rows.append(
            {"date": d, "stock_id": "2337", "foreign_net": 100, "trust_net": 0,
             "dealer_net": 0, "total_net": 100}
        )
        rows.append(
            {"date": d, "stock_id": "2454", "foreign_net": 0, "trust_net": -50,
             "dealer_net": 0, "total_net": -50}
        )
    rows.append(
        {"date": ds[0], "stock_id": "8299", "foreign_net": 10, "trust_net": 0,
         "dealer_net": 0, "total_net": 10}
    )
    return pl.DataFrame(rows)


def test_fund_flows_rolling_sums_with_grid_fill():
    flows = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4
    )
    last = flows.filter(
        (pl.col("sub_industry") == "記憶體") & (pl.col("date") == _dates(6)[-1])
    )
    # 記憶體 = 2337（每天+100）+ 8299（僅 day1 +10，之後網格補 0）
    # 近 2 日：2337 200 + 8299 0 = 200；近 4 日：400
    assert last["net_flow_2d"][0] == 200
    assert last["net_flow_4d"][0] == 400
    assert last["foreign_flow_2d"][0] == 200
    # momentum：近 2 日(200) − 前一個 2 日(200) = 0
    assert last["flow_momentum"][0] == 0


def test_fund_flows_breadth():
    flows = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4
    )
    last_mem = flows.filter(
        (pl.col("sub_industry") == "記憶體") & (pl.col("date") == _dates(6)[-1])
    )
    # 記憶體成員 2 檔：2337 近 2 日 +200（買超）、8299 近 2 日 0（非買超）→ 1/2
    assert last_mem["flow_breadth_2d"][0] == 0.5
    ic_last = flows.filter(
        (pl.col("sub_industry") == "IC設計") & (pl.col("date") == _dates(6)[-1])
    )
    # IC設計：2454 賣超、8299 0 → 0/2
    assert ic_last["flow_breadth_2d"][0] == 0.0
    assert ic_last["members"][0] == 2


def test_fund_flows_first_day_includes_8299():
    """8299 唯一有紀錄的第一天：記憶體 net_flow_2d = 100+10。"""
    flows = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4
    )
    first = flows.filter(
        (pl.col("sub_industry") == "記憶體") & (pl.col("date") == D0)
    )
    assert first["net_flow_2d"][0] == 110  # min_samples=1：首日窗只含當日


def test_fund_flows_concentration_with_volume():
    ds = _dates(6)
    vol_rows = []
    for d in ds:
        for sid in ("2337", "8299", "2454"):
            vol_rows.append({"date": d, "stock_id": sid, "volume": 1000})
    flows = compute_fund_flows(
        _membership(),
        _institutional(6),
        volume_history=pl.DataFrame(vol_rows),
        short_window=2,
        long_window=4,
    )
    last = flows.filter(
        (pl.col("sub_industry") == "記憶體") & (pl.col("date") == ds[-1])
    )
    # 近 2 日：淨買 200 股 / 成交 (2337 2000 + 8299 2000) = 0.05
    assert abs(last["flow_concentration_2d"][0] - 0.05) < 1e-9


def test_fund_flows_concentration_null_without_volume():
    flows = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4
    )
    assert flows["flow_concentration_2d"].null_count() == flows.height


def test_fund_flows_empty_inputs():
    assert compute_fund_flows(pl.DataFrame(), _institutional(6)).is_empty()
    assert compute_fund_flows(_membership(), pl.DataFrame()).is_empty()


def test_fund_flows_multi_window_additive():
    """M-MH Phase 1：windows 多窗只增欄；short/long 子集逐值不變（純加法）。"""
    base = compute_fund_flows(_membership(), _institutional(6), short_window=2, long_window=4)
    multi = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4, windows=(1, 3)
    )
    # 新窗 flow 欄出現（三 net 欄都有）
    for col in ("net_flow_1d", "net_flow_3d", "foreign_flow_3d", "trust_flow_1d"):
        assert col in multi.columns, col
    # 既有 2d/4d 子集 + momentum/breadth（錨在 short/long）逐值不變
    shared = ["net_flow_2d", "net_flow_4d", "foreign_flow_2d", "flow_momentum", "flow_breadth_2d"]
    keyed = ["sub_industry", "date", *shared]
    assert multi.select(keyed).sort(keyed[:2]).equals(base.select(keyed).sort(keyed[:2]))


# ─── rank_flows + ΔRank（復用 attach_rank_delta） ─────────────────────────────


def test_rank_flows_orders_by_key_and_delta():
    flows = compute_fund_flows(
        _membership(), _institutional(6), short_window=2, long_window=4
    )
    ranked = rank_flows(flows, by="net_flow_4d")
    assert ranked["sub_industry"].to_list() == ["記憶體", "IC設計"]
    assert ranked["radar_rank"].to_list() == [1, 2]
    assert ranked["rank_delta"].null_count() == ranked.height  # 無上週快照

    # 上週快照：記憶體第 2、IC設計第 1 → 本週記憶體 ΔRank=+1（跳升）
    prev = pl.DataFrame({"sub_industry": ["IC設計", "記憶體"], "radar_rank": [1, 2]})
    ranked2 = rank_flows(flows, by="net_flow_4d", prev=prev)
    mem_delta = ranked2.filter(pl.col("sub_industry") == "記憶體")["rank_delta"][0]
    assert mem_delta == 1


def test_rank_flows_min_members_filter():
    membership = pl.DataFrame(
        {"sub_industry": ["記憶體", "記憶體", "孤鳥"], "stock_id": ["2337", "8299", "2454"]}
    )
    flows = compute_fund_flows(membership, _institutional(6), short_window=2, long_window=4)
    ranked = rank_flows(flows, by="net_flow_4d", min_members=2)
    assert "孤鳥" not in ranked["sub_industry"].to_list()


# ─── load_market_history ──────────────────────────────────────────────────────


def test_load_market_history_merges_old_and_new_schema(tmp_path: Path):
    pl.DataFrame(
        {"date": [D0], "stock_id": ["2330"], "name": ["台積電"],
         "close": [1000.0], "volume": [5000]}
    ).write_parquet(tmp_path / "daily_20260601.parquet")
    pl.DataFrame(
        {"date": [D0 + timedelta(days=1)], "stock_id": ["2330"],
         "close": [1010.0], "volume": [6000]}
    ).write_parquet(tmp_path / "daily_all_20260602.parquet")

    df = load_market_history(tmp_path, n_days=250)
    assert df.height == 2
    assert df.columns == ["date", "stock_id", "close", "volume"]


def test_load_market_history_n_days_cut(tmp_path: Path):
    for i in range(4):
        d = D0 + timedelta(days=i)
        pl.DataFrame(
            {"date": [d], "stock_id": ["2330"], "close": [100.0 + i], "volume": [1]}
        ).write_parquet(tmp_path / f"daily_all_2026060{i + 1}.parquet")
    df = load_market_history(tmp_path, n_days=2)
    assert df["date"].n_unique() == 2
    assert df["date"].min() == D0 + timedelta(days=2)


def test_load_market_history_empty(tmp_path: Path):
    df = load_market_history(tmp_path)
    assert df.is_empty()


def test_baskets_clip_extreme_return():
    """減資/分割造成的 -74% 假日報酬被夾在 -10%（毒化防護）。"""
    ds = _dates(3)
    rows = []
    for d, c in zip(ds, [546.0, 143.0, 145.0]):  # day2 -73.8%（未還原分割）
        rows.append({"date": d, "stock_id": "2327", "close": c})
    membership = pl.DataFrame({"sub_industry": ["被動元件"], "stock_id": ["2327"]})
    df = compute_subindustry_baskets(membership, pl.DataFrame(rows))
    assert df.sort("date")["basket_return_pct"][0] == pytest.approx(-10.0)
    # 停用 clip 時保留原值
    df_raw = compute_subindustry_baskets(
        membership, pl.DataFrame(rows), clip_daily_return_pct=0
    )
    assert df_raw.sort("date")["basket_return_pct"][0] < -70


def test_load_market_history_includes_stock_day(tmp_path: Path):
    """stock_day_*（上櫃候選股月快取）納入全市場日線（trade_volume → volume）。"""
    pl.DataFrame(
        {"date": [D0], "stock_id": ["2330"], "close": [1000.0], "volume": [5000]}
    ).write_parquet(tmp_path / "daily_all_20260601.parquet")
    pl.DataFrame(
        {"date": [D0], "stock_id": ["8299"], "close": [700.0], "trade_volume": [3000],
         "open": [690.0], "high": [710.0], "low": [688.0]}
    ).write_parquet(tmp_path / "stock_day_8299_202606.parquet")

    df = load_market_history(tmp_path, n_days=250)
    assert set(df["stock_id"].to_list()) == {"2330", "8299"}
    assert df.filter(pl.col("stock_id") == "8299")["volume"][0] == 3000


def test_otc_institutional_lag(tmp_path: Path):
    from tw_screener.analysis.rotation import otc_institutional_lag

    assert otc_institutional_lag(tmp_path) == (0, None, None)
    for d in ("20260609", "20260610", "20260611"):
        pl.DataFrame({"x": [1]}).write_parquet(tmp_path / f"institutional_{d}.parquet")
    pl.DataFrame({"x": [1]}).write_parquet(tmp_path / "institutional_otc_20260609.parquet")
    lag, listed_max, otc_max = otc_institutional_lag(tmp_path)
    assert lag == 2 and listed_max == "20260611" and otc_max == "20260609"


def test_load_market_history_includes_otc_daily(tmp_path: Path):
    pl.DataFrame({"date": [D0], "stock_id": ["2330"], "close": [1000.0], "volume": [1]}
                 ).write_parquet(tmp_path / "daily_all_20260601.parquet")
    pl.DataFrame({"date": [D0], "stock_id": ["8299"], "name": ["群聯"],
                  "trade_volume": [3000], "close": [2310.0]}
                 ).write_parquet(tmp_path / "otc_daily_20260601.parquet")
    df = load_market_history(tmp_path, n_days=250)
    assert set(df["stock_id"].to_list()) == {"2330", "8299"}
    assert df.filter(pl.col("stock_id") == "8299")["volume"][0] == 3000
