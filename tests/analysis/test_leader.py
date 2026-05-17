"""tests/analysis/test_leader.py — rank_within_groups 單元測試（全離線）。"""

import polars as pl
import pytest

from tw_screener.analysis.leader import find_leaders, rank_within_groups

# ─── Helper ───────────────────────────────────────────────────────────────────


def _make_members(
    stock_ids: list[str],
    industry_codes: list[str],
    momentum_values: list[float],
    amounts: list[float],
    strategy_counts: list[int],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "stock_id": stock_ids,
            "name": [f"公司{sid}" for sid in stock_ids],
            "industry_code": industry_codes,
            "industry_name": [f"產業{code}" for code in industry_codes],
            "momentum_5d": momentum_values,
            "rs": momentum_values,
            "change_pct": momentum_values,
            "amount_million": amounts,
            "strategy_count": strategy_counts,
            "goodinfo_url": [f"http://x/{sid}" for sid in stock_ids],
            "in_a_breakout": [True] * len(stock_ids),
        }
    )


# ─── rank_within_groups ───────────────────────────────────────────────────────


def test_rank_within_groups_assigns_rank_1_per_group():
    """每個族群應有且只有一個 rank_in_group == 1。"""
    members = _make_members(
        ["2330", "2454", "3034", "2317", "2382"],
        ["14", "14", "14", "15", "15"],
        [5.0, 3.0, 1.0, 4.0, 2.0],
        [5000.0, 3000.0, 1000.0, 4000.0, 2000.0],
        [1, 1, 1, 1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    assert "rank_in_group" in result.columns

    for code in ["14", "15"]:
        group = result.filter(pl.col("industry_code") == code)
        assert (group["rank_in_group"] == 1).sum() == 1


def test_rank_within_groups_highest_momentum_wins():
    """同等 amount 下，5 日漲幅最高者 rank=1。"""
    members = _make_members(
        ["A", "B"],
        ["14", "14"],
        [10.0, 2.0],
        [1000.0, 1000.0],
        [1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    leader = result.filter(pl.col("rank_in_group") == 1)
    assert leader["stock_id"][0] == "A"


def test_rank_within_groups_score_in_range():
    """leader_score 應在 [0, 1] 範圍內。"""
    members = _make_members(
        ["2330", "2454", "3034"],
        ["14", "14", "14"],
        [5.0, 3.0, 1.0],
        [5000.0, 3000.0, 1000.0],
        [2, 1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    scores = result["leader_score"].to_list()
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_rank_within_groups_institutional_net_used():
    """有法人資料時，inst_net 應影響排序。"""
    from datetime import date

    members = _make_members(
        ["2330", "2454"],
        ["14", "14"],
        [2.0, 2.0],
        [1000.0, 1000.0],
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
    result = rank_within_groups(members, pl.DataFrame(), institutional)
    leader = result.filter(pl.col("rank_in_group") == 1)
    assert leader["stock_id"][0] == "2330"


def test_rank_within_groups_empty_input():
    """空的 group_members → 回傳空 DataFrame（含必要欄位）。"""
    result = rank_within_groups(pl.DataFrame(), pl.DataFrame(), pl.DataFrame())
    assert result.is_empty()
    assert "stock_id" in result.columns
    assert "leader_score" in result.columns
    assert "rank_in_group" in result.columns


def test_rank_within_groups_group_size_column():
    """結果應包含 group_size 欄位，值等於該族群的成員數。"""
    members = _make_members(
        ["2330", "2454", "3034"],
        ["14", "14", "14"],
        [5.0, 3.0, 1.0],
        [5000.0, 3000.0, 1000.0],
        [1, 1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    assert "group_size" in result.columns
    assert result["group_size"].to_list() == [3, 3, 3]


def test_rank_within_groups_two_member_norm():
    """兩成員族群：rank 1 的 momentum_rank_norm = 1.0，rank 2 = 0.0。"""
    members = _make_members(
        ["A", "B"],
        ["14", "14"],
        [5.0, 1.0],
        [1000.0, 1000.0],
        [1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    rank1 = result.filter(pl.col("rank_in_group") == 1).to_dicts()[0]
    rank2 = result.filter(pl.col("rank_in_group") == 2).to_dicts()[0]
    assert rank1["momentum_rank_norm"] == pytest.approx(1.0)
    assert rank2["momentum_rank_norm"] == pytest.approx(0.0)


def test_rank_within_groups_assigns_distinct_ranks():
    """3 個族群成員 → rank_in_group 應為 1, 2, 3。"""
    members = _make_members(
        ["A", "B", "C"],
        ["14", "14", "14"],
        [5.0, 3.0, 1.0],
        [5000.0, 3000.0, 1000.0],
        [1, 1, 1],
    )
    result = rank_within_groups(members, pl.DataFrame(), pl.DataFrame())
    ranks = sorted(result["rank_in_group"].to_list())
    assert ranks == [1, 2, 3]


# ─── back-compat alias ────────────────────────────────────────────────────────


def test_find_leaders_alias():
    """find_leaders 仍可呼叫（向後相容 alias）。"""
    assert find_leaders is rank_within_groups
