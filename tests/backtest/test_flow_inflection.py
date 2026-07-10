"""WS-E flow_inflection 因子 build 單元測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.flow_inflection import FACTOR_SPECS, build_flow_factors

_D0 = date(2026, 1, 5)


def _panel(n_days: int = 70) -> pl.DataFrame:
    days = [_D0 + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(days):
        for sid in ("1111", "2222"):
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "volume": 1000.0,
                    # 1111：前 40 日日流 +100、後 30 日 +500（inflection up）
                    "foreign_net": (100 if i < 40 else 500) if sid == "1111" else 0,
                    "trust_net": 0,
                    "dealer_net": 0,
                }
            )
    return pl.DataFrame(rows)


def test_factor_columns_created() -> None:
    out = build_flow_factors(_panel())
    for c in FACTOR_SPECS:
        assert c in out.columns, c
    assert not any(c.startswith("_") for c in out.columns)  # 中間欄清乾淨


def test_dif_rate_detects_inflection() -> None:
    out = build_flow_factors(_panel()).filter(pl.col("stock_id") == "1111").sort("date")
    # 第 45 日（切換後 5 日）：Σ5/5=500、Σ2 0/20=(15×100+5×500)/20=200 → dif=(500−200)/1000
    r = out.row(44, named=True)
    assert abs(r["f_dif_rate"] - (500 - 200) / 1000) < 1e-9
    # 穩態期（切換前）：Σ5/5 = Σ20/20 = 100 → dif = 0
    r_flat = out.row(30, named=True)
    assert abs(r_flat["f_dif_rate"]) < 1e-9


def test_level_reference_column() -> None:
    out = build_flow_factors(_panel()).filter(pl.col("stock_id") == "1111").sort("date")
    r_flat = out.row(30, named=True)
    assert abs(r_flat["ref_level20"] - 100 / 1000) < 1e-9


def test_null_outside_coverage_and_warmup() -> None:
    """窗未滿（warmup）→ null，不填 0。"""
    out = build_flow_factors(_panel()).filter(pl.col("stock_id") == "1111").sort("date")
    assert out.row(5, named=True)["f_dif_rate"] is None  # vol20/Σ20 皆未滿窗
    assert out.row(0, named=True)["ref_level20"] is None


def test_missing_inst_filled_zero_within_coverage() -> None:
    """覆蓋期內個別檔缺法人紀錄＝0（T86 語義），因子照算不為 null。"""
    base = _panel()
    # 2222 全期 foreign_net=0（模擬「有覆蓋但無進出」——build 前先設 null）
    base = base.with_columns(
        pl.when(pl.col("stock_id") == "2222")
        .then(None)
        .otherwise(pl.col("foreign_net"))
        .alias("foreign_net")
    )
    out = build_flow_factors(base).filter(pl.col("stock_id") == "2222").sort("date")
    r = out.row(44, named=True)
    assert r["f_dif_rate"] == 0.0  # 填 0 後差分為 0，而非 null
