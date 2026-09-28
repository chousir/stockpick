"""tests/backtest/test_intra_pick.py — M-Pick2 族群內因子錦標賽純函式（docs/32；全合成資料）。

期望值皆由測試內手算（註解附算式），不回抄實作。
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest.factor_lab import moving_block_bootstrap_ci
from tw_screener.report.shortlist import sector_trend_table


def _cfg(**kw: object) -> ip.IntraPickConfig:
    """小窗設定：mom (skip 2, lookback 5)、high 窗 4、gate 回看 2、主窗 r+2、無揭露窗。"""
    base: dict[str, object] = {
        "mom_skip": 2, "mom_lookback": 5, "high_window": 4, "gate_disc_lookback": 2,
        "horizon": 2, "disclose_horizons": (), "n_boot": 200,
    }
    base.update(kw)
    return ip.IntraPickConfig(**base)  # type: ignore[arg-type]


def _days(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _price(closes: dict[str, list[float | None]], days: list[date]) -> pl.DataFrame:
    rows = [
        {"date": d, "stock_id": sid, "close": c, "volume": 1e6}
        for sid, series in closes.items()
        for d, c in zip(days, series, strict=True)
        if c is not None
    ]
    return pl.DataFrame(rows)


def _row(df: pl.DataFrame, sid: str, d: date) -> dict:
    return df.filter((pl.col("stock_id") == sid) & (pl.col("date") == d)).row(0, named=True)


# ─── 交易日曆與價格因子 ───────────────────────────────────────────────────────


def test_trading_calendar_drops_sparse_days() -> None:
    d = _days(3)
    price = pl.DataFrame(
        {"date": [d[0], d[0], d[1], d[2], d[2]], "stock_id": ["A", "B", "A", "A", "B"],
         "close": [1.0] * 5}
    )
    assert ip.trading_calendar(price, 2) == [d[0], d[2]]


def test_price_features_values() -> None:
    d = _days(10)
    price = _price(
        {"A": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
         "B": [20, 19, 18, 17, 16, 15, 14, 13, 12, 11]},
        d,
    )
    f = ip.price_features(price, d, _cfg())
    a6 = _row(f, "A", d[6])
    assert a6["mom_6_1"] == pytest.approx(14 / 11 - 1)  # close[4]/close[1]−1
    assert a6["high52_near"] == pytest.approx(1.0)      # 16 / max(13..16)
    assert a6["amount_million"] == pytest.approx(16.0)  # 16×1e6/1e6
    assert _row(f, "B", d[6])["high52_near"] == pytest.approx(14 / 17)  # 14 / max(17..14)
    assert _row(f, "A", d[4])["mom_6_1"] is None        # 回看不足 5 筆 → null
    assert _row(f, "A", d[2])["high52_near"] is None    # 窗不足 4 筆 → null


def test_price_features_discontinuity_guards() -> None:
    d = _days(10)
    price = _price({"C": [10, 10, 10, 10, 10, 20, 20, 20, 20, 20]}, d)  # idx5 +100%
    f = ip.price_features(price, d, _cfg())
    # mom 窗 (t−5, t−2] 含 idx5 → t=7..9 null；t=6 窗 (1,4] 乾淨 → 10/10−1＝0
    assert _row(f, "C", d[6])["mom_6_1"] == pytest.approx(0.0)
    assert all(_row(f, "C", d[t])["mom_6_1"] is None for t in (7, 8, 9))
    # high 窗內日報酬 t−2..t 含 idx5 → t=5..7 null；t=8 → 20/20
    assert all(_row(f, "C", d[t])["high52_near"] is None for t in (5, 6, 7))
    assert _row(f, "C", d[8])["high52_near"] == pytest.approx(1.0)
    # gate：近 2 筆日報酬含 idx5 → t=5,6 為真；t=7 已出窗
    assert [_row(f, "C", d[t])["gate_disc"] for t in (4, 5, 6, 7)] == [False, True, True, False]
    # r+2 前瞻窗＝日報酬 t+2..t+3：t=2,3 含 idx5；
    # t=4 的 entry 當日（idx5）本身不影響 close[7]/close[5]
    assert [_row(f, "C", d[t])["fwd_disc_2"] for t in (1, 2, 3, 4)] == [False, True, True, False]


def test_price_features_gap_nulls_factor() -> None:
    d = _days(10)
    price = _price({"D": [10, 11, 12, None, 14, 15, 16, 17, 18, 19]}, d)  # idx3 停牌
    f = ip.price_features(price, d, _cfg())
    assert _row(f, "D", d[6])["mom_6_1"] is None       # 往回 5 筆跨到 idx0（日曆差 6≠5）
    assert _row(f, "D", d[9])["mom_6_1"] == pytest.approx(17 / 14 - 1)  # 窗 idx4..9 連續


# ─── EPS／月營收 point-in-time ─────────────────────────────────────────────────


def test_latest_available_quarter_conservative_deadlines() -> None:
    avail = ip.IntraPickConfig().eps_available_from
    assert ip.latest_available_quarter(date(2024, 8, 31), avail) == 2024 * 4 + 0  # Q2 9/1 才可用
    assert ip.latest_available_quarter(date(2024, 9, 1), avail) == 2024 * 4 + 1
    assert ip.latest_available_quarter(date(2025, 3, 31), avail) == 2024 * 4 + 2  # Q4 4/1 才可用
    assert ip.latest_available_quarter(date(2025, 4, 1), avail) == 2024 * 4 + 3


def test_eps_accel_and_asof_no_fallback() -> None:
    quarters = [(2023, 1, 1.0), (2023, 2, 1.2), (2023, 3, 1.1), (2023, 4, 1.3),
                (2024, 1, 1.5), (2024, 2, 2.0)]
    fin = pl.DataFrame(
        {"stock_id": ["S"] * 6, "year": [q[0] for q in quarters],
         "quarter": [q[1] for q in quarters], "eps": [q[2] for q in quarters]}
    )
    qclose = ip.quarter_end_close(
        pl.DataFrame({"date": [date(2024, 6, 27), date(2024, 6, 28)], "stock_id": ["S", "S"],
                      "close": [29.0, 30.0]})
    )
    accel = ip.eps_accel_table(fin, qclose)
    # 2024Q2：[(2.0−1.2) − (1.5−1.0)] / 30 ＝ 0.3/30
    got = accel.filter(pl.col("qi") == 2024 * 4 + 1)["eps_accel"].item()
    assert got == pytest.approx(0.3 / 30)
    keys = pl.DataFrame({"date": [date(2024, 8, 30), date(2024, 9, 6)], "stock_id": ["S", "S"]})
    asof = ip.eps_asof(keys, accel, ip.IntraPickConfig()).sort("date")
    # 08-30 應可得＝2024Q1（缺 2022Q4 → null），不得回退更舊季
    assert asof["eps_accel"].to_list()[0] is None
    assert asof["eps_accel"].to_list()[1] == pytest.approx(0.3 / 30)


def _monthly(sid: str, values: dict[tuple[int, int], float]) -> pl.DataFrame:
    return pl.DataFrame(
        {"stock_id": [sid] * len(values), "year": [k[0] for k in values],
         "month": [k[1] for k in values], "revenue": list(values.values())}
    )


def test_revenue_accel_and_asof() -> None:
    vals = {(2023, m): 100.0 for m in range(1, 13)}
    vals |= {(2024, m): 110.0 for m in (1, 2, 3)} | {(2024, m): 130.0 for m in (4, 5, 6)}
    zero = {(2023, m): 0.0 for m in range(1, 13)} | {(2024, m): 5.0 for m in range(1, 7)}
    rev = pl.concat([_monthly("S", vals), _monthly("Z", zero)])
    acc = ip.revenue_accel_table(rev)
    mi = 2024 * 12 + 5  # 2024-06
    # YoY3(6月)＝390/300−1＝0.30；YoY3(3月)＝330/300−1＝0.10 → 0.20
    got = acc.filter((pl.col("stock_id") == "S") & (pl.col("mi") == mi))["rev_accel"].item()
    assert got == pytest.approx(0.20)
    z = acc.filter((pl.col("stock_id") == "Z") & (pl.col("mi") == mi))
    assert z["rev_accel"].item() is None  # 前一年分母 0 → null
    keys = pl.DataFrame({"date": [date(2024, 7, 10), date(2024, 7, 11)], "stock_id": ["S", "S"]})
    asof = ip.revenue_asof(keys, acc, 11).sort("date")["rev_accel"].to_list()
    # 07-10 應可得＝5 月（缺 2022-12 → null、不回退）；07-11 起＝6 月
    assert asof[0] is None
    assert asof[1] == pytest.approx(0.20)


# ─── 族群桶與宇宙 ─────────────────────────────────────────────────────────────


def test_sector_bucket_table_matches_production_and_min_members() -> None:
    d = date(2024, 3, 1)
    trend = pl.DataFrame(
        {"sub_industry": ["A", "B", "C", "D", "E"], "date": [d] * 5,
         "trend_score": [90.0, 70.0, 70.0, 10.0, 99.0]}
    )
    baskets = pl.DataFrame(
        {"sub_industry": ["A", "B", "C", "D", "E"], "date": [d] * 5,
         "members_priced": [5, 5, 4, 6, 3]}
    )
    got = ip.sector_bucket_table(trend, baskets, [d], 4, 5)
    assert "E" not in got["sub_industry"].to_list()  # 3 檔 < rotation.min_members
    want = sector_trend_table(trend.filter(pl.col("sub_industry") != "E"), 5)
    assert got.drop("date").sort("sub_industry").equals(want.sort("sub_industry"))


def test_mark_universe_gates_and_target_mask() -> None:
    rows = [
        # (id, bucket, ma60, amount, gate_disc, fwd_disc, r2)
        ("ok", 1, 5.0, 200.0, False, False, 1.0),
        ("b3", 3, 5.0, 200.0, False, False, 1.0),
        ("ext", 1, 16.0, 200.0, False, False, 1.0),
        ("neg", 1, -1.0, 200.0, False, False, 1.0),
        ("illiq", 1, 5.0, 50.0, False, False, 1.0),
        ("disc", 1, 5.0, 200.0, True, False, 1.0),
        ("nob", None, 5.0, 200.0, False, False, 1.0),
        ("fwd", 1, 12.0, 200.0, False, True, 1.0),
    ]
    df = pl.DataFrame(
        rows, schema=["stock_id", "trend_bucket", "ma60_dist_pct", "amount_million",
                      "gate_disc", "fwd_disc_2", "r2"], orient="row",
    )
    out = ip.mark_universe(df, _cfg()).sort("stock_id")
    main = dict(zip(out["stock_id"], out["in_main"], strict=True))
    alls = dict(zip(out["stock_id"], out["in_all"], strict=True))
    assert main == {"ok": True, "b3": False, "ext": False, "neg": False, "illiq": False,
                    "disc": False, "nob": False, "fwd": True}
    assert alls["b3"] is True and alls["ext"] is False
    fwd = out.filter(pl.col("stock_id") == "fwd").row(0, named=True)
    assert fwd["r2"] is None                     # 前瞻窗有不連續 → target 作廢
    assert fwd["_band_dist"] == pytest.approx(2.0)  # 12 − 帶上緣 10
    assert fwd["log_amount"] == pytest.approx(math.log(200.0))


# ─── 量尺 ─────────────────────────────────────────────────────────────────────


def _panel(week_rows: list[tuple[date, str, str, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        week_rows, schema=["date", "sub_industry", "stock_id", "fx", "r2"], orient="row"
    ).with_columns(pl.lit(100.0).alias("amount_million"), pl.lit(0.0).alias("_band_dist"))


def test_weekly_ic_is_industry_neutral() -> None:
    w1, w2, w3 = date(2024, 1, 5), date(2024, 1, 12), date(2024, 1, 19)
    rows = [
        # w1：G1 同向、G2 反向 → 置中排名乘積 +2/9 與 −2/9 相抵 → IC 0
        *[(w1, "G1", f"a{k}", float(k), float(k)) for k in range(3)],
        *[(w1, "G2", f"b{k}", float(k), float(2 - k)) for k in range(3)],
        (w1, "G3", "lone", 1.0, 99.0),  # 單檔組不計
        # w2：兩組皆同向、但組間水準差很大（G1 高 G2 低）→ 產業中性 IC 仍＝1
        *[(w2, "G1", f"a{k}", float(k), 10.0 + k) for k in range(3)],
        *[(w2, "G2", f"b{k}", float(k), 1.0 + k) for k in range(3)],
        # w3：只有 4 檔 < min_names 6 → 不計
        *[(w3, "G1", f"a{k}", float(k), float(k)) for k in range(4)],
    ]
    got = ip.weekly_ic(_panel(rows), "fx", "r2", min_group=2, min_names=6)
    assert got["date"].to_list() == [w1, w2]
    assert got["ic"].to_list() == pytest.approx([0.0, 1.0])
    assert got["n_names"].to_list() == [6, 6]


def test_pick_table_tie_breaks_and_excess() -> None:
    w = date(2024, 1, 5)
    df = pl.DataFrame(
        {"date": [w] * 3, "sub_industry": ["G"] * 3, "stock_id": ["A", "B", "C"],
         "fx": [3.0, 3.0, 1.0], "amount_million": [100.0, 200.0, 300.0],
         "_band_dist": [2.0, 0.0, 0.0], "r2": [5.0, 1.0, 3.0]}
    )
    got = ip.pick_table(df, "fx", "r2", 2).row(0, named=True)
    assert got["pick"] == "B"        # fx 同分 → 成交額大者
    assert got["base_pick"] == "C"   # 帶距同 0 → 成交額大者
    assert got["m2"] == pytest.approx(1.0 - 3.0)  # 組均 (5+1+3)/3＝3
    assert got["m3"] == pytest.approx(1.0 - 3.0)
    wk = ip.weekly_pick(ip.pick_table(df, "fx", "r2", 2)).row(0, named=True)
    assert wk["same_pick_rate"] == 0.0


# ─── 推論與裁決 ───────────────────────────────────────────────────────────────


def test_segment_means_even_and_short() -> None:
    assert ip.segment_means([1, 1, 1, 1, 1, 2, 2, 2, 2, 2], 5) == [1.0, 1.0, 1.5, 2.0, 2.0]
    assert ip.segment_means([1.0, 2.0, 3.0], 5) == [1.0, None, 2.0, None, 3.0]


def test_bootstrap_alpha_default_unchanged_and_bonferroni_wider() -> None:
    rng = random.Random(7)
    vals = [rng.gauss(0.05, 0.2) for _ in range(120)]
    assert moving_block_bootstrap_ci(vals, 5) == moving_block_bootstrap_ci(vals, 5, alpha=0.05)
    s = ip.summarize(vals, 5, _cfg(n_boot=500))
    assert s.mean == pytest.approx(sum(vals) / len(vals))
    assert s.ci_bonf[0] <= s.ci[0] and s.ci_bonf[1] >= s.ci[1]


def _result(**kw: object) -> ip.FactorResult:
    base: dict[str, object] = {
        "factor": "fx", "horizon": 20, "coverage": 0.9,
        "ic": ip.SeriesStat(200, 0.05, (0.01, 0.09), (0.002, 0.10)),
        "segments": [0.1] * 5, "regime_slices": pl.DataFrame(),
        "regime_label": ip.REGIME_ROBUST,
        "m2": ip.SeriesStat(200, 0.5, (0.1, 0.9), (0.0, 1.0)),
        "m3": ip.SeriesStat(200, 0.2, (-0.3, 0.7), (-0.4, 0.8)),
        "same_pick_rate": 0.3, "ic_weekly": pl.DataFrame(),
    }
    base.update(kw)
    return ip.FactorResult(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "verdict", "failed"),
    [
        ({}, ip.VERDICT_PASS, ()),
        ({"ic": ip.SeriesStat(200, 0.05, (0.01, 0.09), (-0.01, 0.1))}, ip.VERDICT_MARGINAL, ()),
        ({"ic": ip.SeriesStat(200, 0.02, (0.005, 0.04), (0.0, 0.05))}, ip.VERDICT_FAIL, ("C1",)),
        ({"segments": [0.1, 0.1, 0.1, -0.1, -0.1]}, ip.VERDICT_FAIL, ("C3",)),
        ({"regime_label": "bull-only"}, ip.VERDICT_FAIL, ("C4",)),
        ({"m3": ip.SeriesStat(200, -0.1, (-0.5, 0.3), (-0.6, 0.4))}, ip.VERDICT_FAIL, ("C5",)),
        ({"ic": ip.SeriesStat(200, 0.04, (-0.01, 0.09), (-0.02, 0.1))}, ip.VERDICT_FAIL, ("C2",)),
        (
            {"ic": ip.SeriesStat(200, -0.01, (-0.05, 0.03), (-0.06, 0.04)),
             "segments": [-0.1] * 5},
            ip.VERDICT_NONE, ("C1", "C2", "C3", "C4"),
        ),
        (
            {"ic": ip.SeriesStat(200, -0.05, (-0.09, -0.01), (-0.1, 0.0)),
             "segments": [-0.1] * 5},
            ip.VERDICT_REVERSE, ("C1", "C2", "C3", "C4"),
        ),
        ({"coverage": 0.5}, ip.VERDICT_COVERAGE, ()),
        ({"ic": ip.SeriesStat(50, 0.05, (0.01, 0.09), (0.002, 0.1))}, ip.VERDICT_THIN, ()),
    ],
)
def test_classify_decision_table(kw: dict, verdict: str, failed: tuple[str, ...]) -> None:
    assert ip.classify(_result(**kw), ip.IntraPickConfig()) == (verdict, failed)


def _synthetic(sign: float, weeks: int = 120) -> tuple[pl.DataFrame, pl.DataFrame]:
    """3 組 × 4 檔 × weeks 週；r2＝sign·2k＋雜訊（組內因子 k 單調）；現行鍵與 target 無關。"""
    rng = random.Random(11)
    rows, regs = [], []
    for w in range(weeks):
        d = date(2022, 1, 7) + timedelta(weeks=w)
        regs.append({"date": d, "regime": "進攻" if w < weeks // 2 else "中性"})
        for g in range(3):
            for k in range(4):
                rows.append({
                    "date": d, "sub_industry": f"G{g}", "stock_id": f"{g}{k}",
                    "fx": float(k) + rng.random() * 0.1,
                    "r2": sign * 2.0 * k + rng.gauss(0, 3),
                    "amount_million": 100.0 + rng.random(),
                    "_band_dist": float((w + k) % 4), "in_main": True,
                })
    return pl.DataFrame(rows), pl.DataFrame(regs)


def test_evaluate_factor_end_to_end_pass_and_reverse() -> None:
    cfg = _cfg(n_boot=300)
    df, regs = _synthetic(+1.0)
    res = ip.evaluate_factor(df, "fx", cfg, regimes=regs)
    assert res.verdict == ip.VERDICT_PASS
    assert res.ic.n == 120 and res.coverage == pytest.approx(1.0)
    assert res.m2.mean > 0 and res.m3.mean > 0
    assert set(res.regime_slices["regime"].to_list()) >= {"進攻", "中性"}
    rev_df, _ = _synthetic(-1.0)
    assert ip.evaluate_factor(rev_df, "fx", cfg, regimes=regs).verdict == ip.VERDICT_REVERSE


def test_config_from_settings_reads_production_gates() -> None:
    cfg = ip.IntraPickConfig.from_settings(
        {
            "rotation": {"min_members": 5},
            "group_analysis": {"price_discontinuity_pct": 12.0},
            "picks": {"core_ext_ma60_max_pct": 14.0, "shortlist": {"max_bucket_for_top": 3}},
            "propicks_flags": {"low_liquidity_amount": 80},
            "backtest": {"intra_pick": {"horizon_td": 10, "eps_available_from": {"q2": [0, 9, 2]}}},
        }
    )
    assert (cfg.min_sector_members, cfg.disc_pct, cfg.horizon) == (5, 12.0, 10)
    assert cfg.eps_available_from[2] == (0, 9, 2) and cfg.eps_available_from[1] == (0, 5, 31)
    assert cfg.shortlist.ext_max_pct == 14.0 and cfg.shortlist.max_bucket_for_top == 3
    assert cfg.shortlist.low_liquidity_amount == 80.0
    assert cfg.block_len() == 3  # ceil(10/5)+1
