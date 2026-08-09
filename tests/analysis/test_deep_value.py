"""M5 深值成長 tag 測試（委託書 M5）。"""

from __future__ import annotations

from tw_screener.analysis.valuation import deep_value_growth


def _ok(**over):
    base = dict(
        val_pctile=8.0, rev_yoy_pct=45.0, gross_margin_pct=31.0,
        ma60_dist_pct=-4.0, base_zone="",
    )
    base.update(over)
    return deep_value_growth(**base)


def test_all_four_conditions_required():
    assert _ok() is True
    assert _ok(val_pctile=55.0) is False        # 不便宜
    assert _ok(rev_yoy_pct=12.0) is False       # 沒在成長＝低估值陷阱
    assert _ok(gross_margin_pct=9.0) is False   # 薄毛利＝便宜得有道理，非機會
    assert _ok(ma60_dist_pct=22.0) is False     # 位階已延伸（且非貼底）


def test_position_branch_is_or_not_and():
    """位階＝距季線<0 **或** base_zone=貼底；兩條都是 MA60 口徑，無 M4.2 的混用問題。"""
    assert _ok(ma60_dist_pct=6.0, base_zone="貼底") is True
    assert _ok(ma60_dist_pct=6.0, base_zone="") is False
    assert _ok(ma60_dist_pct=None, base_zone="貼底") is True


def test_boundaries_are_inclusive_as_written():
    """門檻語意＝「≤20% ∧ ≥30% ∧ ≥25%」，邊界值算命中。"""
    assert _ok(val_pctile=20.0, rev_yoy_pct=30.0, gross_margin_pct=25.0) is True
    assert _ok(val_pctile=20.01) is False
    assert _ok(rev_yoy_pct=29.99) is False
    assert _ok(gross_margin_pct=24.99) is False


def test_missing_never_hits():
    """缺資料不放行（同 M1／M4）——金融業與缺表者無毛利率，不該被當成命中。"""
    assert _ok(val_pctile=None) is False
    assert _ok(rev_yoy_pct=None) is False
    assert _ok(gross_margin_pct=None) is False
    # 位階兩條都缺 → 不命中
    assert _ok(ma60_dist_pct=None, base_zone=None) is False


def test_thresholds_configurable():
    assert _ok(val_pctile=35.0, max_pctile=40.0) is True
    assert _ok(rev_yoy_pct=15.0, min_yoy_pct=10.0) is True
    assert _ok(gross_margin_pct=15.0, min_gross_margin_pct=10.0) is True
