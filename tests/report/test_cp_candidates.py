"""個股 CP 候選清單測試（B3，docs/13 §4 Phase B）：全離線合成資料、不打外部。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

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


def test_zero_hit_week_keeps_schema_and_renders(tmp_path: Path):
    """零命中週回歸：空表必須帶 schema，下游疊圖＋渲染不崩（W25–W27 無聲斷供根因）。"""
    snap = _snap([{"stock_id": "A"}])  # 全欄預設值 → 三規則皆不命中
    out = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    assert out.is_empty()
    assert "stock_id" in out.columns
    out = attach_subind_quadrant(out, pl.DataFrame(), tmp_path / "no_rotation.csv")
    out = tag_holding_status(out, ["2330"], ["2610"], ["8016"])  # 有庫存/觀察→走逐列路徑
    md = render_cp_candidates_report(
        out, week_tag="2026-W99", output_dir=tmp_path,
        params={"z_min_periods": 30},
        coverage={"n_stocks": 1, "n_trading_days": 18, "inst_coverage_pct": 0.0},
        rules=RULES,
    )
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "本週候選 **0** 檔" in text
    assert "暖機期" in text  # 面板 18 日 < z_min_periods 30 → 標「資料未成熟」非「無訊號」


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


# ── 族群內落後濾鏡（docs/17 C-P2 生產化）─────────────────────────────────────

_LAG = dict(laggard_labels=("ambush",), laggard_boost=0.3, laggard_threshold=0.0)


def test_laggard_boosts_ambush_and_discloses():
    # 同 z/位階的埋伏：落後其族群者 cp_score 輕加權＋揭露；領先者僅揭露不加權
    snap = _snap(
        [
            {"stock_id": "LAG", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
             "rs_subind_20d": -3.0},
            {"stock_id": "LEAD", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
             "rs_subind_20d": 2.0},
        ]
    )
    out = build_cp_candidates(snap, _confirm({"LAG": True, "LEAD": True}), RULES, **_LAG)
    rows = {r["stock_id"]: r for r in out.iter_rows(named=True)}
    base = round(1.0 * (1 - 4 / 60), 3)
    assert rows["LAG"]["cp_boosted"] is True
    assert rows["LAG"]["cp_score"] == round(base * 1.3, 3)
    assert rows["LAG"]["rs_subind"] == -3.0
    assert rows["LEAD"]["cp_boosted"] is False
    assert rows["LEAD"]["cp_score"] == base
    assert rows["LEAD"]["rs_subind"] == 2.0


def test_laggard_boost_reorders_within_type():
    # 落後股 z 略低於領先股，加權後仍浮上型態前；未加權則領先 z 高在前
    snap = _snap(
        [
            {"stock_id": "LAG", "foreign_flow_20d_z": 1.4, "above_low_60d_pct": 4.0,
             "rs_subind_20d": -2.0},
            {"stock_id": "LEAD", "foreign_flow_20d_z": 1.5, "above_low_60d_pct": 4.0,
             "rs_subind_20d": 1.0},
        ]
    )
    ids = _confirm({"LAG": True, "LEAD": True})
    assert build_cp_candidates(snap, ids, RULES, **_LAG)["stock_id"].to_list() == ["LAG", "LEAD"]
    assert build_cp_candidates(snap, ids, RULES)["stock_id"].to_list() == ["LEAD", "LAG"]


def test_laggard_only_applies_to_apply_to_labels():
    # breakout 落後股：揭露 rs_subind 但不加權（D2：加權只作用 ambush）
    snap = _snap(
        [{"stock_id": "BRK", "trust_flow_20d_z": 1.2, "above_low_60d_pct": 5.0,
          "rs_subind_20d": -3.0}]
    )
    row = build_cp_candidates(snap, _confirm({"BRK": True}), RULES, **_LAG).row(0, named=True)
    assert row["primary_label"] == "breakout"
    assert row["cp_boosted"] is False
    assert row["rs_subind"] == -3.0  # 仍揭露
    assert row["cp_score"] == round(1.2 * (1 - 5 / 60), 3)  # 原分不變


def test_laggard_missing_rs_column_graceful():
    # 面板無 rs_subind 欄 → 不報錯、不加權、不出揭露欄
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0}])
    out = build_cp_candidates(snap, _confirm({"A": True}), RULES, **_LAG)
    assert "rs_subind" not in out.columns and "cp_boosted" not in out.columns
    assert out.row(0, named=True)["cp_score"] == round(1 - 4 / 60, 3)


def test_laggard_disabled_keeps_legacy_columns():
    # 預設停用（boost=0/labels 空）→ 完全回退、無新欄（向後相容）
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
                   "rs_subind_20d": -3.0}])
    out = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    assert "rs_subind" not in out.columns and "cp_boosted" not in out.columns


def test_build_handles_nulls_before_floats_over_infer_window():
    # 回歸：>100 列候選、rs_subind 前段全 None、後段才出現浮點時，
    # pl.DataFrame(rows) 預設 infer_schema_length=100 會把該欄推成 Null 型而崩。
    # 修法＝infer_schema_length=None 掃全列（順修自 make week 主流程的 ComputeError）。
    n = 120
    ids = [f"S{i:03d}" for i in range(n)]
    rs = [None] * 110 + [-3.0] * (n - 110)  # 前 110 檔 rs 缺、最後 10 檔才有落後值
    snap = pl.DataFrame(
        {
            "stock_id": ids,
            "date": [date(2026, 6, 12)] * n,
            "foreign_flow_20d_z": [1.0] * n,
            "trust_flow_20d_z": [0.0] * n,
            "flow_momentum": [0.0] * n,
            "trust_momentum": [0.0] * n,
            "foreign_momentum": [0.0] * n,
            "above_low_60d_pct": [4.0] * n,
            "above_high_60d_pct": [-5.0] * n,
            "rs_subind_20d": rs,
        },
        schema_overrides={"rs_subind_20d": pl.Float64},
    )
    out = build_cp_candidates(
        snap, _confirm({sid: True for sid in ids}), RULES, max_candidates=0, **_LAG
    )
    assert out.height == n  # 不崩、全數產出
    assert out.schema["rs_subind"] == pl.Float64
    assert -3.0 in out["rs_subind"].to_list()  # 後段落後值有保留


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


def test_render_shows_laggard_column_and_mark(tmp_path):
    # 族群內欄＋🔻（被加權的落後埋伏）；CSV 帶 rs_subind/cp_boosted
    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0,
                   "flow_momentum": 100.0, "rs_subind_20d": -3.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES,
                                laggard_labels=("ambush",), laggard_boost=0.3)
    cands = attach_subind_quadrant(cands, pl.DataFrame({"sub_industry": ["半導體"],
                                                        "stock_id": ["A"]}),
                                   tmp_path / "missing.csv")
    cands = tag_holding_status(cands, holdings=[], watch=[], hits=[])
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={"A": "台積電"}, data_date="2026-06-12",
    )
    text = md.read_text(encoding="utf-8")
    assert "族群內" in text and "-3.0🔻" in text
    csv = pl.read_csv(tmp_path / "cp_candidates.csv")
    assert {"rs_subind", "cp_boosted"} <= set(csv.columns)


def test_render_without_laggard_unchanged(tmp_path):
    # 未啟用落後濾鏡 → 不出族群內欄（向後相容）
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
    assert "族群內" not in md.read_text(encoding="utf-8")


def test_render_empty_candidates(tmp_path):
    md = render_cp_candidates_report(
        pl.DataFrame(), "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={}, data_date="2026-06-12",
    )
    assert "本週無" in md.read_text(encoding="utf-8")
    assert (tmp_path / "cp_candidates.csv").exists()


# ── M-MH Phase 3：短窗早訊號加值欄 ──────────────────────────────────────────────


def _early_snap(stocks: list[dict]) -> pl.DataFrame:
    """早訊號用快照：含短/長窗 z + flow_decel + 距低欄；缺欄補預設（同 schema）。"""
    defaults = {
        "foreign_flow_5d_z": 0.0, "foreign_flow_20d_z": 0.0,
        "net_flow_5d_z": 0.0, "net_flow_20d_z": 0.0,
        "flow_decel": 1.0, "above_low_60d_pct": 30.0,
    }
    rows = [{"date": date(2026, 6, 12), "stock_id": s["stock_id"], **defaults, **s} for s in stocks]
    return pl.DataFrame(rows)


def test_early_inflow_flags_short_strong_long_silent():
    from tw_screener.report.cp_candidates import compute_early_inflow

    snap = _early_snap([
        {"stock_id": "A", "foreign_flow_5d_z": 2.0, "foreign_flow_20d_z": 0.0},   # 早訊號
        {"stock_id": "B", "foreign_flow_5d_z": 2.0, "foreign_flow_20d_z": 1.0},   # 20d 已追上→否
        {"stock_id": "C", "foreign_flow_5d_z": 2.0, "foreign_flow_20d_z": 0.0,
         "flow_decel": -1.0},                                                      # 減速→否
        {"stock_id": "D", "foreign_flow_5d_z": 0.3, "foreign_flow_20d_z": 0.0},   # 短窗 z 不足→否
    ])
    out = compute_early_inflow(
        snap, prefixes=("foreign_flow",), z_threshold=1.0, long_z_ceiling=0.5, decel_floor=0.0
    )
    assert out["stock_id"].to_list() == ["A"]
    r = out.row(0, named=True)
    assert r["early_signal"] == "foreign_flow_5d_z"
    assert r["early_short_z"] == 2.0 and r["early_long_z"] == 0.0


def test_early_inflow_missing_decel_returns_empty():
    from tw_screener.report.cp_candidates import compute_early_inflow

    snap = pl.DataFrame({"date": [date(2026, 6, 12)], "stock_id": ["A"],
                         "foreign_flow_5d_z": [2.0], "foreign_flow_20d_z": [0.0]})
    assert compute_early_inflow(snap).is_empty()  # 無 flow_decel 欄 → 誠實回空


def test_early_inflow_watch_filters_to_tracked_non_candidates():
    from tw_screener.report.cp_candidates import build_early_inflow_watch, compute_early_inflow

    snap = _early_snap([
        {"stock_id": "HOLD", "foreign_flow_5d_z": 2.0},
        {"stock_id": "WATCH", "foreign_flow_5d_z": 1.5},
        {"stock_id": "CAND", "foreign_flow_5d_z": 2.5},   # 早訊號但已是候選→排除
        {"stock_id": "OTHER", "foreign_flow_5d_z": 2.0},  # 早訊號但不在清單→排除
    ])
    early = compute_early_inflow(snap, prefixes=("foreign_flow",), z_threshold=1.0)
    watch = build_early_inflow_watch(
        early, holdings=["HOLD"], watch=["WATCH"], candidate_ids={"CAND"}
    )
    assert set(watch["stock_id"].to_list()) == {"HOLD", "WATCH"}
    smap = dict(zip(watch["stock_id"].to_list(), watch["watch_status"].to_list()))
    assert smap["HOLD"] == "庫存" and smap["WATCH"] == "觀察"
    assert watch["stock_id"].to_list()[0] == "HOLD"  # 依短窗 z 遞減（2.0 > 1.5）


def test_early_inflow_watch_empty():
    from tw_screener.report.cp_candidates import build_early_inflow_watch

    assert build_early_inflow_watch(pl.DataFrame(), [], [], set()).is_empty()


def test_render_includes_early_inflow_section(tmp_path):
    from tw_screener.report.cp_candidates import build_early_inflow_watch, compute_early_inflow

    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    cands = attach_subind_quadrant(
        cands, pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8}),
        tmp_path / "missing.csv",
    )
    cands = tag_holding_status(cands, holdings=[], watch=[], hits=[])
    early_snap = _early_snap([{"stock_id": "HOLD", "foreign_flow_5d_z": 2.0}])
    early = compute_early_inflow(early_snap, prefixes=("foreign_flow",), z_threshold=1.0)
    ew = build_early_inflow_watch(early, holdings=["HOLD"], watch=[], candidate_ids=set())
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={"HOLD": "測試股"}, data_date="2026-06-12", early_watch=ew,
    )
    text = md.read_text(encoding="utf-8")
    assert "短窗早訊號" in text and "HOLD" in text and "未證實更早或更準" in text


# ── 精修・點 5：過熱-退潮警示 ───────────────────────────────────────────────────


def _oh_snap(stocks: list[dict]) -> pl.DataFrame:
    """過熱用快照：距高/flow_decel/price_flow_div_5d/volume_z_5d/ret_5d；缺欄補預設。"""
    defaults = {
        "above_high_60d_pct": -2.0,   # 已逼近高（>= -near_high_pct）
        "flow_decel": -1.0,           # 減速
        "price_flow_div_5d": 1.0,     # 量價背離
        "volume_z_5d": 0.5,
        "ret_5d": 6.0,
    }
    rows = [{"date": date(2026, 6, 12), "stock_id": s["stock_id"], **defaults, **s} for s in stocks]
    return pl.DataFrame(rows)


def test_overheat_flags_near_high_decel_diverge():
    from tw_screener.report.cp_candidates import compute_overheat_warning

    snap = _oh_snap([
        {"stock_id": "A"},                                     # 高位＋減速＋背離→警示
        {"stock_id": "B", "above_high_60d_pct": -40.0},        # 離高遠→否
        {"stock_id": "C", "flow_decel": 5.0},                  # 未減速→否
        {"stock_id": "D", "price_flow_div_5d": -1.0, "volume_z_5d": 1.0},  # 無背離無量縮→否
        {"stock_id": "E", "price_flow_div_5d": -1.0, "volume_z_5d": -0.5},  # 量縮→警示
    ])
    out = compute_overheat_warning(snap, near_high_pct=8.0, div_floor=0.0, vol_contract_floor=0.0)
    assert set(out["stock_id"].to_list()) == {"A", "E"}
    a = out.filter(pl.col("stock_id") == "A").row(0, named=True)
    assert "短窗減速" in a["reasons"] and "量價背離" in a["reasons"]
    e = out.filter(pl.col("stock_id") == "E").row(0, named=True)
    assert "量縮" in e["reasons"]


def test_overheat_missing_cols_returns_empty():
    from tw_screener.report.cp_candidates import compute_overheat_warning

    snap = pl.DataFrame({"date": [date(2026, 6, 12)], "stock_id": ["A"],
                         "above_high_60d_pct": [-2.0]})  # 缺 flow_decel 等
    assert compute_overheat_warning(snap).is_empty()


def test_overheat_watch_filters_to_tracked():
    from tw_screener.report.cp_candidates import build_overheat_watch, compute_overheat_warning

    snap = _oh_snap([{"stock_id": "HOLD"}, {"stock_id": "WATCH"}, {"stock_id": "OTHER"}])
    oh = compute_overheat_warning(snap)
    watch = build_overheat_watch(oh, holdings=["HOLD"], watch=["WATCH"])
    assert set(watch["stock_id"].to_list()) == {"HOLD", "WATCH"}
    smap = dict(zip(watch["stock_id"].to_list(), watch["watch_status"].to_list()))
    assert smap["HOLD"] == "庫存" and smap["WATCH"] == "觀察"


def test_overheat_watch_empty():
    from tw_screener.report.cp_candidates import build_overheat_watch

    assert build_overheat_watch(pl.DataFrame(), [], []).is_empty()


def test_render_includes_overheat_section(tmp_path):
    from tw_screener.report.cp_candidates import build_overheat_watch, compute_overheat_warning

    snap = _snap([{"stock_id": "A", "foreign_flow_20d_z": 1.0, "above_low_60d_pct": 4.0}])
    cands = build_cp_candidates(snap, _confirm({"A": True}), RULES)
    cands = attach_subind_quadrant(
        cands, pl.DataFrame(schema={"sub_industry": pl.Utf8, "stock_id": pl.Utf8}),
        tmp_path / "missing.csv",
    )
    cands = tag_holding_status(cands, holdings=[], watch=[], hits=[])
    oh = compute_overheat_warning(_oh_snap([{"stock_id": "HOLD"}]))
    ow = build_overheat_watch(oh, holdings=["HOLD"], watch=[])
    md = render_cp_candidates_report(
        cands, "2026-W24", tmp_path,
        params={"drawdown_pct": 20.0, "confirm_days": 2}, coverage=_coverage(),
        rules=RULES, names={"HOLD": "測試股"}, data_date="2026-06-12", overheat_watch=ow,
    )
    text = md.read_text(encoding="utf-8")
    assert "過熱-退潮警示" in text and "HOLD" in text and "未校準" in text


def test_watch_flags_default_off_in_shipped_settings():
    """A3：出貨的 config/settings.yaml 兩個未校準區塊預設關——make week 不背它們（關態）。

    開態行為由 test_render_includes_{early_inflow,overheat}_section 覆蓋（傳入 watch
    DataFrame → 區塊照常出現），test_render_without_*_unchanged 覆蓋 None（不出區塊）。
    """
    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((repo_root / "config" / "settings.yaml").read_text(encoding="utf-8"))
    cp = cfg["cp_value"]
    assert cp["early_watch"]["enabled"] is False
    assert cp["overheat_watch"]["enabled"] is False
