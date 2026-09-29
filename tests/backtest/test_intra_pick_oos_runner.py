"""tests/backtest/test_intra_pick_oos_runner.py — M-Pick3a 保留樣本快照編排（離線合成快取）。"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import yaml

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_oos as oos
from tw_screener.backtest.intra_pick_oos_runner import (
    _adjustment_lines,
    adjustment_validation,
    build_holdout_stock_weeks,
    local_reconcile,
    reconcile_frames,
    render_report,
    sample_reconcile_dates,
)
from tw_screener.backtest.rotation_efficacy import weekly_snapshot_dates
from tw_screener.data.finmind import (
    _DIVIDEND_RESULT_SCHEMA,
    _MONTH_REVENUE_SCHEMA,
    _STOCK_PRICE_SCHEMA,
)

SECTORS = {"甲": ["1101", "1102", "1103"], "乙": ["2201", "2202", "2203"],
           "丙": ["3301", "3302", "3303"]}
EX_SID, EX_DATE, EX_RATIO = "1101", date(2015, 3, 10), 1.05
UNCOVERED = "3303"  # 沒有 dividend_ 檔 → target 全 null


def _weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2013, 12, 2), date(2015, 9, 30))


def _closes(sid: str) -> list[float]:
    rng = random.Random(int(sid))
    px, out = 100.0, []
    for d in DAYS:
        px *= 1 + rng.uniform(-0.02, 0.021)
        if sid == EX_SID and d == EX_DATE:
            px /= EX_RATIO  # 除權息當日價格照比值下跳（真實世界的樣子）
        out.append(round(px, 2))
    return out


def _write_cache(cache: Path) -> None:
    fm = cache / "finmind"
    fm.mkdir(parents=True)
    for sids in SECTORS.values():
        for sid in sids:
            closes = _closes(sid)
            pl.DataFrame(
                {
                    "date": DAYS, "stock_id": [sid] * len(DAYS), "open": closes,
                    "high": closes, "low": closes, "close": closes,
                    "volume": [3_000_000] * len(DAYS),
                    "amount": [c * 3_000_000 for c in closes],
                    "transactions": [1000] * len(DAYS),
                },
                schema=_STOCK_PRICE_SCHEMA,
            ).write_parquet(fm / f"price_{sid}.parquet")
            if sid != UNCOVERED:
                rows = []
                if sid == EX_SID:
                    before = closes[DAYS.index(EX_DATE) - 1]
                    rows.append({
                        "ex_date": EX_DATE, "stock_id": sid, "before_price": before,
                        "after_price": before / EX_RATIO, "reference_price": before / EX_RATIO,
                        "dividend_value": before - before / EX_RATIO, "event_type": "息",
                    })
                pl.DataFrame(rows, schema=_DIVIDEND_RESULT_SCHEMA).write_parquet(
                    fm / f"dividend_{sid}.parquet"
                )
            months = [(y, m) for y in (2013, 2014, 2015) for m in range(1, 13)]
            rng = random.Random(int(sid) + 7)
            pl.DataFrame(
                [{"stock_id": sid, "year": y, "month": m, "revenue": 1e9 * rng.uniform(0.8, 1.2),
                  "create_date": None} for y, m in months],
                schema=_MONTH_REVENUE_SCHEMA,
            ).write_parquet(fm / f"month_revenue_{sid}.parquet")


def _settings(tmp: Path) -> tuple[Path, dict[str, Any]]:
    with open("config/settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["paths"]["cache_dir"] = str(tmp / "cache")
    cfg["backtest"]["intra_pick"]["calendar_min_names"] = 2
    cfg["rotation"]["min_members"] = 2
    cfg["regime"]["breadth"]["min_priced"] = 2
    cfg["backtest"]["intra_pick_oos"].update(
        {"holdout_start": "2015-01-01", "holdout_end": "2015-06-30",
         "output_dir": str(tmp / "out")}
    )
    conf = tmp / "config"
    conf.mkdir()
    path = conf / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (conf / "concepts.yaml").write_text(
        yaml.safe_dump(
            {"concept_themes": [], "concepts": {s: sec for sec, ss in SECTORS.items() for s in ss}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path, cfg


@pytest.fixture()
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[pl.DataFrame, dict[str, Any]]:
    def _no_net(*a: object, **k: object) -> object:
        raise AssertionError("保留樣本快照不應打網")

    monkeypatch.setattr(httpx, "get", _no_net)
    _write_cache(tmp_path / "cache")
    path, cfg = _settings(tmp_path)
    return build_holdout_stock_weeks(
        cfg, path, ip.IntraPickConfig.from_settings(cfg), oos.HoldoutConfig.from_settings(cfg)
    )


def test_holdout_snapshot_weeks_columns_and_uncovered(
    built: tuple[pl.DataFrame, dict[str, Any]],
) -> None:
    sw, meta = built
    weeks = sorted(sw["date"].unique().to_list())
    in_hold = [d for d in weekly_snapshot_dates(DAYS) if date(2015, 1, 1) <= d <= date(2015, 6, 30)]
    assert weeks == in_hold
    need = {
        "sub_industry", "trend_bucket", "ma60_dist_pct", "amount_million", "gate_disc",
        "high52_near", "rev_accel", "mom_6_1", "log_amount", "neg_band_dist", "in_main", "in_all",
        "regime", "div_covered", "r10", "r20", "r40", "bad_evt_20", "fwd_disc_20",
    }
    assert need <= set(sw.columns)
    assert sw.height == 9 * len(weeks)
    unc = sw.filter(pl.col("stock_id") == UNCOVERED)
    assert unc["r20"].null_count() == unc.height and not unc["div_covered"].any()
    assert sw.filter(pl.col("stock_id") != UNCOVERED)["r20"].null_count() == 0  # 全在期內到期
    assert sw["high52_near"].null_count() == 0  # 2013-12 起 → 2015-01 已滿 250 日窗
    assert sw["rev_accel"].null_count() == 0
    assert sw["regime"].null_count() == 0
    assert meta["regime"]["flow_score"].null_count() == meta["regime"].height  # 降級路徑


def test_holdout_snapshot_total_return_matches_hand_calc(
    built: tuple[pl.DataFrame, dict[str, Any]],
) -> None:
    sw, _ = built
    closes = _closes(EX_SID)
    seen: set[float] = set()
    for d in sorted(sw["date"].unique().to_list())[4:12]:  # 跨 2015-03-10 除息日前後的快照
        t = DAYS.index(d)
        e, x = t + 1, t + 1 + 20
        adj = EX_RATIO if DAYS[e] < EX_DATE <= DAYS[x] else 1.0
        seen.add(adj)
        expect = (closes[x] / closes[e] * adj - 1) * 100
        got = sw.filter((pl.col("stock_id") == EX_SID) & (pl.col("date") == d))["r20"].item()
        assert got == pytest.approx(expect), d
    assert seen == {1.0, EX_RATIO}  # 窗內含／不含除息事件兩種情形都驗到


def test_render_report_runs_without_reconcile(built: tuple[pl.DataFrame, dict[str, Any]]) -> None:
    sw, meta = built
    cfg = ip.IntraPickConfig()
    hcfg = oos.HoldoutConfig(start=date(2015, 1, 1), end=date(2015, 6, 30))
    lines = render_report(sw, meta, cfg, hcfg, {}, {"週快照寬表": "x.parquet"})
    text = "\n".join(lines)
    assert "未執行" in text  # 核價缺席如實標註
    assert "不含任何因子×target 統計" in text
    assert "in_main" not in text.split("## 1.")[1]  # 主宇宙規模不在報告裡數


# ── 核價工具 ─────────────────────────────────────────────────────────────────


def test_sample_reconcile_dates_per_year_and_stable() -> None:
    cal = _weekdays(date(2014, 1, 1), date(2016, 12, 31))
    a = sample_reconcile_dates(cal, (2014, 2016), 3, seed=1)
    assert len(a) == 9 and [d.year for d in a] == [2014] * 3 + [2015] * 3 + [2016] * 3
    assert a == sample_reconcile_dates(cal, (2014, 2016), 3, seed=1)
    # 換年份範圍不影響其他年的抽樣（每年獨立種子）
    assert sample_reconcile_dates(cal, (2015, 2015), 3, seed=1) == a[3:6]


def test_reconcile_frames_close_volume_and_missing() -> None:
    d = date(2015, 3, 16)
    fm = pl.DataFrame(
        {"date": [d, d, d], "stock_id": ["A", "B", "C"], "close": [100.0, 50.0, 10.0],
         "volume": [1000, 2000, 3000]}
    )
    ref = pl.DataFrame(
        {"date": [d, d, d, d], "stock_id": ["A", "B", "D", "Z"],
         "close_ref": [100.0, 51.0, 20.0, 5.0], "volume_ref": [1000, 1999, 1, 1]}
    )
    recon, s = reconcile_frames(fm, ref, 0.5, fm_ids={"A", "B", "C", "D"})
    assert (s["n_close"], s["n_close_ok"]) == (2, 1)  # B 差 1.96% > 0.5%
    assert (s["n_vol"], s["n_vol_exact"], s["n_vol_lot"]) == (2, 1, 2)  # B 差 1 股 < 1 張
    assert s["missing_in_finmind"] == 1  # D：FinMind 有檔、當日缺列；Z 不在 fm_ids 不算
    assert recon.filter(~pl.col("within_tol"))["stock_id"].to_list() == ["B"]


def test_local_reconcile_splits_market_and_period() -> None:
    d_in, d_out = date(2021, 12, 30), date(2022, 1, 4)
    fm = pl.DataFrame(
        {"date": [d_in, d_in, d_out, d_out], "stock_id": ["L", "O", "L", "O"],
         "close": [10.0, 20.0, 11.0, 21.0], "volume": [1, 2, 3, 4]}
    )
    listed = pl.DataFrame(
        {"date": [d_in, d_out], "stock_id": ["L", "L"], "close_ref": [10.0, 11.0],
         "volume_ref": [1, 3]}
    )
    stock_day = pl.DataFrame(  # 個股月檔含上市 L 與上櫃 O；O 從未出現在 daily_* → 歸上櫃
        {"date": [d_in, d_in, d_out], "stock_id": ["L", "O", "O"],
         "close_ref": [10.0, 20.5, 21.0], "volume_ref": [1, 2, 4]}
    )
    otc_daily = pl.DataFrame(  # TPEX OpenAPI 日總量（官方）：O 在 d_out 的量 5000 股
        {"date": [d_out], "stock_id": ["O"], "close_ref": [21.0], "volume_ref": [5000]}
    )
    out = local_reconcile(
        fm, listed, stock_day, date(2021, 12, 31), 0.5, {"L", "O"}, otc_daily_ref=otc_daily
    )
    assert set(out) == {
        "listed_holdout", "listed_after", "otc_holdout", "otc_after", "otcdaily_after"
    }
    od = out["otcdaily_after"]["stats"]
    assert (od["n_vol_fm_gt"], od["n_vol_ref_gt"]) == (0, 1)  # 官方 5000 − FinMind 4 ≥ 1 張
    oh = out["otc_holdout"]["stats"]
    assert (oh["n_close"], oh["n_close_ok"]) == (1, 0)  # O 20 vs 20.5 → 差 2.4%
    assert out["otc_after"]["stats"]["n_close_ok"] == 1
    assert out["listed_holdout"]["span"] == (d_in, d_in)


# ── 除權息還原驗證（IO 路徑：全期自洽＋官方預告表快取；零新請求）─────────────────────────


def _write_official(twse: Path, cash: float) -> None:
    """官方預告表：EX_SID 事件（現金＝cash）＋兩筆 FinMind 沒有的事件（一筆定出涵蓋起點）。"""
    pl.DataFrame(
        {"ex_date": [EX_DATE, date(2015, 4, 1), date(2015, 1, 5)],
         "stock_id": [EX_SID, "1102", "2201"], "name": ["x", "y", "z"],
         "type": ["息", "息", "息"], "cash_dividend": [cash, 1.0, 0.5],
         "stock_dividend_ratio": [None, None, None]},
        schema={"ex_date": pl.Date, "stock_id": pl.Utf8, "name": pl.Utf8, "type": pl.Utf8,
                "cash_dividend": pl.Float64, "stock_dividend_ratio": pl.Float64},
    ).write_parquet(twse / "dividend_calendar_20150301.parquet")


@pytest.fixture()
def val_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any], ip.IntraPickConfig, oos.HoldoutConfig, dict[str, Any]]:
    def _no_net(*a: object, **k: object) -> object:
        raise AssertionError("驗證不應打網")

    monkeypatch.setattr(httpx, "get", _no_net)
    _write_cache(tmp_path / "cache")
    path, cfg = _settings(tmp_path)
    (tmp_path / "cache" / "twse").mkdir()
    icfg = ip.IntraPickConfig.from_settings(cfg)
    hcfg = oos.HoldoutConfig.from_settings(cfg)
    _, meta = build_holdout_stock_weeks(cfg, path, icfg, hcfg)
    return tmp_path, cfg, icfg, hcfg, meta


def test_adjustment_validation_without_official_calendar(
    val_env: tuple[Path, dict[str, Any], ip.IntraPickConfig, oos.HoldoutConfig, dict[str, Any]],
) -> None:
    _, cfg, icfg, hcfg, meta = val_env
    av = adjustment_validation(cfg, meta, icfg, hcfg)
    b = av["before"]
    # 合成資料只有 EX_SID 一筆事件、before_price＝前一列收盤 → 全期自洽 1/1（分毫不差）
    assert (b["n_events"], b["n_checked"], b["n_ok"], b["n_exact"]) == (1, 1, 1, 1)
    assert av["events"] is None and av["windows"] is None  # 無官方預告表 → (2)(3) 未執行
    assert av["windows_ratio"] is None
    r = av["ratio"]  # (1b)：合成事件 after＝before/1.05、D＝before−after → 比值誤差 0
    assert r["n_checked"] == 1 and r["err_abs_max"] == pytest.approx(0.0, abs=1e-9)
    txt = "\n".join(_adjustment_lines(av, hcfg))
    assert "**PASS**" in txt and "未執行" in txt and "**FAIL**" not in txt and "(1b)" in txt


def test_adjustment_validation_against_official_calendar(
    val_env: tuple[Path, dict[str, Any], ip.IntraPickConfig, oos.HoldoutConfig, dict[str, Any]],
) -> None:
    tmp, cfg, icfg, hcfg, meta = val_env
    twse = tmp / "cache" / "twse"
    before = _closes(EX_SID)[DAYS.index(EX_DATE) - 1]
    after = before / EX_RATIO
    cash = before - after
    _write_official(twse, cash)
    av = adjustment_validation(cfg, meta, icfg, hcfg)

    # (2) 事件層級：官方 3 筆（EX_SID 配對；1102、2201 僅官方有）；純現金金額同 FinMind
    e = av["events"]
    assert (e["n_official"], e["n_finmind"], e["n_matched"]) == (3, 1, 1)
    assert e["official_only"]["stock_id"].to_list() == ["1102", "2201"]
    assert (e["n_cash_pairs"], e["n_cash_ok"]) == (1, 1)

    # (3) 窗層級：含事件且 r20 有值的窗＝entry 列 e∈[e_idx−20, e_idx−1]，共 20 個
    # （t ≥ 官方涵蓋起點 01-05）
    e_idx = DAYS.index(EX_DATE)
    win = [i for i in range(len(DAYS)) if i + 1 < e_idx <= i + 21 and i + 21 < len(DAYS)]
    assert len(win) == 20 and DAYS[win[0]] >= date(2015, 1, 5)
    w = av["windows"]
    assert (w["n_both"], w["n_fm_only"], w["n_ref_only"]) == (20, 0, 20)  # 1102 的窗僅官方看見
    # 二階項手算：還原 − 線性加回 ＝ (D/entry)·(exit/after − 1)（entry＝t+1 列收盤、exit＝t+21 列）
    c = _closes(EX_SID)
    expected = [cash / c[i + 1] * (c[i + 21] / after - 1) * 100 for i in win]
    assert w["diff_median"] == pytest.approx(sorted(expected)[len(expected) // 2 - 1] / 2
                                             + sorted(expected)[len(expected) // 2] / 2)
    assert w["diff_abs_max"] == pytest.approx(max(abs(x) for x in expected))
    txt = "\n".join(_adjustment_lines(av, hcfg))
    # 判準：(1) PASS；(2) 事件配對 1/3 → FAIL、現金金額 1/1 → PASS；(3) 窗差 p99 < 1.0pp → PASS
    assert txt.count("**FAIL**") == 1 and txt.count("**PASS**") == 3
    # (3′) 比值版官方參考（前收／(前收−官方現金)）＝FinMind 的 before/after → 差 0（無二階項）
    wr = av["windows_ratio"]
    assert (wr["n_both"], wr["n_fm_only"], wr["n_ref_only"]) == (20, 0, 20)
    assert wr["diff_abs_max"] == pytest.approx(0.0, abs=1e-9)
    assert "(3′)" in txt and "(1b)" in txt

    # 官方現金股利被放大 2 倍 → 現金金額不符、線性加回過度（差約 −D/entry）→ 再多兩個 FAIL
    _write_official(twse, 2 * cash)
    av2 = adjustment_validation(cfg, meta, icfg, hcfg)
    assert (av2["events"]["n_cash_pairs"], av2["events"]["n_cash_ok"]) == (1, 0)
    assert av2["windows"]["diff_median"] < -3.0
    assert av2["windows_ratio"]["diff_median"] < -3.0  # 比值版參考同樣抓得到官方金額被放大
    txt2 = "\n".join(_adjustment_lines(av2, hcfg))
    assert txt2.count("**FAIL**") == 3 and txt2.count("**PASS**") == 1
