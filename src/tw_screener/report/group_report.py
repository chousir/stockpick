"""族群分析報告產生器：把 groups / members DataFrame 渲染成 group_analysis.md。"""

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
    "c_low_base_growth": "C",
    "c_quality_value": "C",
}

_STRATEGY_NAME: dict[str, str] = {
    "a_breakout": "動能突破",
    "b_growth_institutional": "成長主力",
    "c_dividend_steady": "穩健存股",
    "c_low_base_growth": "低基期成長",
    "c_quality_value": "品質價值",
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
}

_UNCATEGORIZED = "未分類"
_TOP_PER_GROUP = 3


def _fmt_pct(v: float) -> str:
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def _strategy_str(row: dict, strategy_ids: list[str]) -> str:
    labels = [
        _STRATEGY_LABEL.get(sid, sid[0].upper()) for sid in strategy_ids if row.get(f"in_{sid}")
    ]
    return "+".join(labels) if labels else "-"


def _build_stock_dict(srow: dict, strategy_ids: list[str], group_size: int) -> dict:
    momentum_5d = float(srow.get("momentum_5d", srow.get("rs", 0)) or 0)
    days_used = int(srow.get("momentum_days_used", 0) or 0)
    rank_in_group = int(srow.get("rank_in_group", 0) or 0)
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
        "goodinfo_url": str(srow.get("goodinfo_url", "")),
        "group_size": group_size,
    }


def _build_context(
    groups: pl.DataFrame,
    members: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    top_groups: int,
    top_stocks: int,
) -> dict:
    strategy_ids = sorted(screener_results.keys())
    # Only show strategies that have at least 1 result
    active_strategy_ids = [sid for sid in strategy_ids if len(screener_results.get(sid, [])) > 0]

    # --- summary ---
    counts: dict[str, int] = {sid: len(df) for sid, df in screener_results.items()}
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

    # --- groups table (top N) ---
    top_groups_df = groups.head(top_groups)
    group_list: list[dict] = []

    for rank, row in enumerate(top_groups_df.iter_rows(named=True), start=1):
        industry_code = row["industry_code"]
        members_count = int(row["members_count"])
        total_in = int(row.get("total_in_industry", members_count))

        counts_per_sid = {sid: int(row.get(f"count_{sid}", 0)) for sid in strategy_ids}

        if not members.is_empty():
            group_stocks_df = members.filter(pl.col("industry_code") == industry_code).sort(
                "leader_score", descending=True
            )
        else:
            group_stocks_df = pl.DataFrame()

        n = len(group_stocks_df)
        all_stocks = [
            _build_stock_dict(srow, strategy_ids, n)
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

    # --- priority stocks (skip 未分類) ---
    if not members.is_empty() and not groups.is_empty():
        max_score = float(groups["score"].max() or 1.0)
        group_score_map: dict[str, float] = {}
        for g_row in groups.iter_rows(named=True):
            group_score_map[g_row["industry_code"]] = float(g_row["score"]) / max_score

        group_rank_map = {
            g_row["industry_code"]: rank + 1
            for rank, g_row in enumerate(top_groups_df.iter_rows(named=True))
        }

        priority_rows = []
        for srow in members.iter_rows(named=True):
            ind_code = srow["industry_code"]
            ind_name = srow.get("industry_name", _UNCATEGORIZED)
            # Skip 未分類 from priority recommendations
            if ind_name == _UNCATEGORIZED:
                continue
            g_score_norm = group_score_map.get(ind_code, 0.0)
            rank_in_group = int(srow.get("rank_in_group", 99) or 99)
            strategy_count = int(srow.get("strategy_count", 1))

            # 個股加分：族群內排名第 1 → 1.0；第 2 → 0.7；第 3 → 0.5；其餘 → 0.3
            rank_bonus_map = {1: 1.0, 2: 0.7, 3: 0.5}
            rank_bonus = rank_bonus_map.get(rank_in_group, 0.3)

            p_score = (
                g_score_norm * 0.6
                + rank_bonus * 0.3
                + (1.0 if strategy_count >= 2 else 0.0) * 0.1
            )
            priority_rows.append(
                {
                    "stock_id": srow["stock_id"],
                    "name": srow["name"],
                    "industry_name": ind_name,
                    "group_rank": group_rank_map.get(ind_code, 99),
                    "rank_in_group": rank_in_group,
                    "strategy_count": strategy_count,
                    "strategy_str": _strategy_str(srow, strategy_ids),
                    "priority_score": p_score,
                    "goodinfo_url": str(srow.get("goodinfo_url", "")),
                }
            )

        priority_rows.sort(key=lambda x: x["priority_score"], reverse=True)
        priority_rows = [r for r in priority_rows if r["group_rank"] <= top_groups]
        priority_rows = priority_rows[:top_stocks]
        for i, r in enumerate(priority_rows, start=1):
            r["rank"] = i
    else:
        priority_rows = []

    # --- Claude analysis section: top 4 categorised groups ---
    claude_groups = [g for g in group_list if not g["is_uncategorized"]][:4]

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
        "groups": group_list,
        "top_groups": top_groups,
        "priority_stocks": priority_rows,
        "claude_groups": claude_groups,
    }


def render_group_report(
    groups: pl.DataFrame,
    members: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    output_path: Path,
    top_groups: int = 10,
    top_stocks: int = 10,
) -> None:
    """Render group_analysis.md to output_path using Jinja2 template.

    members: rank_within_groups 的回傳；含 rank_in_group / leader_score / momentum_5d。
    """
    context = _build_context(groups, members, screener_results, week_tag, top_groups, top_stocks)

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
