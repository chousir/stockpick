"""族群分析報告產生器：把 groups / members DataFrame 渲染成 group_analysis.md。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl
from jinja2 import Environment, FileSystemLoader
from loguru import logger

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_STRATEGY_LABEL: dict[str, str] = {
    "a_breakout": "A",
    "b_growth_institutional": "B",
    "c_dividend_steady": "C",
    "c_low_base_growth": "C",
    "c_quality_value": "C",
    "d_quality_leader": "D",
    "e_growth_momentum": "E",
    "f_value_rebound": "F",
    "g_growth_pullback": "G",
}

_STRATEGY_NAME: dict[str, str] = {
    "a_breakout": "動能突破",
    "b_growth_institutional": "成長主力",
    "c_dividend_steady": "穩健存股",
    "c_low_base_growth": "低基期成長",
    "c_quality_value": "品質價值",
    "d_quality_leader": "品質龍頭",
    "e_growth_momentum": "成長動能",
    "f_value_rebound": "價值反彈",
    "g_growth_pullback": "成長拉回",
}

_STRATEGY_DESCRIPTION: dict[str, str] = {
    "a_breakout": (
        "週 MACD 翻多 + 5/10/20 均線多頭排列且走揚 + 流動性過濾"
        "（短線 1–4 週・攻擊）"
    ),
    "b_growth_institutional": (
        "月營收 YoY ≥ 15% + 連續 2 季稅後淨利成長 + 外資連買"
        "（中線 1–3 月・主力）"
    ),
    "c_dividend_steady": "連續配息 8 年 + 殖利率 ≥ 4% + 股利持續成長（長線 6 月以上）",
    "c_low_base_growth": "月營收 YoY ≥ 10% + 外資連續買超（中線 1–3 月）",
    "c_quality_value": (
        "近 4 季 ROE ≥ 20% + 連續配息 10 年以上 + 殖利率 ≥ 4%（長線・頂尖品質）"
    ),
    "d_quality_leader": (
        "市值 ≥ 100 億 + 近 4 季 ROE ≥ 15% + 連續配息 8 年 + 連 2 季淨利"
        "（長線品質龍頭・ProPicks TWCH15 風格）"
    ),
    "e_growth_momentum": (
        "市值 ≥ 100 億 + 月營收 YoY ≥ 20% + 連 2 季淨利 + 均線多頭排列"
        "（中線成長動能・ProPicks Tech Titans 風格）"
    ),
    "f_value_rebound": (
        "市值 ≥ 100 億 + 本益比 ≤ 15 + 殖利率 ≥ 3% + 累計營收 YoY ≥ 10%"
        "（中線價值反彈・ProPicks Top Value 風格）"
    ),
    "g_growth_pullback": (
        "市值 ≥ 100 億 + 月營收 YoY ≥ 20% + 季線上揚回踩（乖離 −5%~+10%）+ 量縮"
        "（中線成長拉回・回檔買點，E 的逆勢孿生）"
    ),
}

_UNCATEGORIZED = "未分類"
_TOP_PER_GROUP = 3
_TOP_STRONG = 15  # 跨族群強勢領漲股 Top N（個股領漲鏡頭，補族群廣度鏡頭的盲點）


def _fmt_pct(v: float) -> str:
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def _strategy_str(row: dict, strategy_ids: list[str]) -> str:
    labels = [
        _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids if row.get(f"in_{sid}")
    ]
    return "+".join(labels) if labels else "-"


def _build_stock_dict(
    srow: dict,
    strategy_ids: list[str],
    group_size: int,
    theme_str_map: dict[str, str] | None = None,
) -> dict:
    momentum_5d = float(srow.get("momentum_5d", srow.get("rs", 0)) or 0)
    days_used = int(srow.get("momentum_days_used", 0) or 0)
    rank_in_group = int(srow.get("rank_in_group", 0) or 0)
    inst_net_lots = float(srow.get("inst_net", 0) or 0) / 1000.0  # 股 → 張
    foreign_lots = float(srow.get("foreign_net", 0) or 0) / 1000.0  # 外資
    trust_lots = float(srow.get("trust_net", 0) or 0) / 1000.0  # 投信
    vol_ratio = float(srow.get("vol_ratio", 0) or 0)
    vol_ratio_str = f"{vol_ratio:.1f}x" if vol_ratio > 0 else "-"

    def _ma_str(key: str) -> str:
        v = srow.get(key)
        if v is None or (isinstance(v, float) and v != v):  # None or NaN
            return "-"
        return f"{v:+.1f}%"

    ma20_str = _ma_str("ma20_dist_pct")
    ma60_str = _ma_str("ma60_dist_pct")
    sub_industry_str = (theme_str_map or {}).get(str(srow["stock_id"]), "—")
    return {
        "stock_id": srow["stock_id"],
        "name": srow["name"],
        "strategy_str": _strategy_str(srow, strategy_ids),
        "strategy_count": int(srow.get("strategy_count", 1)),
        "change_pct": float(srow.get("change_pct", 0) or 0),
        "change_pct_str": _fmt_pct(float(srow.get("change_pct", 0) or 0)),
        "momentum_5d": momentum_5d,
        "momentum_5d_str": _fmt_pct(momentum_5d),
        "momentum_days_used": days_used,
        "momentum_partial": days_used > 0 and days_used < 5,
        "amount_million": float(srow.get("amount_million", 0) or 0),
        "leader_score": float(srow.get("leader_score", 0) or 0),
        "rank_in_group": rank_in_group,
        "is_top_in_group": rank_in_group == 1,
        "inst_net_lots": inst_net_lots,
        "inst_net_str": f"{inst_net_lots:+,.0f}",
        "foreign_net_str": f"{foreign_lots:+,.0f}",
        "trust_net_str": f"{trust_lots:+,.0f}",
        "vol_ratio": vol_ratio,
        "vol_ratio_str": vol_ratio_str,
        "ma20_str": ma20_str,
        "ma60_str": ma60_str,
        "sub_industry_str": sub_industry_str,
        "goodinfo_url": str(srow.get("goodinfo_url", "")),
        "group_size": group_size,
    }


def _build_theme_str_map(
    members: pl.DataFrame,
    themes_long: pl.DataFrame,
    ranked: pl.DataFrame,
    cap: int = 3,
) -> dict[str, str]:
    """每檔的主題顯示字串：次產業（身分）優先、再補當前最強的概念股主題，合計上限 cap。

    「哪個多標籤重要」交給強度排名回答：身分必顯示，題材按 ranked 的 score 由高到低取。
    """
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND

    if members.is_empty() or themes_long.is_empty():
        return {}
    score_map: dict[str, float] = {}
    if not ranked.is_empty():
        score_map = {r["theme"]: float(r["score"]) for r in ranked.iter_rows(named=True)}
    member_ids = set(members["stock_id"].to_list())
    by_stock: dict[str, list[tuple[int, float, str]]] = {}
    for row in themes_long.iter_rows(named=True):
        sid = row["stock_id"]
        if sid not in member_ids:
            continue
        kind_rank = 0 if row["kind"] == SUB_INDUSTRY_KIND else 1  # 次產業優先
        by_stock.setdefault(sid, []).append(
            (kind_rank, -score_map.get(row["theme"], 0.0), row["theme"])
        )
    out: dict[str, str] = {}
    for sid, items in by_stock.items():
        items.sort()  # 次產業優先，組內按 score 由高到低
        out[sid] = "、".join(t for _, _, t in items[:cap])
    return out


def _rank_delta_str(rd: object) -> str:
    """ΔRank 顯示：▲升／▼降／0 持平／—（無上週快照）。"""
    if rd is None:
        return "—"
    n = int(rd)  # type: ignore[call-overload]
    if n > 0:
        return f"▲{n}"
    if n < 0:
        return f"▼{-n}"
    return "0"


_SNAPSHOT_COLS = [
    "theme",
    "kind",
    "radar_rank",
    "lead_score",
    "score",
    "momentum_5d",
    "members_count",
    "foreign_score",
    "vol_surge_score",
    "rank_delta",
]


def _week_key(name: str) -> tuple[int, int] | None:
    """'2026-W23' → (2026, 23)，供跨週排序；非法名稱回 None。"""
    m = re.match(r"(\d{4})-W(\d{1,2})", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _load_prev_theme_snapshot(report_dir: Path, week_tag: str) -> pl.DataFrame | None:
    """掃 reports/*/theme_strength.csv，取週序 < 本週的最近一份（供 ΔRank）；無則 None。"""
    cur = _week_key(week_tag)
    root = report_dir.parent
    if cur is None or not root.exists():
        return None
    found: list[tuple[tuple[int, int], Path]] = []
    for d in root.iterdir():
        snap = d / "theme_strength.csv"
        k = _week_key(d.name) if d.is_dir() else None
        if k is not None and k < cur and snap.exists():
            found.append((k, snap))
    if not found:
        return None
    found.sort()
    try:
        return pl.read_csv(found[-1][1])
    except Exception as exc:  # noqa: BLE001 — 壞快照不該擋住本週報告
        logger.warning("讀上週 theme_strength 失敗：{}", exc)
        return None


def _write_theme_snapshot(radar: pl.DataFrame, report_dir: Path) -> None:
    """寫本週 theme_strength.csv（機器可讀快照，供下週算 ΔRank）。"""
    if radar.is_empty():
        return
    cols = [c for c in _SNAPSHOT_COLS if c in radar.columns]
    report_dir.mkdir(parents=True, exist_ok=True)
    radar.select(cols).write_csv(report_dir / "theme_strength.csv")


def _load_rotation_overlay(report_dir: Path | None) -> dict[str, dict]:
    """讀本週 sector_rotation.csv（R3 全宇宙資金流向輪動），供 Section 2.8 並列對照。

    回傳 {次產業: {rank, quadrant, triggered}}；檔不存在（沒先跑 make rotation）→ 空 dict、
    雷達表該兩欄顯示 —（優雅降級，不報錯）。make week 已將 rotation 排在 group 之前。
    """
    if report_dir is None:
        return {}
    snap = report_dir / "sector_rotation.csv"
    if not snap.exists():
        return {}
    try:
        df = pl.read_csv(snap)
    except Exception as exc:  # noqa: BLE001 — 壞快照不該擋本週報告
        logger.warning("讀 sector_rotation.csv 失敗：{}", exc)
        return {}
    if not {"sub_industry", "radar_rank", "quadrant"}.issubset(df.columns):
        return {}
    return {
        r["sub_industry"]: {
            "rank": int(r["radar_rank"]),
            "quadrant": str(r["quadrant"]),
            "triggered": bool(r.get("entry_triggered", False)),
        }
        for r in df.iter_rows(named=True)
    }


# CSV quadrant 值（狀態常數）→ 中性顯示名（與 M1 輪動報表語意一致）
_QUAD_LABEL = {
    "主升續勢": "流入×已漲",
    "出貨警訊": "流出×已漲",
    "下一棒": "流入×未漲",
    "冷卻觀望": "流出×未漲",
}


def _build_rotation_axis(
    report_dir: Path | None,
    themes_long: pl.DataFrame,
    candidate_ids: set[str],
    covered_subs: set[str],
    axis_cfg: dict | None,
) -> dict | None:
    """本週族群主軸（問題3・M3）：從 sector_rotation.csv 蒸餾趨勢分 top／流入×未漲／
    ★觸發，並標各次產業「候選股中成員 N 檔」——把全市場輪動訊號接回個股選股宇宙。

    uncovered＝趨勢 top∪流入×未漲 中未被 Section 5 雷達六塊涵蓋者（如安全監控型：
    族群強但無候選命中），附 RS 領頭股供人工補看。無 sector_rotation.csv → None（降級略段）。
    """
    if report_dir is None:
        return None
    snap = report_dir / "sector_rotation.csv"
    if not snap.exists():
        return None
    try:
        df = pl.read_csv(snap)
    except Exception as exc:  # noqa: BLE001 — 壞快照不擋報告
        logger.warning("讀 sector_rotation.csv（主軸）失敗：{}", exc)
        return None
    if not {"sub_industry", "trend_score", "quadrant"}.issubset(df.columns):
        return None

    cfg = axis_cfg or {}
    trend_top_n = int(cfg.get("trend_top_n", 5))
    # 候選股中各次產業成員數（themes_long kind=次產業 ∩ 本週候選股；theme 欄＝次產業名）
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND

    cand_count: dict[str, int] = {}
    if not themes_long.is_empty() and candidate_ids and "theme" in themes_long.columns:
        hit = themes_long.filter(
            (pl.col("kind") == SUB_INDUSTRY_KIND)
            & pl.col("stock_id").is_in(list(candidate_ids))
        )
        for r in hit.group_by("theme").len().iter_rows(named=True):
            cand_count[str(r["theme"])] = int(r["len"])

    def _item(r: dict) -> dict:
        sub = str(r["sub_industry"])
        quad = str(r["quadrant"]) if r.get("quadrant") is not None else None
        n = cand_count.get(sub, 0)
        return {
            "sub_industry": sub,
            "trend_score_str": (
                f"{float(r['trend_score']):.0f}" if r.get("trend_score") is not None else "—"
            ),
            "quadrant_label": (
                _QUAD_LABEL.get(quad, quad) if quad else "位階未取得"
            ),
            "radar_rank": int(r["radar_rank"]) if r.get("radar_rank") is not None else None,
            "n_candidates": n,
            "triggered": bool(r.get("entry_triggered", False)),
            "precision": bool(r.get("next_precision", False)),
            "leader_stock_id": str(r["leader_stock_id"]) if r.get("leader_stock_id") else "",
            "leader_rs_str": (
                f"{float(r['leader_rs_pct']):+.0f}%" if r.get("leader_rs_pct") is not None else ""
            ),
        }

    rows = list(df.iter_rows(named=True))
    by_trend = sorted(
        rows, key=lambda r: (r.get("trend_score") is not None, r.get("trend_score") or 0),
        reverse=True,
    )
    trend_top = [_item(r) for r in by_trend[:trend_top_n]]
    next_up = [_item(r) for r in rows if str(r.get("quadrant") or "") == "下一棒"]
    triggered = [_item(r) for r in rows if bool(r.get("entry_triggered", False))]

    # uncovered：主軸關注（趨勢 top∪流入×未漲）但 Section 5 雷達未涵蓋者
    axis_subs = {it["sub_industry"] for it in trend_top} | {it["sub_industry"] for it in next_up}
    # 趨勢 top∪流入未漲 中未被雷達六塊涵蓋者，去重（兩清單可能重疊）保序
    uncovered: list[dict] = []
    seen: set[str] = set()
    for it in trend_top + next_up:
        sub = it["sub_industry"]
        if sub not in covered_subs and sub in axis_subs and sub not in seen:
            seen.add(sub)
            uncovered.append(it)

    return {
        "trend_top": trend_top,
        "next_up": next_up,
        "triggered": triggered,
        "uncovered": uncovered,
    }


def _build_radar(
    ranked: pl.DataFrame,
    members: pl.DataFrame,
    themes_long: pl.DataFrame,
    theme_str_map: dict[str, str],
    strategy_ids: list[str],
    report_dir: Path | None,
    week_tag: str,
    radar_cfg: dict | None,
) -> tuple[list[dict], list[dict]]:
    """輪動雷達：以 lead_score 重排（領先鏡頭）＋週對週 ΔRank，並寫本週快照。

    回傳 (radar_groups〔Section 2.8〕, radar_deep_dive〔Section 6・top-N 次產業含成員股〕)。
    """
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND
    from tw_screener.analysis.grouping import attach_rank_delta

    if ranked.is_empty():
        return [], []
    radar = ranked.sort("lead_score", descending=True).with_row_index("radar_rank", offset=1)
    prev = _load_prev_theme_snapshot(report_dir, week_tag) if report_dir else None
    radar = attach_rank_delta(radar, prev)
    if report_dir is not None:
        _write_theme_snapshot(radar, report_dir)
    rotation_map = _load_rotation_overlay(report_dir)  # R5：全宇宙資金輪動並列對照

    def _row_common(row: dict) -> dict:
        cnt = int(row["members_count"])
        fb = int(row.get("foreign_buy_count", 0) or 0)
        vs = int(row.get("vol_surge_count", 0) or 0)
        rot = rotation_map.get(row["theme"])
        return {
            "theme": row["theme"],
            "kind": row["kind"],
            "members_count": cnt,
            "momentum_5d_str": _fmt_pct(float(row.get("momentum_5d", 0) or 0)),
            "foreign_breadth_str": f"{fb}/{cnt}" if cnt else "0/0",
            "vol_surge_str": f"{vs}/{cnt}" if cnt else "0/0",
            "rank_delta_str": _rank_delta_str(row.get("rank_delta")),
            "lead_score_str": f"{float(row['lead_score']):.1f}",
            "score_str": f"{float(row['score']):.1f}",
            "rotation_rank_str": f"#{rot['rank']}" if rot else "—",
            "rotation_quadrant_str": (
                f"{rot['quadrant']}{'★' if rot['triggered'] else ''}" if rot else "—"
            ),
        }

    radar_groups = [
        {"radar_rank": int(row["radar_rank"]), **_row_common(row)}
        for row in radar.iter_rows(named=True)
    ]

    # Section 6 深度解讀：top-N 次產業（kind=次產業）by lead_score，逐塊列其成員候選股
    deep_n = int((radar_cfg or {}).get("deep_dive_top_n", 6))
    radar_deep_dive: list[dict] = []
    if not members.is_empty() and not themes_long.is_empty():
        member_ids = set(members["stock_id"].to_list())
        theme_members: dict[str, list[str]] = {}
        for tr in themes_long.filter(pl.col("kind") == SUB_INDUSTRY_KIND).iter_rows(named=True):
            sid = tr["stock_id"]
            if sid in member_ids:
                theme_members.setdefault(tr["theme"], []).append(sid)
        sub_radar = radar.filter(pl.col("kind") == SUB_INDUSTRY_KIND).head(deep_n)
        for row in sub_radar.iter_rows(named=True):
            ids = theme_members.get(row["theme"], [])
            sub_df = members.filter(pl.col("stock_id").is_in(ids)).sort(
                "leader_score", descending=True
            )
            stocks = [
                _build_stock_dict(sr, strategy_ids, len(ids), theme_str_map)
                for sr in sub_df.iter_rows(named=True)
            ]
            radar_deep_dive.append({**_row_common(row), "stocks": stocks})

    return radar_groups, radar_deep_dive


def _build_context(
    groups: pl.DataFrame,
    members: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    top_groups: int,
    top_stocks: int,
    dividend_events: pl.DataFrame | None = None,
    themes_long: pl.DataFrame | None = None,
    macro_events: pl.DataFrame | None = None,
    report_dir: Path | None = None,
    radar_cfg: dict | None = None,
) -> dict:
    strategy_ids = sorted(screener_results.keys())
    # Only show strategies that have at least 1 result
    active_strategy_ids = [sid for sid in strategy_ids if len(screener_results.get(sid, [])) > 0]

    # --- summary ---
    # 「共 N 檔」採有效命中數（分析池內 in_{sid}=True）。對 D/E/F 等於其 CSV 命中；
    # 對 G 則為「過拉回過濾後的有效命中」（其 CSV 是較大的基本面成長宇宙）。
    if not members.is_empty():
        counts: dict[str, int] = {
            sid: (
                int(members.select(pl.col(f"in_{sid}").sum()).item() or 0)
                if f"in_{sid}" in members.columns
                else len(screener_results.get(sid, []))
            )
            for sid in strategy_ids
        }
    else:
        counts = {sid: len(df) for sid, df in screener_results.items()}
    # G 的原始基本面宇宙大小（CSV 列數），供 Section 1 註解對照
    g_universe_size = len(screener_results.get("g_growth_pullback", []))
    total_union = len(members) if not members.is_empty() else 0

    intersections: dict[str, list[str]] = {}
    if not members.is_empty() and len(active_strategy_ids) >= 2:
        for i, sid_a in enumerate(active_strategy_ids):
            for sid_b in active_strategy_ids[i + 1 :]:
                def _lbl(sid: str) -> str:
                    lbl = _STRATEGY_LABEL.get(sid, sid[0].upper())
                    return f"{lbl}（{_STRATEGY_NAME.get(sid, sid)}）"
                key = f"{_lbl(sid_a)}∩{_lbl(sid_b)}"
                mask = pl.col(f"in_{sid_a}") & pl.col(f"in_{sid_b}")
                intersections[key] = members.filter(mask)["stock_id"].to_list()

        if len(active_strategy_ids) >= 3:
            mask_all = pl.lit(True)
            for sid in active_strategy_ids:
                mask_all = mask_all & pl.col(f"in_{sid}")
            key_all = "∩".join(
                _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in active_strategy_ids
            )
            intersections[key_all] = members.filter(mask_all)["stock_id"].to_list()

    summary = {"counts": counts, "total_union": total_union, "intersections": intersections}

    # --- strategy legend (Section 0) ---
    strategy_legend = [
        {
            "label": _STRATEGY_LABEL.get(sid, sid[0].upper()),
            "name": _STRATEGY_NAME.get(sid, sid),
            "description": _STRATEGY_DESCRIPTION.get(sid, ""),
        }
        for sid in active_strategy_ids
    ]

    # --- 主題強度排名（次產業 + 概念股，多標籤 long table）＋ 逐股主題顯示字串 ---
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND
    from tw_screener.analysis.grouping import rank_themes

    themes_long = themes_long if themes_long is not None else pl.DataFrame()
    rc = radar_cfg or {}
    ranked = rank_themes(
        members,
        themes_long,
        vol_surge_ratio=float(rc.get("vol_surge_ratio", 1.5)),
        lead_weights=rc.get("lead_weights"),
    )
    theme_str_map = _build_theme_str_map(members, themes_long, ranked)
    radar_groups, radar_deep_dive = _build_radar(
        ranked,
        members,
        themes_long,
        theme_str_map,
        strategy_ids,
        report_dir,
        week_tag,
        radar_cfg,
    )
    # 本週族群主軸（問題3・M3）：sector_rotation 趨勢/流入未漲/★ → 候選成員數 → uncovered 補位
    covered_subs = {d["theme"] for d in radar_deep_dive}
    candidate_ids = set(members["stock_id"].to_list()) if not members.is_empty() else set()
    rotation_axis = _build_rotation_axis(
        report_dir,
        themes_long,
        candidate_ids,
        covered_subs,
        (radar_cfg or {}).get("main_axis"),
    )

    sub_groups: list[dict] = []
    concept_groups: list[dict] = []
    for row in ranked.iter_rows(named=True):
        cnt = int(row["members_count"])
        up = int(row.get("up_count", 0) or 0)
        ib = int(row.get("inst_buy_count", 0) or 0)
        mom = float(row.get("momentum_5d", 0) or 0)
        entry = {
            "members_count": cnt,
            "momentum_5d_str": _fmt_pct(mom),
            "breadth_str": f"{up}/{cnt}" if cnt else "0/0",
            "inst_breadth_str": f"{ib}/{cnt}" if cnt else "0/0",
            "score_str": f"{float(row['score']):.1f}",
        }
        if row["kind"] == SUB_INDUSTRY_KIND:
            sub_groups.append({"sub_industry": row["theme"], **entry})
        else:
            concept_groups.append({"theme": row["theme"], **entry})

    # --- groups table (top N) ---
    top_groups_df = groups.head(top_groups)
    group_list: list[dict] = []

    for rank, row in enumerate(top_groups_df.iter_rows(named=True), start=1):
        industry_code = row["industry_code"]
        members_count = int(row["members_count"])
        total_in = int(row.get("total_in_industry", members_count))
        up_count = int(row.get("up_count", 0) or 0)
        breadth_str = (
            f"{up_count}/{members_count} ({up_count / members_count * 100:.0f}%)"
            if members_count
            else "0/0"
        )
        inst_buy_count = int(row.get("inst_buy_count", 0) or 0)
        inst_breadth_str = f"{inst_buy_count}/{members_count}" if members_count else "0/0"

        counts_per_sid = {sid: int(row.get(f"count_{sid}", 0)) for sid in strategy_ids}

        if not members.is_empty():
            group_stocks_df = members.filter(pl.col("industry_code") == industry_code).sort(
                "leader_score", descending=True
            )
        else:
            group_stocks_df = pl.DataFrame()

        n = len(group_stocks_df)
        all_stocks = [
            _build_stock_dict(srow, strategy_ids, n, theme_str_map)
            for srow in group_stocks_df.iter_rows(named=True)
        ]

        top_stocks_in_group = all_stocks[:_TOP_PER_GROUP]
        rest_stocks = all_stocks[_TOP_PER_GROUP:]

        momentum_5d = float(row.get("momentum_5d", row.get("rs_avg", 0)) or 0)
        days_used = int(row.get("momentum_5d_days_used", 0) or 0)

        group_list.append(
            {
                "rank": rank,
                "industry_name": row["industry_name"],
                "industry_code": industry_code,
                "is_uncategorized": row["industry_name"] == _UNCATEGORIZED,
                "counts": counts_per_sid,
                "members_count": members_count,
                "up_count": up_count,
                "breadth_str": breadth_str,
                "inst_buy_count": inst_buy_count,
                "inst_breadth_str": inst_breadth_str,
                "total_in_industry": total_in,
                "entry_rate_pct_str": f"{float(row['entry_rate']) * 100:.1f}%",
                "momentum_5d": momentum_5d,
                "momentum_5d_str": _fmt_pct(momentum_5d),
                "momentum_5d_days_used": days_used,
                "momentum_partial": days_used > 0 and days_used < 5,
                "score_str": f"{float(row['score']):.1f}",
                "stocks": all_stocks,
                "top_stocks": top_stocks_in_group,
                "rest_stocks": rest_stocks,
            }
        )

    # --- 跨族群強勢領漲股（純 5 日漲幅排序，補族群廣度鏡頭埋掉的權值領漲）---
    # 半導體這種「少數龍頭噴出、多數拉回」的大族群，5 日中位被稀釋→族群排名靠後，
    # 但領漲個股（如台積電）仍應被看見。此處不分族群、純動能排序攤出。
    strong_stocks: list[dict] = []
    if not members.is_empty() and "momentum_5d" in members.columns:
        strong_df = members.sort("momentum_5d", descending=True).head(_TOP_STRONG)
        for srow in strong_df.iter_rows(named=True):
            d = _build_stock_dict(srow, strategy_ids, len(strong_df), theme_str_map)
            d["industry_name"] = srow.get("industry_name", _UNCATEGORIZED)
            strong_stocks.append(d)

    # --- Claude analysis section: top 4 categorised groups ---
    claude_groups = [g for g in group_list if not g["is_uncategorized"]][:4]

    # --- dividend calendar (forward 除權息 events for candidates) ---
    dividend_rows: list[dict] = []
    if dividend_events is not None and not dividend_events.is_empty():
        for r in dividend_events.iter_rows(named=True):
            cash = r.get("cash_dividend")
            dividend_rows.append(
                {
                    "ex_date": r["ex_date"].isoformat() if r.get("ex_date") else "",
                    "stock_id": r.get("stock_id", ""),
                    "name": r.get("name", ""),
                    "type": r.get("type", ""),
                    "cash_dividend": f"{cash:.2f}" if cash is not None else "-",
                }
            )

    # --- macro calendar (forward 市場級總經事件) ---
    macro_rows: list[dict] = []
    if macro_events is not None and not macro_events.is_empty():
        for r in macro_events.iter_rows(named=True):
            mdate = r.get("date")
            macro_rows.append(
                {
                    "date": mdate.isoformat() if mdate else "",
                    "name": r.get("name", ""),
                    "category": r.get("category", ""),
                    "severity": r.get("severity", ""),
                    "verified": bool(r.get("verified", False)),
                    "note": r.get("note", ""),
                }
            )

    return {
        "week_tag": week_tag,
        "generated_at": date.today().isoformat(),
        "strategy_ids": strategy_ids,
        "active_strategy_ids": active_strategy_ids,
        "strategy_labels": {sid: _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids},
        "strategy_names": {
            sid: f"{_STRATEGY_LABEL.get(sid, sid[0].upper())}（{_STRATEGY_NAME.get(sid, sid)}）"
            for sid in strategy_ids
        },
        "strategy_legend": strategy_legend,
        "summary": summary,
        "g_universe_size": g_universe_size,
        "strong_stocks": strong_stocks,
        "sub_groups": sub_groups,
        "concept_groups": concept_groups,
        "groups": group_list,
        "top_groups": top_groups,
        "claude_groups": claude_groups,
        "radar_groups": radar_groups,
        "radar_deep_dive": radar_deep_dive,
        "rotation_axis": rotation_axis,
        "dividend_rows": dividend_rows,
        "macro_rows": macro_rows,
    }


def render_group_report(
    groups: pl.DataFrame,
    members: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    output_path: Path,
    top_groups: int = 10,
    top_stocks: int = 10,
    dividend_events: pl.DataFrame | None = None,
    themes_long: pl.DataFrame | None = None,
    macro_events: pl.DataFrame | None = None,
    radar_cfg: dict | None = None,
    density_note: str = "",
    regime: dict | None = None,
    portfolio: dict | None = None,
) -> None:
    """Render group_analysis.md to output_path using Jinja2 template.

    members: rank_within_groups 的回傳；含 rank_in_group / leader_score / momentum_5d。
    dividend_events: 候選股未來窗內除權息（filter_dividend_calendar 的回傳）；None/空則不渲染該段。
    themes_long: load_themes() 的 (stock_id, theme, kind) long table；None/空則主題排名留空。
    regime: regime.describe_regime() 的顯示 dict（規劃書 03 V2）；None 則不渲染大盤姿態段。
    portfolio: portfolio.describe_portfolio_check() 的顯示 dict（規劃書 03 V3）；
        None 則不渲染組合體檢段。
    """
    context = _build_context(
        groups,
        members,
        screener_results,
        week_tag,
        top_groups,
        top_stocks,
        dividend_events,
        themes_long,
        macro_events,
        report_dir=output_path.parent,
        radar_cfg=radar_cfg,
    )
    context["density_note"] = density_note
    context["regime"] = regime
    context["portfolio"] = portfolio

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("group_analysis.md.j2")
    content = template.render(**context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("group_analysis.md 輸出 → {}", output_path)


def _annotate_sector_flag_coverage(
    rows: list[dict],
    themes_long: pl.DataFrame | None,
    coverage_pct: float,
    min_members: int,
    flags_watched: tuple[str, ...] = ("土洋對作", "過熱"),
) -> None:
    """M-WS5a（WS5-②）：把 sector-wide 旗標降為輪動訊號（in-place 填 rows 的 sector_flag_note）。

    同一旗標若在某次產業的候選股裡掛旗佔比 ≥ coverage_pct（且該族群候選 ≥ min_members），
    ＝機械/輪動足跡（全族群同掛，非個股利空）——標「族群共振」提醒別當個股排除理由（診斷 §5
    金融 W21 土洋對作 8/17、起漲 4 檔 100% 掛旗的假誤殺實例）。次產業身分取自 themes_long
    kind=次產業；無 themes_long／該股無次產業／族群過小 → 不標（如實留空）。純揭露非 gate。
    """
    from tw_screener.analysis.concepts import SUB_INDUSTRY_KIND

    if not rows or themes_long is None or themes_long.is_empty():
        return
    member_ids = {row["stock_id"] for row in rows}
    # 每檔的次產業身分（一檔多次產業取第一筆；多為單一身分）
    sub_of: dict[str, str] = {}
    for tr in themes_long.filter(pl.col("kind") == SUB_INDUSTRY_KIND).iter_rows(named=True):
        sid = str(tr["stock_id"])
        if sid in member_ids and sid not in sub_of:
            sub_of[sid] = tr["theme"]
    if not sub_of:
        return
    # 各次產業候選數與各旗標掛旗數
    members_in_sub: dict[str, int] = {}
    flagged_in_sub: dict[tuple[str, str], int] = {}
    for row in rows:
        sub = sub_of.get(row["stock_id"])
        if sub is None:
            continue
        members_in_sub[sub] = members_in_sub.get(sub, 0) + 1
        row_flags = set((row.get("flags") or "").split(";"))
        for flag in flags_watched:
            if flag in row_flags:
                flagged_in_sub[(sub, flag)] = flagged_in_sub.get((sub, flag), 0) + 1
    # 回填：該股掛的旗標若在其族群覆蓋度達標 → 註記「族群共振」＋覆蓋%
    for row in rows:
        sub = sub_of.get(row["stock_id"])
        if sub is None or members_in_sub.get(sub, 0) < min_members:
            continue
        row_flags = set((row.get("flags") or "").split(";"))
        notes: list[str] = []
        for flag in flags_watched:
            if flag not in row_flags:
                continue
            cov = flagged_in_sub.get((sub, flag), 0) / members_in_sub[sub] * 100
            if cov >= coverage_pct:
                notes.append(f"{flag}(族群共振{cov:.0f}%)")
        if notes:
            row["sector_flag_note"] = ";".join(notes)


def _build_enriched_rows(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
    fundamentals_map: dict | None = None,
    valuation_map: dict | None = None,
    big_holder_map: dict | None = None,
    margin_map: dict | None = None,
    near_flow_cfg: dict | None = None,
) -> list[dict]:
    """組「每檔 × 技術/籌碼/估值/基本面 + flags」列（candidates / 庫存 / 觀察 共用）。

    near_flow_cfg（F5）：近端籌碼揭露欄門檻（settings.near_flow）；None＝欄位仍輸出、
    用預設門檻。flow_state/near_share_5d_pct/risk_kind 為**純揭露非 gate**（§1.4）。
    """
    if members.is_empty():
        return []
    from tw_screener.analysis.grouping import classify_risk_kind, near_flow_state, rank_themes

    themes_long = themes_long if themes_long is not None else pl.DataFrame()
    ranked = rank_themes(members, themes_long)
    theme_map = _build_theme_str_map(members, themes_long, ranked)
    strategy_ids = sorted(screener_results.keys())

    # 估值（PE/PB）：官方 BWIBBU（valuation_map）為主、Goodinfo screener CSV 兜底（官方缺才用）。
    # Goodinfo 值仍從 screener CSV 收進 pe_map/pb_map 當 fallback。
    pe_map: dict[str, object] = {}
    pb_map: dict[str, object] = {}
    vol_map: dict[str, object] = {}  # 今日成交張，用來算法人集中度
    close_map: dict[str, object] = {}  # 收盤價，用來還原 MA20/MA60 絕對價
    for df in screener_results.values():
        if df.is_empty():
            continue
        cols = df.columns
        for rr in df.iter_rows(named=True):
            sid = str(rr["stock_id"])
            if "pe_ratio" in cols and sid not in pe_map:
                pe_map[sid] = rr.get("pe_ratio")
            if "pb_ratio" in cols and sid not in pb_map:
                pb_map[sid] = rr.get("pb_ratio")
            if "volume_lots" in cols and sid not in vol_map:
                vol_map[sid] = rr.get("volume_lots")
            if "close" in cols and sid not in close_map:
                close_map[sid] = rr.get("close")

    fc = flags_cfg or {}
    overheated = float(fc.get("overheated_ma60_pct", 40))
    low_liq = float(fc.get("low_liquidity_amount", 100))
    high_pe = float(fc.get("high_pe", 50))
    cross_lots = float(fc.get("cross_trade_lots", 5000)) * 1000.0  # 張 → 股
    cross_rel_pct = float(fc.get("cross_trade_rel_pct", 4))  # 弱邊張數須達近 20 日總量此% 才算對作
    rally = float(fc.get("strong_rally_pct", 15))
    strong_leader_yoy = float(fc.get("strong_leader_yoy_pct", 20))
    # M-WS5a 揭露欄門檻（WS5-① 貼底、WS5-② sector-wide 旗標覆蓋度）；純揭露非 gate。
    base_zone_max = float(fc.get("base_zone_ma60_max_pct", 10))
    sector_cov_pct = float(fc.get("sector_coverage_pct", 60))
    sector_cov_min = int(fc.get("sector_coverage_min_members", 5))
    nf = near_flow_cfg or {}
    nf_min_shares = float(nf.get("min_lots", 1000)) * 1000.0  # 張 → 股
    nf_stall = float(nf.get("stall_share_pct", 5))
    nf_accel = float(nf.get("accel_share_pct", 40))
    nf_ext = float(nf.get("risk_ext_ma60_pct", 15))
    nf_down = float(nf.get("risk_down_5d_pct", -5))

    def _num(v: object, nd: int = 1) -> float | None:
        if v is None or (isinstance(v, float) and v != v):
            return None
        return round(float(v), nd)  # type: ignore[arg-type]

    def _lots(v: object) -> float | None:
        n = _num(v, 0)
        return round(n / 1000.0) if n is not None else None

    def _lvl(close: float | None, dist: float | None) -> float | None:
        """由收盤價與距離% 還原均線絕對價。"""
        if close is None or dist is None:
            return None
        denom = 1 + dist / 100.0
        return round(close / denom, 2) if denom > 0 else None

    rows: list[dict] = []
    sort_col = "momentum_5d" if "momentum_5d" in members.columns else "stock_id"
    for r in members.sort(sort_col, descending=True).iter_rows(named=True):
        sid = str(r["stock_id"])
        vr = _num(r.get("vol_ratio"), 2)
        mom = _num(r.get("momentum_5d"), 2)
        m60 = _num(r.get("ma60_dist_pct"), 1)
        m20 = _num(r.get("ma20_dist_pct"), 1)
        close = _num(close_map.get(sid), 2)
        ma20_price = _lvl(close, m20)
        ma60_price = _lvl(close, m60)
        # M-修法7（7a）進場區間絕對價：T3 結構價（前波低 low_60d）＋回檔深度檢核（距區間低/高）
        low_20d = _num(r.get("low_20d"), 2)
        high_20d = _num(r.get("high_20d"), 2)
        low_60d = _num(r.get("low_60d"), 2)
        high_60d = _num(r.get("high_60d"), 2)
        ryoy = _num(rev_yoy_map.get(sid), 1) if rev_yoy_map else None
        fund = fundamentals_map.get(sid) if fundamentals_map else None
        gross_margin = _num(fund.get("gross_margin_pct"), 1) if fund else None
        eps_q = _num(fund.get("eps"), 2) if fund else None
        # D5 體質：稅後純益率＋負債比＋單季ROE（一般業；金融業/缺表者 null）
        net_margin = _num(fund.get("net_margin_pct"), 1) if fund else None
        debt_ratio = _num(fund.get("debt_ratio_pct"), 1) if fund else None
        roe_q = _num(fund.get("roe_q_pct"), 2) if fund else None
        amt = _num(r.get("amount_million"), 0)
        # 估值：官方 BWIBBU 為主、Goodinfo 兜底（官方缺才用爬來值）；殖利率僅官方有
        vrow = valuation_map.get(sid) if valuation_map else None
        off_pe = vrow.get("pe") if vrow else None
        off_pb = vrow.get("pbr") if vrow else None
        pe = _num(off_pe if off_pe is not None else pe_map.get(sid), 1)
        pb = _num(off_pb if off_pb is not None else pb_map.get(sid), 2)
        dy = _num(vrow.get("dividend_yield"), 2) if vrow else None
        # 次產業相對位階（官方 build_valuation；PE 主、PB 補虧損股）：每檔候選 inline 帶相對便宜
        val_metric = (vrow.get("val_metric") or "") if vrow else ""
        val_pctile = _num(vrow.get("val_pctile"), 0) if vrow else None
        cheap_flag = (vrow.get("cheap_flag") or "") if vrow else ""
        fn = _num(r.get("foreign_net"), 0)
        tn = _num(r.get("trust_net"), 0)
        instn = _num(r.get("inst_net"), 0)
        inst_lots = _lots(r.get("inst_net"))
        foreign_lots = _lots(r.get("foreign_net"))
        trust_lots = _lots(r.get("trust_net"))
        # 修法6（6a）+ 分析層補窗：三大法人/外資/投信近端窗，揭露 20 日累計蓋住的近 5/10 日轉向
        foreign_5d_lots = _lots(r.get("foreign_net_5d"))
        foreign_10d_lots = _lots(r.get("foreign_net_10d"))
        trust_5d_lots = _lots(r.get("trust_net_5d"))
        trust_10d_lots = _lots(r.get("trust_net_10d"))
        inst_5d_lots = _lots(r.get("inst_net_5d"))
        inst_10d_lots = _lots(r.get("inst_net_10d"))
        # 修法6（6b）近 10 日報酬：趨勢鏡頭，區分健康回踩 vs 下跌反彈
        ret_10d = _num(r.get("ret_10d"), 2)
        # D3 集保大戶持股比（占集保庫存≈流通量）＋WoW；TDCC 獨立來源，不受 inst_missing 影響
        bh = (big_holder_map or {}).get(sid)
        big_holder_pct = _num(bh.get("big_holder_pct"), 2) if bh else None
        big_holder_1000_pct = _num(bh.get("big_holder_1000_pct"), 2) if bh else None
        big_holder_wow = _num(bh.get("big_holder_wow"), 2) if bh else None
        big_holder_1000_wow = _num(bh.get("big_holder_1000_wow"), 2) if bh else None
        vlots = _num(vol_map.get(sid), 0)
        # 近 20 日總成交量（張）：20日均量=今日量/vol_ratio，×20。供集中度與土洋對作相對門檻共用
        tot20 = (vlots / vr) * 20.0 if (vlots and vr and vr > 0) else None
        # 法人淨買超佔近 20 日成交量%（集中度）
        inst_pct20d = None
        if inst_lots is not None and tot20 and tot20 > 0:
            inst_pct20d = round(inst_lots / tot20 * 100, 1)

        # D4 上市融資融券（張）：散戶槓桿動向；MI_MARGN 獨立來源，不受 inst_missing 影響
        mg = (margin_map or {}).get(sid)
        margin_balance_lots = _num(mg.get("margin_balance"), 0) if mg else None
        margin_chg_lots = _num(mg.get("margin_chg"), 0) if mg else None
        margin_chg_5d_lots = _num(mg.get("margin_chg_5d"), 0) if mg else None
        short_balance_lots = _num(mg.get("short_balance"), 0) if mg else None
        short_chg_lots = _num(mg.get("short_chg"), 0) if mg else None
        # 融資餘額相當於幾日均量（融資沉澱/籌碼壓力 proxy）；20 日均量＝tot20/20
        margin_to_vol = (
            round(margin_balance_lots / (tot20 / 20.0), 1)
            if (margin_balance_lots is not None and tot20 and tot20 > 0)
            else None
        )

        # 法人快取缺漏（join 不到，非真實零買賣超）：四欄顯示空白，由 flag 標示供人工查證
        inst_missing = bool(r.get("inst_missing"))
        if inst_missing:
            inst_lots = inst_pct20d = foreign_lots = trust_lots = None
            foreign_5d_lots = foreign_10d_lots = None
            trust_5d_lots = trust_10d_lots = inst_5d_lots = inst_10d_lots = None

        # F5（沿舊 06 NF1）：近端籌碼狀態＋三風險分類（純揭露非 gate，§1.4 佔比單獨無判別力）
        if inst_missing:
            flow_state, near_share = None, None
        else:
            flow_state, near_share = near_flow_state(
                fn, _num(r.get("foreign_net_5d"), 0), tn, _num(r.get("trust_net_5d"), 0),
                min_shares=nf_min_shares, stall_share_pct=nf_stall, accel_share_pct=nf_accel,
            )
        risk_kind = classify_risk_kind(
            flow_state, m60, m20, mom, ext_ma60_pct=nf_ext, down_5d_pct=nf_down
        )

        flags: list[str] = []
        if m60 is not None and m60 > overheated:
            # 距季線高：區分「強勢領頭（順勢分批，不預設踢核心）」與「過熱（追高風險）」。
            # 起漲領頭羊天生距季線遠，若外資投信同向買 + 營收 YoY 達標，不該與投信獨拉的
            # 小型過熱股一視同仁判死（見 docs/11 排雷段「強勢領頭」例外與買強勢 ladder）。
            strong_leader = (
                fn is not None and fn > 0
                and tn is not None and tn > 0
                and ryoy is not None and ryoy >= strong_leader_yoy
            )
            flags.append("強勢領頭" if strong_leader else "過熱")
        if amt is not None and amt < low_liq:
            flags.append("低流動")
        if pe is not None and pe > high_pe:
            flags.append("高PE")
        if fn is not None and tn is not None and fn * tn < 0 and min(abs(fn), abs(tn)) > cross_lots:
            # 修法4：弱邊張數須達近 20 日總量的相對門檻才算對作——濾掉權值股小量反向誤判
            # （如台積電投信 7,826 張對其流通量＝雜訊級）。量資料缺則退回絕對判定、不漏標。
            weak_lots = min(abs(fn), abs(tn)) / 1000.0
            rel_ok = (not tot20) or tot20 <= 0 or (weak_lots / tot20 * 100 >= cross_rel_pct)
            if rel_ok:
                flags.append("土洋對作")
        if mom is not None and mom > rally and instn is not None and instn < 0:
            flags.append("強漲法人賣")
        if inst_missing:
            flags.append("法人缺漏")
        # M-WS5a（WS5-①）貼底揭露：距季線 ≤ 門檻＝起漲 base 位階（過熱旗標的對稱面，
        # 別被延伸股埋掉）。純揭露非 gate、非 flags（flags 是排雷／PO4 偽陰性帳，貼底是
        # 正向訊號故獨立欄）；深破線由 risk_kind/pullback_quality 另揭露。docs/20 §WS5-①。
        base_zone = "貼底" if (m60 is not None and m60 <= base_zone_max) else ""

        # 除息還原：5 日視窗內現金股利已加回 momentum_5d（修假負）；標旗供人工查證
        ex_div_cash = _num(r.get("ex_div_cash"), 2)
        div_addback_pct = _num(r.get("div_addback_pct"), 2)
        if ex_div_cash is not None and ex_div_cash > 0:
            flags.append(f"除息還原{ex_div_cash}元")

        rows.append(
            {
                "stock_id": sid,
                "name": r.get("name", ""),
                "industry": r.get("industry_name", ""),
                "theme": theme_map.get(sid, ""),
                "strategy": _strategy_str(r, strategy_ids),
                "rank_in_group": int(r.get("rank_in_group", 0) or 0),
                "momentum_5d_pct": mom,  # 已含除息還原（見 div_addback_pct）
                "ret_10d_pct": ret_10d,  # 近10日報酬(除息還原)：≥−3%＝健康回踩、<−5%＝下跌反彈
                "ex_div_cash": ex_div_cash,            # 5 日內現金股利合計（元），無＝空
                "div_addback_pct": div_addback_pct,    # 還原加回 momentum 的百分點
                "change_pct": _num(r.get("change_pct"), 2),
                "close": close,
                "vol_ratio": vr if (vr or 0) > 0 else None,
                "ma20_dist_pct": m20,
                "ma60_dist_pct": m60,
                "ma20_price": ma20_price,
                "ma60_price": ma60_price,
                "low_20d": low_20d,    # 近20日收盤低（區間下緣）：回檔深度檢核
                "high_20d": high_20d,  # 近20日收盤高（區間上緣）
                "low_60d": low_60d,    # 近60日收盤低：進場階梯 T3 結構價（前波低）
                "high_60d": high_60d,  # 近60日收盤高
                "amount_million": amt,
                "pe_ratio": pe,
                "pb_ratio": pb,
                "dividend_yield_pct": dy,  # 官方殖利率（BWIBBU/peratio）；Goodinfo 無此欄
                "val_metric": val_metric,  # 相對位階用 PE 或 PB（虧損股退 PB）
                "val_pctile": val_pctile,  # 次產業升冪百分位（0=同業最便宜；官方 trailing 橫斷面）
                "cheap_flag": cheap_flag,  # 相對便宜 / 相對便宜(PB) / 空（同儕不足）
                "rev_yoy_pct": ryoy,
                "gross_margin_pct": gross_margin,  # 最新單季毛利率（TWSE/TPEX OpenAPI）
                "net_margin_pct": net_margin,      # 最新單季稅後純益率（%，D5 體質）
                "eps_q": eps_q,                    # 最新單季 EPS（元）
                "debt_ratio_pct": debt_ratio,      # 負債比＝負債/資產（%，一般業；金融業空）
                "roe_q_pct": roe_q,                # 單季ROE＝EPS/每股淨值（%，歸屬母公司）
                "volume_lots_today": vlots,
                "inst_net_lots": inst_lots,
                "inst_net_5d_lots": inst_5d_lots,    # 三大法人近5日：揭露近端轉向(20日恐為殘留)
                "inst_net_10d_lots": inst_10d_lots,  # 三大法人近10日
                "inst_pct20d": inst_pct20d,
                "foreign_net_lots": foreign_lots,
                "foreign_net_5d_lots": foreign_5d_lots,   # 外資近5日：揭露近端轉向(20日恐為殘留)
                "foreign_net_10d_lots": foreign_10d_lots,  # 外資近10日
                "trust_net_lots": trust_lots,
                "trust_net_5d_lots": trust_5d_lots,    # 投信近5日
                "trust_net_10d_lots": trust_10d_lots,  # 投信近10日
                # D3 集保大戶（占集保庫存≈流通量）：≥400張(級距12-15)、≥1000張(千張大戶,級距15)＋WoW
                "big_holder_pct": big_holder_pct,
                "big_holder_wow": big_holder_wow,
                "big_holder_1000_pct": big_holder_1000_pct,
                "big_holder_1000_wow": big_holder_1000_wow,
                # D4 上市融資融券（張）：融資餘額/增減/近5日增減、融券餘額/增減、融資相當幾日均量
                "margin_balance_lots": margin_balance_lots,
                "margin_chg_lots": margin_chg_lots,
                "margin_chg_5d_lots": margin_chg_5d_lots,
                "short_balance_lots": short_balance_lots,
                "short_chg_lots": short_chg_lots,
                "margin_to_vol": margin_to_vol,
                # F5 揭露欄（沿舊 06 NF1＋07 TR1）：近端籌碼狀態＋回踩品質軌跡——
                # 純揭露非 gate（§1.4 近端佔比單獨無判別力）；軌跡欄缺歷史＝null 不臆造
                "flow_state": flow_state,          # 轉賣/熄火/加速/平穩(主體)；null＝無大額買超邊
                "near_share_5d_pct": near_share,   # 近5日佔20日累計%（台新新光金 2% 型）
                "risk_kind": risk_kind,            # 價格已跌＞籌碼熄火＞價格延伸（三種動作不同）
                "down_days_streak": r.get("down_days_streak"),
                "pullback_vol_ratio": _num(r.get("pullback_vol_ratio"), 2),
                "above_ma20_days": r.get("above_ma20_days"),
                "pullback_quality": r.get("pullback_quality"),  # 止穩/觀察/破線（啟發式輔助）
                "flags": ";".join(flags),
                # M-WS5a 揭露欄（純揭露非 gate、獨立於 flags 排雷欄）
                "base_zone": base_zone,          # WS5-①：貼底（距季線 ≤ 門檻＝起漲 base 位階）／空
                "sector_flag_note": "",          # WS5-②：sector-wide 旗標覆蓋度註記（post-pass 填）
                "goodinfo_url": str(r.get("goodinfo_url", "")),
            }
        )

    # M-WS5a（WS5-②）sector-wide 旗標降輪動：同旗標覆蓋整族＝機械/輪動足跡（金融 W21 土洋對作
    # 8/17 掛旗、起漲 4 檔 100% 掛＝假土洋對作誤殺）。標「族群共振」提醒分析師別當個股排除理由。
    _annotate_sector_flag_coverage(
        rows, themes_long, coverage_pct=sector_cov_pct, min_members=sector_cov_min
    )
    return rows


def write_candidates_enriched_csv(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    path: Path,
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
    fundamentals_map: dict | None = None,
    valuation_map: dict | None = None,
    big_holder_map: dict | None = None,
    margin_map: dict | None = None,
    near_flow_cfg: dict | None = None,
) -> list[dict]:
    """輸出「全候選股 × 技術/籌碼/估值/基本面 + flags 排雷欄」CSV，供 ProPicks 全宇宙挑股。

    補 group_analysis.md 只列部分股的盲點：每檔都有 5 日漲幅/距月線/距季線/量比/法人(張)拆分/
    PE/PB/主題，並程式預算 flags（過熱/強勢領頭/低流動/高PE/土洋對作/強漲法人賣/法人缺漏）讓 AI
    快速排雷、把腦力留給判斷（「強勢領頭」＝距季線高但籌碼+基本面確認的例外，非排雷理由）。
    缺值（無快取）寫空白 → 標「需查證」而非編造。回傳已建立的列（list[dict]，
    供庫存/觀察清單重用同一筆來源值以保持跨 CSV 一致）。
    """
    rows = _build_enriched_rows(
        members, themes_long, screener_results, flags_cfg, rev_yoy_map,
        fundamentals_map, valuation_map, big_holder_map, margin_map,
        near_flow_cfg=near_flow_cfg,
    )
    if not rows:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path)
    logger.info("candidates_enriched.csv 輸出 → {}（{} 檔）", path, len(rows))
    return rows


# 重疊股（同時在 candidates 宇宙與庫存/觀察）一律重用 candidates 那筆，避免兩條 enrich 路徑
# 來源/視窗不同造成同一檔量比/集中度/成交額分岔。只重用市場/籌碼/估值/flags，識別欄與策略欄保留。
_CANONICAL_REUSE_FIELDS = (
    "momentum_5d_pct",
    "ret_10d_pct",
    "ex_div_cash",
    "div_addback_pct",
    "change_pct",
    "vol_ratio",
    "ma20_dist_pct",
    "ma60_dist_pct",
    "ma20_price",
    "ma60_price",
    "low_20d",
    "high_20d",
    "low_60d",
    "high_60d",
    "amount_million",
    "pe_ratio",
    "pb_ratio",
    "dividend_yield_pct",
    "val_metric",
    "val_pctile",
    "cheap_flag",
    "rev_yoy_pct",
    "gross_margin_pct",
    "net_margin_pct",
    "eps_q",
    "debt_ratio_pct",
    "roe_q_pct",
    "volume_lots_today",
    "inst_net_lots",
    "inst_net_5d_lots",
    "inst_net_10d_lots",
    "inst_pct20d",
    "foreign_net_lots",
    "foreign_net_5d_lots",
    "foreign_net_10d_lots",
    "trust_net_lots",
    "trust_net_5d_lots",
    "trust_net_10d_lots",
    "big_holder_pct",
    "big_holder_wow",
    "big_holder_1000_pct",
    "big_holder_1000_wow",
    "margin_balance_lots",
    "margin_chg_lots",
    "margin_chg_5d_lots",
    "short_balance_lots",
    "short_chg_lots",
    "margin_to_vol",
    "flags",
    "base_zone",         # M-WS5a：跟隨 canonical 的 ma60_dist，重疊股一致
    "sector_flag_note",  # M-WS5a：覆蓋度屬候選宇宙口徑，庫存/觀察重疊股沿用 candidates
)


def write_named_list_csv(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    path: Path,
    *,
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
    fundamentals_map: dict | None = None,
    valuation_map: dict | None = None,
    big_holder_map: dict | None = None,
    margin_map: dict | None = None,
    holdings_map: dict | None = None,
    canonical_rows: dict[str, dict] | None = None,
    near_flow_cfg: dict | None = None,
) -> int:
    """輸出庫存/觀察清單 enriched CSV（同 candidates 欄位）。

    holdings_map={stock_id: {"buy_price": x, "shares": y}} 時，每列加 買入價/報酬率%/現值(千)，
    供「續抱/加碼/減碼/停利/停損」決策對照。
    canonical_rows={stock_id: candidates_row} 時，重疊股重用 candidates 的市場/籌碼/估值/flags
    欄位，使三份 CSV 對同一檔股票數字一致。回傳寫入檔數。
    """
    rows = _build_enriched_rows(
        members, themes_long, screener_results, flags_cfg, rev_yoy_map,
        fundamentals_map, valuation_map, big_holder_map, margin_map,
        near_flow_cfg=near_flow_cfg,
    )
    if not rows:
        return 0
    if canonical_rows:
        for row in rows:
            canon = canonical_rows.get(row["stock_id"])
            if not canon:
                continue
            for field in _CANONICAL_REUSE_FIELDS:
                if field in canon:
                    row[field] = canon.get(field)
            # close 取 canonical（None 時保留 named 值，避免清掉持股 return_pct 依據）
            if canon.get("close") is not None:
                row["close"] = canon.get("close")
    if holdings_map is not None:
        for row in rows:
            h = holdings_map.get(row["stock_id"]) or {}
            buy = h.get("buy_price")
            shares = h.get("shares")
            close = row.get("close")
            row["buy_price"] = buy
            row["return_pct"] = (
                round((close - buy) / buy * 100, 1) if (buy and close and buy > 0) else None
            )
            row["market_value_k"] = round(close * shares / 1000) if (close and shares) else None
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path)
    logger.info("{} 輸出 → {}（{} 檔）", path.name, path, len(rows))
    return len(rows)
