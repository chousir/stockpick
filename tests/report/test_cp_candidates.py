"""個股 CP 候選清單測試（B3，docs/13 §4 Phase B）：全離線合成資料、不打外部。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.report.cp_candidates import (
    attach_subind_quadrant,
    attach_valuation,
    build_cp_candidates,
    latest_snapshot,
    recent_buy_confirm,
    render_cp_candidates_report,
    tag_holding_status,
)

RULES: list[dict] = [
    {"name": "L1埋伏", "label": "ambush", "flow_z_col": "foreign_flow_20d_z",
     "z_threshold": 0.5, "modifier": "low"},
    {"name": "L2追突破", "label": "breakout", "flow_z_col": "trust_flow_20d_z",
     "z_threshold": 1.0, "modifier": "low"},
    {"name": "L3反轉", "label": "reversal", "flow_z_col": "trust_flow_20d_z",
     "z_threshold": 1.5, "modifier": "mom", "require_confirm": True},
]

_SNAP_DEFAULTS = {
    "foreign_flow_20d_z": 0.0,
    "trust_flow_20d_z": 0.0,
    "flow_momentum": 0.0,
    "foreign_momentum": 0.0,
    "trust_momentum": 0.0,
    "above_low_60d_pct": 50.0,
    "above_high_60d_pct": -5.0,
}


def _snap(stocks: list[dict]) -> pl.DataFrame:
    """每檔一列快照；缺欄補 _SNAP_DEFAULTS。date 統一給今天。"""
    rows = [{"date": date(2026, 6, 12), **_SNAP_DEFAULTS, **s} for s in stocks]
    return pl.DataFrame(rows)


def _confirm(mapping: dict[str, bool]) -> pl.DataFrame:
    return pl.DataFrame(
        {"stock_id": list(mapping), "confirm_buy": list(mapping.values())},
        schema={"stock_id": pl.Utf8, "confirm_buy": pl.Boolean},
    )


# ── latest_snapshot ───────────────────────────────────────────────────────────


def test_latest_snapshot_one_row_per_stock():
    d0 = date(2026, 6, 1)
    dd = [d0 + timedelta(days=i) for i in range(3)]
    panel = pl.DataFrame(
        {
            "stock_id": ["A", "A", "A", "B", "B"],
            "date": [dd[0], dd[1], dd[2], dd[0], dd[1]],
            "close": [10.0, 11.0, 12.0, 5.0, 6.0],
        }
    )
    snap = latest_snapshot(panel)
    assert snap.height == 2
    assert snap.filter(pl.col("stock_id") == "A")["close"][0] == 12.0
    assert snap.filter(pl.col("stock_id") == "B")["close"][0] == 6.0


def test_latest_snapshot_empty():
    assert latest_snapshot(pl.DataFrame()).is_empty()


# ── recent_buy_confirm（L3 第三關）────────────────────────────────────────────


def test_recent_buy_confirm_two_consecutive_buys():
    d = [date(2026, 6, 1) + timedelta(days=i) for i in range(3)]
    panel = pl.DataFrame({"stock_id": ["A"] * 3 + ["B"] * 3 + ["C"] * 3, "date": d * 3})
    inst = pl.DataFrame(
        {
            "stock_id": ["A", "A", "A", "B", "B", "B"],
            "date": d + d,
            # A：末兩日連續淨買 → 確認；B：末日賣超 → 不確認；C：完全無紀錄 → 補 0 → 不確認
            "total_net": [100, 200, 300, 100, 200, -50],
        }
    )
    out = recent_buy_confirm(panel, inst, confirm_days=2)
    m = dict(zip(out["stock_id"].to_list(), out["confirm_buy"].to_list()))
    assert m["A"] is True
    assert m["B"] is False
    assert m["C"] is False


def test_recent_buy_confirm_empty_institutional():
    d = [date(2026, 6, 1) + timedelta(days=i) for i in range(2)]
    panel = pl.DataFrame({"stock_id": ["A", "A"], "date": d})
    out = recent_buy_confirm(panel, pl.DataFrame(), confirm_days=2)
    assert out.filter(pl.col("stock_id") == "A")["confirm_buy"][0] is False


# ── build_cp_candidates ───────────────────────────────────────────────────────


def test_ambush_low_candidate_selected():
    snap = _snap(
        [
            {"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
             "flow_momentum": 100.0},  # 錢進 + 貼低 → 埋伏
            {"stock_id": "B", "foreign_flow_20d_z": 0.2, "above_low_60d_pct": 4.0},  # z 不足
            {"stock_id": "D", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 40.0},  # 已漲、非貼低
        ]
    )
    out = build_cp_candidates(snap, _confirm({"A": True, "B": True, "D": True}), RULES)
    assert out["stock_id"].to_list() == ["A"]
    row = out.row(0, named=True)
    assert row["labels"] == "埋伏"
    assert row["rules"] == "L1埋伏"
    assert row["flow_z"] == 1.0
    assert row["freshness"] == "加速"
    # cp_score = 1.0 × room(=1-4/60)，四捨五入 3 位
    assert row["cp_score"] == round(1 - 4 / 60, 3)


def test_reversal_requires_drawdown_and_confirm():
    base = {"stock_id": "R", "trust_flow_20d_z": 2.0, "trust_momentum": 50.0,
            "flow_momentum": 50.0, "above_low_60d_pct": 40.0}
    # (1) 深跌 + 確認 → 上榜
    ok = build_cp_candidates(
        _snap([{**base, "above_high_60d_pct": -25.0}]), _confirm({"R": True}), RULES
    )
    assert ok["stock_id"].to_list() == ["R"]
    assert ok.row(0, named=True)["labels"] == "反轉"
    assert ok.row(0, named=True)["confirm_buy"] is True
    # (2) 深跌但未確認（單日 spike）→ 剔除
    no_confirm = build_cp_candidates(
        _snap([{**base, "above_high_60d_pct": -25.0}]), _confirm({"R": False}), RULES
    )
    assert no_confirm.is_empty()
    # (3) 已確認但非深跌（距高僅 −10%）→ 剔除
    not_deep = build_cp_candidates(
        _snap([{**base, "above_high_60d_pct": -10.0}]), _confirm({"R": True}), RULES
    )
    assert not_deep.is_empty()


def test_multi_label_hit_lists_both():
    # trust z 高 + 貼低 → 同時命中 L2（貼低）；foreign z 高 → L1
    snap = _snap(
        [{"stock_id": "M", "foreign_flow_20d_z": 1.0, "trust_flow_20d_z": 2.0,
          "above_low_60d_pct": 5.0, "flow_momentum": 10.0}]
    )
    out = build_cp_candidates(snap, _confirm({"M": True}), RULES)
    row = out.row(0, named=True)
    assert "埋伏" in row["labels"] and "追突破" in row["labels"]
    assert "L1埋伏" in row["rules"] and "L2追突破" in row["rules"]


def test_ranks_by_cp_score_and_caps_per_label():
    # 同型態（皆埋伏）：依 cp_score 排序、每型態截斷
    snap = _snap(
        [
            {"stock_id": "HI", "foreign_flow_20d_z": 2.0, "above_low_60d_pct": 2.0},  # 高分
            {"stock_id": "LO", "foreign_flow_20d_z": 0.6, "above_low_60d_pct": 12.0},  # 低分
        ]
    )
    full = build_cp_candidates(snap, _confirm({"HI": True, "LO": True}), RULES)
    assert full["stock_id"].to_list() == ["HI", "LO"]  # 高分在前
    capped = build_cp_candidates(snap, _confirm({"HI": True, "LO": True}), RULES, max_candidates=1)
    assert capped["stock_id"].to_list() == ["HI"]


def test_cap_is_per_label_reversal_not_starved():
    # 高分埋伏 + 低分反轉；每型態取前 1 → 兩者都要在（全域 top-1 會漏掉反轉）
    snap = _snap(
        [
            {"stock_id": "AMB", "foreign_flow_20d_z": 3.0, "above_low_60d_pct": 2.0},  # 埋伏高分
            {"stock_id": "REV", "trust_flow_20d_z": 2.0, "trust_momentum": 9.0,
             "flow_momentum": 9.0, "above_low_60d_pct": 45.0,  # 離低 → cp_score 低
             "above_high_60d_pct": -25.0},  # 深跌
        ]
    )
    out = build_cp_candidates(snap, _confirm({"AMB": True, "REV": True}), RULES, max_candidates=1)
    assert set(out["stock_id"].to_list()) == {"AMB", "REV"}
    labels = dict(zip(out["stock_id"].to_list(), out["primary_label"].to_list()))
    assert labels["AMB"] == "ambush" and labels["REV"] == "reversal"


def test_build_empty_inputs():
    assert build_cp_candidates(pl.DataFrame(), _confirm({}), RULES).is_empty()
    assert build_cp_candidates(_snap([{"stock_id": "A"}]), _confirm({"A": True}), []).is_empty()


# ── 疊圖：象限 / 持有狀態 ─────────────────────────────────────────────────────


def test_attach_subind_quadrant(tmp_path):
    cands = pl.DataFrame({"stock_id": ["A", "Z"]})
    membership = pl.DataFrame({"sub_industry": ["半導體"], "stock_id": ["A"]})
    rot = tmp_path / "sector_rotation.csv"
    pl.DataFrame({"sub_industry": ["半導體"], "quadrant": ["下一棒"]}).write_csv(rot)
    out = attach_subind_quadrant(cands, membership, rot)
    m = dict(zip(out["stock_id"].to_list(), out["subind_quadrant"].to_list()))
    assert m["A"] == "半導體(下一棒)"
    assert m["Z"] == "未標次產業"


def test_attach_subind_quadrant_no_rotation_csv(tmp_path):
    cands = pl.DataFrame({"stock_id": ["A"]})
    membership = pl.DataFrame({"sub_industry": ["半導體"], "stock_id": ["A"]})
    out = attach_subind_quadrant(cands, membership, tmp_path / "missing.csv")
    assert out["subind_quadrant"][0] == "—"  # 無快照可對 → 誠實降級


def test_attach_valuation_triple_filter_states():
    cands = pl.DataFrame({"stock_id": ["CHEAP", "RICH", "PB", "NOVAL"]})
    valuation = pl.DataFrame(
        {
            "stock_id": ["CHEAP", "RICH", "PB"],  # NOVAL 缺估值
            "pe": [8.0, 30.0, None],   # PB 為虧損股無 PE，退用 PBR
            "pbr": [1.2, 3.0, 0.7],
            "val_metric": ["PE", "PE", "PB"],
            "val_pctile": [10.0, 80.0, 12.0],  # ≤30 便宜（含 PB 補的虧損股）；>30 不便宜
        }
    )
    out = attach_valuation(cands, valuation, cheap_pctile=30.0)
    m = dict(zip(out["stock_id"].to_list(), out["triple_filter"].to_list()))
    assert m["CHEAP"] == "✓三重"
    assert m["RICH"] == "—"
    assert m["PB"] == "✓三重"  # 虧損股以 PB 補也能過三重濾網
    assert m["NOVAL"] == "估值缺"  # 無相對位階 → 誠實標未知、不剔除
    # 新增欄齊全
    assert {"pe", "pbr", "val_metric", "val_pctile", "triple_filter"} <= set(out.columns)


def test_attach_valuation_empty_valuation_marks_all_unknown():
    cands = pl.DataFrame({"stock_id": ["A", "B"]})
    out = attach_valuation(cands, pl.DataFrame(), cheap_pctile=30.0)
    assert out["triple_filter"].to_list() == ["估值缺", "估值缺"]


def test_tag_holding_status():
    cands = pl.DataFrame({"stock_id": ["A", "B", "C", "D"]})
    out = tag_holding_status(cands, holdings=["A"], watch=["B"], hits=["A", "C"])
    m = dict(zip(out["stock_id"].to_list(), out["watch_status"].to_list()))
    assert m["A"] == "庫存/本週命中"
    assert m["B"] == "觀察"
    assert m["C"] == "本週命中"
    assert m["D"] == ""


# ── render ────────────────────────────────────────────────────────────────────


def _coverage() -> dict:
    return {"universe": "listed", "n_stocks": 100, "n_trading_days": 250,
            "date_max": "2026-06-12", "inst_coverage_pct": 88.0, "subind_coverage_pct": 60.0}


def test_render_writes_md_and_csv(tmp_path):
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
                   "flow_momentum": 100.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    cands = attach_subind_quadrant(cands, pl.DataFrame({"sub_industry": ["半導體"],
                                                        "stock_id": ["A"]}),
                                   tmp_path / "missing.csv")
    cands = tag_holding_status(cands, holdings=["A"], watch=[], hits=[])
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={"A": "台積電"}, data_date="2026-06-12",
    )
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "台積電" in text and "埋伏" in text
    assert "觀察清單" in text  # 人設但書
    csv = pl.read_csv(tmp_path / "cp_candidates.csv")
    assert "stock_name" in csv.columns and csv["stock_name"][0] == "台積電"


def test_render_with_valuation_shows_triple_filter(tmp_path):
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
                   "flow_momentum": 100.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    cands = attach_subind_quadrant(cands, pl.DataFrame({"sub_industry": ["半導體"],
                                                        "stock_id": ["A"]}),
                                   tmp_path / "missing.csv")
    cands = tag_holding_status(cands, holdings=[], watch=[], hits=[])
    valuation = pl.DataFrame(
        {"stock_id": ["A"], "pe": [8.0], "pbr": [1.5],
         "val_metric": ["PE"], "val_pctile": [5.0]}
    )
    cands = attach_valuation(cands, valuation, cheap_pctile=30.0)
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={"A": "台積電"}, data_date="2026-06-12",
    )
    text = md.read_text(encoding="utf-8")
    assert "三重濾網全過 1" in text  # 表頭統計
    assert "✓三重" in text and "三重 |" in text  # 表格欄
    assert "PE8.0" in text  # 估值欄顯示鏡頭+值
    csv = pl.read_csv(tmp_path / "cp_candidates.csv")
    assert {"pe", "pbr", "val_metric", "val_pctile", "triple_filter"} <= set(csv.columns)


def test_render_without_valuation_unchanged(tmp_path):
    # 未疊估值（B3 原路徑）→ 不出三重欄，保持向後相容
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    cands = attach_subind_quadrant(cands, pl.DataFrame(schema={"sub_industry": pl.Utf8,
                                                               "stock_id": pl.Utf8}),
                                   tmp_path / "missing.csv")
    cands = tag_holding_status(cands, holdings=[], watch=[], hits=[])
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={}, data_date="2026-06-12",
    )
    text = md.read_text(encoding="utf-8")
    assert "三重" not in text and "三重濾網全過" not in text


def test_render_empty_candidates(tmp_path):
    md = render_cp_candidates_report(
        pl.DataFrame(), "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={}, data_date="2026-06-12",
    )
    assert "本週無" in md.read_text(encoding="utf-8")
    assert (tmp_path / "cp_candidates.csv").exists()
