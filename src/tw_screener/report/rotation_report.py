"""次產業資金流向輪動報表（rotation 引擎 R3 生產軌，docs/12-sector-rotation.md §5）。

讀 analysis/rotation.py 的流向/籃子輸出，套 R2 校準訊號（settings rotation.entry_signal），
產出 reports/YYYY-Www/sector_rotation.md（人讀）+ .csv（機器讀，供下週 ΔRank）。

四象限（cryptocity 式）：
  資金軸＝長窗法人淨流（>0 流入）；價格軸＝籃子距 position_window 日低點位階
  （≤ position_low_pct% 未漲）。流入×未漲＝下一棒（重點）。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from jinja2 import Environment, FileSystemLoader
from loguru import logger

from tw_screener.analysis.rotation import rank_flows
from tw_screener.backtest.rotation_calib import standardize_signals
from tw_screener.report.group_report import _week_key

Q_NEXT = "下一棒"
Q_TREND = "主升續勢"
Q_DISTRIBUTE = "出貨警訊"
Q_COOL = "冷卻觀望"

# 下週 ΔRank 必需欄（其餘欄位照單全寫，多多益善供 UI/研究）
_TEMPLATE_DIR = Path(__file__).parent / "templates"


def load_prev_rotation_snapshot(reports_dir: Path, week_tag: str) -> pl.DataFrame | None:
    """掃 reports/*/sector_rotation.csv，取週序 < 本週的最近一份（ΔRank 用）；無則 None。"""
    cur = _week_key(week_tag)
    if cur is None or not reports_dir.exists():
        return None
    found: list[tuple[tuple[int, int], Path]] = []
    for d in reports_dir.iterdir():
        snap = d / "sector_rotation.csv"
        k = _week_key(d.name) if d.is_dir() else None
        if k is not None and k < cur and snap.exists():
            found.append((k, snap))
    if not found:
        return None
    found.sort()
    try:
        return pl.read_csv(found[-1][1])
    except Exception as exc:  # noqa: BLE001 — 壞快照不該擋本週報告
        logger.warning("讀上週 sector_rotation.csv 失敗：{}", exc)
        return None


def _basket_position(
    baskets: pl.DataFrame, position_window: int
) -> pl.DataFrame:
    """每次產業最新位階：5 日籃子報酬、距 position_window 日低點 %。"""
    rows = []
    for sub_df in baskets.sort(["sub_industry", "date"]).partition_by(
        "sub_industry", maintain_order=True
    ):
        idx = sub_df["basket_index"].to_list()
        ret_5d = (idx[-1] / idx[-6] - 1) * 100 if len(idx) >= 6 else None
        low = min(idx[-position_window:])
        rows.append(
            {
                "sub_industry": sub_df["sub_industry"][0],
                "basket_ret_5d_pct": ret_5d,
                "above_low_pct": (idx[-1] / low - 1) * 100,
                "members_priced": sub_df["members_priced"][-1],
            }
        )
    return pl.DataFrame(rows)


def build_rotation_table(
    flows: pl.DataFrame,
    baskets: pl.DataFrame,
    short_window: int,
    long_window: int,
    entry_signal: dict,
    position_window: int = 60,
    position_low_pct: float = 10.0,
    rank_by: str | None = None,
    min_members: int = 5,
    prev: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """組輪動主表：流向 × 位階 × 象限 × 校準訊號 × ΔRank（每次產業一列）。"""
    if flows.is_empty() or baskets.is_empty():
        return pl.DataFrame()
    s, lw = short_window, long_window
    rank_by = rank_by or f"net_flow_{lw}d"

    # 校準訊號（R2）：z 模式查 {signal}_z、abs 模式查原欄；+confirm 鏡頭
    sig_name = str(entry_signal.get("signal", f"trust_flow_{lw}d"))
    sig_thr = float(entry_signal.get("threshold", 1.0))
    sig_col = f"{sig_name}_z" if entry_signal.get("mode", "z") == "z" else sig_name
    req_mom = bool(entry_signal.get("require_momentum", False))
    confirm_name = entry_signal.get("confirm_signal")
    confirm_thr = float(entry_signal.get("confirm_threshold", 0.6))

    flows_z = standardize_signals(flows)
    sig_expr = pl.col(sig_col) > sig_thr if sig_col in flows_z.columns else pl.lit(False)
    if req_mom:
        sig_expr = sig_expr & (pl.col("flow_momentum") > 0)
    flows_z = flows_z.with_columns(sig_expr.fill_null(False).alias("entry_triggered"))
    if confirm_name and confirm_name in flows_z.columns:
        flows_z = flows_z.with_columns(
            (pl.col(confirm_name) > confirm_thr).fill_null(False).alias("confirm_triggered")
        )
    else:
        flows_z = flows_z.with_columns(pl.lit(False).alias("confirm_triggered"))

    table = rank_flows(flows_z, by=rank_by, prev=prev, min_members=min_members)
    table = table.join(_basket_position(baskets, position_window), on="sub_industry", how="left")

    inflow = pl.col(f"net_flow_{lw}d") > 0
    risen = pl.col("above_low_pct") > position_low_pct
    return table.with_columns(
        pl.when(inflow & ~risen)
        .then(pl.lit(Q_NEXT))
        .when(inflow & risen)
        .then(pl.lit(Q_TREND))
        .when(~inflow & risen)
        .then(pl.lit(Q_DISTRIBUTE))
        .otherwise(pl.lit(Q_COOL))
        .alias("quadrant"),
        # 報表用張數（法人快取單位：股）
        (pl.col(f"net_flow_{lw}d") / 1000).round(0).alias(f"net_flow_{lw}d_lots"),
        (pl.col(f"net_flow_{s}d") / 1000).round(0).alias(f"net_flow_{s}d_lots"),
        (pl.col("flow_momentum") / 1000).round(0).alias("flow_momentum_lots"),
    )


def render_rotation_report(
    table: pl.DataFrame,
    week_tag: str,
    output_dir: Path,
    short_window: int,
    long_window: int,
    entry_signal: dict,
    position_low_pct: float,
    top_n: int = 10,
    data_date: str = "",
) -> Path:
    """渲染 sector_rotation.md + 寫 sector_rotation.csv，回傳 md 路徑。"""
    s, lw = short_window, long_window
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), keep_trailing_newline=True)
    tpl = env.get_template("sector_rotation.md.j2")

    def _rows(df: pl.DataFrame) -> list[dict]:
        return df.iter_rows(named=True) if not df.is_empty() else []

    quadrants = {
        q: table.filter(pl.col("quadrant") == q).sort("radar_rank")
        for q in (Q_NEXT, Q_TREND, Q_DISTRIBUTE, Q_COOL)
    }
    sig_label = (
        f"{entry_signal.get('signal')}"
        f"（{entry_signal.get('mode', 'z')}>{entry_signal.get('threshold')}"
        f"{'・動能>0' if entry_signal.get('require_momentum') else ''}）"
    )
    # lstrip：模板開頭 macro 定義行會殘留空行
    md = tpl.render(
        week_tag=week_tag,
        data_date=data_date,
        s=s,
        lw=lw,
        top_n=top_n,
        position_low_pct=position_low_pct,
        sig_label=sig_label,
        confirm_label=entry_signal.get("confirm_signal", ""),
        top_rows=list(_rows(table.sort("radar_rank").head(top_n))),
        outflow_rows=list(_rows(quadrants[Q_DISTRIBUTE])),
        q_next=list(_rows(quadrants[Q_NEXT])),
        q_trend=list(_rows(quadrants[Q_TREND])),
        q_cool=list(_rows(quadrants[Q_COOL])),
        triggered=list(_rows(table.filter(pl.col("entry_triggered")).sort("radar_rank"))),
        has_prev=table["rank_delta"].null_count() < table.height if not table.is_empty() else False,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "sector_rotation.md"
    md_path.write_text(md.lstrip("\n"), encoding="utf-8")
    table.write_csv(output_dir / "sector_rotation.csv")
    return md_path
