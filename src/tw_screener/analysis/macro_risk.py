"""M8 宏觀窄橋（委託書 M8・裁決 D）——消費每日美股風險掃描的機器摘要。

**這是一座窄橋，不是一條管線。** 每日 17 指標掃描是**獨立 project**（M9／
`.claude/commands/macro-scan.md`），走網路搜尋、產出人讀報告；本模組只讀它吐出的一小段
機器摘要 `macro_risk_latest.yaml`，把結論帶進週報的**倉位節奏**判斷。掃描本身的 17 項
指標**不進 pipeline**，理由見 docs/26 §1–§3（一半不可程式化取得、頻率錯配、對台股目標
從未實測）。

## 與 docs/26 既有裁決的關係（必讀，否則會以為互相矛盾）

docs/26 §2 已裁決「`3-of-7` 在本 repo 永遠算不出來」——七條硬觸發只有 2 條能自動求值，
把分母寫成 7 是「規則本身無法被求值」。本模組**不推翻這條**，而是照 docs/26 §6.2(5)
已定的口徑落地：**只報「已求值 N 項中觸發 M 項」**，`of` 欄承載的是 N（掃描當次實際
求得值的項數），不是常數 7。人工掃描可以用網路搜尋補到 FRED 拿不到的那批，故 N 通常
> 2 但**不會滿 7**。

docs/26 §6.2(4) 原本寫「輸出不得建議調整本專案的排序/剔除/燈色」。**裁決 D 局部覆寫了
這一條**：現在允許影響**姿態與倉位節奏**（降一級、新倉封 ⅓），但**仍不影響選股**——
不改排序、不改剔除、不改燈色。這個界線是刻意的：docs/26 §3.2 指出這 17 項對**台股目標**
從未實測，傳導鏈（台股 beta、外資流）真實但慢且弱，所以它可以調節「買多大」，不能決定
「買哪檔」。

## 三態容錯（委託書 M8：檔缺席/schema 錯/日期過期一律降級為註記、不擋流程）

  ok      → 依 §gate 影響姿態與新倉上限
  missing → 檔案不在（沒跑掃描）：明寫「宏觀掃描缺席，不當 gate」
  stale   → 日期落後 > N 個交易日：明寫「宏觀掃描過期，不當 gate」
  invalid → 檔在但 schema 壞：同 missing 處理，另印壞在哪（不猜、不半信半疑地用）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"

DEFAULT_FILENAME = "macro_risk_latest.yaml"


@dataclass(frozen=True)
class MacroRisk:
    """`macro_risk_latest.yaml` 的解析結果。非 ok 狀態時數值欄一律 None（不半信半疑地用）。"""

    status: str
    date: date | None = None
    triggers_hit: int | None = None
    of: int | None = None
    hits: list[str] = field(default_factory=list)
    vix: float | None = None
    fng: float | None = None
    days_behind: int | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == STATUS_OK


def weekday_gap(earlier: date, later: date) -> int:
    """兩日期之間的**平日**數（週末不算）。

    刻意不扣國定假日——本 repo 沒有美股行事曆，硬編一份會過期。誤差上限約 1–2 天，
    且方向已知：連假期間可能**提早**判 stale。stale 的後果是「降級為註記、不當 gate」，
    亦即少用一個未經本系統驗證的外部訊號——在 docs/26 §3.2 的立場下這是可接受的一邊。
    """
    if later <= earlier:
        return 0
    days = 0
    cur = earlier
    while cur < later:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def parse_macro_risk(payload: Any, as_of: date, stale_trading_days: int = 5) -> MacroRisk:
    """把 yaml 內容解析成 `MacroRisk`（純函式，不碰檔案）。

    預期結構（委託書 M8）：
        macro_risk: {date: 2026-08-08, triggers_hit: 2, of: 7,
                     hits: [HY_spread, margin_debt_mom], vix: 18.3, fng: 41}

    `of` 依 docs/26 §6.2(5) 解讀為「**已求值**項數」，不是常數 7。
    """
    if not isinstance(payload, dict):
        return MacroRisk(STATUS_INVALID, detail="yaml 頂層不是 mapping")
    block = payload.get("macro_risk")
    if not isinstance(block, dict):
        return MacroRisk(STATUS_INVALID, detail="缺 `macro_risk:` 區塊")

    raw_date = block.get("date")
    if isinstance(raw_date, date):
        d = raw_date
    elif isinstance(raw_date, str):
        try:
            d = date.fromisoformat(raw_date.strip())
        except ValueError:
            return MacroRisk(STATUS_INVALID, detail=f"date 非 ISO 格式：{raw_date!r}")
    else:
        return MacroRisk(STATUS_INVALID, detail="缺 date 或型別不對")

    def _int(key: str) -> int | None:
        v = block.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def _float(key: str) -> float | None:
        v = block.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    hit_n, of_n = _int("triggers_hit"), _int("of")
    if hit_n is None or of_n is None:
        return MacroRisk(STATUS_INVALID, date=d, detail="triggers_hit／of 缺值或非數字")
    if of_n <= 0 or hit_n < 0 or hit_n > of_n:
        return MacroRisk(
            STATUS_INVALID, date=d,
            detail=f"triggers_hit={hit_n}／of={of_n} 不成立（須 0 ≤ hit ≤ of，of > 0）",
        )
    raw_hits = block.get("hits") or []
    hits = [str(h) for h in raw_hits] if isinstance(raw_hits, list) else []

    behind = weekday_gap(d, as_of)
    if behind > stale_trading_days:
        return MacroRisk(
            STATUS_STALE, date=d, days_behind=behind,
            detail=f"掃描日 {d} 落後 {behind} 個平日 > {stale_trading_days}，過期不當 gate",
        )
    return MacroRisk(
        STATUS_OK, date=d, triggers_hit=hit_n, of=of_n, hits=hits,
        vix=_float("vix"), fng=_float("fng"), days_behind=behind,
        detail=f"已求值 {of_n} 項中觸發 {hit_n} 項",
    )


def load_macro_risk(
    path: Path, as_of: date, stale_trading_days: int = 5
) -> MacroRisk:
    """讀 `macro_risk_latest.yaml`（可選檔）。檔不在 → `missing`；讀不動/壞 → `invalid`。"""
    if not path.exists():
        return MacroRisk(STATUS_MISSING, detail=f"{path} 不存在（本週未附宏觀掃描摘要）")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("讀 {} 失敗：{}", path, exc)
        return MacroRisk(STATUS_INVALID, detail=f"讀取/解析失敗：{exc}")
    return parse_macro_risk(payload, as_of, stale_trading_days=stale_trading_days)


@dataclass(frozen=True)
class MacroRiskGate:
    """patch-6 的消費結果。**只影響倉位節奏，不影響選股。**"""

    downgrade_posture: bool
    new_position_cap: str | None  # 如 "1/3"；None＝不封
    note: str


def macro_risk_gate(
    risk: MacroRisk,
    min_hits: int = 3,
    min_coverage: int = 5,
    cap: str = "1/3",
) -> MacroRiskGate:
    """委託書 patch-6：`triggers_hit ≥ min_hits` → 姿態降一級、新倉一律封 cap。

    Args:
        risk: `load_macro_risk` 結果。
        min_hits: 觸發項數達此即 gate（委託書：≥3）。
        min_coverage: **已求值項數**低於此時，「0–2 項觸發」不得讀成全清——
            註記必須帶上 coverage（docs/26 §2「分母不完整」的圍欄）。
        cap: 觸發時的新倉上限文字。

    設計說明（為什麼 gate 這一側不需要 coverage 門檻）：未求值的項只會**少算**觸發數，
    所以「已觸發 ≥3」在任何 coverage 下都是保守安全的——它不會因分母薄而誤觸發。
    危險的是反過來：分母薄時把「0–2」讀成「風險已清」。故 coverage 檢核只掛在註記那側。
    """
    if not risk.usable:
        kind = {STATUS_MISSING: "缺席", STATUS_STALE: "過期", STATUS_INVALID: "格式錯"}.get(
            risk.status, risk.status
        )
        return MacroRiskGate(
            False, None,
            f"宏觀掃描{kind}，不當 gate（{risk.detail}）——決策卡姿態行照常，不因此鬆綁也不收緊",
        )
    hit, of = int(risk.triggers_hit or 0), int(risk.of or 0)
    hits_txt = ("：" + "、".join(risk.hits)) if risk.hits else ""
    if hit >= min_hits:
        return MacroRiskGate(
            True, cap,
            f"🔴 宏觀觸發 {hit}/{of}（已求值 {of} 項）{hits_txt} → 姿態降一級、新倉一律封 {cap}"
            f"；**只影響倉位節奏，不改選股**",
        )
    thin = of < min_coverage
    warn = (
        f"；⚠️ 已求值僅 {of} 項（< {min_coverage}），**低觸發數不等於風險已清**——"
        f"未求值項只會少算，不可讀成全清"
        if thin else ""
    )
    return MacroRiskGate(
        False, None,
        f"宏觀觸發 {hit}/{of}（已求值 {of} 項）{hits_txt} → 僅決策卡註記一行{warn}",
    )


def describe_macro_risk(risk: MacroRisk, gate: MacroRiskGate) -> dict[str, object]:
    """給渲染端/CLI 的顯示 dict。"""
    extras = []
    if risk.vix is not None:
        extras.append(f"VIX {risk.vix}")
    if risk.fng is not None:
        extras.append(f"F&G {risk.fng}")
    return {
        "status": risk.status,
        "date": risk.date.isoformat() if risk.date else None,
        "line": gate.note + ("｜" + "・".join(extras) if extras else ""),
        "downgrade_posture": gate.downgrade_posture,
        "new_position_cap": gate.new_position_cap,
    }


def to_disclosure(risk: MacroRisk) -> dict[str, object]:
    """資料品質披露 yaml 的 `macro_risk` 鍵（委託書 M8）。

    鍵名固定 `status`／`date`／`detail`，值如實填——這段會跟著 pick.md 永久存檔，
    未來季度覆盤時才知道「那一週到底有沒有宏觀掃描、是不是過期的」。
    """
    return {
        "status": risk.status,
        "date": risk.date.isoformat() if risk.date else None,
        "detail": risk.detail,
    }
