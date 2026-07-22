"""M-BR1 Phase 2 contrarian_efficacy 單元測試（全離線合成資料）。

核心意圖（同 §4 ground-truth）：分類器**不得退化成「跌深就接」**——加速賣（京元型）
與中段未貼低（華邦型）必須被排除在 contrarian_base 之外；並證 point-in-time 重建
的 flow_inflection/base_proximity 與 analysis/contrarian.py 純函式**逐值一致**（口徑不漂移）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import yaml

from tw_screener.analysis.contrarian import base_proximity, dist_to_low_pct, flow_inflection
from tw_screener.backtest import contrarian_efficacy as ce
from tw_screener.backtest.contrarian_efficacy_runner import run_contrarian_efficacy

_D0 = date(2023, 1, 2)


def _tile(pattern: list[int], n: int) -> list[int]:
    """把節奏 pattern 平鋪到剛好 n 長。"""
    return ([*pattern] * (n // len(pattern) + 1))[:n]


def _stock(stock_id: str, closes: list[float], fnets: list[int]) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame(
        {
            "date": [_D0 + timedelta(days=i) for i in range(n)],
            "stock_id": [stock_id] * n,
            "close": closes,
            "foreign_net": fnets,
            "regime": ["防禦"] * n,
            "alpha5": [0.0] * n, "alpha10": [0.0] * n,
            "alpha20": [0.0] * n, "alpha40": [0.0] * n,
        }
    )


def test_signal_reuse_fidelity() -> None:
    """point-in-time flow_inflection/base_proximity 與純函式逐值一致（口徑不漂移）。"""
    # 70 日，close 造出末日貼近 60 日低；foreign_net 前 15 日賣、末 5 日買
    closes = [100.0 - i * 0.3 for i in range(70)]  # 單調下滑 → 末日=60 日低
    fnets = [-400_000] * 65 + [200_000] * 5
    sig = ce.contrarian_signal_panel(_stock("4967", closes, fnets))
    last = sig.sort("date").row(-1, named=True)
    # 手算 rolling sums（末日）
    net20 = sum(fnets[-20:])
    net5 = sum(fnets[-5:])
    low60 = min(closes[-60:])
    exp_flow = flow_inflection(net20, net5)
    exp_dist = dist_to_low_pct(closes[-1], low60)
    exp_prox = base_proximity(exp_dist)
    assert last["flow_inflection"] == exp_flow
    assert last["dist_low_60d_pct"] == exp_dist
    assert last["base_proximity"] == exp_prox


def test_warmup_null_point_in_time() -> None:
    """滾動窗未滿 → 衍生欄 null/空（誠實 null，非臆造；前視安全）。"""
    closes = [100.0 - i * 0.2 for i in range(40)]
    fnets = [-100_000] * 40
    sig = ce.contrarian_signal_panel(_stock("1111", closes, fnets)).sort("date")
    # 60 日低窗未滿 → base_proximity 全空（dist null → 空字串）
    assert sig["base_proximity"].to_list().count("") == 40
    # 20 日流向窗：前 19 列 null
    assert sig["flow_inflection"][:19].null_count() == 19
    assert sig["flow_inflection"][19] is not None


def test_ground_truth_buckets() -> None:
    """§4：十銓型（轉買×在低）True；京元型（加速賣）False；華邦型（中段）False。"""
    n = 70
    # 十銓：末日貼 60 日低、20 日賣超但近 5 日翻買
    tm = _stock("4967", [100.0 - i * 0.3 for i in range(n)],
                [-400_000] * 65 + [200_000] * 5)
    # 京元：末日也在低，但 20 日大買、近 5 日加速賣（buy dominant → 加速賣）
    ky = _stock("2449", [100.0 - i * 0.3 for i in range(n)],
                [500_000] * 65 + [-900_000] * 5)
    # 華邦：20 日賣近 5 日買（轉買），但末日在中段（距 60 日低 > 5%）
    wb_close = [100.0 - i * 0.5 for i in range(60)] + [100.0] * 10  # 末段拉回中段
    wb = _stock("2344", wb_close, [-400_000] * 65 + [200_000] * 5)
    for df, sid, expect in ((tm, "4967", True), (ky, "2449", False), (wb, "2344", False)):
        last = ce.contrarian_signal_panel(df).sort("date").row(-1, named=True)
        assert last["contrarian_base"] is expect, (sid, last["flow_inflection"],
                                                   last["base_proximity"])
    # 加速賣的具體標籤與中段位階佐證
    ky_last = ce.contrarian_signal_panel(ky).sort("date").row(-1, named=True)
    assert ky_last["flow_inflection"] == "加速賣"
    wb_last = ce.contrarian_signal_panel(wb).sort("date").row(-1, named=True)
    assert wb_last["base_proximity"] in ("中段", "高檔")


def test_lift_table_structure() -> None:
    """各前瞻窗都出焦點桶＋全體兩列。"""
    sig = ce.contrarian_signal_panel(
        _stock("4967", [100.0 - i * 0.3 for i in range(70)],
               [-400_000] * 65 + [200_000] * 5)
    )
    lift = ce.contrarian_lift_table(sig, horizons=(5, 20), n_boot=50)
    buckets = set(lift["bucket"].to_list())
    assert ce.FOCAL_LABEL in buckets and ce.ALL_LABEL in buckets
    assert set(lift["horizon"].to_list()) == {5, 20}


def test_walk_forward_counts_positive_lift() -> None:
    """walk-forward 計「lift 為正」段數（打敗宇宙）；恆負 lift → 0 段（一致落後非 edge）。"""
    # 直接建 signal（含 lift20＋contrarian_base），驗「lift 為正段數」計數
    dates = [_D0 + timedelta(days=7 * i) for i in range(40)]  # 40 週快照
    base = pl.DataFrame(
        {"date": dates, "stock_id": ["4967"] * 40, "contrarian_base": [True] * 40}
    )
    pos = base.with_columns(pl.lit(1.5).alias("lift20"))
    _wf, n_pos, valid = ce.contrarian_walk_forward(
        pos, horizon=20, n_splits=3, embargo_td=2, metric="lift"
    )
    assert valid >= 1 and n_pos == valid  # 恆正 lift → 全段為正
    neg = base.with_columns(pl.lit(-1.5).alias("lift20"))
    _wf2, n_pos2, valid2 = ce.contrarian_walk_forward(
        neg, horizon=20, n_splits=3, embargo_td=2, metric="lift"
    )
    assert valid2 >= 1 and n_pos2 == 0  # 恆負 lift → 0 段為正（一致落後非 edge）


def _regime_slices(defensive_mean: float, defensive_thin: bool = False) -> pl.DataFrame:
    """三 regime lift 切片：進攻/中性 正且可判，防禦依參數。"""
    return pl.DataFrame(
        {
            "regime": ["進攻", "中性", "防禦"],
            "n": [100, 100, 100],
            "n_dates": [50, 50, 20 if defensive_thin else 50],
            "mean": [1.0, 1.0, defensive_mean],
            "ci_lo": [0.5, 0.5, None], "ci_hi": [1.5, 1.5, None],
            "thin": [False, False, defensive_thin],
        },
        schema={"regime": pl.Utf8, "n": pl.Int64, "n_dates": pl.Int64,
                "mean": pl.Float64, "ci_lo": pl.Float64, "ci_hi": pl.Float64,
                "thin": pl.Boolean},
    )


def test_gate_verdict_three_prong() -> None:
    """三門檻各自獨立否決（含 §1「必含 2022 空頭＝防禦」硬化）；三齊過才升。"""
    full_ok = (1.0, 0.3, 1.7, 100)      # lift block-CI 下界 >0
    full_cross = (0.5, -0.3, 1.4, 100)  # CI 跨 0
    reg_ok = _regime_slices(0.8)                       # 防禦可判且正
    reg_def_thin = _regime_slices(0.8, defensive_thin=True)  # 防禦樣本不足
    reg_def_neg = _regime_slices(-0.5)                 # 防禦為負
    # 三齊過
    assert ce.contrarian_gate_verdict(full_ok, 4, 5, reg_ok)[0]
    # CI 跨 0 → 否
    assert not ce.contrarian_gate_verdict(full_cross, 5, 5, reg_ok)[0]
    # walk-forward 3/5 < 0.8 → 否
    assert not ce.contrarian_gate_verdict(full_ok, 3, 5, reg_ok)[0]
    # 防禦樣本不足（不含 2022 空頭）→ 否
    assert not ce.contrarian_gate_verdict(full_ok, 5, 5, reg_def_thin)[0]
    # 防禦為負（含空頭但不正）→ 否
    assert not ce.contrarian_gate_verdict(full_ok, 5, 5, reg_def_neg)[0]


def test_runner_e2e(tmp_path: Path) -> None:
    """端到端：小面板 parquet → runner 產 MD＋CSV＋裁決字串（regime 段不崩）。"""
    frames = []
    for k, sid in enumerate(("4967", "2449", "3008", "2454")):
        n = 260
        closes = [100.0 + k - (i % 60) * 0.3 for i in range(n)]
        fnets = _tile([-400_000] * 15 + [200_000] * 5, n)
        df = _stock(sid, closes, fnets).with_columns(
            pl.Series("regime", ["防禦"] * 130 + ["進攻"] * 130),
            pl.Series("alpha20", [1.5 if k == 0 else -0.2] * n),
            pl.col("close").alias("close"),
        )
        # 補面板欄
        df = df.with_columns(pl.lit(0.0).alias("ma60_dist_pct"), pl.lit(1000.0).alias("volume"))
        frames.append(df)
    panel = pl.concat(frames)
    panel_path = tmp_path / "panel.parquet"
    panel.write_parquet(panel_path)

    settings = tmp_path / "settings.yaml"
    settings.write_text(
        yaml.safe_dump(
            {
                "paths": {"cache_dir": str(tmp_path), "reports_dir": str(tmp_path)},
                "contrarian_base": {"min_lots": 1000, "at_low_pct": 2.0, "near_low_pct": 5.0,
                                    "mid_pct": 20.0, "stall_share_pct": 5, "accel_ratio": 1.0},
                "backtest": {
                    "factor_lab": {"panel_path": str(panel_path)},
                    "contrarian_efficacy": {"horizons_td": [20], "focal_horizon": 20,
                                            "n_splits": 3, "n_boot": 50,
                                            "output_dir": str(tmp_path / "out")},
                },
            }
        ),
        encoding="utf-8",
    )
    run_contrarian_efficacy(settings, None)
    out = tmp_path / "out"
    mds = list(out.glob("contrarian_efficacy_*.md"))
    assert len(mds) == 1
    text = mds[0].read_text(encoding="utf-8")
    assert "M-BR1 底部左側偵測 Phase 2" in text
    assert "裁決：" in text
    assert list(out.glob("contrarian_lift_*.csv"))
