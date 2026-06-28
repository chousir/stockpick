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
    cp_position_ceiling: float = 60.0,
    rank_by: str | None = None,
    min_members: int = 5,
    prev: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """組輪動主表：流向 × 位階 × 象限 × CP 分數 × 校準訊號 × ΔRank（每次產業一列）。

    cp_score（A1 連續補漲分數）＝資金 z ×「位階剩餘空間」room，
    room = clip(1 − above_low_pct / cp_position_ceiling, 0, 1)。
    錢進越多（z 高）× 越貼低點（room 高）→ CP 越高（補漲機會）。z 或位階缺 → null。

    freshness（A2 資金動能新鮮度）＝ flow_momentum 正負：「加速」（動能轉強，
    流入續勢／流出收斂）vs「放緩」（動能轉弱）。流入象限 加速＝新鮮、放緩＝退潮；
    流出象限 加速＝資金轉向（反轉前哨）。動能 0 或缺 → null。

    flow_turn（修法 3・資金轉向覆蓋）＝長窗(lw)與短窗(s)淨流「符號背離」前哨。
    四象限 x 軸只看 lw 日淨流正負，會把「lw 日仍負但近 s 日已轉買」誤判為純出貨、
    把「lw 日正但近 s 日轉賣」誤判為純主升。故獨立標：
    「資金回流」＝ lw 日<0 且 s 日>0（出貨警訊可能緩解，逐檔複核）；
    「退潮」＝ lw 日>0 且 s 日<0（主升動能近端轉弱，留意）；同號或缺 → null。
    與 freshness 區分：freshness 看「加速度」（s 日 vs 前 s 日），flow_turn 看「近端實際買賣方向」。
    """
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
    table = table.with_columns(
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
    # A1：連續 CP 補漲分數＝資金 z（跨族群可比）× 位階剩餘空間 room
    z_col = f"net_flow_{lw}d_z"
    if z_col in table.columns:
        room = (1 - pl.col("above_low_pct") / cp_position_ceiling).clip(0.0, 1.0)
        cp_expr = (pl.col(z_col) * room).round(2)
    else:  # z 欄缺（理論上不會，standardize_signals 必產）→ 全 null，誠實降級
        cp_expr = pl.lit(None, dtype=pl.Float64)
    # A2：資金動能新鮮度＝flow_momentum（=Tide 資金加速度）正負
    fresh_expr = (
        pl.when(pl.col("flow_momentum") > 0)
        .then(pl.lit("加速"))
        .when(pl.col("flow_momentum") < 0)
        .then(pl.lit("放緩"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
    # 修法 3：資金轉向覆蓋＝長窗 vs 短窗淨流符號背離（解「20 日累積到後面其實在賣/在買」盲點）
    long_flow, short_flow = pl.col(f"net_flow_{lw}d"), pl.col(f"net_flow_{s}d")
    turn_expr = (
        pl.when((long_flow < 0) & (short_flow > 0))
        .then(pl.lit("資金回流"))  # 20 日流出但近 5 日轉買→出貨警訊可能緩解
        .when((long_flow > 0) & (short_flow < 0))
        .then(pl.lit("退潮"))  # 20 日流入但近 5 日轉賣→主升近端轉弱
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
    return table.with_columns(
        cp_expr.alias("cp_score"),
        fresh_expr.alias("freshness"),
        turn_expr.alias("flow_turn"),
    )


def build_participation(
    sources: list[tuple[str, list[str], bool]],
    membership: pl.DataFrame,
    table: pl.DataFrame,
    long_window: int,
    names: dict[str, str] | None = None,
) -> list[dict]:
    """「我的參與度」疊圖（docs/12 §5.4）：每股 → 所屬次產業的象限/資金方向。

    Args:
        sources: [(來源名, stock_ids, compact)]；compact=True 按象限壓縮列（命中股多用）
        membership: (sub_industry, stock_id)；table: build_rotation_table 輸出
        names: stock_id → 名稱（缺時只列股號）

    Returns:
        每來源一 dict：source / compact / stocks（逐檔 tags）/ by_quadrant /
        n_total / n_inflow / n_warning / n_untagged。空 stock_ids 的來源略過。
    """
    names = names or {}
    flow_col = f"net_flow_{long_window}d"
    sub_info: dict[str, dict] = {
        r["sub_industry"]: {
            "quadrant": r["quadrant"],
            "inflow": (r[flow_col] or 0) > 0,
            "triggered": bool(r["entry_triggered"]),
        }
        for r in table.iter_rows(named=True)
    }
    subs_by_stock: dict[str, list[str]] = {}
    for sub, sid in membership.iter_rows():
        subs_by_stock.setdefault(sid, []).append(sub)

    out: list[dict] = []
    for source, stock_ids, compact in sources:
        if not stock_ids:
            continue
        stocks: list[dict] = []
        by_quadrant: dict[str, list[str]] = {}
        n_inflow = n_warning = n_untagged = 0
        for sid in dict.fromkeys(stock_ids):  # 去重保序
            label = f"{sid} {names.get(sid, '')}".strip()
            tags = []
            for sub in subs_by_stock.get(sid, []):
                info = sub_info.get(sub)
                tags.append(
                    {
                        "sub": sub,
                        # quadrant None＝榜外（成員數 < min_members 未列輪動排名）
                        "quadrant": info["quadrant"] if info else None,
                        "inflow": info["inflow"] if info else None,
                        "triggered": info["triggered"] if info else False,
                    }
                )
            if not tags:
                n_untagged += 1
                by_quadrant.setdefault("未標次產業", []).append(label)
            else:
                if any(t["inflow"] for t in tags):
                    n_inflow += 1
                if any(t["quadrant"] == Q_DISTRIBUTE for t in tags):
                    n_warning += 1
                for t in tags:
                    q = t["quadrant"] or "榜外"
                    by_quadrant.setdefault(q, []).append(f"{label}（{t['sub']}）")
            stocks.append({"stock_id": sid, "label": label, "tags": tags})
        out.append(
            {
                "source": source,
                "compact": compact,
                "stocks": stocks,
                "by_quadrant": by_quadrant,
                "n_total": len(stocks),
                "n_inflow": n_inflow,
                "n_warning": n_warning,
                "n_untagged": n_untagged,
            }
        )
    return out


def _quadrant_chart_points(
    rows: list[dict], long_window: int, position_low_pct: float
) -> list[dict]:
    """把次產業 (淨流, 位階) 轉成 Mermaid quadrantChart 的 [0,1] 相對座標（Section 3 象限圖）。

    x 軸＝資金流出→流入：x=0.5 對齊淨流=0；y 軸＝位階低→高：y=0.5 對齊門檻 position_low_pct。
    如此確保點落入與 `quadrant` 欄一致的象限。幅度以本批最大絕對值正規化（避免「股」單位讓點全擠在
    中線），夾限 [0.04, 0.96]。同次產業取首見、缺淨流或位階者略過。
    """
    flow_col = f"net_flow_{long_window}d"
    seen: set[str] = set()
    cand: list[tuple[str, float, float]] = []
    for r in rows:
        sub = r.get("sub_industry")
        nf = r.get(flow_col)
        al = r.get("above_low_pct")
        if sub is None or sub in seen or nf is None or al is None:
            continue
        seen.add(str(sub))
        cand.append((str(sub), float(nf), float(al)))
    if not cand:
        return []
    max_nf = max((abs(nf) for _, nf, _ in cand), default=0.0) or 1.0
    max_d = max((abs(al - position_low_pct) for _, _, al in cand), default=0.0) or 1.0

    def _clamp(v: float) -> float:
        return round(min(0.96, max(0.04, v)), 3)

    return [
        {
            "label": sub,
            "x": _clamp(0.5 + 0.42 * nf / max_nf),
            "y": _clamp(0.5 + 0.42 * (al - position_low_pct) / max_d),
        }
        for sub, nf, al in cand
    ]


def render_rotation_report(
    table: pl.DataFrame,
    week_tag: str,
    output_dir: Path,
    short_window: int,
    long_window: int,
    entry_signal: dict,
    position_low_pct: float,
    cp_position_ceiling: float = 60.0,
    top_n: int = 10,
    data_date: str = "",
    density_note: str = "",
    participation: list[dict] | None = None,
) -> Path:
    """渲染 sector_rotation.md + 寫 sector_rotation.csv，回傳 md 路徑。

    participation：build_participation 輸出；None/空 → 略過「我的參與度」段（誠實降級）。
    """
    s, lw = short_window, long_window
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), keep_trailing_newline=True)
    tpl = env.get_template("sector_rotation.md.j2")

    def _rows(df: pl.DataFrame) -> list[dict]:
        return df.iter_rows(named=True) if not df.is_empty() else []

    quadrants = {
        q: table.filter(pl.col("quadrant") == q).sort("radar_rank")
        for q in (Q_NEXT, Q_TREND, Q_DISTRIBUTE, Q_COOL)
    }
    # A1：CP 候選＝正分（資金流入 × 仍有位階空間）取前 top_n，跨象限排序
    cp_rows = (
        list(_rows(table.filter(pl.col("cp_score") > 0).sort("cp_score", descending=True).head(top_n)))
        if "cp_score" in table.columns
        else []
    )
    sig_label = (
        f"{entry_signal.get('signal')}"
        f"（{entry_signal.get('mode', 'z')}>{entry_signal.get('threshold')}"
        f"{'・動能>0' if entry_signal.get('require_momentum') else ''}）"
    )
    # Section 3 象限圖：取「資金流入前 N（含下一棒/主升）＋出貨警訊」兩組，去重後算相對座標
    chart_rows = list(_rows(table.sort("radar_rank").head(top_n))) + list(
        _rows(quadrants[Q_DISTRIBUTE])
    )
    chart_points = _quadrant_chart_points(chart_rows, lw, position_low_pct)
    # lstrip：模板開頭 macro 定義行會殘留空行
    md = tpl.render(
        week_tag=week_tag,
        data_date=data_date,
        density_note=density_note,
        s=s,
        lw=lw,
        top_n=top_n,
        position_low_pct=position_low_pct,
        cp_ceiling=cp_position_ceiling,
        cp_rows=cp_rows,
        sig_label=sig_label,
        confirm_label=entry_signal.get("confirm_signal", ""),
        top_rows=list(_rows(table.sort("radar_rank").head(top_n))),
        outflow_rows=list(_rows(quadrants[Q_DISTRIBUTE])),
        q_next=list(_rows(quadrants[Q_NEXT])),
        q_trend=list(_rows(quadrants[Q_TREND])),
        q_cool=list(_rows(quadrants[Q_COOL])),
        chart_points=chart_points,
        triggered=list(_rows(table.filter(pl.col("entry_triggered")).sort("radar_rank"))),
        has_prev=table["rank_delta"].null_count() < table.height if not table.is_empty() else False,
        participation=participation or [],
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "sector_rotation.md"
    md_path.write_text(md.lstrip("\n"), encoding="utf-8")
    table.write_csv(output_dir / "sector_rotation.csv")
    return md_path
