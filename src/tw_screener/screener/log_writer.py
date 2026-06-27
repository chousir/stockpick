"""log_writer.py — 產出 reports/YYYY-Www/screen_log.md（每週篩選統計）。

純機械統計：三組篩出檔數 + 4 種集合交集。不做觀察判斷。
"""

from datetime import date
from itertools import combinations
from pathlib import Path

import polars as pl
from loguru import logger


def _stock_set(df: pl.DataFrame) -> set[str]:
    if df.is_empty() or "stock_id" not in df.columns:
        return set()
    return set(df["stock_id"].to_list())


def _fmt_stock_line(stock_ids: set[str], results: dict[str, pl.DataFrame]) -> str:
    """把交集股號連同股名輸出成一行，按 stock_id 排序。空集合回傳「（無）」。"""
    if not stock_ids:
        return "（無）"

    name_map: dict[str, str] = {}
    for df in results.values():
        if df.is_empty() or "name" not in df.columns:
            continue
        for sid, nm in zip(df["stock_id"].to_list(), df["name"].to_list(), strict=False):
            if sid in stock_ids and sid not in name_map:
                name_map[sid] = nm or ""

    parts = [f"{sid} {name_map.get(sid, '')}".rstrip() for sid in sorted(stock_ids)]
    return "、".join(parts)


def write_screen_log(
    results: dict[str, pl.DataFrame],
    strategy_names: dict[str, str],
    week_tag: str,
    reports_dir: Path,
    failures: dict[str, str] | None = None,
) -> Path:
    """寫入 reports/YYYY-Www/screen_log.md。

    內容：
      - 各策略篩出檔數表
      - 「本週未取得」策略段（failures，規劃書 02 D1 韌性：誠實標記 parse 失敗的策略）
      - 兩兩交集 + 三方交集（依 strategy_id 字典序排列）

    results 空 dict 或全部策略 0 檔仍會寫檔（內容會顯示 0 / 無）。
    """
    failures = failures or {}
    report_dir = reports_dir / week_tag
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "screen_log.md"

    today = date.today().isoformat()
    strategy_ids = sorted(results.keys())

    lines: list[str] = [f"# 本週篩選紀錄 — {week_tag}", "", f"執行日期：{today}", ""]

    if failures:
        lines += ["## 本週未取得策略（資料源異常，已略過）", ""]
        for sid in sorted(failures):
            name = strategy_names.get(sid, "")
            lines.append(f"- {sid} {name}：{failures[sid]}")
        lines.append("")

    lines += ["## 各策略篩出數量", "", "| 策略 ID | 名稱 | 篩出檔數 |", "|---|---|---|"]
    for sid in strategy_ids:
        name = strategy_names.get(sid, "")
        count = len(results[sid])
        lines.append(f"| {sid} | {name} | {count} |")
    lines.append("")

    sets = {sid: _stock_set(results[sid]) for sid in strategy_ids}

    lines += ["## 重疊標的", ""]

    for a, b in combinations(strategy_ids, 2):
        inter = sets[a] & sets[b]
        lines.append(f"### {a} ∩ {b} — {len(inter)} 檔")
        lines.append(_fmt_stock_line(inter, results))
        lines.append("")

    if len(strategy_ids) >= 3:
        for combo in combinations(strategy_ids, 3):
            inter = set.intersection(*(sets[s] for s in combo))
            lines.append(f"### {' ∩ '.join(combo)} — {len(inter)} 檔")
            lines.append(_fmt_stock_line(inter, results))
            lines.append("")

    output.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Screen log written → {}", output)
    return output
