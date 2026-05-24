"""parser.py — 解析 Yahoo 股市 類股/概念股頁面（純函式，離線可測）。

資料藏在 SSR `root.App.main` 內。本檔用針對性 regex 擷取（比整塊 JSON 解析穩健、
也比逐字大括號配對簡單）：
- 主題索引（/class）：抽 `category=` 連結 → 主題名 + categoryLabel（kind）。
- 概念股成分頁：抽相鄰的 `symbolName` + `systexId`（4 碼台股股號）；list 在頁面渲染
  兩次，靠 systexId 去重。SSR 每主題只內嵌前約 30 檔。
驗證日期：2026-05-24（fixtures: tests/fixtures/yahoo/）。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import NamedTuple

from loguru import logger

# /class 主題連結：category=<enc>&categoryLabel=<enc>（HTML 內 & 寫成 &amp;）
_INDEX_LINK_RE = re.compile(
    r'href="(/class-quote\?category=([^"&]+)&(?:amp;)?categoryLabel=([^"&]+))"'
)
# 概念股成分頁：symbolName 緊接 systexId（4 碼台股；-KY/* 在名稱不影響股號）
_MEMBER_RE = re.compile(r'"symbolName":"([^"]*)","systexId":"(\d{4})"')


class YahooParseError(Exception):
    """頁面結構解析失敗（Yahoo 改版或非預期回應）。"""


class ThemeRef(NamedTuple):
    """一個主題連結：名稱、kind（categoryLabel）、成分頁相對路徑。"""

    name: str  # 主題名，如「衛星/低軌衛星」
    kind: str  # categoryLabel：概念股 / 電子產業 / 集團股
    href: str  # /class-quote?category=...&categoryLabel=...（已把 &amp; 還原成 &）


def parse_class_index(html: str) -> list[ThemeRef]:
    """從 /class 索引頁抽出所有主題（概念股/電子產業/集團股全收），去重保序。

    回傳全部主題；要哪幾類由呼叫端（build-themes）依 categoryLabel 白名單過濾。
    找不到任何主題連結 → raise YahooParseError（多半是 Yahoo 改版）。
    """
    refs: list[ThemeRef] = []
    seen: set[tuple[str, str]] = set()
    for m in _INDEX_LINK_RE.finditer(html):
        href = m.group(1).replace("&amp;", "&")
        name = urllib.parse.unquote(m.group(2))
        kind = urllib.parse.unquote(m.group(3))
        key = (name, kind)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ThemeRef(name=name, kind=kind, href=href))
    if not refs:
        raise YahooParseError("找不到任何 class-quote?category= 主題連結，可能 Yahoo 改版")
    logger.info("Yahoo 主題索引：解析出 {} 個主題", len(refs))
    return refs


def parse_category_members(html: str) -> list[tuple[str, str]]:
    """從概念股成分頁抽 (stock_id, name)；只留 4 碼台股股號，去重保序。

    SSR 每主題只內嵌前約 30 檔（成分 list 渲染兩次，以 systexId 去重）。
    0 筆（無台股成分或解析不到）回空 list，由呼叫端依 min_members 過濾。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _MEMBER_RE.finditer(html):
        name, sid = m.group(1), m.group(2)
        if sid in seen:
            continue
        seen.add(sid)
        out.append((sid, name))
    return out
