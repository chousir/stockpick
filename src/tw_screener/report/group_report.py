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


def _build_context(
    groups: pl.DataFrame,
    members: pl.DataFrame,
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    top_groups: int,
    top_stocks: int,
    dividend_events: pl.DataFrame | None = None,
    themes_long: pl.DataFrame | None = None,
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
    ranked = rank_themes(members, themes_long)
    theme_str_map = _build_theme_str_map(members, themes_long, ranked)
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
        "priority_stocks": priority_rows,
        "claude_groups": claude_groups,
        "dividend_rows": dividend_rows,
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
) -> None:
    """Render group_analysis.md to output_path using Jinja2 template.

    members: rank_within_groups 的回傳；含 rank_in_group / leader_score / momentum_5d。
    dividend_events: 候選股未來窗內除權息（filter_dividend_calendar 的回傳）；None/空則不渲染該段。
    themes_long: load_themes() 的 (stock_id, theme, kind) long table；None/空則主題排名留空。
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
    )

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


def _build_enriched_rows(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
) -> list[dict]:
    """組「每檔 × 技術/籌碼/估值/基本面 + flags」列（candidates / 庫存 / 觀察 共用）。"""
    if members.is_empty():
        return []
    from tw_screener.analysis.grouping import rank_themes

    themes_long = themes_long if themes_long is not None else pl.DataFrame()
    ranked = rank_themes(members, themes_long)
    theme_map = _build_theme_str_map(members, themes_long, ranked)
    strategy_ids = sorted(screener_results.keys())

    # 估值（PE/PB）來自 screener CSV，不在 members → 建 stock_id 對照（首見為準）
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
    rally = float(fc.get("strong_rally_pct", 15))

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
        ryoy = _num(rev_yoy_map.get(sid), 1) if rev_yoy_map else None
        amt = _num(r.get("amount_million"), 0)
        pe = _num(pe_map.get(sid), 1)
        pb = _num(pb_map.get(sid), 2)
        fn = _num(r.get("foreign_net"), 0)
        tn = _num(r.get("trust_net"), 0)
        instn = _num(r.get("inst_net"), 0)
        inst_lots = _lots(r.get("inst_net"))
        foreign_lots = _lots(r.get("foreign_net"))
        trust_lots = _lots(r.get("trust_net"))
        vlots = _num(vol_map.get(sid), 0)
        # 法人淨買超佔近 20 日成交量%（集中度）：20日均量=今日量/vol_ratio，×20≈20日總量
        inst_pct20d = None
        if inst_lots is not None and vlots and vr and vr > 0:
            tot20 = (vlots / vr) * 20.0
            if tot20 > 0:
                inst_pct20d = round(inst_lots / tot20 * 100, 1)

        # 法人快取缺漏（join 不到，非真實零買賣超）：四欄顯示空白，由 flag 標示供人工查證
        inst_missing = bool(r.get("inst_missing"))
        if inst_missing:
            inst_lots = inst_pct20d = foreign_lots = trust_lots = None

        flags: list[str] = []
        if m60 is not None and m60 > overheated:
            flags.append("過熱")
        if amt is not None and amt < low_liq:
            flags.append("低流動")
        if pe is not None and pe > high_pe:
            flags.append("高PE")
        if fn is not None and tn is not None and fn * tn < 0 and min(abs(fn), abs(tn)) > cross_lots:
            flags.append("土洋對作")
        if mom is not None and mom > rally and instn is not None and instn < 0:
            flags.append("強漲法人賣")
        if inst_missing:
            flags.append("法人缺漏")

        rows.append(
            {
                "stock_id": sid,
                "name": r.get("name", ""),
                "industry": r.get("industry_name", ""),
                "theme": theme_map.get(sid, ""),
                "strategy": _strategy_str(r, strategy_ids),
                "rank_in_group": int(r.get("rank_in_group", 0) or 0),
                "momentum_5d_pct": mom,
                "change_pct": _num(r.get("change_pct"), 2),
                "close": close,
                "vol_ratio": vr if (vr or 0) > 0 else None,
                "ma20_dist_pct": m20,
                "ma60_dist_pct": m60,
                "ma20_price": ma20_price,
                "ma60_price": ma60_price,
                "amount_million": amt,
                "pe_ratio": pe,
                "pb_ratio": pb,
                "rev_yoy_pct": ryoy,
                "volume_lots_today": vlots,
                "inst_net_lots": inst_lots,
                "inst_pct20d": inst_pct20d,
                "foreign_net_lots": foreign_lots,
                "trust_net_lots": trust_lots,
                "flags": ";".join(flags),
                "goodinfo_url": str(r.get("goodinfo_url", "")),
            }
        )
    return rows


def write_candidates_enriched_csv(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    path: Path,
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
) -> list[dict]:
    """輸出「全候選股 × 技術/籌碼/估值/基本面 + flags 排雷欄」CSV，供 ProPicks 全宇宙挑股。

    補 group_analysis.md 只列部分股的盲點：每檔都有 5 日漲幅/距月線/距季線/量比/法人(張)拆分/
    PE/PB/主題，並程式預算 flags（過熱/低流動/高PE/土洋對作/強漲法人賣/法人缺漏）讓 AI 快速
    排雷、把腦力留給判斷。缺值（無快取）寫空白 → 標「需查證」而非編造。回傳已建立的列（list[dict]，
    供庫存/觀察清單重用同一筆來源值以保持跨 CSV 一致）。
    """
    rows = _build_enriched_rows(members, themes_long, screener_results, flags_cfg, rev_yoy_map)
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
    "change_pct",
    "vol_ratio",
    "ma20_dist_pct",
    "ma60_dist_pct",
    "ma20_price",
    "ma60_price",
    "amount_million",
    "pe_ratio",
    "pb_ratio",
    "rev_yoy_pct",
    "volume_lots_today",
    "inst_net_lots",
    "inst_pct20d",
    "foreign_net_lots",
    "trust_net_lots",
    "flags",
)


def write_named_list_csv(
    members: pl.DataFrame,
    themes_long: pl.DataFrame | None,
    screener_results: dict[str, pl.DataFrame],
    path: Path,
    *,
    flags_cfg: dict | None = None,
    rev_yoy_map: dict | None = None,
    holdings_map: dict | None = None,
    canonical_rows: dict[str, dict] | None = None,
) -> int:
    """輸出庫存/觀察清單 enriched CSV（同 candidates 欄位）。

    holdings_map={stock_id: {"buy_price": x, "shares": y}} 時，每列加 買入價/報酬率%/現值(千)，
    供「續抱/加碼/減碼/停利/停損」決策對照。
    canonical_rows={stock_id: candidates_row} 時，重疊股重用 candidates 的市場/籌碼/估值/flags
    欄位，使三份 CSV 對同一檔股票數字一致。回傳寫入檔數。
    """
    rows = _build_enriched_rows(members, themes_long, screener_results, flags_cfg, rev_yoy_map)
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
