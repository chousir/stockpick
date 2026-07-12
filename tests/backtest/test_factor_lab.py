"""WS-B 統一因子實驗台單元測試（全離線合成資料、固定種子）。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from tw_screener.backtest.factor_lab import (
    _stride_dates,
    bootstrap_mean_ci,
    bucket_table,
    evaluate,
    grid_scan,
    inference_footer,
    moving_block_bootstrap_ci,
    regime_alignment_verdict,
    regime_ic_slices,
    regime_mean_slices,
    residual_ic,
    walk_forward_splits,
)

_D0 = date(2025, 7, 1)


def _dates(n: int) -> list[date]:
    return [_D0 + timedelta(days=i) for i in range(n)]


def _panel(
    n_days: int = 120, n_stocks: int = 40, beta: float = -0.5, seed: int = 7
) -> pl.DataFrame:
    """合成面板：target = beta×factor ＋ 控制項 c ＋ noise（横斷面關係固定）。"""
    rng = random.Random(seed)
    rows = []
    for d in _dates(n_days):
        for s in range(n_stocks):
            f = rng.gauss(0, 1)
            c = rng.gauss(0, 1)
            y = beta * f + 1.0 * c + rng.gauss(0, 0.5)
            rows.append({"date": d, "stock_id": f"s{s:03d}", "factor": f, "ctrl": c, "y": y})
    return pl.DataFrame(rows)


# ── walk_forward_splits ───────────────────────────────────────────────────────


def test_splits_expanding_nonoverlap_embargo() -> None:
    ds = _dates(100)
    splits = walk_forward_splits(ds, n_splits=4, min_train_frac=0.4, embargo_td=6)
    assert len(splits) == 4
    for i, s in enumerate(splits):
        # embargo：train_end 與 test_start 中間至少隔 6 個交易日
        gap = (s.test_start - s.train_end).days
        assert gap >= 7
        # 測試塊彼此不重疊且遞增
        if i:
            assert s.test_start > splits[i - 1].test_end
    # expanding：train_end 單調遞增
    ends = [s.train_end for s in splits]
    assert ends == sorted(ends)
    # 末段吃到最後一天
    assert splits[-1].test_end == ds[-1]


def test_splits_too_short_returns_empty() -> None:
    assert walk_forward_splits(_dates(15), n_splits=3) == []
    assert walk_forward_splits(_dates(40), n_splits=8) == []  # 每塊 <5 日


# ── evaluate ─────────────────────────────────────────────────────────────────


def test_evaluate_detects_negative_ic_with_ci() -> None:
    panel = _panel(beta=-0.5)
    rep = evaluate(panel, "factor", horizon=5, target="y", controls=(), n_splits=3)
    row = rep.pooled.row(0, named=True)
    assert row["ic"] < -0.2
    assert row["ci_hi"] < 0  # CI 不跨 0
    assert row["n"] == panel.height
    # walk-forward 各段同號
    assert rep.consistent_sign is True
    assert rep.splits.height == 3


def test_evaluate_null_factor_ignored_and_expr_input() -> None:
    panel = _panel(n_days=60, n_stocks=20)
    expr = (pl.col("factor") * -1).alias("neg_factor")
    rep = evaluate(panel, expr, horizon=5, target="y", n_splits=3)
    assert rep.factor == "neg_factor"
    assert rep.pooled.row(0, named=True)["ic"] > 0.2  # 翻號


def test_evaluate_no_signal_ci_crosses_zero() -> None:
    panel = _panel(beta=0.0, seed=11)
    rep = evaluate(panel, "factor", horizon=5, target="y", n_splits=3)
    row = rep.pooled.row(0, named=True)
    assert row["ci_lo"] < 0 < row["ci_hi"]  # 無證據＝CI 跨 0


# ── buckets ──────────────────────────────────────────────────────────────────


def test_bucket_monotonic_for_strong_factor() -> None:
    panel = _panel(beta=1.0, seed=3)
    tbl = bucket_table(panel, "factor", "y", buckets=5)
    assert tbl.height == 5
    meds = tbl.sort("bucket")["median"].to_list()
    assert meds == sorted(meds)  # 桶 1→5 單調遞增
    assert int(tbl["n"].sum()) == panel.height


def test_bucket_skips_thin_days() -> None:
    panel = _panel(n_days=5, n_stocks=3)
    assert bucket_table(panel, "factor", "y", buckets=5).is_empty()


# ── residual_ic ──────────────────────────────────────────────────────────────


def test_residual_ic_removes_control_channel() -> None:
    """因子只透過 c 相關：raw IC 顯著、控制 c 後 ≈ 0。"""
    rng = random.Random(5)
    rows = []
    for d in _dates(80):
        for s in range(40):
            c = rng.gauss(0, 1)
            f = c + rng.gauss(0, 0.3)   # 因子 ≈ 控制項
            y = c + rng.gauss(0, 0.5)   # target 只吃 c
            rows.append({"date": d, "stock_id": f"s{s}", "factor": f, "ctrl": c, "y": y})
    panel = pl.DataFrame(rows)
    raw = evaluate(panel, "factor", horizon=5, target="y").pooled.row(0, named=True)
    assert raw["ci_lo"] > 0.3  # raw 顯著正
    resid = residual_ic(panel, "factor", "y", ["ctrl"]).row(0, named=True)
    assert abs(resid["ic"]) < 0.1  # 控制後幾乎歸零


def test_residual_ic_survives_when_independent() -> None:
    panel = _panel(beta=-0.5, seed=9)  # y = -0.5f + c + noise（f ⊥ c）
    resid = residual_ic(panel, "factor", "y", ["ctrl"]).row(0, named=True)
    assert resid["ic"] < -0.2 and resid["ci_hi"] < 0  # 控制 c 後仍活著


# ── grid_scan ────────────────────────────────────────────────────────────────


def test_grid_scan_reports_all_cells_and_limits() -> None:
    panel = _panel(n_days=100, n_stocks=25)

    def mk(mult: float) -> pl.Expr:
        return (pl.col("factor") * mult).alias("f_scaled")

    out = grid_scan(panel, mk, {"mult": [1.0, -1.0, 2.0]}, horizon=5, target="y", n_splits=3)
    # 所有 cell × split 全列（3 cell × 3 split）
    assert out.height == 9
    assert {"train_ic", "test_ic", "test_ci_lo", "selected"} <= set(out.columns)
    # selected 恰標一個 cell（3 列）
    assert out.filter(pl.col("selected")).height == 3

    with pytest.raises(ValueError, match="維度"):
        grid_scan(panel, mk, {f"d{i}": [1] for i in range(4)}, horizon=5, target="y")
    with pytest.raises(ValueError, match="檔位"):
        grid_scan(panel, mk, {"mult": [1, 2, 3, 4, 5, 6]}, horizon=5, target="y")


def test_grid_scan_train_test_separation() -> None:
    """測試段樣本不含訓練段日期（embargo 后不重疊）。"""
    panel = _panel(n_days=100, n_stocks=25)

    def mk(mult: float) -> pl.Expr:
        return (pl.col("factor") * mult).alias("f_scaled")

    out = grid_scan(panel, mk, {"mult": [1.0]}, horizon=5, target="y", n_splits=3)
    # train_n + embargo 段 + test_n ≤ 總樣本（有 embargo 就必然嚴格小於）
    total = panel.height
    for r in out.iter_rows(named=True):
        assert r["train_n"] + r["test_n"] < total


# ── WS-I：moving-block bootstrap 推論硬化 ───────────────────────────────────


def test_moving_block_bootstrap_ci_deterministic_and_block_structured() -> None:
    """同 seed 決定性；重抽序列可切成長度=block_len 的連續段，段內值全來自原序列相鄰位置。"""
    values = [float(i) for i in range(200)]  # 平移可辨識序列：values[i] == i
    seen: list[list[float]] = []

    def _record(seq: list[float]) -> float:
        seen.append(list(seq))
        return sum(seq) / len(seq)

    ci1 = moving_block_bootstrap_ci(values, block_len=10, n_boot=50, seed=7, stat=_record)
    ci2 = moving_block_bootstrap_ci(values, block_len=10, n_boot=50, seed=7, stat=_record)
    assert ci1 == ci2  # 同 seed 全 deterministic

    assert len(seen) == 100  # 兩次呼叫各 50 次重抽都被記錄
    for seq in seen:
        assert len(seq) == 200  # 200/10 整除，無截斷邊界
        for start in range(0, 200, 10):
            block = seq[start : start + 10]
            diffs = [b - a for a, b in zip(block, block[1:])]
            assert diffs == [1.0] * 9  # 塊內連續遞增 1 ＝來自原序列連續段


def test_moving_block_bootstrap_wider_than_iid_under_autocorrelation() -> None:
    """強自相關序列：block bootstrap 保留依賴結構，CI 寬度應大於（誤當獨立的）iid bootstrap。"""
    rng = random.Random(99)
    t = 300
    x = [rng.gauss(0, 1)]
    for _ in range(t - 1):
        x.append(0.9 * x[-1] + rng.gauss(0, 0.3))  # AR(1)，rho=0.9 強自相關
    block_lo, block_hi = moving_block_bootstrap_ci(x, block_len=21, n_boot=500, seed=3)
    iid_lo, iid_hi = bootstrap_mean_ci(x, n_boot=500, seed=3)
    assert block_lo is not None and iid_lo is not None
    assert (block_hi - block_lo) > (iid_hi - iid_lo)


def test_moving_block_bootstrap_thin_sample_none() -> None:
    assert moving_block_bootstrap_ci([1.0] * 5, block_len=3, n_boot=100, seed=1) == (None, None)


def test_nonoverlap_stride_selects_correct_dates() -> None:
    """30 日、horizon=10 → 非重疊子樣本取日 0/10/20（索引，含第一天）。"""
    ds = _dates(30)
    picked = _stride_dates(ds, 10)
    assert picked == [ds[0], ds[10], ds[20]]


def test_inference_footer_is_exactly_three_lines() -> None:
    lines = inference_footer(
        sample_span="2025-01-10~2026-07-09",
        regime_dist="regime 標籤未附（WS-H 前）＝2025-01~2026-07 多頭偏",
        method_desc="moving-block bootstrap（block=21td・B=1000・seed=42）",
        membership_desc="今日 concepts.yaml（非 point-in-time）",
    )
    assert len(lines) == 3
    assert all(isinstance(line, str) and line.startswith("- ") for line in lines)


def test_evaluate_ws_i_fields_present_and_pooled_unchanged() -> None:
    """evaluate() 回傳新欄存在；pooled 舊欄數值不變（機器等價，只加欄不改值）。"""
    panel = _panel(beta=-0.5, n_days=120, n_stocks=40)
    rep = evaluate(panel, "factor", horizon=5, target="y", n_splits=3)

    # 舊欄數值不變（同 test_evaluate_detects_negative_ic_with_ci 的斷言）
    row = rep.pooled.row(0, named=True)
    assert row["ic"] < -0.2
    assert row["ci_hi"] < 0
    assert row["n"] == panel.height
    assert rep.consistent_sign is True
    assert rep.splits.height == 3

    # 新欄存在且方向與 pooled 一致（強訊號，per-date mean 也應顯著負）
    assert rep.mean_daily_ic is not None and rep.mean_daily_ic < -0.2
    bs_lo, bs_hi = rep.bs_ci
    assert bs_lo is not None and bs_hi is not None and bs_hi < 0
    assert rep.nonoverlap.get("n_dates", 0) >= 1
    assert {"method", "B", "L", "seed", "T", "span"} <= set(rep.inference_meta)
    assert rep.inference_meta["method"] == "block_bootstrap"


# ── WS-H.4a：regime 切片＋跨 regime 升降級（docs/23 §1c）──────────────────────


def _regime_panel(
    betas: dict[str, float], days_per_regime: dict[str, int], n_stocks: int = 20, seed: int = 13
) -> pl.DataFrame:
    """各 regime 段落各自的 factor→y 斜率（橫斷面關係依 regime 而變）。"""
    rng = random.Random(seed)
    rows = []
    i = 0
    for regime, n_days in days_per_regime.items():
        beta = betas[regime]
        for _ in range(n_days):
            d = _D0 + timedelta(days=i)
            i += 1
            for s in range(n_stocks):
                f = rng.gauss(0, 1)
                rows.append(
                    {
                        "date": d, "stock_id": f"s{s:03d}", "regime": regime,
                        "factor": f, "y": beta * f + rng.gauss(0, 0.3),
                    }
                )
    return pl.DataFrame(rows)


def test_regime_ic_slices_all_null_or_missing_returns_empty() -> None:
    """regime 欄全 null 或缺席（WS-H 標籤未產）→ 空表，呼叫端誠實跳過不炸。"""
    panel = _regime_panel({"進攻": 1.0}, {"進攻": 15})
    all_null = panel.with_columns(pl.lit(None, dtype=pl.Utf8).alias("regime"))
    assert regime_ic_slices(all_null, "factor", target="y", horizon=5).is_empty()
    no_col = panel.drop("regime")
    assert regime_ic_slices(no_col, "factor", target="y", horizon=5).is_empty()
    assert regime_ic_slices(pl.DataFrame(), "factor", target="y", horizon=5).is_empty()


def test_regime_ic_slices_three_regimes_rows_and_alignment() -> None:
    """三 regime 各 40 日：切片 3 列、非 thin；同向數＝與全樣本符號一致的切片數。"""
    panel = _regime_panel(
        {"進攻": 1.0, "中性": 1.0, "防禦": -1.0},
        {"進攻": 40, "中性": 40, "防禦": 40},
    )
    slices = regime_ic_slices(panel, "factor", target="y", horizon=5)
    assert slices.height == 3
    rows = {r["regime"]: r for r in slices.iter_rows(named=True)}
    assert all(not rows[k]["thin"] and rows[k]["n_dates"] == 40 for k in rows)
    assert rows["進攻"]["mean_ic"] > 0.5 and rows["中性"]["mean_ic"] > 0.5
    assert rows["防禦"]["mean_ic"] < -0.5
    # 全樣本符號＝正 → 進攻/中性 同向（2/3）→ 跨 regime 穩健
    verdict, same, present = regime_alignment_verdict(slices, 1)
    assert verdict == "跨 regime 穩健"
    assert sorted(same) == ["中性", "進攻"] and present == 3


def test_regime_alignment_verdict_bull_only_and_dependent() -> None:
    """僅進攻同向＝bull-only；僅中性同向＝regime-dependent（docs/23 §1c）。"""
    panel = _regime_panel(
        {"進攻": 1.0, "中性": -1.0, "防禦": -1.0},
        {"進攻": 40, "中性": 40, "防禦": 40},
    )
    slices = regime_ic_slices(panel, "factor", target="y", horizon=5)
    verdict, same, present = regime_alignment_verdict(slices, 1)  # 全樣本視為正
    assert verdict == "bull-only" and same == ["進攻"] and present == 3
    # 同一張表、全樣本視為負 → 同向＝中性/防禦 2 片 → 穩健
    verdict_neg, same_neg, _ = regime_alignment_verdict(slices, -1)
    assert verdict_neg == "跨 regime 穩健" and sorted(same_neg) == ["中性", "防禦"]

    panel2 = _regime_panel(
        {"進攻": -1.0, "中性": 1.0, "防禦": -1.0},
        {"進攻": 40, "中性": 40, "防禦": 40},
    )
    slices2 = regime_ic_slices(panel2, "factor", target="y", horizon=5)
    verdict2, same2, _ = regime_alignment_verdict(slices2, 1)
    assert verdict2 == "regime-dependent" and same2 == ["中性"]


def test_regime_ic_slices_thin_excluded_from_denominator() -> None:
    """n_dates<30 的切片標 thin、照列但不進裁決分母。"""
    panel = _regime_panel(
        {"進攻": 1.0, "中性": 1.0, "防禦": 1.0},
        {"進攻": 40, "中性": 40, "防禦": 15},  # 防禦僅 15 日 → thin
    )
    slices = regime_ic_slices(panel, "factor", target="y", horizon=5)
    rows = {r["regime"]: r for r in slices.iter_rows(named=True)}
    assert rows["防禦"]["thin"] is True and rows["防禦"]["n_dates"] == 15
    assert rows["防禦"]["mean_ic"] is not None  # 照列（表上有值）
    verdict, same, present = regime_alignment_verdict(slices, 1)
    assert present == 2 and verdict == "跨 regime 穩健"  # 分母排除 thin


def test_regime_alignment_verdict_empty_or_all_thin() -> None:
    empty = pl.DataFrame(
        schema={"regime": pl.Utf8, "mean_ic": pl.Float64, "thin": pl.Boolean}
    )
    verdict, same, present = regime_alignment_verdict(empty, 1)
    assert verdict == "regime 樣本不足（無可判切片）" and same == [] and present == 0
    all_thin = pl.DataFrame(
        {"regime": ["進攻"], "mean_ic": [0.5], "thin": [True]}
    )
    verdict2, _, present2 = regime_alignment_verdict(all_thin, 1)
    assert verdict2 == "regime 樣本不足（無可判切片）" and present2 == 0


def test_regime_mean_slices_rows_and_empty_slice_listed() -> None:
    """mean-lift 切片：各 regime per-date mean 序列；無資料 regime 照列 n=0 不藏格。"""
    rng = random.Random(21)
    rows = []
    for i in range(24):  # 進攻 12 日、中性 12 日；防禦 0 日（缺席）
        d = _D0 + timedelta(days=i)
        regime = "進攻" if i < 12 else "中性"
        sign = 1.0 if regime == "進攻" else -1.0
        for g in range(3):  # 每日 3 筆（籃子）
            rows.append(
                {"date": d, "grp": f"g{g}", "regime": regime,
                 "val": sign * 2.0 + rng.gauss(0, 0.2)}
            )
    df = pl.DataFrame(rows)
    slices = regime_mean_slices(df, target="val", block_len=3, min_n_dates=10)
    assert slices.height == 3
    r = {x["regime"]: x for x in slices.iter_rows(named=True)}
    assert r["進攻"]["n"] == 36 and r["進攻"]["n_dates"] == 12 and r["進攻"]["mean"] > 1.5
    assert r["中性"]["mean"] < -1.5 and not r["中性"]["thin"]
    assert r["防禦"]["n"] == 0 and r["防禦"]["mean"] is None and r["防禦"]["thin"] is True
    # 進攻/中性可判、僅進攻與正號同向 → bull-only（docs/23 §1c）
    verdict, same, present = regime_alignment_verdict(slices, 1, value_col="mean")
    assert verdict == "bull-only" and same == ["進攻"] and present == 2


def test_regime_mean_slices_all_null_regime_empty() -> None:
    df = pl.DataFrame(
        {"date": [_D0], "regime": [None], "val": [1.0]},
        schema={"date": pl.Date, "regime": pl.Utf8, "val": pl.Float64},
    )
    assert regime_mean_slices(df, target="val", block_len=3).is_empty()
