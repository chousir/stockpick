"""tests/analysis/test_leader.py — find_leaders 單元測試（全離線）。"""

import polars as pl
import pytest

from tw_screener.analysis.leader import find_leaders

# ─── Helper ───────────────────────────────────────────────────────────────────


def _make_members(
    stock_ids: list[str],
    industry_codes: list[str],
    rs_values: list[float],
    amounts: list[float],
    strategy_counts: list[int],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": stock_ids,
            "name": [f"公司{sid}" for sid in stock_ids],
            "industry_code": industry_codes,
            "industry_name": [f"產業{code}" for code in industry_codes],
            "rs": rs_values,
            "change_pct": rs_values,
            "amount_million": amounts,
            "strategy_count": strategy_counts,
            "goodinfo_url": [f"http://x/{sid}" for sid in stock_ids],
            "in_a_breakout": [True] * len(stock_ids),
        }
    )


# ─── find_leaders ─────────────────────────────────────────────────────────────


def test_find_leaders_marks_one_leader_per_group():
    """每個族群應有且只有一個 is_leader=True。"""
    members = _make_members(
        ["2330", "2454", "3034", "2317", "2382"],
        ["14", "14", "14", "15", "15"],
        [5.0, 3.0, 1.0, 4.0, 2.0],
        [5000.0, 3000.0, 1000.0, 4000.0, 2000.0],
        [1, 1, 1, 1, 1],
    )
    result = find_leaders(members, pl.DataFrame(), pl.DataFrame())
    assert "is_leader" in result.columns

    for code in ["14", "15"]:
        group = result.filter(pl.col("industry_code") == code)
        assert group["is_leader"].sum() == 1


def test_find_leaders_highest_rs_wins_leader():
    """在同等 amount 條件下，RS 最高者應成為領頭羊。"""
    members = _make_members(
        ["A", "B"],
        ["14", "14"],
        [10.0, 2.0],  # A has much higher RS
        [1000.0, 1000.0],  # same amount
        [1, 1],
    )
    result = find_leaders(members, pl.DataFrame(), pl.DataFrame())
    leader = result.filter(pl.col("is_leader"))
    assert leader["stock_id"][0] == "A"


def test_find_leaders_score_in_range():
    """leader_score 應在 [0, 1] 範圍內。"""
    members = _make_members(
        ["2330", "2454", "3034"],
        ["14", "14", "14"],
        [5.0, 3.0, 1.0],
        [5000.0, 3000.0, 1000.0],
        [2, 1, 1],
    )
    result = find_leaders(members, pl.DataFrame(), pl.DataFrame())
    scores = result["leader_score"].to_list()
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_find_leaders_institutional_net_used():
    """有法人資料時，inst_net 應影響排序。"""
    from datetime import date

    members = _make_members(
        ["2330", "2454"],
        ["14", "14"],
        [2.0, 2.0],  # same RS
        [1000.0, 1000.0],  # same amount
        [1, 1],
    )
    institutional = pl.DataFrame(
        {
            "date": [date(2026, 5, 15), date(2026, 5, 15)],
            "stock_id": ["2330", "2454"],
            "stock_name": ["台積電", "聯發科"],
            "foreign_net": [5000, -500],
            "trust_net": [0, 0],
            "dealer_net": [0, 0],
            "total_net": [5000, -500],
        }
    )
    result = find_leaders(members, pl.DataFrame(), institutional)
    leader = result.filter(pl.col("is_leader"))
    assert leader["stock_id"][0] == "2330"


def test_find_leaders_empty_input():
    """空的 group_members → 回傳空 DataFrame（含必要欄位）。"""
    result = find_leaders(pl.DataFrame(), pl.DataFrame(), pl.DataFrame())
    assert result.is_empty()
    assert "stock_id" in result.columns
    assert "leader_score" in result.columns
    assert "is_leader" in result.columns


def test_find_leaders_group_size_column():
    """結果應包含 group_size 欄位，值等於該族群的成員數。"""
    members = _make_members(
        ["2330", "2454", "3034"],
        ["14", "14", "14"],
        [5.0, 3.0, 1.0],
        [5000.0, 3000.0, 1000.0],
        [1, 1, 1],
    )
    result = find_leaders(members, pl.DataFrame(), pl.DataFrame())
    assert "group_size" in result.columns
    assert all(result["group_size"].to_list() == [3, 3, 3] for _ in [1])


def test_find_leaders_two_member_group_norm():
    """兩成員族群：leader 的 rs_rank_norm = 1.0，follower = 0.0。"""
    members = _make_members(
        ["A", "B"],
        ["14", "14"],
        [5.0, 1.0],
        [1000.0, 1000.0],
        [1, 1],
    )
    result = find_leaders(members, pl.DataFrame(), pl.DataFrame())
    leader_row = result.filter(pl.col("is_leader")).to_dicts()[0]
    follower_row = result.filter(~pl.col("is_leader")).to_dicts()[0]
    assert leader_row["rs_rank_norm"] == pytest.approx(1.0)
    assert follower_row["rs_rank_norm"] == pytest.approx(0.0)
