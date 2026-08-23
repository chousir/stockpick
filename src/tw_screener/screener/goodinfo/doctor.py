"""doctor.py — Goodinfo 健康檢查（規劃書 02 D1）。

用一個極簡固定探針（config/doctor_probe.yaml）打一次 Goodinfo，判斷
「正常／被擋／JS session 改了／改版／欄位改名／結果異常」並回傳明確診斷碼。

兩種模式：
- live（run_doctor）：實際打網，偵測 Goodinfo 端的改版／封鎖。
- replay（replay_doctor）：離線解析既有 committed fixture，驗 parser 本身沒退化（不打網）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, Tag
from loguru import logger

from tw_screener.screener.goodinfo.fetcher import (
    GoodinfoBlockedError,
    GoodinfoFetcher,
    create_fetcher,
)
from tw_screener.screener.goodinfo.parser import (
    GoodinfoParseError,
    GoodinfoTooManyResultsError,
    parse_screener_result,
)
from tw_screener.screener.goodinfo.url_builder import build_data_url, load_strategy

# Goodinfo 「篩選條件範圍過大」訊號（與 parser._TOO_MANY 同源語意）
_TOO_MANY_MARKER = "篩選條件範圍過大"

# Cloudflare 人機驗證挑戰頁（非 Goodinfo 自家的流量異常封鎖頁/JS init 頁，格式不同、
# 2026-08-23 直接抓包確認過；不加這條會被 tblStockList 檢查誤判成 STRUCTURE_CHANGED
# （像改版），實際上是被擋，見 docs/31 §19.3）
_CLOUDFLARE_MARKER = "__CF$cv$params"

# 解析成功時必須出現的關鍵中文欄名（parser._COL_MAP 的核心子集）。
# 缺任一 → 視為欄位改名（parser 會默默回傳整欄 null、不會 raise，故 doctor 要顯式檢查）。
_CRITICAL_HEADERS = ("代號", "名稱", "成交", "漲跌幅")


class DoctorStatus(enum.StrEnum):
    """健康檢查診斷碼。OK 以外皆視為需要人工介入（CLI 以 exit 1 回報）。"""

    OK = "OK"
    BLOCKED = "BLOCKED"                  # 流量異常封鎖頁
    JS_UNRESOLVED = "JS_UNRESOLVED"      # 仍停在 JS session 初始化頁（CLIENT_KEY/重導向機制改了）
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"  # 找不到 tblStockList（改版）
    COLUMNS_RENAMED = "COLUMNS_RENAMED"  # 表在但關鍵欄名變了
    EMPTY_RESULT = "EMPTY_RESULT"        # 解析成功但 0 筆（探針理應恆 >0，可疑）
    TOO_MANY = "TOO_MANY"               # 超過匿名 300 筆上限（探針理應遠低於，可疑）
    NETWORK_ERROR = "NETWORK_ERROR"      # 連線/重試耗盡


@dataclass(frozen=True)
class DoctorResult:
    status: DoctorStatus
    message: str
    count: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is DoctorStatus.OK


def diagnose_html(html: str) -> DoctorResult:
    """純函式：把一段 Goodinfo HTML 分類成診斷碼。live 與 replay 共用。

    判斷順序由「最外層異常」往內：封鎖 → JS 未解 → 範圍過大 → 結構 → 欄名 → 筆數。
    """
    if GoodinfoFetcher._BLOCKED_MARKER in html:
        return DoctorResult(DoctorStatus.BLOCKED, "Goodinfo 回傳流量異常封鎖頁（您的瀏覽量異常）")

    if _CLOUDFLARE_MARKER in html:
        return DoctorResult(
            DoctorStatus.BLOCKED,
            "Goodinfo 回傳 Cloudflare 人機驗證頁（非 Goodinfo 自家封鎖頁，同屬連線被擋）",
        )

    if GoodinfoFetcher._is_js_init_page(html):
        return DoctorResult(
            DoctorStatus.JS_UNRESOLVED,
            "仍停在 JS session 初始化頁，CLIENT_KEY 還原或重導向機制可能已改",
        )

    if _TOO_MANY_MARKER in html:
        return DoctorResult(
            DoctorStatus.TOO_MANY,
            "Goodinfo 回報篩選範圍過大（超過匿名 300 筆上限）；探針門檻或上限機制可能變了",
        )

    soup = BeautifulSoup(html, "lxml")
    tbl: Tag | None = soup.find("table", id="tblStockList")  # type: ignore[assignment]
    if tbl is None:
        return DoctorResult(
            DoctorStatus.STRUCTURE_CHANGED,
            "找不到結果表 tblStockList，Goodinfo 可能改版或回應格式異常",
        )

    header_row = tbl.find("tr")
    headers = (
        {c.get_text(strip=True) for c in header_row.find_all(["th", "td"])}
        if isinstance(header_row, Tag)
        else set()
    )
    missing = [h for h in _CRITICAL_HEADERS if h not in headers]
    if missing:
        return DoctorResult(
            DoctorStatus.COLUMNS_RENAMED,
            f"結果表缺關鍵欄位 {missing}，欄名可能已改（parser 會默默回傳 null）",
        )

    try:
        df = parse_screener_result(html)
    except GoodinfoTooManyResultsError as exc:
        return DoctorResult(DoctorStatus.TOO_MANY, str(exc), count=exc.count)
    except GoodinfoParseError as exc:
        return DoctorResult(DoctorStatus.STRUCTURE_CHANGED, str(exc))

    if df.is_empty():
        return DoctorResult(
            DoctorStatus.EMPTY_RESULT,
            "解析成功但 0 筆；高流動性探針理應恆 >0，可能放假無資料或上游異常",
            count=0,
        )

    return DoctorResult(DoctorStatus.OK, f"正常：探針解析出 {len(df)} 檔", count=len(df))


# ── live / replay ─────────────────────────────────────────────────────────────


def _load_settings(settings_path: Path) -> dict:
    with open(settings_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _probe_data_url(settings: dict) -> str:
    gi = settings["goodinfo"]
    probe_path = Path(gi["doctor"]["probe_strategy"])
    strategy = load_strategy(probe_path)
    return build_data_url(strategy, gi["base_url"])


def run_doctor(
    settings_path: Path,
    *,
    force: bool = False,
    save_fixture: bool = False,
    fetcher: GoodinfoFetcher | None = None,
) -> DoctorResult:
    """live：打探針 URL，分類結果。被擋/連線失敗會被攔下分類成診斷碼，不外拋。

    force=True 略過快取強制打網（doctor 預設沿用 screen-all 的快取行為）。
    save_fixture=True 且結果 OK 時，把抓到的 HTML 落地到 settings 指定的 fixture 路徑。
    fetcher 參數供測試注入；正式呼叫由 settings 建立。
    """
    settings = _load_settings(settings_path)
    if fetcher is None:
        cache_dir = Path(settings["paths"]["cache_dir"])
        fetcher = create_fetcher(settings, cache_dir)

    data_url = _probe_data_url(settings)
    try:
        html = fetcher.get(data_url, force=force)
    except GoodinfoBlockedError:
        return DoctorResult(DoctorStatus.BLOCKED, "Goodinfo 回傳流量異常封鎖頁（您的瀏覽量異常）")
    except Exception as exc:  # noqa: BLE001 — 連線層/重試耗盡：httpx 例外、逾時等
        logger.error("doctor 取數失敗：{}", exc)
        return DoctorResult(DoctorStatus.NETWORK_ERROR, f"取數失敗：{exc}")

    result = diagnose_html(html)
    if save_fixture and result.ok:
        _save_fixture(settings, html)
    return result


def replay_doctor(settings_path: Path, fixture: Path | None = None) -> DoctorResult:
    """replay：離線解析既有 committed fixture，驗 parser 沒退化（不打網）。

    fixture 為 None 時讀 settings 的 doctor.replay_fixture。
    """
    settings = _load_settings(settings_path)
    fixture_path = fixture or Path(settings["goodinfo"]["doctor"]["replay_fixture"])
    if not fixture_path.exists():
        return DoctorResult(
            DoctorStatus.NETWORK_ERROR, f"找不到 replay fixture：{fixture_path}"
        )
    html = fixture_path.read_text(encoding="utf-8")
    return diagnose_html(html)


def _save_fixture(settings: dict, html: str) -> None:
    path = Path(settings["goodinfo"]["doctor"]["save_fixture_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info("doctor 探針 HTML 已落地 → {}", path)
