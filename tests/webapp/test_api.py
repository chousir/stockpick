"""webapp 路由 happy-path 測試（docs/17 M-Dash 0 驗收）。

讀本機 reports/（gitignore、自用）；無資料的環境自動 skip，不硬失敗。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tw_screener.webapp.app import app

client = TestClient(app)


def _weeks() -> list[str]:
    return client.get("/api/weeks").json()["weeks"]


def _week_with_candidates() -> str | None:
    for w in _weeks():
        if client.get(f"/api/weeks/{w}/candidates").status_code == 200:
            return w
    return None


def test_weeks_ok() -> None:
    r = client.get("/api/weeks")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["weeks"], list)
    # 週次升冪、latest 為最後一個
    assert body["weeks"] == sorted(body["weeks"])
    if body["weeks"]:
        assert body["latest"] == body["weeks"][-1]


def test_candidates_happy_path() -> None:
    week = _week_with_candidates()
    if week is None:
        pytest.skip("本機 reports/ 無含 candidates 的週次")
    rows = client.get(f"/api/weeks/{week}/candidates").json()
    assert isinstance(rows, list) and len(rows) > 0
    # stock_id 一律字串（避免 ETF 前導零遺失）
    assert all(isinstance(row["stock_id"], str) for row in rows)
    # 共用 schema 關鍵欄位存在（值可能為 null）
    assert "momentum_5d_pct" in rows[0]
    assert "flags" in rows[0]


def test_missing_week_404() -> None:
    assert client.get("/api/weeks/9999-W99/candidates").status_code == 404
    assert client.get("/api/weeks/9999-W99/meta").status_code == 404


def test_missing_file_404_not_500() -> None:
    """缺檔的週次（如只有 screen_result 的週）回 404、非 500（docs/17 §2.4）。"""
    for w in _weeks():
        r = client.get(f"/api/weeks/{w}/candidates")
        assert r.status_code in (200, 404)


def test_meta_shape() -> None:
    week = _week_with_candidates()
    if week is None:
        pytest.skip("本機 reports/ 無資料")
    meta = client.get(f"/api/weeks/{week}/meta").json()
    assert meta["week"] == week
    assert isinstance(meta["files"], dict)
    assert "candidates" in meta["files"]
