"""M4 轉折早段欄與候選源 E 測試（委託書 M4）。"""

from __future__ import annotations

import polars as pl

from tw_screener.analysis.inflection import (
    flow_diff_5_20,
    is_inflection_ambush,
    margin_slim,
)
from tw_screener.report.inflection_ambush import (
    build_inflection_ambush,
    render_inflection_ambush,
)

# ── M4.1 三個描述欄 ──────────────────────────────────────────────────────


def test_flow_diff_5_20_separates_early_from_late():
    """20 日大正但近端已停 → 本欄為負＝建倉尾段；近端加速 → 正＝早段。"""
    # 尾段：20 日 +40,000，近 5 日只剩 +1,000 → 1000 − 10000 = −9000
    assert flow_diff_5_20(1_000, 40_000) == -9_000.0
    # 早段：20 日 −4,000（還在賣），近 5 日 +3,000 → 3000 + 1000 = +4000
    assert flow_diff_5_20(3_000, -4_000) == 4_000.0
    # 均勻買：20 日 +20,000、近 5 日 +5,000 → 0（不加速也不減速）
    assert flow_diff_5_20(5_000, 20_000) == 0.0


def test_flow_diff_5_20_missing_is_none_not_zero():
    """缺值回 None——0 的語意是「剛好打平」，不能拿來代替「沒資料」。"""
    assert flow_diff_5_20(None, 20_000) is None
    assert flow_diff_5_20(5_000, None) is None


def test_margin_slim_needs_both_conditions():
    assert margin_slim(-1_200, 3.5) is True      # 融資減＋股價漲
    assert margin_slim(-1_200, -0.4) is True     # 融資減＋股價平（> −1%）
    assert margin_slim(-1_200, -5.0) is False    # 融資減但股價跌＝殺融資，不是減肥
    assert margin_slim(+1_200, 3.5) is False     # 融資增＋股價漲＝散戶追價


def test_margin_slim_missing_is_none_not_false():
    """上櫃股天生無融資資料——標 False 會讓人以為查過了，必須是 None。"""
    assert margin_slim(None, 3.5) is None
    assert margin_slim(-1_200, None) is None


def test_margin_slim_flat_threshold_configurable():
    assert margin_slim(-1_000, -2.0, flat_pct=-1.0) is False
    assert margin_slim(-1_000, -2.0, flat_pct=-3.0) is True


# ── M4.2 候選源 E ────────────────────────────────────────────────────────


def _ok(**over):
    base = dict(
        fundamental_health="穩健", base_zone="貼底", dist_low_60d_pct=6.0,
        foreign_inflection_days=2, foreign_net_20d_lots=-3_000.0,
    )
    base.update(over)
    return is_inflection_ambush(**base)


def test_inflection_ambush_all_four_conditions():
    assert _ok() is True
    assert _ok(fundamental_health="減速") is False       # 基本面未達標
    assert _ok(base_zone="", dist_low_60d_pct=25.0) is False  # 位階不在底部
    assert _ok(base_zone="", dist_low_60d_pct=8.0) is True    # 距低 ≤10% 也算
    assert _ok(foreign_inflection_days=0) is False       # 最新日沒買
    assert _ok(foreign_inflection_days=12) is False      # 買太久＝尾段，正是要避開的
    assert _ok(foreign_net_20d_lots=90_000.0) is False   # 20 日已大買＝確認訊號＝尾段


def test_inflection_ambush_base_zone_branch_is_ma60_not_near_low():
    """貼底＝距季線≤10%，與距低點無關——2026-W32 的 7610（距低 +133.6%）就是靠這條過的。

    這條分支可用一行 settings 關掉（回收設計同 M1.6），關掉後只留「距低」型。
    """
    assert _ok(base_zone="貼底", dist_low_60d_pct=133.6) is True
    assert _ok(base_zone="貼底", dist_low_60d_pct=133.6,
               allow_base_zone_branch=False) is False
    # 距低型不受開關影響
    assert _ok(base_zone="", dist_low_60d_pct=6.0,
               allow_base_zone_branch=False) is True


def test_inflection_ambush_missing_never_passes():
    """缺資料不放行（同 M1 的處理）。"""
    assert _ok(fundamental_health=None) is False
    assert _ok(foreign_inflection_days=None) is False
    assert _ok(foreign_net_20d_lots=None) is False
    assert _ok(base_zone=None, dist_low_60d_pct=None) is False


def _enriched(rows: list[dict]) -> pl.DataFrame:
    cols = {
        "stock_id": "", "name": "", "theme": "", "fundamental_health": "穩健",
        "rev_yoy_pct": 30.0, "base_zone": "", "dist_low_60d_pct": 5.0,
        "foreign_inflection_days": 2, "foreign_net_lots": -3000.0,
        "foreign_flow_diff_5_20": 500.0, "margin_slim": True, "close": 100.0,
        "low_60d": 95.0, "ma60_dist_pct": -8.0, "flags": "",
    }
    return pl.DataFrame([{**cols, **r} for r in rows])


def test_build_inflection_ambush_splits_qualified_and_near_miss():
    df = _enriched([
        {"stock_id": "1101", "name": "合格"},
        {"stock_id": "1102", "name": "差基本面", "fundamental_health": "減速"},
        {"stock_id": "1103", "name": "差兩條", "fundamental_health": "減速",
         "foreign_inflection_days": 0},
    ])
    q, nm = build_inflection_ambush(df)
    assert q["stock_id"].to_list() == ["1101"]
    # 只差一條的進近似清單；差兩條的不進（否則清單會變成全宇宙）
    assert nm["stock_id"].to_list() == ["1102"]
    assert nm["差哪一條"].to_list() == ["基本面"]


def test_build_inflection_ambush_sorts_near_low_before_base_zone_only():
    """兩種「低位階」語意不同——距低型（原意）必須排在僅貼底型前面，讓人先看到。"""
    df = _enriched([
        {"stock_id": "9001", "base_zone": "貼底", "dist_low_60d_pct": 88.0},
        {"stock_id": "9002", "base_zone": "", "dist_low_60d_pct": 4.0},
    ])
    q, _ = build_inflection_ambush(df)
    assert q["stock_id"].to_list() == ["9002", "9001"]
    assert q["位階依據"].to_list() == ["距低≤10%", "僅貼底(距季線)"]


def test_render_truncates_near_miss_but_states_total():
    """近似清單全列會蓋掉合格清單；截斷可以，但總數與去處必須寫出來。"""
    df = _enriched([
        {"stock_id": f"80{i:02d}", "fundamental_health": "減速"} for i in range(20)
    ])
    q, nm = build_inflection_ambush(df)
    assert nm.height == 20
    body = render_inflection_ambush(q, nm, "2026-W32", 10.0, (1, 5), 5000.0,
                                    near_miss_limit=5)
    assert "只差一條（20 檔" in body
    assert "另有 15 檔" in body
    assert "inflection_ambush_near_miss.csv" in body


def test_build_inflection_ambush_missing_columns_returns_empty_not_guess():
    df = pl.DataFrame({"stock_id": ["1101"], "name": ["缺欄"]})
    q, nm = build_inflection_ambush(df)
    assert q.is_empty() and nm.is_empty()


def test_render_states_zero_qualified_explicitly():
    """零命中週也要產出，且必須明寫 0 檔——quota 條款要的就是這句可查核的事實。"""
    q, nm = build_inflection_ambush(
        _enriched([{"stock_id": "1101", "fundamental_health": "減速"}])
    )
    body = render_inflection_ambush(q, nm, "2026-W32", 10.0, (1, 5), 5000.0)
    assert "轉折早段 0 檔合格" in body
    assert "差哪一條" in body
    assert "不自動進 picks、不改排序、不改剔除" in body
