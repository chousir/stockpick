"""backtest/valuation_gap_panel.py — docs/31 §20.11：重建 `val_gap_pct_composite`
（估值回歸參考價綜合版，§20.9）的逐週歷史面板，供效度初測（P0–P5）。

**這是「重建工具」不是「新訊號」**：`val_gap_pct_composite` 自 2026-08-29（commit
`6679d5d`）上線，只在 `reports/2026-W35/` 有一週橫斷面。本模組逐 ISO 週回放
`data/cache/twse/valuation_ratios_*.parquet` 快照（約 12 週深度），用
`analysis/valuation.py` 既有純函式重算 6 條腿 + 綜合缺口%，接上前瞻報酬（fresh
`build_price_panel`，同 `redesign_prelim_read` 慣例）。

誠實邊界（寫進 docs/31 §20.11 覆蓋率聲明）：
- 自身歷史腿需 `min_snapshots=8` → 6 腿綜合版最早只存在於 ~2026-07-03。
- `valuation_ratios` 27 個 dated 快照裡有數組相隔 1 日（0622/0623/0624、
  0826/0827/0828），ISO 週去重後 ~12 週——**數可用 ISO 週、不數快照數**（§22.9
  訂正過的錯：近重複列餵進 moving-block bootstrap 會反保守）。
- forward-return 截斷：實測 r+40 可判 ISO 週數 = 0、r+20 ≈ 4、r+10 ≈ 7，全部
  低於 `moving_block_bootstrap_ci` 的 `T≥10` 下限 → 本輪一律「初測、無正式裁決」。

正確性硬 gate（§20.1 雙路徑校驗、§23.4 script-anchor 紀律）：重建面板的
2026-W35 列 `val_gap_pct_composite` 必須與 `reports/2026-W35/candidates_enriched.csv`
逐檔一致——見 `tests/backtest/test_valuation_gap_panel.py`。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from tw_screener.analysis.valuation import (
    build_valuation,
    compute_composite_valuation_gap,
    compute_self_history_median,
    compute_self_history_median_pb,
    compute_self_history_median_yield,
    compute_valuation_legs,
    implied_price_from_ratio_median,
    implied_price_from_yield_median,
    implied_price_gap_pct,
)

HORIZONS: tuple[int, ...] = (10, 20, 40)
_SNAPSHOT_GAP_TD = 5  # ISO 週快照，約 5 個交易日一次

#: 6 條腿的欄名（順序＝`compute_composite_valuation_gap` 的傳入順序，與
#: `report/group_report.py:1136-1139` 一致）。
COMPOSITE_LEG_COLS: tuple[str, ...] = (
    "val_gap_pct_peer",
    "val_gap_pct_self",
    "val_gap_pct_pb_peer",
    "val_gap_pct_pb_self",
    "val_gap_pct_yield_peer",
    "val_gap_pct_yield_self",
)

_PANEL_SCHEMA: dict[str, type[pl.DataType]] = {
    "date": pl.Date,
    "stock_id": pl.Utf8,
    "sub_industry": pl.Utf8,
    "peer_source": pl.Utf8,
    "val_metric": pl.Utf8,
    "off_pe": pl.Float64,
    "off_pb": pl.Float64,
    "dividend_yield": pl.Float64,
    "close": pl.Float64,
    "val_gap_pct_peer": pl.Float64,
    "val_gap_pct_self": pl.Float64,
    "val_gap_pct_pb_peer": pl.Float64,
    "val_gap_pct_pb_self": pl.Float64,
    "val_gap_pct_yield_peer": pl.Float64,
    "val_gap_pct_yield_self": pl.Float64,
    "val_gap_pct_composite": pl.Float64,
    "val_composite_n_legs": pl.Int64,
    "ma60_dist_pct": pl.Float64,
    "trail_r20": pl.Float64,
    "regime": pl.Utf8,
    "alpha10": pl.Float64,
    "alpha20": pl.Float64,
    "alpha40": pl.Float64,
}


#: 獨立薄 ledger 的 schema（`research/valuation_gap/ledger.csv`）——**刻意不併進
#: `g1_g2_g5_watch` 的 `LEDGER_SCHEMA`**：那份正為另一個 pre-registered 問題累積中，
#: §20.3／§20.4 兩度拒絕中途改 ledger 定義。本 ledger 純為 ~2027 正式裁決時有一份
#: 「生產實際印出的 composite」勿-prune 記錄（`valuation_ratios` 快取 retention 400 天，
#: 逐週重建仍可行，此為 belt-and-suspenders + 抓生產值非重建值）。
LEDGER_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "close": pl.Float64,
    "ma60_dist_pct": pl.Float64,
    "val_metric": pl.Utf8,
    "val_gap_pct_peer": pl.Float64,
    "val_gap_pct_self": pl.Float64,
    "val_gap_pct_pb_peer": pl.Float64,
    "val_gap_pct_pb_self": pl.Float64,
    "val_gap_pct_yield_peer": pl.Float64,
    "val_gap_pct_yield_self": pl.Float64,
    "val_gap_pct_composite": pl.Float64,
    "val_composite_n_legs": pl.Int64,
}


def build_ledger_snapshot(
    cand_rows: list[dict], week: str, data_date: date
) -> pl.DataFrame:
    """把 `group_report.write_candidates_enriched_csv` 回傳的 `cand_rows`（已含
    §20.9 的 6 腿 + composite）抽成 ledger 列——只留有 composite 值的股。"""
    keep = [k for k in LEDGER_SCHEMA if k not in ("week", "data_date")]
    rows: list[dict] = []
    for r in cand_rows:
        if r.get("val_gap_pct_composite") is None:
            continue
        row: dict = {"week": week, "data_date": data_date}
        for k in keep:
            row[k] = r.get(k)
        rows.append(row)
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


def upsert_valuation_gap_ledger(path: Path, snapshot: pl.DataFrame) -> None:
    """`(week, stock_id)` 去重 upsert（同 `g1_g2_g5_watch.upsert_ledger` 慣例）。"""
    if snapshot.is_empty():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        prior = pl.read_csv(path, try_parse_dates=True, schema_overrides=LEDGER_SCHEMA)
        combined = pl.concat([prior, snapshot], how="diagonal_relaxed")
    else:
        combined = snapshot
    combined.unique(subset=["week", "stock_id"], keep="last").sort(
        ["week", "stock_id"]
    ).write_csv(path)


def _num(v: float | int | None, ndigits: int) -> float | None:
    """對齊 `report/group_report.py` 的 `_num`：None 傳遞、否則 round。"""
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def iso_week_snapshot_dates(val_history: pl.DataFrame) -> list[date]:
    """每 ISO 週保留最後一個 `valuation_ratios` 內部 `date`（27 → ~12）。

    §22.9 訂正的教訓：相隔 1 日的近重複快照若都當獨立觀察餵進 moving-block
    bootstrap，會把 ~4 個真實觀察灌成 `T≥10`——反保守 CI。這裡在資料層先去重。
    """
    if val_history.is_empty() or "date" not in val_history.columns:
        return []
    dates = sorted({d for d in val_history["date"].to_list() if d is not None})
    by_week: dict[tuple[int, int], date] = {}
    for d in dates:
        iso = d.isocalendar()
        by_week[(iso[0], iso[1])] = d  # 升序遍歷 → 每 ISO 週留最後一個
    return sorted(by_week.values())


def compute_gap_legs_for_snapshot(
    snap_ratios: pl.DataFrame,
    close_map: dict[str, float],
    membership: pl.DataFrame,
    broad_membership: pl.DataFrame,
    self_history_upto: pl.DataFrame,
    min_peers: int = 5,
    min_snapshots: int = 8,
) -> pl.DataFrame:
    """單一快照日的 6 腿 + 綜合缺口%（逐檔複製 `group_report.py:1101-1139` 的算式）。

    Args:
        snap_ratios: 單日橫斷面 `(stock_id, market, pe, pbr, dividend_yield)`。
        close_map: `stock_id -> close`（該快照 `date` 當日收盤，供
            `implied_price_*` 的中間 round(,2) 對齊——gap% 對 close 幾乎不敏感，
            但為了與生產逐檔一致仍照傳）。
        membership / broad_membership: 同 `report/group_runner.py:391-392`。
        self_history_upto: `val_history.filter(date <= 該快照 date)`——point-in-time，
            自身腿只能看當時為止的歷史。
    """
    if snap_ratios.is_empty():
        return pl.DataFrame(schema={k: _PANEL_SCHEMA[k] for k in _PANEL_SCHEMA})

    valuation = build_valuation(
        snap_ratios, membership, min_peers=min_peers, cheap_pctile=30.0,
        broad_membership=broad_membership,
    )
    legs = compute_valuation_legs(
        snap_ratios, membership, min_peers=min_peers, broad_membership=broad_membership,
    )
    pe_self = compute_self_history_median(self_history_upto, min_snapshots=min_snapshots)
    pb_self = compute_self_history_median_pb(self_history_upto, min_snapshots=min_snapshots)
    yield_self = compute_self_history_median_yield(
        self_history_upto, min_snapshots=min_snapshots
    )

    df = (
        valuation.select(
            "stock_id", "val_metric", "val_median", "peer_source",
            pl.col("pe").alias("off_pe"), pl.col("pbr").alias("off_pb"),
            pl.col("dividend_yield").alias("dy"),
        )
        .join(legs, on="stock_id", how="left")
        .join(pe_self.select("stock_id", "pe_self_median"), on="stock_id", how="left")
        .join(pb_self.select("stock_id", "pb_self_median"), on="stock_id", how="left")
        .join(yield_self.select("stock_id", "yield_self_median"), on="stock_id", how="left")
    )

    rows: list[dict] = []
    for r in df.iter_rows(named=True):
        sid = str(r["stock_id"])
        close = close_map.get(sid)
        if close is None:
            continue
        close = _num(close, 2)
        off_pe = r["off_pe"]
        off_pb = r["off_pb"]
        dy = _num(r["dy"], 2)
        val_metric = r["val_metric"] or ""
        val_median = _num(r["val_median"], 2)

        current_ratio_peer = (
            _num(off_pe, 4) if val_metric == "PE"
            else (_num(off_pb, 4) if val_metric == "PB" else None)
        )
        gap_peer = implied_price_gap_pct(
            implied_price_from_ratio_median(close, current_ratio_peer, val_median), close
        )
        gap_self = implied_price_gap_pct(
            implied_price_from_ratio_median(
                close, _num(off_pe, 4), _num(r["pe_self_median"], 2)
            ),
            close,
        )
        gap_pb_peer = implied_price_gap_pct(
            implied_price_from_ratio_median(
                close, _num(off_pb, 4), _num(r["pb_peer_median"], 2)
            ),
            close,
        )
        gap_pb_self = implied_price_gap_pct(
            implied_price_from_ratio_median(
                close, _num(off_pb, 4), _num(r["pb_self_median"], 2)
            ),
            close,
        )
        gap_yield_peer = implied_price_gap_pct(
            implied_price_from_yield_median(
                close, dy, _num(r["yield_peer_median"], 2)
            ),
            close,
        )
        gap_yield_self = implied_price_gap_pct(
            implied_price_from_yield_median(
                close, dy, _num(r["yield_self_median"], 2)
            ),
            close,
        )
        legs_list = [
            gap_peer, gap_self, gap_pb_peer, gap_pb_self, gap_yield_peer, gap_yield_self,
        ]
        composite, n_legs = compute_composite_valuation_gap(legs_list)
        rows.append(
            {
                "stock_id": sid,
                "peer_source": r["peer_source"],
                "val_metric": val_metric or None,
                "off_pe": _num(off_pe, 4),
                "off_pb": _num(off_pb, 4),
                "dividend_yield": dy,
                "close": close,
                "val_gap_pct_peer": gap_peer,
                "val_gap_pct_self": gap_self,
                "val_gap_pct_pb_peer": gap_pb_peer,
                "val_gap_pct_pb_self": gap_pb_self,
                "val_gap_pct_yield_peer": gap_yield_peer,
                "val_gap_pct_yield_self": gap_yield_self,
                "val_gap_pct_composite": composite,
                "val_composite_n_legs": n_legs,
            }
        )
    leg_schema = {
        k: _PANEL_SCHEMA[k]
        for k in (
            "stock_id", "peer_source", "val_metric", "off_pe", "off_pb",
            "dividend_yield", "close", *COMPOSITE_LEG_COLS,
            "val_gap_pct_composite", "val_composite_n_legs",
        )
    }
    return pl.DataFrame(rows, schema=leg_schema)


def build_valuation_gap_panel(
    val_history: pl.DataFrame,
    membership: pl.DataFrame,
    broad_membership: pl.DataFrame,
    price_panel: pl.DataFrame,
    subindustry_map: pl.DataFrame | None = None,
    regime: pl.DataFrame | None = None,
    min_peers: int = 5,
    min_snapshots: int = 8,
    horizons: tuple[int, ...] = HORIZONS,
    min_rows_per_day: int = 900,
) -> pl.DataFrame:
    """逐 ISO 週重建 `val_gap_pct_composite` 面板 + 前瞻報酬 + 動能控制 + regime。

    Args:
        val_history: `client.load_valuation_ratios_history()` 輸出
            （`date/stock_id/market/pe/pbr/dividend_yield`，逐日累積長表）。
        membership / broad_membership: `build_peer_membership()` /
            `build_broad_industry_membership()`（同生產路徑；本模組視為靜態
            ——12 週內次產業標籤幾乎不動，非 point-in-time，此假設寫進 docs/31）。
        price_panel: `build_price_panel(load_market_history(...), horizons=horizons)`
            輸出（fresh，同 `redesign_prelim_read` 慣例，非 `research/panel/panel.parquet`
            ——後者尾端 forward-return 已 stale）。需 `date/stock_id/close/
            ma60_dist_pct/alpha{h}`。
        subindustry_map: `list_subindustries()`（`sub_industry/stock_id`）；多標籤股
            取第一個標籤（僅供 P0/P5 分群，非估值計算輸入）。
        regime: `date/regime_label` 長表（`research/panel/regime_labels.parquet`）；
            缺席或未涵蓋 → `regime` 欄 null，如實留白。
        min_rows_per_day: 日線快取當日股數低於此值視為「部分抓取日」，排除出
            price_panel 的交易日序列（避免 forward-return 的 row-shift 錯位）——
            預設 900，比照 `factor_lab.daily_ic_series` 的「當日 <10 檔跳過」精神。

    Returns:
        long 面板（`_PANEL_SCHEMA`）：每列＝一個 (ISO 週快照日, stock_id)。
    """
    empty = pl.DataFrame(schema=_PANEL_SCHEMA)
    if val_history.is_empty() or price_panel.is_empty():
        return empty

    # 部分抓取日排除：price_panel 已由 build_price_panel 去重，這裡按當日股數再篩
    day_sizes = price_panel.group_by("date").agg(pl.len().alias("_n"))
    full_days = set(day_sizes.filter(pl.col("_n") >= min_rows_per_day)["date"].to_list())
    pp = price_panel.filter(pl.col("date").is_in(list(full_days)))
    if pp.is_empty():
        return empty

    # 動能控制欄：trailing 20 交易日報酬（自建，**不用 panel 的 forward `r{h}`**——
    # 那是 shift(-1) 前瞻報酬，當控制欄＝把結果灌進訊號側，§22.2 已記錄的 look-ahead）
    pp = pp.sort(["stock_id", "date"]).with_columns(
        (
            (pl.col("close") / pl.col("close").shift(20).over("stock_id") - 1) * 100
        ).alias("trail_r20")
    )

    sub_lookup: dict[str, str] = {}
    if subindustry_map is not None and not subindustry_map.is_empty():
        for r in subindustry_map.group_by("stock_id").agg(
            pl.col("sub_industry").first()
        ).iter_rows(named=True):
            sub_lookup[str(r["stock_id"])] = r["sub_industry"]

    regime_lookup: dict[date, str] = {}
    if regime is not None and not regime.is_empty() and "regime_label" in regime.columns:
        for r in regime.iter_rows(named=True):
            if r["date"] is not None and r["regime_label"] is not None:
                regime_lookup[r["date"]] = r["regime_label"]

    alpha_cols = [f"alpha{h}" for h in horizons]
    snap_dates = iso_week_snapshot_dates(val_history)
    out_frames: list[pl.DataFrame] = []
    for snap_date in snap_dates:
        snap_ratios = val_history.filter(pl.col("date") == snap_date)
        if snap_ratios.is_empty():
            continue
        px_day = pp.filter(pl.col("date") == snap_date)
        if px_day.is_empty():
            continue
        close_map = {
            str(r["stock_id"]): r["close"]
            for r in px_day.select("stock_id", "close").iter_rows(named=True)
            if r["close"] is not None
        }
        self_upto = val_history.filter(pl.col("date") <= snap_date)
        legs = compute_gap_legs_for_snapshot(
            snap_ratios, close_map, membership, broad_membership, self_upto,
            min_peers=min_peers, min_snapshots=min_snapshots,
        )
        if legs.is_empty():
            continue
        joined = legs.drop("close").join(
            px_day.select(
                "stock_id", "close", "ma60_dist_pct", "trail_r20", *alpha_cols
            ),
            on="stock_id", how="inner",
        ).with_columns(
            pl.lit(snap_date).alias("date"),
            pl.col("stock_id")
            .replace_strict(sub_lookup, default=None, return_dtype=pl.Utf8)
            .alias("sub_industry"),
            pl.lit(regime_lookup.get(snap_date), dtype=pl.Utf8).alias("regime"),
        )
        out_frames.append(joined.select(list(_PANEL_SCHEMA.keys())))

    if not out_frames:
        return empty
    return pl.concat(out_frames, how="vertical").sort(["date", "stock_id"])
