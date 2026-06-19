"""rotation_report 測試（R3）：四象限 / 校準訊號 / ΔRank / 渲染，全離線合成資料。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from tw_screener.analysis.rotation import compute_fund_flows, compute_subindustry_baskets
from tw_screener.report.rotation_report import (
    Q_COOL,
    Q_DISTRIBUTE,
    Q_NEXT,
    Q_TREND,
    build_rotation_table,
    load_prev_rotation_snapshot,
    render_rotation_report,
)

D0 = date(2026, 1, 1)
N = 80


def _dates(n: int = N) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _membership() -> pl.DataFrame:
    """四個次產業 × 各 1 檔（min_members=1 測試用），對應四象限。"""
    return pl.DataFrame(
        {
            "sub_industry": ["甲流入未漲", "乙流入已漲", "丙流出已漲", "丁流出未漲"],
            "stock_id": ["1111", "2222", "3333", "4444"],
        }
    )


def _price_history() -> pl.DataFrame:
    """甲/丁 全程走平（未漲）；乙/丙 後段 +30%（已漲）。"""
    ds = _dates()
    rows = []
    for sid, flat in [("1111", True), ("2222", False), ("3333", False), ("4444", True)]:
        px = 100.0
        for i, d in enumerate(ds):
            if not flat and i >= N - 20:
                px *= 1.015  # 尾段拉升 → 距 60 日低 > 10%
            rows.append({"date": d, "stock_id": sid, "close": px, "volume": 1000})
    return pl.DataFrame(rows)


def _institutional() -> pl.DataFrame:
    """甲/乙 法人每日買超（流入）；丙/丁 每日賣超（流出）。"""
    rows = []
    for d in _dates():
        for sid, net in [("1111", 500), ("2222", 300), ("3333", -400), ("4444", -200)]:
            rows.append(
                {"date": d, "stock_id": sid, "foreign_net": net, "trust_net": net,
                 "dealer_net": 0, "total_net": net * 2}
            )
    return pl.DataFrame(rows)


@pytest.fixture()
def table() -> pl.DataFrame:
    members = _membership()
    market = _price_history()
    baskets = compute_subindustry_baskets(members, market)
    flows = compute_fund_flows(
        members, _institutional(),
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=5, long_window=20,
    )
    return build_rotation_table(
        flows, baskets, short_window=5, long_window=20,
        entry_signal={"signal": "trust_flow_20d", "mode": "z", "threshold": 1.0,
                      "require_momentum": True,
                      "confirm_signal": "flow_breadth_20d", "confirm_threshold": 0.6},
        position_window=60, position_low_pct=10.0, min_members=1,
    )


def test_quadrant_assignment(table: pl.DataFrame):
    got = {r["sub_industry"]: r["quadrant"] for r in table.iter_rows(named=True)}
    assert got["甲流入未漲"] == Q_NEXT
    assert got["乙流入已漲"] == Q_TREND
    assert got["丙流出已漲"] == Q_DISTRIBUTE
    assert got["丁流出未漲"] == Q_COOL


def test_table_ranked_by_flow(table: pl.DataFrame):
    # 甲日淨 1000 > 乙 600 > 丁 -400 > 丙 -800（20 日滾動同序）
    assert table.sort("radar_rank")["sub_industry"].to_list() == [
        "甲流入未漲", "乙流入已漲", "丁流出未漲", "丙流出已漲",
    ]
    assert table["rank_delta"].null_count() == table.height  # 無 prev → 全 null


def test_lots_conversion(table: pl.DataFrame):
    row = table.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    # total_net=1000 股/日 × 20 日 = 20000 股 = 20 張
    assert row["net_flow_20d_lots"] == 20.0


def test_confirm_flag_set(table: pl.DataFrame):
    # 甲/乙 breadth_20d = 1.0 > 0.6 → 確認鏡頭達標
    row = table.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    assert row["confirm_triggered"] is True
    # 常數流入 → z std=0 → 校準訊號不觸發（誠實）
    assert row["entry_triggered"] is False


def test_render_report_and_csv(table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "2026-W24",
        short_window=5, long_window=20,
        entry_signal={"signal": "trust_flow_20d", "mode": "z", "threshold": 1.0},
        position_low_pct=10.0, top_n=10, data_date="2026-03-21",
    )
    md = md_path.read_text(encoding="utf-8")
    assert "# 2026-W24 次產業資金流向輪動" in md
    assert "🟢 下一棒候選" in md and "甲流入未漲" in md
    assert "出貨警訊" in md and "丙流出已漲" in md
    assert "資料來源與時間" in md
    assert "首週正常" in md  # 無 prev 的誠實標註
    for banned in ("目標價", "強烈建議", "飆股", "保證"):
        assert banned not in md
    csv = pl.read_csv(md_path.parent / "sector_rotation.csv")
    assert {"sub_industry", "radar_rank", "quadrant"}.issubset(csv.columns)


# ─── A1 連續 CP 補漲分數 ──────────────────────────────────────────────────────


def _cp_membership() -> pl.DataFrame:
    """兩檔同樣強流入，但甲走平（room≈1）、乙已漲（room<1）→ 隔離 room 效果。"""
    return pl.DataFrame(
        {"sub_industry": ["甲流入未漲", "乙流入已漲"], "stock_id": ["1111", "2222"]}
    )


def _cp_institutional() -> pl.DataFrame:
    """兩檔『相同』的帶波動流入序列（std>0 → z 有定義），尾段加大 → 末日 z>0。"""
    rows = []
    for i, d in enumerate(_dates()):
        net = 300 + 150 * ((i % 7) - 3) + (300 if i >= N - 20 else 0)
        for sid in ("1111", "2222"):
            rows.append(
                {"date": d, "stock_id": sid, "foreign_net": net, "trust_net": net,
                 "dealer_net": 0, "total_net": net}
            )
    return pl.DataFrame(rows)


def _cp_price() -> pl.DataFrame:
    ds = _dates()
    rows = []
    for sid, flat in [("1111", True), ("2222", False)]:
        px = 100.0
        for i, d in enumerate(ds):
            if not flat and i >= N - 20:
                px *= 1.015  # 尾段拉升 → 距低點變大 → room 變小
            rows.append({"date": d, "stock_id": sid, "close": px, "volume": 1000})
    return pl.DataFrame(rows)


@pytest.fixture()
def cp_table() -> pl.DataFrame:
    members = _cp_membership()
    market = _cp_price()
    flows = compute_fund_flows(
        members, _cp_institutional(),
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=5, long_window=20,
    )
    return build_rotation_table(
        flows, compute_subindustry_baskets(members, market),
        short_window=5, long_window=20, entry_signal={},
        position_window=60, position_low_pct=10.0, cp_position_ceiling=60.0,
        min_members=1,
    )


def test_cp_score_room_favours_unrisen(cp_table: pl.DataFrame):
    rows = {r["sub_industry"]: r for r in cp_table.iter_rows(named=True)}
    cp_low = rows["甲流入未漲"]["cp_score"]
    cp_high = rows["乙流入已漲"]["cp_score"]
    # 流入強度相同（z 同），甲位階低 room 大 → CP 應高於已漲的乙
    assert cp_low is not None and cp_high is not None
    assert cp_low > cp_high > 0


def test_cp_score_null_follows_z(table: pl.DataFrame):
    # 誠實降級：z 缺（warmup / std=0）→ cp_score 也缺；此 fixture 位階皆有值，故 cp null ⇔ z null
    assert "cp_score" in table.columns
    assert table["above_low_pct"].null_count() == 0
    assert table["cp_score"].is_null().to_list() == table["net_flow_20d_z"].is_null().to_list()


def test_render_cp_section(cp_table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        cp_table, week_tag="2026-W24", output_dir=tmp_path / "cp",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
        cp_position_ceiling=60.0,
    )
    md = md_path.read_text(encoding="utf-8")
    assert "🎯 CP 候選排名" in md
    assert "甲流入未漲" in md  # 正分候選入榜
    assert "room = max(0, 1 −" in md  # 公式註腳
    for banned in ("目標價", "強烈建議", "飆股", "保證"):
        assert banned not in md


def test_render_cp_section_empty_when_all_null(table: pl.DataFrame, tmp_path: Path):
    md = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "cpx",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
    ).read_text(encoding="utf-8")
    assert "本週無正分 CP 候選" in md


# ─── A2 資金動能新鮮度標 ──────────────────────────────────────────────────────


def _accel_membership() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "sub_industry": ["甲流入加速", "乙流入放緩", "丙流出轉向"],
            "stock_id": ["1111", "2222", "3333"],
        }
    )


def _accel_institutional() -> pl.DataFrame:
    """甲：流入逐日放大（momentum>0）；乙：流入逐日縮小（<0）；丙：流出但收斂（淨負、momentum>0）。"""
    rows = []
    for i, d in enumerate(_dates()):
        for sid, net in [("1111", 100 + i * 10), ("2222", 3000 - i * 10), ("3333", -2000 + i * 10)]:
            rows.append(
                {"date": d, "stock_id": sid, "foreign_net": net, "trust_net": net,
                 "dealer_net": 0, "total_net": net}
            )
    return pl.DataFrame(rows)


def _accel_price() -> pl.DataFrame:
    """甲/乙 尾段拉升（已漲→主升續勢）；丙走平（未漲→冷卻，作反轉前哨）。"""
    ds = _dates()
    rows = []
    for sid, flat in [("1111", False), ("2222", False), ("3333", True)]:
        px = 100.0
        for i, d in enumerate(ds):
            if not flat and i >= N - 20:
                px *= 1.015
            rows.append({"date": d, "stock_id": sid, "close": px, "volume": 1000})
    return pl.DataFrame(rows)


@pytest.fixture()
def accel_table() -> pl.DataFrame:
    members = _accel_membership()
    market = _accel_price()
    flows = compute_fund_flows(
        members, _accel_institutional(),
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=5, long_window=20,
    )
    return build_rotation_table(
        flows, compute_subindustry_baskets(members, market),
        short_window=5, long_window=20, entry_signal={},
        position_window=60, position_low_pct=10.0, min_members=1,
    )


def test_freshness_sign(accel_table: pl.DataFrame):
    f = {r["sub_industry"]: r["freshness"] for r in accel_table.iter_rows(named=True)}
    assert f["甲流入加速"] == "加速"   # 流入放大 → momentum>0
    assert f["乙流入放緩"] == "放緩"   # 流入縮小 → momentum<0
    assert f["丙流出轉向"] == "加速"   # 淨流出但收斂 → momentum>0（反轉前哨）


def test_render_freshness_and_reversal(accel_table: pl.DataFrame, tmp_path: Path):
    md = render_rotation_report(
        accel_table, week_tag="2026-W24", output_dir=tmp_path / "a2",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
    ).read_text(encoding="utf-8")
    assert "新鮮度" in md                       # CP 候選表新增欄
    assert "甲流入加速（加速中）" in md          # 主升續勢標新鮮度
    assert "乙流入放緩（放緩中）" in md
    # 丙＝流出×未漲（冷卻）＋動能轉正 → 反轉前哨（僅冷卻象限掛此標，出貨警訊不掛）
    assert "丙流出轉向" in md and "反轉前哨" in md
    for banned in ("目標價", "強烈建議", "飆股", "保證"):
        assert banned not in md


# ─── 修法 3：資金轉向覆蓋（flow_turn） ────────────────────────────────────────


def _turn_membership() -> pl.DataFrame:
    return pl.DataFrame(
        {"sub_industry": ["戊主升轉退潮", "己出貨轉回流"], "stock_id": ["5555", "6666"]}
    )


def _turn_institutional() -> pl.DataFrame:
    """戊：長期買超但近 5 日轉賣（退潮）；己：長期賣超但近 5 日轉買（資金回流）。"""
    rows = []
    for i, d in enumerate(_dates()):
        last5 = i >= N - 5
        for sid, net in [("5555", -500 if last5 else 1000), ("6666", 500 if last5 else -1000)]:
            rows.append(
                {"date": d, "stock_id": sid, "foreign_net": net, "trust_net": net,
                 "dealer_net": 0, "total_net": net}
            )
    return pl.DataFrame(rows)


def _turn_price() -> pl.DataFrame:
    """兩檔尾段拉升（已漲）：戊→主升續勢、己→出貨警訊。"""
    ds = _dates()
    rows = []
    for sid in ("5555", "6666"):
        px = 100.0
        for i, d in enumerate(ds):
            if i >= N - 20:
                px *= 1.015
            rows.append({"date": d, "stock_id": sid, "close": px, "volume": 1000})
    return pl.DataFrame(rows)


@pytest.fixture()
def turn_table() -> pl.DataFrame:
    members = _turn_membership()
    market = _turn_price()
    flows = compute_fund_flows(
        members, _turn_institutional(),
        volume_history=market.select(["date", "stock_id", "volume"]),
        short_window=5, long_window=20,
    )
    return build_rotation_table(
        flows, compute_subindustry_baskets(members, market),
        short_window=5, long_window=20, entry_signal={},
        position_window=60, position_low_pct=10.0, min_members=1,
    )


def test_flow_turn_sign_divergence(turn_table: pl.DataFrame):
    t = {r["sub_industry"]: r for r in turn_table.iter_rows(named=True)}
    # 20d>0 但 5d<0：主升續勢象限，但近端退潮（解「20 日累積到後面其實在賣」）
    assert t["戊主升轉退潮"]["quadrant"] == Q_TREND
    assert t["戊主升轉退潮"]["flow_turn"] == "退潮"
    # 20d<0 但 5d>0：出貨警訊象限，但近端資金回流（解「累積仍負但近 5 日已轉買」）
    assert t["己出貨轉回流"]["quadrant"] == Q_DISTRIBUTE
    assert t["己出貨轉回流"]["flow_turn"] == "資金回流"


def test_render_flow_turn_markers(turn_table: pl.DataFrame, tmp_path: Path):
    md = render_rotation_report(
        turn_table, week_tag="2026-W24", output_dir=tmp_path / "turn",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
    ).read_text(encoding="utf-8")
    assert "資金回流" in md  # 出貨警訊段揭露近端轉買
    assert "退潮" in md  # 主升續勢段揭露近端轉賣
    for banned in ("目標價", "強烈建議", "飆股", "保證"):
        assert banned not in md


def test_delta_rank_with_prev_snapshot(tmp_path: Path, table: pl.DataFrame):
    # 上週快照：甲排第 3 → 本週第 1 → ΔRank +2
    prev_dir = tmp_path / "2026-W23"
    prev_dir.mkdir(parents=True)
    pl.DataFrame(
        {"sub_industry": ["乙流入已漲", "丙流出已漲", "甲流入未漲"], "radar_rank": [1, 2, 3]}
    ).write_csv(prev_dir / "sector_rotation.csv")

    prev = load_prev_rotation_snapshot(tmp_path, "2026-W24")
    assert prev is not None

    members = _membership()
    market = _price_history()
    flows = compute_fund_flows(
        members, _institutional(), short_window=5, long_window=20,
    )
    t2 = build_rotation_table(
        flows, compute_subindustry_baskets(members, market),
        short_window=5, long_window=20, entry_signal={},
        min_members=1, prev=prev,
    )
    row = t2.filter(pl.col("sub_industry") == "甲流入未漲").row(0, named=True)
    assert row["rank_delta"] == 2


def test_prev_snapshot_ignores_future_weeks(tmp_path: Path):
    for wk in ("2026-W25", "2026-W30"):
        d = tmp_path / wk
        d.mkdir(parents=True)
        pl.DataFrame({"sub_industry": ["X"], "radar_rank": [1]}).write_csv(
            d / "sector_rotation.csv"
        )
    assert load_prev_rotation_snapshot(tmp_path, "2026-W24") is None


def test_render_no_leading_blank_lines(table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "w",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
    )
    assert md_path.read_text(encoding="utf-8").startswith("# 2026-W24")


# ─── R4 我的參與度疊圖 ────────────────────────────────────────────────────────


def test_build_participation_tags_and_counts(table: pl.DataFrame):
    from tw_screener.report.rotation_report import build_participation

    membership = _membership()  # 1111→甲(下一棒) 2222→乙(主升) 3333→丙(出貨) 4444→丁(冷卻)
    out = build_participation(
        [
            ("庫存", ["1111", "3333", "9999"], False),  # 9999 未標次產業
            ("觀察", [], False),                          # 空 → 略過
            ("命中", ["1111", "1111", "2222"], True),     # 重複去重
        ],
        membership,
        table,
        long_window=20,
    )
    assert [s["source"] for s in out] == ["庫存", "命中"]  # 空來源被略過
    hold = out[0]
    assert hold["n_total"] == 3
    assert hold["n_inflow"] == 1     # 1111（甲流入）
    assert hold["n_warning"] == 1    # 3333（丙出貨警訊）
    assert hold["n_untagged"] == 1   # 9999
    t1111 = next(s for s in hold["stocks"] if s["stock_id"] == "1111")["tags"][0]
    assert t1111["quadrant"] == Q_NEXT and t1111["inflow"] is True
    hits = out[1]
    assert hits["n_total"] == 2  # 去重後 1111/2222
    assert Q_NEXT in hits["by_quadrant"]


def test_build_participation_sub_below_min_members(table: pl.DataFrame):
    """股票屬於榜外次產業（< min_members 未進 table）→ quadrant None、不計流入。"""
    from tw_screener.report.rotation_report import build_participation

    membership = pl.DataFrame(
        {"sub_industry": ["小眾", "甲流入未漲"], "stock_id": ["5555", "1111"]}
    )
    out = build_participation([("庫存", ["5555"], False)], membership, table, long_window=20)
    tag = out[0]["stocks"][0]["tags"][0]
    assert tag["quadrant"] is None and tag["inflow"] is None
    assert out[0]["n_inflow"] == 0


def test_render_participation_section(table: pl.DataFrame, tmp_path: Path):
    from tw_screener.report.rotation_report import build_participation

    participation = build_participation(
        [("庫存（holdings.csv）", ["1111", "9999"], False), ("本週命中", ["2222"], True)],
        _membership(),
        table,
        long_window=20,
        names={"1111": "甲一", "2222": "乙二"},
    )
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "w",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
        participation=participation,
    )
    md = md_path.read_text(encoding="utf-8")
    assert "## 5. 我的參與度" in md
    assert "1111 甲一" in md and "甲流入未漲（下一棒・流入）" in md
    assert "未標次產業" in md  # 9999
    assert "## 6. 資料來源與時間" in md  # 段號順延


def test_render_without_participation_skips_section(table: pl.DataFrame, tmp_path: Path):
    md_path = render_rotation_report(
        table, week_tag="2026-W24", output_dir=tmp_path / "w2",
        short_window=5, long_window=20, entry_signal={}, position_low_pct=10.0,
        participation=None,
    )
    md = md_path.read_text(encoding="utf-8")
    assert "我的參與度" not in md
    assert "## 5. 資料來源與時間" in md
