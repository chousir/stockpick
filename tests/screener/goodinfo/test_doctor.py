"""tests/screener/goodinfo/test_doctor.py — Goodinfo 健康檢查（規劃書 02 D1）全離線測試。"""

from pathlib import Path

from tw_screener.screener.goodinfo.doctor import (
    DoctorStatus,
    diagnose_html,
    replay_doctor,
    run_doctor,
)

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "goodinfo"
SETTINGS_PATH = Path("config/settings.yaml")


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


_TABLE_HEAD = (
    "<tr><td>代號</td><td>名稱</td><td>市場</td><td>成交</td>"
    "<td>漲跌幅</td><td>成交張數</td><td>成交額(百萬)</td><td>PER</td><td>PBR</td></tr>"
)


def _table(rows: str, head: str = _TABLE_HEAD) -> str:
    return (
        f'<html><body><table id="tblStockList">{head}{rows}</table></body></html>'
    )


# ─── diagnose_html：各診斷碼 ───────────────────────────────────────────────────


def test_diagnose_ok_real_fixture():
    result = diagnose_html(_fixture("screener_result.html"))
    assert result.status is DoctorStatus.OK
    assert result.ok
    assert result.count == 61


def test_diagnose_blocked():
    result = diagnose_html(_fixture("blocked.html"))
    assert result.status is DoctorStatus.BLOCKED
    assert not result.ok


def test_diagnose_cloudflare_challenge_classified_as_blocked():
    # 2026-08-23 實測抓包的真實特徵（見 docs/31 §19.3）：非 Goodinfo 自家封鎖頁/JS
    # init 頁格式，若不特判會被 tblStockList 檢查誤判成 STRUCTURE_CHANGED（像改版）
    html = (
        "<html><head><title>初始化失敗</title></head><body>"
        "請開啟瀏覽器的JavaScript及Cookies功能，以支援網站運作。"
        "<script>window.__CF$cv$params={r:'x',t:'y'}</script></body></html>"
    )
    result = diagnose_html(html)
    assert result.status is DoctorStatus.BLOCKED
    assert not result.ok


def test_diagnose_js_unresolved():
    html = "<html><script>var CLIENT_KEY; window.location.replace('x.asp')</script></html>"
    assert diagnose_html(html).status is DoctorStatus.JS_UNRESOLVED


def test_diagnose_structure_changed():
    html = "<html><body><table id='somethingElse'></table></body></html>"
    assert diagnose_html(html).status is DoctorStatus.STRUCTURE_CHANGED


def test_diagnose_columns_renamed():
    # 表在但關鍵欄名改了（代號→股票代號 等）→ parser 會默默回 null，doctor 要抓出
    head = "<tr><td>股票代號</td><td>股票名稱</td><td>收盤價</td><td>漲跌</td></tr>"
    html = _table("<tr><td>2330</td><td>台積電</td><td>1000</td><td>1%</td></tr>", head=head)
    assert diagnose_html(html).status is DoctorStatus.COLUMNS_RENAMED


def test_diagnose_empty_result():
    # 欄名正常但無資料列
    assert diagnose_html(_table("")).status is DoctorStatus.EMPTY_RESULT


def test_diagnose_too_many():
    html = "<html><body>篩選條件範圍過大，共計5000筆</body></html>"
    assert diagnose_html(html).status is DoctorStatus.TOO_MANY


# ─── replay_doctor：離線吃 committed fixture ────────────────────────────────────


def test_replay_uses_settings_fixture():
    result = replay_doctor(SETTINGS_PATH)
    assert result.status is DoctorStatus.OK


def test_replay_explicit_fixture_path():
    result = replay_doctor(SETTINGS_PATH, fixture=FIXTURE_DIR / "blocked.html")
    assert result.status is DoctorStatus.BLOCKED


def test_replay_missing_fixture():
    result = replay_doctor(SETTINGS_PATH, fixture=FIXTURE_DIR / "does_not_exist.html")
    assert result.status is DoctorStatus.NETWORK_ERROR


# ─── run_doctor：注入 fetcher（不打網）───────────────────────────────────────────


class _MockFetcher:
    def __init__(self, html: str) -> None:
        self._html = html

    def get(self, url: str, *, force: bool = False) -> str:
        return self._html


class _BlockedFetcher:
    def get(self, url: str, *, force: bool = False) -> str:
        from tw_screener.screener.goodinfo.fetcher import GoodinfoBlockedError

        raise GoodinfoBlockedError(url)


class _NetworkErrorFetcher:
    def get(self, url: str, *, force: bool = False) -> str:
        raise OSError("connection reset")


def test_run_doctor_ok_with_injected_fetcher():
    result = run_doctor(SETTINGS_PATH, fetcher=_MockFetcher(_fixture("screener_result.html")))
    assert result.status is DoctorStatus.OK


def test_run_doctor_blocked_is_classified_not_raised():
    result = run_doctor(SETTINGS_PATH, fetcher=_BlockedFetcher())
    assert result.status is DoctorStatus.BLOCKED


def test_run_doctor_network_error_classified():
    result = run_doctor(SETTINGS_PATH, fetcher=_NetworkErrorFetcher())
    assert result.status is DoctorStatus.NETWORK_ERROR


def test_run_doctor_save_fixture(tmp_path: Path, monkeypatch):
    # OK 且 save_fixture=True → 落地 HTML 到 settings 指定路徑
    import tw_screener.screener.goodinfo.doctor as doctor_mod

    saved: dict[str, str] = {}

    def fake_save(settings: dict, html: str) -> None:
        saved["html"] = html

    monkeypatch.setattr(doctor_mod, "_save_fixture", fake_save)
    run_doctor(
        SETTINGS_PATH,
        fetcher=_MockFetcher(_fixture("screener_result.html")),
        save_fixture=True,
    )
    assert "html" in saved


def test_run_doctor_no_save_when_not_ok(monkeypatch):
    import tw_screener.screener.goodinfo.doctor as doctor_mod

    called = {"n": 0}
    monkeypatch.setattr(
        doctor_mod, "_save_fixture", lambda settings, html: called.__setitem__("n", called["n"] + 1)
    )
    run_doctor(SETTINGS_PATH, fetcher=_BlockedFetcher(), save_fixture=True)
    assert called["n"] == 0
