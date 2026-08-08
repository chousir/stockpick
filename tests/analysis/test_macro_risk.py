"""M8 宏觀窄橋測試（委託書 M8・裁決 D）。

重點在**三態容錯**與**分母口徑**：檔缺席/過期/格式錯一律降級為註記不當 gate；
`of` 是「已求值項數」不是常數 7（docs/26 §6.2(5)），薄 coverage 時「低觸發」必須帶警語。
"""

from __future__ import annotations

from datetime import date

import yaml

from tw_screener.analysis.macro_risk import (
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_STALE,
    load_macro_risk,
    macro_risk_gate,
    parse_macro_risk,
    to_disclosure,
    weekday_gap,
)

_AS_OF = date(2026, 8, 10)  # 週一


def _payload(**over) -> dict:
    block = {
        "date": "2026-08-07",
        "triggers_hit": 2,
        "of": 7,
        "hits": ["HY_spread", "margin_debt_mom"],
        "vix": 18.3,
        "fng": 41,
    }
    block.update(over)
    return {"macro_risk": block}


def test_weekday_gap_skips_weekends():
    assert weekday_gap(date(2026, 8, 7), date(2026, 8, 10)) == 1  # 五 → 一
    assert weekday_gap(date(2026, 8, 3), date(2026, 8, 10)) == 5
    assert weekday_gap(date(2026, 8, 10), date(2026, 8, 10)) == 0
    assert weekday_gap(date(2026, 8, 12), date(2026, 8, 10)) == 0  # 未來日不算負


def test_parse_ok_and_fields():
    r = parse_macro_risk(_payload(), _AS_OF)
    assert r.status == STATUS_OK and r.usable
    assert r.date == date(2026, 8, 7)
    assert (r.triggers_hit, r.of) == (2, 7)
    assert r.hits == ["HY_spread", "margin_debt_mom"]
    assert (r.vix, r.fng) == (18.3, 41.0)


def test_parse_stale_when_beyond_threshold():
    r = parse_macro_risk(_payload(date="2026-07-20"), _AS_OF, stale_trading_days=5)
    assert r.status == STATUS_STALE and not r.usable
    assert "過期不當 gate" in r.detail
    assert r.triggers_hit is None  # 非 ok 狀態不半信半疑地給數值


def test_parse_invalid_shapes():
    assert parse_macro_risk("not a mapping", _AS_OF).status == STATUS_INVALID
    assert parse_macro_risk({}, _AS_OF).status == STATUS_INVALID
    assert parse_macro_risk(_payload(date="08/07/2026"), _AS_OF).status == STATUS_INVALID
    assert parse_macro_risk(_payload(triggers_hit=None), _AS_OF).status == STATUS_INVALID
    # hit > of ＝不成立的分數，不可當 ok
    assert parse_macro_risk(_payload(triggers_hit=9, of=7), _AS_OF).status == STATUS_INVALID
    assert parse_macro_risk(_payload(of=0), _AS_OF).status == STATUS_INVALID


def test_load_missing_file(tmp_path):
    r = load_macro_risk(tmp_path / "nope.yaml", _AS_OF)
    assert r.status == STATUS_MISSING and not r.usable


def test_load_broken_yaml(tmp_path):
    p = tmp_path / "macro_risk_latest.yaml"
    p.write_text("macro_risk: {date: [unclosed", encoding="utf-8")
    assert load_macro_risk(p, _AS_OF).status == STATUS_INVALID


def test_load_roundtrip(tmp_path):
    p = tmp_path / "macro_risk_latest.yaml"
    p.write_text(yaml.safe_dump(_payload()), encoding="utf-8")
    assert load_macro_risk(p, _AS_OF).status == STATUS_OK


# ── patch-6 消費規則 ─────────────────────────────────────────────────────


def test_gate_fires_at_min_hits():
    r = parse_macro_risk(_payload(triggers_hit=3), _AS_OF)
    g = macro_risk_gate(r, min_hits=3, cap="1/3")
    assert g.downgrade_posture is True
    assert g.new_position_cap == "1/3"
    assert "3/7" in g.note and "不改選股" in g.note


def test_gate_low_hits_is_note_only():
    g = macro_risk_gate(parse_macro_risk(_payload(triggers_hit=2), _AS_OF), min_hits=3)
    assert g.downgrade_posture is False and g.new_position_cap is None
    assert "僅決策卡註記" in g.note


def test_gate_thin_coverage_warns_on_low_hits():
    """docs/26 §2 圍欄：分母薄時，「0–2 觸發」不得讀成風險已清。"""
    g = macro_risk_gate(parse_macro_risk(_payload(triggers_hit=1, of=2), _AS_OF), min_coverage=5)
    assert g.downgrade_posture is False
    assert "不等於風險已清" in g.note
    # coverage 足夠時不加這句
    ok = macro_risk_gate(parse_macro_risk(_payload(triggers_hit=1, of=7), _AS_OF), min_coverage=5)
    assert "不等於風險已清" not in ok.note


def test_gate_fires_even_with_thin_coverage():
    """未求值項只會少算觸發數 → ≥3 命中在任何 coverage 下都是保守安全的，照樣 gate。"""
    g = macro_risk_gate(parse_macro_risk(_payload(triggers_hit=3, of=3), _AS_OF), min_hits=3)
    assert g.downgrade_posture is True


def test_gate_never_tightens_or_loosens_when_unusable():
    for r in (
        parse_macro_risk(_payload(date="2026-06-01"), _AS_OF),   # stale
        parse_macro_risk({}, _AS_OF),                             # invalid
    ):
        g = macro_risk_gate(r)
        assert g.downgrade_posture is False and g.new_position_cap is None
        assert "不當 gate" in g.note


def test_disclosure_keys_are_fixed():
    d = to_disclosure(parse_macro_risk(_payload(), _AS_OF))
    assert set(d) == {"status", "date", "detail"}
    assert d["status"] == STATUS_OK and d["date"] == "2026-08-07"
