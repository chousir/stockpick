#!/usr/bin/env python3
"""PoC（隔離沙盒）：抓單一主動式 ETF 每日持股 — 群益 00992A 為樣本。

刻意與 src/tw_screener 主流程完全脫鉤：純 stdlib、不 import 專案模組、
不碰 config/ 與 data/，輸出只寫到 poc/active_etf/snapshots/。

兩條路徑（見 README）：
  1) 公開頁(www) — 只有「前十大」成分。本沙盒(US)可達，預設走這條。
  2) 後端 API(125.227.3.107/CapitalFundAPI) — 完整持股，但 TW geo-fence，
     美國環境連不到（HTTP 000）。請在台灣本機 / TW proxy 跑 --full。

用法：
  python fetch_capital.py            # 抓公開頁 top10，存 snapshot CSV
  python fetch_capital.py --full     # 試後端 API 完整持股（需 TW 網路）
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PUBLIC_URL = "https://www.capitalfund.com.tw/etf/product/detail/500/portfolio"
# 後端 API base（從 main.js 反解；TW geo-fence）。fund 內部 id = 500 = 00992A。
API_BASE = "http://125.227.3.107/CapitalFundAPI/api/etf"
ETF_CODE = "00992A"
SNAP_DIR = Path(__file__).parent / "snapshots"

# 公開頁一列：代號(th) 名稱(th) 權重%(td) 股數(td sm-full)
_ROW_RE = re.compile(
    r'class="th">\s*([0-9A-Z]{4,6})\s*</div>'
    r'<div[^>]*class="th">\s*([^<]+?)\s*</div>'
    r'<div[^>]*class="td">\s*([0-9.]+)%\s*</div>'
    r'<div[^>]*class="td sm-full">\s*([0-9,]+)\s*</div>'
)
_DATE_RE = re.compile(r"20\d{2}/\d{1,2}/\d{1,2}")


def _get(url: str, accept: str = "text/html") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (公開資料)
        return resp.read().decode("utf-8", errors="replace")


def parse_public(html: str) -> tuple[str | None, list[dict[str, str]]]:
    """回傳 (資料日期, [ {stock_id,name,weight_pct,shares} ... ])。公開頁僅前十大。"""
    rows = [
        {"stock_id": m.group(1), "name": m.group(2),
         "weight_pct": m.group(3), "shares": m.group(4).replace(",", "")}
        for m in _ROW_RE.finditer(html)
    ]
    dm = _DATE_RE.search(html)
    return (dm.group(0) if dm else None), rows


def fetch_full_via_api() -> list[dict]:
    """完整持股：逐一試後端 endpoint。US 環境會 connect timeout（預期）。"""
    for ep in ("detail/500", "buyback/500", "items/500", "basic/500"):
        try:
            body = _get(f"{API_BASE}/{ep}", accept="application/json")
            data = json.loads(body)
            print(f"[ok] {ep} 回 JSON，keys={list(data)[:8] if isinstance(data, dict) else type(data)}")
            return data if isinstance(data, list) else [data]
        except Exception as e:  # noqa: BLE001 (PoC：把每個 endpoint 的失敗印出來)
            print(f"[fail] {ep}: {type(e).__name__}: {e}")
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="試後端 API 抓完整持股（需 TW 網路）")
    args = ap.parse_args()

    if args.full:
        print(f"=== {ETF_CODE} 後端 API 完整持股嘗試 ===")
        full = fetch_full_via_api()
        print(f"取得 {len(full)} 筆（0 = 多半被 geo-fence 擋）")
        return 0

    print(f"=== {ETF_CODE} 公開頁（前十大）===")
    html = _get(PUBLIC_URL)
    date, rows = parse_public(html)
    print(f"資料日期: {date} ｜ 解析到 {len(rows)} 檔")
    for r in rows:
        print(f"  {r['stock_id']:<6} {r['name']:<8} {r['weight_pct']:>6}%  {int(r['shares']):>12,}")

    if rows:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = (date or dt.date.today().isoformat()).replace("/", "")
        out = SNAP_DIR / f"{ETF_CODE}_{stamp}_top10.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["stock_id", "name", "weight_pct", "shares"])
            w.writeheader()
            w.writerows(rows)
        print(f"已存 snapshot → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
