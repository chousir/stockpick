"""tests/backtest/test_intra_pick_holdout_eval.py — M-Pick3b 保留樣本驗證（docs/34；全合成資料）。

期望值皆由測試內手算（註解附算式），不回抄實作。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
import typer
import yaml

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_holdout_eval as ev
from tw_screener.backtest.intra_pick_holdout_eval_runner import run_intra_pick_holdout_eval

# ─── 完整性防線 ───────────────────────────────────────────────────────────────


def test_git_blob_id_matches_git_known_values(tmp_path: Path) -> None:
    hello = tmp_path / "hello.txt"
    hello.write_bytes(b"hello\n")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    # `echo hello | git hash-object --stdin`／git 空 blob 的公認值
    assert ev.git_blob_id(hello) == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert ev.git_blob_id(empty) == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_verify_snapshot_match_mismatch_and_missing_pin(tmp_path: Path) -> None:
    f = tmp_path / "snap.parquet"
    f.write_bytes(b"abc" * 1000)
    want = hashlib.sha256(b"abc" * 1000).hexdigest()
    assert ev.sha256_file(f) == want
    assert ev.verify_snapshot(f, want.upper()) == want  # 大小寫不敏感
    with pytest.raises(ev.PinMismatchError, match="不符"):
        ev.verify_snapshot(f, "0" * 64)
    with pytest.raises(ev.PinMismatchError, match="缺"):
        ev.verify_snapshot(f, "")


def test_verify_blob_pins(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"hello\n")
    ok = {"a.py": "ce013625030ba8dba906f756967f9e9ca394464a"}
    assert ev.verify_blob_pins(ok, tmp_path) == ok
    with pytest.raises(ev.PinMismatchError, match=r"a\.py"):
        ev.verify_blob_pins({"a.py": "0" * 40}, tmp_path)
    with pytest.raises(ev.PinMismatchError, match="不存在"):
        ev.verify_blob_pins({"gone.py": "0" * 40}, tmp_path)
    assert ev.verify_blob_pins({}, tmp_path) == {}  # 沒設釘版＝不檢查（由報表標「未設釘版」）


# ─── 樣本規則、鏡像因子、分族設定 ─────────────────────────────────────────────


def test_apply_sample_rule_boundary_inclusive() -> None:
    dates = [date(2021, 11, 26), date(2021, 11, 30), date(2021, 12, 3), date(2021, 12, 30)]
    sw = pl.DataFrame({"date": dates, "stock_id": ["A"] * 4})
    kept, dropped = ev.apply_sample_rule(sw, date(2021, 11, 30))
    assert kept["date"].to_list() == dates[:2]        # ≤ 邊界日保留（含等於）
    assert dropped == [date(2021, 12, 3), date(2021, 12, 30)]
    kept2, dropped2 = ev.apply_sample_rule(sw, date(2022, 1, 31))
    assert kept2.height == 4 and dropped2 == []


def test_mirror_factor_ic_is_exact_negative_and_pick_semantics() -> None:
    # 兩週、每週一組 4 檔；_band_dist 帶內＝0 有 tie（組內置中排名 tie 取平均）
    rows = []
    for w, d in enumerate([date(2024, 1, 5), date(2024, 1, 12)]):
        stocks = [
            ("a", 0.0, 100.0, 1.0 + w), ("b", 0.0, 300.0, 4.0),
            ("c", 2.0, 200.0, 2.0), ("d", 5.0, 400.0, 3.0 - w),
        ]
        for sid, band, amt, r in stocks:
            rows.append({
                "date": d, "sub_industry": "G", "stock_id": f"{sid}{w}", "_band_dist": band,
                "amount_million": amt, "r20": r,
            })
    df = ev.add_mirror_factor(pl.DataFrame(rows)).with_columns(
        (-pl.col("_band_dist")).alias("neg_band_dist")
    )
    assert df["band_dist"].to_list() == df["_band_dist"].to_list()
    ic_pos = ip.weekly_ic(df, "band_dist", "r20", 2, 4)["ic"].to_list()
    ic_neg = ip.weekly_ic(df, "neg_band_dist", "r20", 2, 4)["ic"].to_list()
    assert len(ic_pos) == 2 and ic_pos != [0.0, 0.0]
    assert ic_pos == pytest.approx([-x for x in ic_neg], abs=1e-12)  # 嚴格反號
    # M2/M3 語意：因子＝band_dist → 首選＝距偏好帶最遠者 d；現行首選＝帶內且成交額大者 b
    week0 = df.filter(pl.col("date") == date(2024, 1, 5))
    pick = ip.pick_table(week0, "band_dist", "r20", 2).row(0, named=True)
    assert (pick["pick"], pick["base_pick"]) == ("d0", "b0")
    assert pick["m3"] == pytest.approx(3.0 - 4.0)          # r20(d)−r20(b)
    assert pick["m2"] == pytest.approx(3.0 - (1 + 4 + 2 + 3) / 4)  # r20(d)−組均 2.5


def test_family_config_sets_k_per_family() -> None:
    base = ip.IntraPickConfig()
    ecfg = ev.HoldoutEvalConfig(n_tests_primary=3, n_tests_secondary=5)
    p = ev.family_config(base, ecfg, ev.FAMILY_PRIMARY)
    s = ev.family_config(base, ecfg, ev.FAMILY_SECONDARY)
    assert (p.n_tests, s.n_tests) == (3, 5) and base.n_tests == 4
    assert replace(p, n_tests=base.n_tests) == base  # 其餘判準值一字不動


def test_hypotheses_registry_matches_prereg() -> None:
    assert [(h.key, h.family, h.factor) for h in ev.HYPOTHESES] == [
        ("H1", "primary", "log_amount"), ("H2", "primary", "band_dist"),
        ("F2", "secondary", "high52_near"), ("F4", "secondary", "rev_accel"),
    ]


def test_config_from_settings_parses_dates_and_pins() -> None:
    cfg = ev.HoldoutEvalConfig.from_settings({
        "backtest": {"intra_pick_holdout": {
            "snapshot_sha256": "ABCDEF", "last_snapshot": date(2021, 11, 30),
            "n_tests_primary": 2, "n_tests_secondary": 3, "tdr_ids": ["9105"],
            "pinned_blobs": {"x.py": "ABC"}, "output_dir": "out",
        }}
    })
    assert cfg.snapshot_sha256 == "abcdef" and cfg.last_snapshot == date(2021, 11, 30)
    assert (cfg.n_tests(ev.FAMILY_PRIMARY), cfg.n_tests(ev.FAMILY_SECONDARY)) == (2, 3)
    assert cfg.pinned_blobs == {"x.py": "abc"} and cfg.tdr_ids == ("9105",)
    assert cfg.output_dir == "out"
    from_str = ev.HoldoutEvalConfig.from_settings(
        {"backtest": {"intra_pick_holdout": {"last_snapshot": "2021-11-30"}}}
    )
    assert from_str.last_snapshot == date(2021, 11, 30)
    assert ev.HoldoutEvalConfig.from_settings({}).n_tests_primary == 2  # 缺鍵走預設


def test_yearly_ic_groups_by_snapshot_year() -> None:
    ic = pl.DataFrame({
        "date": [date(2015, 3, 6), date(2015, 3, 13), date(2016, 1, 8)],
        "ic": [0.1, 0.3, -0.2], "n_names": [10, 10, 10], "n_groups": [2, 2, 2],
    })
    got = ev.yearly_ic(ic)
    assert got["year"].to_list() == [2015, 2016] and got["n_weeks"].to_list() == [2, 1]
    assert got["ic_mean"].to_list() == pytest.approx([0.2, -0.2])  # (0.1+0.3)/2、−0.2
    assert ev.yearly_ic(ic.head(0)).is_empty()


# ─── 可行性檢查：只讀旗標 ─────────────────────────────────────────────────────


def _feas_frame() -> pl.DataFrame:
    """w1（進攻）：組 A 4 檔（a4＝TDR 9105）；w2（中性）：組 A 2 檔＋組 B 1 檔。
    high52_near：a4@w1 null；rev_accel：a3、a4@w1 null；其餘因子皆有值。
    in_main 全真、r20 皆有值。"""
    w1, w2 = date(2020, 1, 3), date(2020, 1, 10)
    rows = [
        (w1, "a1", "A", "進攻", 1.0, 0.9, 0.1), (w1, "a2", "A", "進攻", 2.0, 0.8, 0.2),
        (w1, "a3", "A", "進攻", 3.0, 0.7, None), (w1, "9105", "A", "進攻", 4.0, None, None),
        (w2, "a1", "A", "中性", 1.5, 0.9, 0.1), (w2, "a2", "A", "中性", 2.5, 0.8, 0.2),
        (w2, "b1", "B", "中性", 3.5, 0.7, 0.3),
    ]
    return pl.DataFrame(
        rows,
        schema=["date", "stock_id", "sub_industry", "regime", "r20", "high52_near", "rev_accel"],
        orient="row",
    ).with_columns(
        pl.lit(True).alias("in_main"),
        pl.col("r20").alias("log_amount"),
        pl.col("r20").alias("band_dist"),
    )


def test_feasibility_counts_by_hand() -> None:
    cfg = ip.IntraPickConfig(min_group=2, min_names_week=3)
    st = ev.feasibility(_feas_frame(), cfg, tdr_ids=("9105",))
    u = st["universe"]
    # 宇宙：w1 組 A 4 檔 ≥2 → 4；w2 組 A 2 檔 ≥2 → 2、組 B 1 檔剔除
    # → 共 6 股週、2 週；每週檔數 [4,2]→中位 3、組數 [1,1]→中位 1
    assert (u["stock_weeks"], u["weeks"], u["median_names"], u["median_groups"]) == (6, 2, 3.0, 1.0)
    assert u["tdr_stock_weeks"] == 1
    assert u["by_year"] == [{"year": 2020, "stock_weeks": 6, "weeks": 2}]
    h = st["by_hypothesis"]
    # H1：涵蓋 7 列全有；w1 4 檔 ≥3 ✓、w2 2 檔 <3 ✗ → 1 週可計、中位 4、regime {進攻:1}
    assert h["H1"]["coverage"] == pytest.approx(1.0) and h["H1"]["computable_weeks"] == 1
    assert (h["H1"]["median_names"], h["H1"]["regime_weeks"]) == (4.0, {"進攻": 1})
    # F2：涵蓋 6/7；w1 可用 a1,a2,a3＝3 檔 ≥3 ✓ → 1 週、中位 3
    assert h["F2"]["coverage"] == pytest.approx(6 / 7) and h["F2"]["computable_weeks"] == 1
    assert h["F2"]["median_names"] == 3.0
    # F4：涵蓋 5/7；w1 可用 a1,a2＝2 檔 <3 → 0 週
    assert h["F4"]["coverage"] == pytest.approx(5 / 7) and h["F4"]["computable_weeks"] == 0
    assert h["F4"]["median_names"] is None and h["F4"]["regime_weeks"] == {}
    assert st["snapshot_weeks"] == 2 and st["snapshot_first"] == date(2020, 1, 3)


def test_feasibility_depends_only_on_null_pattern_not_values() -> None:
    cfg = ip.IntraPickConfig(min_group=2, min_names_week=3)
    base = _feas_frame()
    rng = random.Random(3)
    scrambled = base.with_columns(
        *[
            pl.Series(c, [None if v is None else rng.uniform(-50, 50) for v in base[c].to_list()])
            for c in ("r20", "high52_near", "rev_accel", "log_amount", "band_dist")
        ]
    )
    assert ev.feasibility(scrambled, cfg, ("9105",)) == ev.feasibility(base, cfg, ("9105",))
    lines = ev.render_feasibility(ev.feasibility(base, cfg, ("9105",)), cfg)
    assert any("H1 `log_amount`" in x for x in lines) and any("| 2020 |" in x for x in lines)


# ─── 端到端（合成快照）────────────────────────────────────────────────────────


def _snapshot(weeks: int = 130, seed: int = 5) -> pl.DataFrame:
    """3 組 × 6 檔 × weeks 週。組內各因子為 0..5 的獨立隨機置換；
    r20 = 2·rank(成交額) + 1.5·rank(帶距) − 2·rank(營收加速) + N(0,3)；high52_near 與 r20 無關。
    regime 週次奇偶交替（各 ≥30 週）。"""
    rng = random.Random(seed)
    start = date(2019, 1, 4)
    rows = []
    for w in range(weeks):
        d = start + timedelta(weeks=w)
        for g in range(3):
            perms = {k: rng.sample(range(6), 6) for k in ("amt", "band", "rev", "hi")}
            for i in range(6):
                amt, band, rev, hi = (perms[k][i] for k in ("amt", "band", "rev", "hi"))
                rows.append({
                    "date": d, "stock_id": f"{g}{i}", "sub_industry": f"G{g}", "in_main": True,
                    "regime": "進攻" if w % 2 == 0 else "中性",
                    "amount_million": 100.0 + 50.0 * amt, "_band_dist": float(band),
                    "rev_accel": 0.01 * rev, "high52_near": 0.5 + 0.05 * hi,
                    "r20": 2.0 * amt + 1.5 * band - 2.0 * rev + rng.gauss(0, 3),
                })
    return pl.DataFrame(rows).with_columns(
        (-pl.col("_band_dist")).alias("neg_band_dist"),
        pl.col("amount_million").log().alias("log_amount"),
    )


def test_evaluate_hypotheses_end_to_end_and_family_bonferroni() -> None:
    cfg = ip.IntraPickConfig(n_boot=400)
    ecfg = ev.HoldoutEvalConfig()
    sw = ev.add_mirror_factor(_snapshot())
    regimes = sw.select("date", "regime").unique(subset=["date"])
    res = {hr.hypothesis.key: hr.result for hr in ev.evaluate_hypotheses(sw, cfg, ecfg, regimes)}
    assert list(res) == ["H1", "H2", "F2", "F4"]
    # 訊號依構造：H1、H2（鏡像）強正 → 成立；F4 強負 → 反向顯著；F2 無關 → 不成立
    assert res["H1"].verdict == ip.VERDICT_PASS and res["H1"].ic.mean > 0.3
    assert res["H2"].verdict == ip.VERDICT_PASS and res["H2"].ic.mean > 0.2
    assert res["F4"].verdict == ip.VERDICT_REVERSE and res["F4"].ic.mean < -0.2
    assert res["F2"].verdict != ip.VERDICT_PASS
    assert all(r.ic.n == 130 and r.coverage == pytest.approx(1.0) for r in res.values())
    # Bonferroni 用該族 k＝2（不是 M-Pick2 的 4）
    ic_series = res["H1"].ic_weekly["ic"].to_list()
    k2 = ip.summarize(ic_series, cfg.block_len(), replace(cfg, n_tests=2))
    k4 = ip.summarize(ic_series, cfg.block_len(), replace(cfg, n_tests=4))
    assert res["H1"].ic.ci_bonf == k2.ci_bonf and res["H1"].ic.ci_bonf != k4.ci_bonf
    # 鏡像：H2 換成 neg_band_dist 的週 IC 逐週反號
    neg = ip.weekly_ic(
        sw, "neg_band_dist", "r20", cfg.min_group, cfg.min_names_week
    )["ic"].to_list()
    assert res["H2"].ic_weekly["ic"].to_list() == pytest.approx([-x for x in neg], abs=1e-12)


# ─── 編排（runner）：釘版、單次執行、產物 ─────────────────────────────────────


def _setup_run(tmp_path: Path, weeks: int = 130, kept: int = 120) -> tuple[Path, Path, dict]:
    sw = _snapshot(weeks)
    dates = sorted(sw["date"].unique().to_list())
    snap = tmp_path / "snap.parquet"
    sw.write_parquet(snap)
    settings = tmp_path / "settings.yaml"
    cfg = {
        "backtest": {
            "intra_pick": {"n_boot": 200},
            "intra_pick_holdout": {
                "snapshot_path": str(snap),
                "snapshot_sha256": hashlib.sha256(snap.read_bytes()).hexdigest(),
                "last_snapshot": dates[kept - 1].isoformat(),
                "output_dir": str(tmp_path / "out"),
                "tdr_ids": [],
            },
        }
    }
    settings.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return settings, snap, cfg


def test_run_formal_outputs_then_refuses_second_run(tmp_path: Path) -> None:
    settings, _, _ = _setup_run(tmp_path)
    out = tmp_path / "out"
    run_intra_pick_holdout_eval(settings, None)
    verdicts = sorted(out.glob("holdout_eval_verdicts_*.csv"))
    assert len(verdicts) == 1 and len(list(out.glob("holdout_eval_weekly_*.csv"))) == 1
    v = pl.read_csv(verdicts[0])
    assert v["hypothesis"].to_list() == ["H1", "H2", "F2", "F4"]
    assert v["family"].to_list() == ["primary", "primary", "secondary", "secondary"]
    assert v["n_weeks"].to_list() == [120] * 4  # 樣本規則：130 週只留 120 週
    report = next(out.glob("holdout_eval_2*.md")).read_text(encoding="utf-8")
    assert "主族（H1、H2）（Bonferroni k＝2）" in report
    assert "副族（F2、F4 複驗）（Bonferroni k＝2）" in report
    assert "剔除 10 個快照" in report and "H2 換算" in report and "未設釘版" in report
    with pytest.raises(typer.Exit) as exc:  # 單次正式執行：有結果就拒絕
        run_intra_pick_holdout_eval(settings, None)
    assert exc.value.exit_code == 1


def test_run_feasibility_only_writes_no_verdicts(tmp_path: Path) -> None:
    settings, _, _ = _setup_run(tmp_path)
    run_intra_pick_holdout_eval(settings, None, feasibility_only=True)
    out = tmp_path / "out"
    assert len(list(out.glob("holdout_feasibility_*.md"))) == 1
    assert not list(out.glob("holdout_eval_verdicts_*.csv"))
    assert not list(out.glob("holdout_eval_weekly_*.csv"))
    run_intra_pick_holdout_eval(settings, None)  # 可行性檢查不占用「單次正式執行」名額
    assert len(list(out.glob("holdout_eval_verdicts_*.csv"))) == 1


def test_run_aborts_on_snapshot_or_pin_mismatch(tmp_path: Path) -> None:
    settings, snap, cfg = _setup_run(tmp_path)
    # 1) 快照被動過（SHA 不符）
    tampered = tmp_path / "t"
    tampered.mkdir()
    settings_t, snap_t, _ = _setup_run(tampered)
    snap_t.write_bytes(snap_t.read_bytes() + b"x")
    with pytest.raises(typer.Exit):
        run_intra_pick_holdout_eval(settings_t, None)
    assert not list((tampered / "out").glob("holdout_eval_verdicts_*.csv"))
    # 2) 判準邏輯檔釘版不符
    cfg["backtest"]["intra_pick_holdout"]["pinned_blobs"] = {"pyproject.toml": "0" * 40}
    settings.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(typer.Exit):
        run_intra_pick_holdout_eval(settings, None)
    assert not list((tmp_path / "out").glob("holdout_eval_verdicts_*.csv"))
    # 3) 快照不存在
    snap.unlink()
    cfg["backtest"]["intra_pick_holdout"]["pinned_blobs"] = {}
    settings.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(typer.Exit):
        run_intra_pick_holdout_eval(settings, None)
