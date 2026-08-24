"""docs/31 §21.4：G1/G2/G4/G5/L6 初步forward alpha讀值測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.backtest.redesign_prelim_read import (
    compute_prelim_forward_alpha,
    format_prelim_report,
)

_DAYS = [date(2026, 1, 5) + timedelta(days=i) for i in range(15)]


def _price(stock_id: str, closes: list[float]) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame(
        {"date": _DAYS[:n], "stock_id": [stock_id] * n, "close": closes, "volume": [1000.0] * n}
    )


def _ledger(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "week": pl.Utf8,
        "data_date": pl.Date,
        "stock_id": pl.Utf8,
        "g1": pl.Boolean,
        "g2": pl.Boolean,
    }
    return pl.DataFrame(rows, schema=schema)


def test_flagged_stock_outperforming_shows_positive_delta() -> None:
    # AAA 一路上漲、BBB 持平——AAA 應該有正的 alpha/delta，BBB 接近 0
    aaa_closes = [100.0 * (1.02**i) for i in range(15)]
    bbb_closes = [100.0] * 15
    price = pl.concat([_price("1101", aaa_closes), _price("2330", bbb_closes)])
    ledger = _ledger(
        [
            {
                "week": "2026-W02",
                "data_date": _DAYS[0],
                "stock_id": "1101",
                "g1": True,
                "g2": False,
            },
            {
                "week": "2026-W02",
                "data_date": _DAYS[0],
                "stock_id": "2330",
                "g1": False,
                "g2": False,
            },
        ]
    )
    out = compute_prelim_forward_alpha(ledger, price, ("g1", "g2"), horizons=(10,))
    g1_row = out.filter(pl.col("flag") == "g1").row(0, named=True)
    assert g1_row["n"] == 1
    assert g1_row["delta_mean"] > 0  # AAA跑贏當日全樣本(AAA+BBB)均值
    # g2沒有任何True列，不應出現在輸出
    assert out.filter(pl.col("flag") == "g2").is_empty()


def test_empty_ledger_returns_empty_schema() -> None:
    price = _price("1101", [100.0] * 15)
    empty_ledger = pl.DataFrame(
        schema={"week": pl.Utf8, "data_date": pl.Date, "stock_id": pl.Utf8, "g1": pl.Boolean}
    )
    out = compute_prelim_forward_alpha(empty_ledger, price, ("g1",))
    assert out.is_empty()
    assert set(out.columns) == {
        "flag",
        "horizon",
        "n",
        "n_dates",
        "mean",
        "median",
        "win_rate",
        "delta_mean",
        "ci_lo",
        "ci_hi",
    }


def test_empty_price_history_returns_empty() -> None:
    ledger = _ledger(
        [{"week": "2026-W02", "data_date": _DAYS[0], "stock_id": "1101", "g1": True, "g2": False}]
    )
    out = compute_prelim_forward_alpha(ledger, pl.DataFrame(), ("g1",))
    assert out.is_empty()


def test_small_sample_ci_is_none() -> None:
    """n_dates<10 → moving_block_bootstrap_ci回(None,None)，不硬套CI。"""
    aaa_closes = [100.0 * (1.01**i) for i in range(15)]
    price = _price("1101", aaa_closes)
    ledger = _ledger(
        [{"week": "2026-W02", "data_date": _DAYS[0], "stock_id": "1101", "g1": True, "g2": False}]
    )
    out = compute_prelim_forward_alpha(ledger, price, ("g1",), horizons=(10,))
    row = out.row(0, named=True)
    assert row["n_dates"] == 1
    assert row["ci_lo"] is None
    assert row["ci_hi"] is None


def test_format_prelim_report_handles_empty_and_populated() -> None:
    aaa_closes = [100.0 * (1.02**i) for i in range(15)]
    bbb_closes = [100.0] * 15
    price = pl.concat([_price("1101", aaa_closes), _price("2330", bbb_closes)])
    ledger = _ledger(
        [
            {
                "week": "2026-W02",
                "data_date": _DAYS[0],
                "stock_id": "1101",
                "g1": True,
                "g2": False,
            },
            {
                "week": "2026-W02",
                "data_date": _DAYS[0],
                "stock_id": "2330",
                "g1": False,
                "g2": False,
            },
        ]
    )
    populated = compute_prelim_forward_alpha(ledger, price, ("g1", "g2"), horizons=(10,))
    empty = compute_prelim_forward_alpha(pl.DataFrame(schema=ledger.schema), price, ("g4",))
    report = format_prelim_report(populated, empty)
    assert "非規劃書§7.4正式驗證" in report
    assert "g1" in report
    assert "無資料" in report
