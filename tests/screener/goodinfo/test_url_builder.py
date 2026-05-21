"""tests/screener/goodinfo/test_url_builder.py — url_builder 單元測試（全離線）。"""

from pathlib import Path
from urllib.parse import unquote

from tw_screener.screener.goodinfo.url_builder import (
    FilterCondition,
    StrategyConfig,
    build_screener_url,
    load_strategy,
)

STRATEGY_PATH = Path("config/strategies/a_breakout.yaml")
BASE_URL = "https://goodinfo.tw/tw"


# ─── load_strategy ────────────────────────────────────────────────────────────


def test_load_strategy_id():
    s = load_strategy(STRATEGY_PATH)
    assert s.id == "a_breakout"


def test_load_strategy_name():
    s = load_strategy(STRATEGY_PATH)
    assert s.name == "動能突破"


def test_load_strategy_filters_count():
    s = load_strategy(STRATEGY_PATH)
    assert len(s.filters) == 1


def test_load_strategy_rules_count():
    s = load_strategy(STRATEGY_PATH)
    assert len(s.rules) == 2


def test_load_strategy_filter_no_period():
    """period 欄位是選填，a_breakout 的過濾條件不使用 period。"""
    s = load_strategy(STRATEGY_PATH)
    assert s.filters[0].period is None


def test_load_strategy_g_growth_pullback_empty_rules():
    """G 策略：3 個基本面 filter、rules 為空（拉回 timing 在分析層），schema 通過。"""
    s = load_strategy(Path("config/strategies/g_growth_pullback.yaml"))
    assert s.id == "g_growth_pullback"
    assert s.name == "成長拉回"
    assert len(s.filters) == 3
    assert s.rules == []  # 不放技術 rule
    # 與 E 相同的三個基本面 item
    items = {f.item for f in s.filters}
    assert "市值 (億元)" in items
    assert "累計月營收年增減率(%)" in items


def test_build_url_empty_rules_no_fl_rule():
    """rules 為空時不產生任何 FL_RULE 參數。"""
    s = load_strategy(Path("config/strategies/g_growth_pullback.yaml"))
    url = build_screener_url(s, BASE_URL)
    assert "FL_RULE0" not in unquote(url)


def test_load_strategy_filter_min():
    s = load_strategy(STRATEGY_PATH)
    assert s.filters[0].min == 15000


def test_load_strategy_filter_min_only():
    """成交筆數 filter 只有 min，沒有 max。"""
    s = load_strategy(STRATEGY_PATH)
    assert s.filters[0].max is None


# ─── FilterCondition ──────────────────────────────────────────────────────────


def test_filter_condition_with_period():
    f = FilterCondition(item="成交筆數", period="日", min=5000)
    assert f.period == "日"
    assert f.min == 5000


def test_filter_condition_no_period():
    f = FilterCondition(item="月成交均量", min=1000)
    assert f.period is None


def test_filter_condition_min_max():
    f = FilterCondition(item="股價", min=15, max=300)
    assert f.min == 15
    assert f.max == 300


# ─── build_screener_url ───────────────────────────────────────────────────────


def test_url_contains_stock_list():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    assert "StockList.asp" in url


def test_url_contains_market_cat():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "MARKET_CAT" in decoded
    assert "自訂篩選" in decoded


def test_url_contains_filter_items():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    assert "FL_ITEM0" in url


def test_url_filter_item_no_period_suffix():
    """period 欄位不應附加到 item name（Goodinfo 篩選項目名稱為完整字串）。"""
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "成交筆數" in decoded
    assert "成交筆數–日" not in decoded  # period 不應附加


def test_url_contains_min_value():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "15000" in decoded  # 成交筆數 min


def test_url_contains_rules():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    assert "FL_RULE0" in url
    assert "FL_RULE1" in url


def test_url_rules_use_goodinfo_format():
    """Rules must use Goodinfo format: category||value@@display1@@display2."""
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "MACD||" in decoded
    assert "@@" in decoded


def test_url_contains_display_sheet():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "交易狀況" in decoded


def test_url_contains_market():
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "上市/上櫃" in decoded


def test_url_empty_max_for_min_only_filter():
    """min-only filter 的 FL_VAL_E 應為空字串（FL_VAL_E0 = 成交筆數，無 max）。"""
    s = load_strategy(STRATEGY_PATH)
    url = build_screener_url(s, BASE_URL)
    assert "FL_VAL_E0=" in url


def test_url_build_minimal_strategy():
    s = StrategyConfig(
        id="test",
        name="Test",
        description="Test strategy",
        market="上市",
        filters=[FilterCondition(item="股價", min=10, max=100)],
        rules=["規則1"],
        display_sheet="交易狀況",
        display_period="日",
    )
    url = build_screener_url(s, BASE_URL)
    decoded = unquote(url)
    assert "股價" in decoded
    assert "10" in decoded
    assert "100" in decoded
    assert "規則1" in decoded
