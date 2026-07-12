"""WS-D laggard_grid 單元測試（全離線合成資料）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import yaml

from tw_screener.backtest.laggard_grid import (
    laggard_cell_grid,
    laggard_cell_grid_by_regime,
    stock_rs_vs_group,
)
from tw_screener.backtest.laggard_grid_runner import run_laggard_grid

_D0 = date(2026, 1, 5)


def test_stock_rs_vs_group_basket_diff() -> None:
    """rs_subind＝個股窗報酬 − 籃等權窗報酬（pp）。"""
    days = [_D0 + timedelta(days=i) for i in range(3)]
    # A：100→110（+10%）；B：100→120（+20%）→ 籃 +15%；rs_A=−5pp、rs_B=+5pp
    panel = pl.DataFrame(
        {
            "date": days * 2,
            "stock_id": ["1111"] * 3 + ["2222"] * 3,
            "close": [100.0, 105.0, 110.0, 100.0, 110.0, 120.0],
        }
    )
    membership = pl.DataFrame(
        {"sub_industry": ["甲", "甲"], "stock_id": ["1111", "2222"]}
    )
    rs = stock_rs_vs_group(panel, membership, window=2)
    rows = {r["stock_id"]: r for r in rs.filter(pl.col("date") == days[2]).iter_rows(named=True)}
    assert abs(rows["1111"]["rs_subind"] - (-5.0)) < 1e-9
    assert abs(rows["2222"]["rs_subind"] - 5.0) < 1e-9


def test_laggard_cell_grid_dimensions_and_baseline() -> None:
    """cell 維度標籤正確、全體基準列存在、n 加總守恆。"""
    n_days, n_stocks = 30, 30
    days = [_D0 + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(days):
        for s in range(n_stocks):
            rows.append(
                {
                    "date": d,
                    "stock_id": f"s{s:03d}",
                    "sub_industry": "甲" if s < 15 else "乙",
                    "rs_subind": -1.0 if s % 2 == 0 else 1.0,
                    "trend_score": 80.0 if s < 15 else 20.0,
                    "ma60_dist_pct": [5.0, 12.0, 30.0][s % 3],
                    "alpha20": 1.0 * (s % 5) - 1.0 + 0.01 * i,
                }
            )
    df = pl.DataFrame(rows)
    grid = laggard_cell_grid(df, horizons=(20,), tier_edges=(10.0, 15.0), n_boot=100)
    cells = grid.filter(pl.col("group_strength") != "（全體）")
    # 2×2×3＝12 cells 全在
    assert cells.height == 12
    assert set(cells["tier"].to_list()) == {"貼低≤10", "中段10–15", "延伸>15"}
    baseline = grid.filter(pl.col("group_strength") == "（全體）")
    assert baseline.height == 1
    assert int(cells["n"].sum()) == baseline.row(0, named=True)["n"]
    # 前後半段欄有值
    r0 = cells.row(0, named=True)
    assert r0["mean_h1"] is not None and r0["mean_h2"] is not None


def test_laggard_cell_grid_null_inputs_dropped() -> None:
    df = pl.DataFrame(
        {
            "date": [_D0] * 3,
            "rs_subind": [None, -1.0, 1.0],
            "trend_score": [50.0, None, 60.0],
            "ma60_dist_pct": [5.0, 5.0, None],
            "alpha20": [1.0, 1.0, 1.0],
        }
    )
    grid = laggard_cell_grid(df, horizons=(20,), n_boot=100)
    assert grid.is_empty() or grid["n"].sum() == 0  # 全列有 null → 沒有可用樣本


# ── WS-H.4a：cell×regime 全列舉（含空 cell n=0）──────────────────────────────


def _regime_grid_fixture(defense_drop_extended: bool = True) -> pl.DataFrame:
    """30 日×30 檔、regime 各 10 日；防禦期（可選）剔除延伸股 → 該 regime 延伸 cell n=0。"""
    n_days, n_stocks = 30, 30
    days = [_D0 + timedelta(days=i) for i in range(n_days)]
    rows = []
    for i, d in enumerate(days):
        regime = ("進攻", "中性", "防禦")[i // 10]
        for s in range(n_stocks):
            tier_val = [5.0, 12.0, 30.0][s % 3]
            if defense_drop_extended and regime == "防禦" and tier_val == 30.0:
                continue
            rows.append(
                {
                    "date": d,
                    "stock_id": f"s{s:03d}",
                    "sub_industry": "甲" if s < 15 else "乙",
                    "regime": regime,
                    "rs_subind": -1.0 if s % 2 == 0 else 1.0,
                    "trend_score": 80.0 if s < 15 else 20.0,
                    "ma60_dist_pct": tier_val,
                    "alpha20": 1.0 * (s % 5) - 1.0 + 0.01 * i,
                }
            )
    return pl.DataFrame(rows)


def test_laggard_cell_grid_by_regime_full_enumeration_with_empty_cells() -> None:
    """12 cell×3 regime＝36 列全列舉；空 cell（防禦×延伸）n=0 照列不藏格。"""
    df = _regime_grid_fixture()
    grid = laggard_cell_grid_by_regime(df, horizons=(20,), tier_edges=(10.0, 15.0), n_boot=100)
    assert grid.height == 36  # 2×2×3×3 全列舉
    assert set(grid["regime"].to_list()) == {"進攻", "中性", "防禦"}
    empty = grid.filter((pl.col("regime") == "防禦") & (pl.col("tier") == "延伸>15"))
    assert empty.height == 4  # 2×2 個空 cell 全在
    assert empty["n"].to_list() == [0, 0, 0, 0]
    assert all(r["mean"] is None and r["thin"] for r in empty.iter_rows(named=True))
    filled = grid.filter((pl.col("regime") == "進攻") & (pl.col("tier") == "貼低≤10"))
    assert all(r["n"] > 0 and r["n_dates"] == 10 for r in filled.iter_rows(named=True))
    # n 加總守恆＝fixture 可用列數（全部 alpha20 非 null）
    assert int(grid["n"].sum()) == df.height


def test_laggard_cell_grid_by_regime_null_regime_returns_empty() -> None:
    """regime 欄全 null 或缺席（標籤未產）→ 空表＝runner 誠實跳過路徑。"""
    df = _regime_grid_fixture().with_columns(pl.lit(None, dtype=pl.Utf8).alias("regime"))
    assert laggard_cell_grid_by_regime(df, horizons=(20,), n_boot=100).is_empty()
    df2 = _regime_grid_fixture().drop("regime")
    assert laggard_cell_grid_by_regime(df2, horizons=(20,), n_boot=100).is_empty()


def test_laggard_cell_grid_unchanged_by_refactor() -> None:
    """_assign_cells 抽取後，基礎 12 cell 表行為不變（口徑守恆抽查）。"""
    df = _regime_grid_fixture(defense_drop_extended=False).drop("regime")
    grid = laggard_cell_grid(df, horizons=(20,), tier_edges=(10.0, 15.0), n_boot=100)
    cells = grid.filter(pl.col("group_strength") != "（全體）")
    assert cells.height == 12
    assert int(cells["n"].sum()) == df.height


# ── WS-J.2 membership robustness：run_laggard_grid --membership 煙測 ─────────
# 全離線：settings/panel/concepts.yaml／industry 快取皆為 tmp_path 合成檔，不打網。

_MEMBER_STOCKS = ["1101", "1102", "1103", "2201", "2202", "2203"]


def _weekdays(n: int, start: date = _D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _membership_panel(stocks: list[str], n_days: int = 60) -> pl.DataFrame:
    """6 檔×60 交易日合成 panel：close/volume/ma60_dist_pct/alpha5 全無 null。"""
    days = _weekdays(n_days)
    rows = []
    for i, d in enumerate(days):
        for si, sid in enumerate(stocks):
            rows.append(
                {
                    "date": d,
                    "stock_id": sid,
                    "close": 100.0 + si * 5.0 + i * (0.3 + si * 0.15),
                    "volume": 1_000_000,
                    "ma60_dist_pct": [5.0, 12.0, 30.0][(i + si) % 3],
                    "alpha5": 1.0 + 0.05 * i - 0.2 * si,
                }
            )
    return pl.DataFrame(rows)


def _write_laggard_settings(
    tmp_path: Path, panel: pl.DataFrame, out_dir: Path, cache_dir: Path | None = None
) -> Path:
    panel_path = tmp_path / "panel.parquet"
    panel.write_parquet(panel_path)
    cfg: dict = {
        "backtest": {
            "factor_lab": {"panel_path": str(panel_path)},
            "laggard_grid": {
                "horizons_td": [5], "rs_window": 5, "n_boot": 30,
                "output_dir": str(out_dir),
            },
        },
    }
    if cache_dir is not None:
        cfg["paths"] = {"cache_dir": str(cache_dir)}
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return settings


def test_run_laggard_grid_concepts_default_filename_has_no_suffix(tmp_path, monkeypatch):
    """不傳 membership_source（預設 concepts）＝現行行為零變化，輸出檔名無 _official 後綴。"""
    panel = _membership_panel(_MEMBER_STOCKS)
    out_dir = tmp_path / "out"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "concepts.yaml").write_text(
        yaml.safe_dump(
            {
                "concepts": {
                    "1101": "甲次產業", "1102": "甲次產業", "1103": "甲次產業",
                    "2201": "乙次產業", "2202": "乙次產業", "2203": "乙次產業",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = _write_laggard_settings(tmp_path, panel, out_dir)
    monkeypatch.chdir(tmp_path)

    run_laggard_grid(settings, out_dir)  # membership_source 用預設值

    files = {p.name for p in out_dir.iterdir()}
    tag = date.today().strftime("%Y%m%d")
    assert f"laggard_grid_{tag}.csv" in files
    assert f"laggard_grid_{tag}.md" in files
    assert not any("_official" in f for f in files)
    md_text = (out_dir / f"laggard_grid_{tag}.md").read_text(encoding="utf-8")
    assert "membership=TWSE 官方產業別" not in md_text
    assert "對照基準版" not in md_text


def _industry_df(rows: list[tuple[str, str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema=["stock_id", "stock_name", "industry_code", "industry_name"],
        orient="row",
    )


def test_run_laggard_grid_official_membership_filename_has_suffix(tmp_path):
    """membership_source="official"：輸出檔名加 _official 後綴、不覆蓋基準版；報告標明來源。"""
    panel = _membership_panel(_MEMBER_STOCKS)
    out_dir = tmp_path / "out"
    cache_dir = tmp_path / "cache"
    twse_cache = cache_dir / "twse"
    twse_cache.mkdir(parents=True)
    _industry_df(
        [
            ("1101", "股1101", "10", "水泥工業"),
            ("1102", "股1102", "10", "水泥工業"),
            ("1103", "股1103", "10", "水泥工業"),
            ("2201", "股2201", "20", "運輸業"),
            ("2202", "股2202", "20", "運輸業"),
            ("2203", "股2203", "20", "運輸業"),
            ("9999", "新股尚未歸類", "", ""),  # industry_name 空 → 應被誠實排除
        ]
    ).write_parquet(twse_cache / "industry_202607.parquet")
    settings = _write_laggard_settings(tmp_path, panel, out_dir, cache_dir=cache_dir)

    run_laggard_grid(settings, out_dir, "official")

    tag = date.today().strftime("%Y%m%d") + "_official"
    files = {p.name for p in out_dir.iterdir()}
    assert f"laggard_grid_{tag}.csv" in files
    assert f"laggard_grid_{tag}.md" in files
    md_text = (out_dir / f"laggard_grid_{tag}.md").read_text(encoding="utf-8")
    assert "membership=TWSE 官方產業別" in md_text
    assert "官方缺分類" in md_text and "排除 1 檔" in md_text
    assert "對照基準版" in md_text
    base_tag = date.today().strftime("%Y%m%d")
    assert f"laggard_grid_{base_tag}.md" in md_text  # 對照提示指到基準版路徑（不覆蓋）


def test_run_laggard_grid_invalid_membership_source_exits(tmp_path):
    panel = _membership_panel(_MEMBER_STOCKS)
    out_dir = tmp_path / "out"
    settings = _write_laggard_settings(tmp_path, panel, out_dir)
    import pytest
    import typer

    with pytest.raises(typer.Exit):
        run_laggard_grid(settings, out_dir, "bogus")
