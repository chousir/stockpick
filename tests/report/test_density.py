"""資料密度誠實註記（規劃書 02 D2）。"""

from tw_screener.report.density import (
    CALIB_TARGET_DAYS,
    Z_MIN_DAYS,
    data_density_note,
)


def test_density_note_sufficient():
    note = data_density_note(CALIB_TARGET_DAYS + 10)
    assert "充足" in note
    assert str(CALIB_TARGET_DAYS + 10) in note


def test_density_note_medium_band():
    note = data_density_note(Z_MIN_DAYS + 5)
    assert "中等" in note
    assert "統計意義有限" not in note
    assert str(Z_MIN_DAYS + 5) in note


def test_density_note_insufficient():
    note = data_density_note(Z_MIN_DAYS - 1)
    assert "統計意義有限" in note
    assert str(Z_MIN_DAYS - 1) in note


def test_density_note_boundaries():
    # 邊界：恰達 z 門檻＝中等；恰達校準窗＝充足
    assert "中等" in data_density_note(Z_MIN_DAYS)
    assert "充足" in data_density_note(CALIB_TARGET_DAYS)
