"""backtest/g1_g2_g5_watch.py — docs/31 §11：G1/G2/G5 前瞻累積軌每週快照。

跟`l6_g4_watch.py`同一種理由（§9 item3/4）：G1/G5 用到的`Δnet_margin`/`Δop_margin`
只能拿到**一個**QoQ差分值（`fundamentals_*.parquet`目前只有2026Q1/Q2兩季），沒有
時間序列深度可回測——但這是「只能前瞻累積、不能回溯」，不是「做不到」。本檔只做
記錄，不做裁決。

**三式定義（docs/31 §4.1，門檻可由 settings 覆寫）**：
- G1｜利潤率擴張優先：`Δnet_margin≥+1.5pp ∧ Δop_margin≥0 ∧ cum_rev_yoy_pct≥0 ∧
  ma60_dist_pct≤+15`。
- G2｜單季ROE×資產負債表體質：`roe_q_pct≥3.5 ∧ debt_ratio_pct≤60 ∧ current_ratio≥1.2
  ∧ market_cap_billion≥300`（§11查核：欄位皆已在生產`fundamentals`快取，不需擴欄）。
- G5｜估值未反映利潤率改善：`val_pctile≤40 ∧ gross_margin_pct≥同次產業中位 ∧
  Δop_margin≥0 ∧ amount_million≥300`。

**cadence差異（跟L6/G4不同，讀底帳時要記住）**：`fundamentals`衍生欄位（net/op/gross
margin、roe/debt/current_ratio、Δ）每季才更新一次（MOPS財報公告頻率）——同一季內
連續好幾週的ledger快照，這些欄位數值會完全相同，只有市值/量能/MA60這類市場面欄位
每週會變。這不是bug，是財報資料本質使然。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

LEDGER_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "market_cap_billion": pl.Float64,
    "cum_rev_yoy_pct": pl.Float64,
    "ma60_dist_pct": pl.Float64,
    "amount_million": pl.Float64,
    "val_pctile": pl.Float64,
    "fundamentals_quarter": pl.Utf8,     # 財報季別（如"2026Q2"），供未來判讀時對照
    "net_margin_pct": pl.Float64,
    "delta_net_margin_pct": pl.Float64,
    "op_margin_pct": pl.Float64,
    "delta_op_margin_pct": pl.Float64,
    "gross_margin_pct": pl.Float64,
    "gross_margin_peer_median": pl.Float64,
    "roe_q_pct": pl.Float64,
    "debt_ratio_pct": pl.Float64,
    "current_ratio": pl.Float64,
    "g1": pl.Boolean,
    "g2": pl.Boolean,
    "g5": pl.Boolean,
}


def build_g1_g2_g5_snapshot(
    universe: pl.DataFrame,
    fundamentals: pl.DataFrame,
    gross_margin_peer: pl.DataFrame,
    valuation: pl.DataFrame,
    ma60_map: dict[str, float | None],
    amount_map: dict[str, float | None],
    week: str,
    data_date: date,
    g1_delta_net_margin_min: float = 1.5,
    g1_ma60_max_pct: float = 15.0,
    g2_roe_min: float = 3.5,
    g2_debt_max_pct: float = 60.0,
    g2_current_min: float = 1.2,
    g2_mktcap_min_billion: float = 300.0,
    g5_val_pctile_max: float = 40.0,
    g5_amount_min_million: float = 300.0,
) -> pl.DataFrame:
    """把全市場當週快照併成 G1/G2/G5 判準列，只回傳至少命中一式的股票。

    Args:
        universe: `screener.local.universe.build_local_universe()` 輸出（需
            stock_id/name/market_cap_billion/cum_rev_yoy_pct）。
        fundamentals: `client.load_fundamentals_history()` 輸出。
        gross_margin_peer: `analysis.valuation.compute_subind_relative(fundamentals,
            membership, value_col="gross_margin_pct")` 輸出（需 stock_id/subind_median）。
        valuation: `analysis.valuation.build_valuation()` 輸出（需 stock_id/val_pctile）。
        ma60_map / amount_map: {stock_id: 值}，呼叫端算好傳入（見 runner）。
    """
    need = {"stock_id", "name", "market_cap_billion", "cum_rev_yoy_pct"}
    if universe.is_empty() or not need.issubset(universe.columns):
        return pl.DataFrame(schema=LEDGER_SCHEMA)

    base = universe.select("stock_id", "name", "market_cap_billion", "cum_rev_yoy_pct")
    if not fundamentals.is_empty():
        base = base.join(
            fundamentals.select(
                "stock_id", "quarter_label", "net_margin_pct", "delta_net_margin_pct",
                "op_margin_pct", "delta_op_margin_pct", "gross_margin_pct",
                "roe_q_pct", "debt_ratio_pct", "current_ratio",
            ),
            on="stock_id", how="left",
        )
    if not gross_margin_peer.is_empty():
        peer_col = pl.col("subind_median").alias("gross_margin_peer_median")
        base = base.join(
            gross_margin_peer.select("stock_id", peer_col), on="stock_id", how="left",
        )
    if not valuation.is_empty():
        base = base.join(
            valuation.select("stock_id", "val_pctile"), on="stock_id", how="left"
        )

    rows: list[dict] = []
    for r in base.iter_rows(named=True):
        sid = r["stock_id"]
        cum_yoy = r.get("cum_rev_yoy_pct")
        mktcap = r.get("market_cap_billion")
        delta_net = r.get("delta_net_margin_pct")
        delta_op = r.get("delta_op_margin_pct")
        net_margin = r.get("net_margin_pct")
        gross_margin = r.get("gross_margin_pct")
        peer_median = r.get("gross_margin_peer_median")
        roe = r.get("roe_q_pct")
        debt = r.get("debt_ratio_pct")
        current = r.get("current_ratio")
        val_pctile = r.get("val_pctile")
        ma60 = ma60_map.get(sid)
        amount = amount_map.get(sid)

        g1 = bool(
            delta_net is not None and delta_net >= g1_delta_net_margin_min
            and delta_op is not None and delta_op >= 0
            and cum_yoy is not None and cum_yoy >= 0
            and ma60 is not None and ma60 <= g1_ma60_max_pct
        )
        g2 = bool(
            roe is not None and roe >= g2_roe_min
            and debt is not None and debt <= g2_debt_max_pct
            and current is not None and current >= g2_current_min
            and mktcap is not None and mktcap >= g2_mktcap_min_billion
        )
        g5 = bool(
            val_pctile is not None and val_pctile <= g5_val_pctile_max
            and gross_margin is not None and peer_median is not None
            and gross_margin >= peer_median
            and delta_op is not None and delta_op >= 0
            and amount is not None and amount >= g5_amount_min_million
        )
        if not (g1 or g2 or g5):
            continue
        rows.append(
            {
                "week": week,
                "data_date": data_date,
                "stock_id": sid,
                "name": r.get("name"),
                "market_cap_billion": mktcap,
                "cum_rev_yoy_pct": cum_yoy,
                "ma60_dist_pct": ma60,
                "amount_million": amount,
                "val_pctile": val_pctile,
                "fundamentals_quarter": r.get("quarter_label"),
                "net_margin_pct": net_margin,
                "delta_net_margin_pct": delta_net,
                "op_margin_pct": r.get("op_margin_pct"),
                "delta_op_margin_pct": delta_op,
                "gross_margin_pct": gross_margin,
                "gross_margin_peer_median": peer_median,
                "roe_q_pct": roe,
                "debt_ratio_pct": debt,
                "current_ratio": current,
                "g1": g1,
                "g2": g2,
                "g5": g5,
            }
        )
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


@dataclass(frozen=True)
class G1G2G5Inputs:
    """`build_g1_g2_g5_snapshot()` 需要的五個輸入（見 `build_g1_g2_g5_inputs()`）。"""

    fundamentals: pl.DataFrame
    gross_margin_peer: pl.DataFrame
    valuation: pl.DataFrame
    ma60_map: dict[str, float | None]
    amount_map: dict[str, float | None]


def build_g1_g2_g5_inputs(client, cfg: dict, universe: pl.DataFrame) -> G1G2G5Inputs:  # noqa: ANN001 — TWSEClient，避免循環 import
    """組 `build_g1_g2_g5_snapshot()` 需要的五個輸入（純讀既有快取，不打網）。

    抽出自原本寫死在 `g1_g2_g5_watch_runner.run_g1_g2_g5_watch` 裡的邏輯，供該 CLI
    runner 與 `report/group_runner.py`（docs/31 使用者要求把 G1/G2/G5 揭露進
    `candidates_enriched.csv` 後新增）共用，避免兩處各自維護一份等價邏輯。
    """
    from pathlib import Path as _Path

    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.analysis.sector_universe import (
        build_peer_membership,
        list_subindustries,
        load_industry_mapping,
    )
    from tw_screener.analysis.valuation import build_valuation, compute_subind_relative

    wc = cfg.get("backtest", {}).get("g1_g2_g5_watch", {})
    ma60_window = int(wc.get("ma60_window", 60))
    market_history_days = int(wc.get("market_history_days", 90))
    min_peers = int(wc.get("min_peers", 5))
    cache_dir = _Path(cfg["paths"]["cache_dir"]) / "twse"

    fundamentals = client.load_fundamentals_history()

    industry = load_industry_mapping(cache_dir)
    hand = list_subindustries()
    membership = build_peer_membership(hand, industry)

    gross_margin_peer = (
        compute_subind_relative(
            fundamentals, membership, value_col="gross_margin_pct", min_peers=min_peers
        )
        if not fundamentals.is_empty() and not membership.is_empty()
        else pl.DataFrame(schema={"stock_id": pl.Utf8, "subind_median": pl.Float64})
    )

    ratios = client.load_latest_valuation_ratios()
    valuation = (
        build_valuation(ratios, membership, min_peers=min_peers)
        if not ratios.is_empty() and not membership.is_empty()
        else pl.DataFrame(schema={"stock_id": pl.Utf8, "val_pctile": pl.Float64})
    )

    # ma60_dist_pct：全市場累積日線快取算 rolling MA60，取最新一日
    market_hist = load_market_history(cache_dir, n_days=market_history_days)
    ma60_map: dict[str, float | None] = {}
    if not market_hist.is_empty():
        ma60_expr = (
            pl.col("close")
            .rolling_mean(ma60_window, min_samples=ma60_window)
            .over("stock_id")
            .alias("_ma60")
        )
        ma = (
            market_hist.sort(["stock_id", "date"])
            .with_columns(ma60_expr)
            .filter(pl.col("date") == pl.col("date").max())
            .with_columns(
                pl.when(pl.col("_ma60") > 0)
                .then((pl.col("close") - pl.col("_ma60")) / pl.col("_ma60") * 100)
                .otherwise(None)
                .alias("_dist")
            )
        )
        ma60_map = {
            str(r["stock_id"]): (float(r["_dist"]) if r["_dist"] is not None else None)
            for r in ma.iter_rows(named=True)
        }

    # amount_million：今日成交金額（daily_*/otc_daily_* 已有 trade_value，原始新台幣元）
    amount_map: dict[str, float | None] = {}
    for df in (client.fetch_daily_all(), client.fetch_otc_daily_all()):
        if df.is_empty() or "trade_value" not in df.columns:
            continue
        for r in df.iter_rows(named=True):
            tv = r.get("trade_value")
            amount_map[str(r["stock_id"])] = (float(tv) / 1e6) if tv is not None else None

    return G1G2G5Inputs(
        fundamentals=fundamentals,
        gross_margin_peer=gross_margin_peer,
        valuation=valuation,
        ma60_map=ma60_map,
        amount_map=amount_map,
    )


def _read_ledger(path: Path) -> pl.DataFrame:
    """讀既有底帳 CSV，全欄位明帶 `schema_overrides=LEDGER_SCHEMA`。

    同`l6_g4_watch._read_ledger`修法（2026-08-23）：全欄位明帶schema，避免某週
    命中列的某數值欄全null時被polars推斷成Utf8、下次concat把整欄（含先前週真實值）
    污染成字串。
    """
    if not path.exists():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    return pl.read_csv(path, try_parse_dates=True, schema_overrides=LEDGER_SCHEMA)


def upsert_ledger(path: Path, new_rows: pl.DataFrame) -> pl.DataFrame:
    """把本週快照併入底帳（以 (week, stock_id) 去重、冪等——同週重跑覆寫舊列）。"""
    if new_rows.is_empty():
        return _read_ledger(path)
    existing = _read_ledger(path)
    if not existing.is_empty():
        week = new_rows["week"][0]
        existing = existing.filter(pl.col("week") != week)
    merged = pl.concat([existing, new_rows], how="diagonal_relaxed").sort(["week", "stock_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_csv(path)
    return merged


def ledger_progress_summary(ledger: pl.DataFrame) -> dict[str, object]:
    """底帳累積進度一行摘要（runner 印給人看，不是統計裁決）。"""
    if ledger.is_empty():
        return {"n_weeks": 0, "weeks": [], "n_g1": 0, "n_g2": 0, "n_g5": 0}
    return {
        "n_weeks": ledger["week"].n_unique(),
        "weeks": sorted(ledger["week"].unique().to_list()),
        "n_g1": int(ledger["g1"].fill_null(False).sum()),
        "n_g2": int(ledger["g2"].fill_null(False).sum()),
        "n_g5": int(ledger["g5"].fill_null(False).sum()),
    }
