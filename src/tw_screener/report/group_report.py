"""族群分析報告產生器：把 groups / leaders DataFrame 渲染成 group_analysis.md。"""

from __future__ import annotations

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
}


def _fmt_pct(v: float) -> str:
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def _strategy_str(row: dict, strategy_ids: list[str]) -> str:
    labels = [
        _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids if row.get(f"in_{sid}")
    ]
    return "+".join(labels) if labels else "-"


def _build_context(
    groups: pl.DataFrame,
    leaders: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    top_groups: int,
    top_stocks: int,
) -> dict:
    strategy_ids = sorted(screener_results.keys())

    # --- summary ---
    counts: dict[str, int] = {sid: len(df) for sid, df in screener_results.items()}
    total_union = len(leaders) if not leaders.is_empty() else 0

    # Compute intersections from in_{sid} boolean columns in leaders
    intersections: dict[str, list[str]] = {}
    if not leaders.is_empty():
        for i, sid_a in enumerate(strategy_ids):
            for sid_b in strategy_ids[i + 1 :]:
                key = f"{_STRATEGY_LABEL.get(sid_a, sid_a)}∩{_STRATEGY_LABEL.get(sid_b, sid_b)}"
                mask = pl.col(f"in_{sid_a}") & pl.col(f"in_{sid_b}")
                intersections[key] = leaders.filter(mask)["stock_id"].to_list()

        if len(strategy_ids) >= 3:
            mask_all = pl.lit(True)
            for sid in strategy_ids:
                mask_all = mask_all & pl.col(f"in_{sid}")
            key_all = "∩".join(
                _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids
            )
            intersections[key_all] = leaders.filter(mask_all)["stock_id"].to_list()

    summary = {
        "counts": counts,
        "total_union": total_union,
        "intersections": intersections,
    }

    # --- groups table ---
    top_groups_df = groups.head(top_groups)
    group_list: list[dict] = []

    for rank, row in enumerate(top_groups_df.iter_rows(named=True), start=1):
        industry_code = row["industry_code"]
        members_count = int(row["members_count"])
        total_in = int(row.get("total_in_industry", members_count))
        entry_rate_pct = float(row["entry_rate"]) * 100
        rs_avg = float(row["rs_avg"])
        score = float(row["score"])

        counts_per_sid = {sid: int(row.get(f"count_{sid}", 0)) for sid in strategy_ids}

        # Collect stocks in this group
        if not leaders.is_empty():
            group_stocks_df = leaders.filter(pl.col("industry_code") == industry_code).sort(
                "leader_score", descending=True
            )
        else:
            group_stocks_df = pl.DataFrame()

        # Within-group ranks for display
        stock_list: list[dict] = []
        n = len(group_stocks_df)
        for i, srow in enumerate(group_stocks_df.iter_rows(named=True), start=1):
            stock_list.append(
                {
                    "stock_id": srow["stock_id"],
                    "name": srow["name"],
                    "strategy_str": _strategy_str(srow, strategy_ids),
                    "strategy_count": int(srow.get("strategy_count", 1)),
                    "change_pct": float(srow.get("change_pct", 0) or 0),
                    "change_pct_str": _fmt_pct(float(srow.get("change_pct", 0) or 0)),
                    "amount_million": float(srow.get("amount_million", 0) or 0),
                    "amount_rank_in_group": int(srow.get("amount_rank", i)),
                    "rs_rank_in_group": int(srow.get("rs_rank", i)),
                    "leader_score": float(srow.get("leader_score", 0)),
                    "is_leader": bool(srow.get("is_leader", False)),
                    "goodinfo_url": str(srow.get("goodinfo_url", "")),
                    "group_size": n,
                }
            )

        group_list.append(
            {
                "rank": rank,
                "industry_name": row["industry_name"],
                "industry_code": industry_code,
                "counts": counts_per_sid,
                "members_count": members_count,
                "total_in_industry": total_in,
                "entry_rate_pct_str": f"{entry_rate_pct:.1f}%",
                "rs_avg_str": _fmt_pct(rs_avg),
                "score_str": f"{score:.1f}",
                "stocks": stock_list,
            }
        )

    # --- priority stocks ---
    if not leaders.is_empty() and not groups.is_empty():
        # Build a score map: group_rank → group_score (0-10) → normalised to 0-1
        max_score = float(groups["score"].max() or 1.0)
        group_score_map = {
            int(r["industry_code"] if False else g["industry_code"]): float(g["score"]) / max_score
            for g in [dict(zip(groups.columns, row)) for row in groups.iter_rows()]
            for r in [g]  # just alias
        }
        # Simpler: dict comprehension directly
        group_score_map = {}
        for g_row in groups.iter_rows(named=True):
            group_score_map[g_row["industry_code"]] = float(g_row["score"]) / max_score

        group_rank_map = {
            g_row["industry_code"]: rank + 1
            for rank, g_row in enumerate(top_groups_df.iter_rows(named=True))
        }

        priority_rows = []
        for srow in leaders.iter_rows(named=True):
            ind_code = srow["industry_code"]
            g_score_norm = group_score_map.get(ind_code, 0.0)
            is_leader = bool(srow.get("is_leader", False))
            strategy_count = int(srow.get("strategy_count", 1))

            p_score = (
                g_score_norm * 0.6
                + (1.0 if is_leader else 0.5) * 0.3
                + (1.0 if strategy_count >= 2 else 0.0) * 0.1
            )
            priority_rows.append(
                {
                    "stock_id": srow["stock_id"],
                    "name": srow["name"],
                    "industry_name": srow["industry_name"],
                    "group_rank": group_rank_map.get(ind_code, 99),
                    "is_leader": is_leader,
                    "strategy_count": strategy_count,
                    "strategy_str": _strategy_str(srow, strategy_ids),
                    "priority_score": p_score,
                    "goodinfo_url": str(srow.get("goodinfo_url", "")),
                }
            )

        priority_rows.sort(key=lambda x: x["priority_score"], reverse=True)
        # Filter to stocks in top_groups and take top_stocks
        priority_rows = [r for r in priority_rows if r["group_rank"] <= top_groups]
        priority_rows = priority_rows[:top_stocks]
        for i, r in enumerate(priority_rows, start=1):
            r["rank"] = i
    else:
        priority_rows = []

    return {
        "week_tag": week_tag,
        "generated_at": date.today().isoformat(),
        "strategy_ids": strategy_ids,
        "strategy_labels": {sid: _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids},
        "summary": summary,
        "groups": group_list,
        "top_groups": top_groups,
        "priority_stocks": priority_rows,
    }


def render_group_report(
    groups: pl.DataFrame,
    leaders: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    output_path: Path,
    top_groups: int = 10,
    top_stocks: int = 10,
) -> None:
    """Render group_analysis.md to output_path using Jinja2 template."""
    context = _build_context(groups, leaders, screener_results, week_tag, top_groups, top_stocks)

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
