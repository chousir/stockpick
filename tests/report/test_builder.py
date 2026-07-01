"""tests/report/test_builder.py — 報告 builder 配置化與截斷偵測（規劃書 04 A7）。"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from tw_screener.report.builder import (
    _DEFAULT_LLM,
    _build_data_draft,
    _resolve_llm_cfg,
    _warn_if_truncated,
)


def _capture_warnings(func: Callable[[], None]) -> list[str]:
    """以 loguru 自帶 sink 攔截 WARNING 訊息（repo 未接 loguru→caplog）。"""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        func()
    finally:
        logger.remove(sink_id)
    return messages


# ─── _resolve_llm_cfg：settings 讀取與回退 ──────────────────────────────────────


def test_resolve_llm_cfg_uses_settings():
    llm = {"model": "claude-sonnet-4-6", "max_tokens": 1234, "temperature": 0.0}
    out = _resolve_llm_cfg({"report": {"llm": llm}})
    assert out == llm


def test_resolve_llm_cfg_falls_back_when_missing():
    # 舊版 / 編輯過的 settings 缺 report.llm 段 → 回退預設（向後相容）
    assert _resolve_llm_cfg({}) == _DEFAULT_LLM
    assert _resolve_llm_cfg({"report": {}}) == _DEFAULT_LLM
    assert _resolve_llm_cfg({"report": {"llm": None}}) == _DEFAULT_LLM


def test_resolve_llm_cfg_partial_override():
    out = _resolve_llm_cfg({"report": {"llm": {"max_tokens": 8000}}})
    assert out["max_tokens"] == 8000          # 覆寫
    assert out["model"] == _DEFAULT_LLM["model"]  # 其餘鍵保留預設


# ─── _warn_if_truncated：截斷偵測 ───────────────────────────────────────────────


def test_no_warning_when_complete():
    content = "## 技術面\n...\n## 資料來源\n- 證交所日線快取"
    warnings = _capture_warnings(lambda: _warn_if_truncated(content, "end_turn"))
    assert warnings == []


def test_warning_when_tail_missing():
    content = "## 技術面\n...\n## 進場條件\n- 觸發訊號"  # 缺「資料來源」尾段
    warnings = _capture_warnings(lambda: _warn_if_truncated(content, "end_turn"))
    assert len(warnings) == 1
    assert "資料來源" in warnings[0]


def test_warning_when_stop_reason_max_tokens():
    content = "## 資料來源\n- 證交所日線快取"  # 尾段在但達 token 上限
    warnings = _capture_warnings(lambda: _warn_if_truncated(content, "max_tokens"))
    assert len(warnings) == 1
    assert "max_tokens" in warnings[0]


def test_two_warnings_when_both_signals():
    content = "## 進場條件\n- 觸發訊號"  # 缺尾段 ＋ 達上限
    warnings = _capture_warnings(lambda: _warn_if_truncated(content, "max_tokens"))
    assert len(warnings) == 2


# ─── 草稿模式（無 API key 路徑）不受影響 ────────────────────────────────────────


def _draft_bundle() -> dict:
    return {
        "stock_id": "2330",
        "name": "台積電",
        "goodinfo_url": "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2330",
        "fetched_at": "2026-06-30",
        "group_info": {"strategy_str": "D 品質龍頭"},
        "industry_name": "半導體",
        "price_summary": "(price)",
        "revenue_summary": "(revenue)",
        "fundamentals_summary": "(fundamentals)",
        "institutional_summary": "(institutional)",
        "big_holder_summary": "(big_holder)",
        "margin_summary": "(margin)",
    }


def test_draft_mode_has_data_source_tail():
    draft = _build_data_draft(_draft_bundle())
    assert "## 資料來源" in draft
    # 草稿尾段完整 → 不會被截斷偵測誤報
    assert _capture_warnings(lambda: _warn_if_truncated(draft, "end_turn")) == []
