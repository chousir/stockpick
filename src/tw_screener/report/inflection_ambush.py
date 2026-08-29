"""M4.2 「轉折埋伏」候選源 E（委託書 M4.2）——法人剛開始買、價格還在底部。

**為什麼要獨立成一份產物，而不是讓分析師自己從 CSV 篩**：委託書 M4 的 quota 條款要求
「當週無合格者必須明寫『轉折早段 0 檔合格＋原因』」。如果清單是分析師心算出來的，
「0 檔」就只是一句宣稱；由管線產出，「0 檔」才是**可查核的事實**，而且列得出「最接近
合格者差哪一條」。

**放在哪、為什麼不在 cp_candidates.md**（與委託書的差異，已記進 docs/28 差異清單）：
委託書寫「產出到 cp_candidates.md 新段」，但 `make week` 的順序是
⑧ cp-value-candidates → ⑨ group——`cp_candidates.md` 在 `candidates_enriched` 的欄位
存在**之前**就已寫完，而本清單的四個條件全部依賴那些欄。硬塞回去只能靠事後改寫別的
步驟的產物（髒）。故改為獨立產物 `inflection_ambush.md`＋`.csv`，同時進 docs/11 貼檔
清單與 week-check。

**定位＝候選源，不是進場資格**：清單只 surfacing 給人逐檔過（可判進機會層、可判觀察、
也可判不要），**不自動進 picks、不改排序、不改剔除**。欄位本身未經前瞻檢驗（沿 docs/22
§2 flow_turn 與 docs/24 §3.1 的教訓，直覺 ≠ 證據）。

**`base_zone=貼底` 的語意陷阱（2026-08-09 實跑發現，已記進差異清單）**：委託書 M4.2 的
位階條件寫「`base_zone=貼底` **或** 距 low_60d ≤10%」。但本 repo 的 `base_zone=貼底`
＝**距季線 MA60 ≤ +10%**（`propicks_flags.base_zone_ma60_max_pct`，docs/20 §WS5-①），
量的是「未延伸／回到季線 base」，**不是「貼近 60 日低點」**。兩者可以差很遠：2026-W32
實跑中 7610 距季線 +4.2%（＝貼底）但距 60 日低 +133.6%。故本清單的 OR 實際涵蓋**兩種
不同的低位階**——「回到季線 base 的回檔股」與「真的貼近結構低的落難股」。

處理方式＝**照委託書原式實作，不自行收緊**（同 M1 的作法），但表上加 `位階依據` 欄把兩
條分支攤開、並讓「距低」型排前面，由人分辨。若日後判定只要後者，關掉 `貼底` 分支是一行
settings（`inflection.ambush_allow_base_zone_branch: false`）。
"""

from __future__ import annotations

import polars as pl
from loguru import logger

from tw_screener.analysis.inflection import is_inflection_ambush

# 清單欄位（md 與 csv 共用）
_COLUMNS = (
    "stock_id", "name", "theme", "fundamental_health", "rev_yoy_pct",
    "base_zone", "dist_low_60d_pct", "foreign_inflection_days",
    "foreign_net_lots", "foreign_flow_diff_5_20", "margin_slim",
    "close", "low_60d", "ma60_dist_pct", "flags",
)
# 「最接近合格」的診斷順序＝條件由粗到細，回報第一個沒過的
_CHECK_ORDER = ("基本面", "位階", "剛轉買", "20日外資")


def _row_checks(r: dict, near_low_pct: float, days_range: tuple[int, int],
                small_pos: float, allow_base_zone: bool = True) -> dict[str, bool]:
    lo, hi = days_range
    dist = r.get("dist_low_60d_pct")
    days = r.get("foreign_inflection_days")
    f20 = r.get("foreign_net_lots")
    return {
        "基本面": r.get("fundamental_health") in ("強化", "穩健"),
        "位階": (allow_base_zone and r.get("base_zone") == "貼底")
        or (dist is not None and float(dist) <= near_low_pct),
        "剛轉買": days is not None and lo <= int(days) <= hi,
        "20日外資": f20 is not None and float(f20) <= small_pos,
    }


def _position_basis(r: dict, near_low_pct: float) -> str:
    """位階是靠哪條分支過的——`貼底`＝距季線≤10%（未延伸），與「距低」是兩回事。

    攤開這一欄，人才分得出「回到季線 base 的回檔股」與「真的貼近結構低的落難股」；
    否則表上兩者混在一起，還被開頭那句「價格還在底部」一起誤述。
    """
    dist = r.get("dist_low_60d_pct")
    near = dist is not None and float(dist) <= near_low_pct
    base = r.get("base_zone") == "貼底"
    if near and base:
        return "皆是"
    if near:
        return f"距低≤{near_low_pct:g}%"
    return "僅貼底(距季線)" if base else "—"


def build_inflection_ambush(
    enriched: pl.DataFrame,
    near_low_pct: float = 10.0,
    inflection_days_range: tuple[int, int] = (1, 5),
    small_positive_lots: float = 5000.0,
    allow_base_zone_branch: bool = True,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """回 (合格清單, 近似清單)。

    近似清單＝**只差一條**就合格者（依 `_CHECK_ORDER` 回報缺哪條），供 quota 條款的
    「最接近合格者差哪一條」有據可寫。

    排序刻意讓**距低型排在貼底型前面**（`_position_basis`）：兩條分支語意不同，距低型
    才是「法人剛開始買、價格還在底部」的原意，貼底型是回檔到季線 base，需人分辨。
    """
    empty = pl.DataFrame(schema={c: pl.Utf8 for c in _COLUMNS})
    if enriched.is_empty():
        return empty, empty
    have = [c for c in _COLUMNS if c in enriched.columns]
    required = {"fundamental_health", "dist_low_60d_pct",
                "foreign_inflection_days", "foreign_net_lots"}
    if not required.issubset(enriched.columns):
        logger.warning(
            "candidates_enriched 缺 {} → 轉折埋伏清單無法計算（欄位不足，不猜）",
            "、".join(sorted(required - set(enriched.columns))),
        )
        return empty, empty

    qualified: list[dict] = []
    near_miss: list[dict] = []
    for r in enriched.iter_rows(named=True):
        basis = _position_basis(r, near_low_pct)
        if is_inflection_ambush(
            r.get("fundamental_health"), r.get("base_zone"), r.get("dist_low_60d_pct"),
            r.get("foreign_inflection_days"), r.get("foreign_net_lots"),
            near_low_pct=near_low_pct, inflection_days_range=inflection_days_range,
            small_positive_lots=small_positive_lots,
            allow_base_zone_branch=allow_base_zone_branch,
        ):
            qualified.append({**{c: r.get(c) for c in have}, "位階依據": basis})
            continue
        checks = _row_checks(
            r, near_low_pct, inflection_days_range, small_positive_lots,
            allow_base_zone_branch,
        )
        failed = [k for k in _CHECK_ORDER if not checks[k]]
        if len(failed) == 1:
            near_miss.append({
                **{c: r.get(c) for c in have},
                "位階依據": basis, "差哪一條": failed[0],
            })

    def _frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return empty
        # infer_schema_length=None：rows>100 時預設只看前 100 列猜型別，晚出現的字串值
        # （如次產業「主機板」）會撞現有數值欄猜測、write_csv 崩潰（2026-08-29 實跑觸發，
        # 同 group_report.py 已修過的成因，見該檔 write_candidates_enriched_csv 附近註解）
        df = pl.DataFrame(rows, infer_schema_length=None)
        if "位階依據" not in df.columns:
            return df
        # 距低型（含「皆是」）優先，其次剛轉買天數越少越前，最後距低越小越前
        order = pl.col("位階依據").str.starts_with("僅貼底").cast(pl.Int8)
        by = [order]
        for c in ("foreign_inflection_days", "dist_low_60d_pct"):
            if c in df.columns:
                by.append(pl.col(c))
        return df.sort(by, nulls_last=True)

    return _frame(qualified), _frame(near_miss)


def _fmt(v: object, nd: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return f"{v:,.{nd}f}" if isinstance(v, float) else f"{v:,}"
    return str(v)


def render_inflection_ambush(
    qualified: pl.DataFrame,
    near_miss: pl.DataFrame,
    week_tag: str,
    near_low_pct: float,
    inflection_days_range: tuple[int, int],
    small_positive_lots: float,
    near_miss_limit: int = 15,
) -> str:
    """`inflection_ambush.md` 全文。零命中週也要產出（「0 檔」本身就是要交代的事實）。"""
    lo, hi = inflection_days_range
    n_near = 0
    if "位階依據" in qualified.columns:
        n_near = int(
            qualified.filter(~pl.col("位階依據").str.starts_with("僅貼底")).height
        )
    lines = [
        f"# {week_tag} 轉折埋伏候選（候選源 E・委託書 M4.2）",
        "",
        "> **要修的病**：主排序鑰匙「外資 5/10/20 三窗同買」是**確認**訊號——成立時法人多半"
        "已買了一段，買到的是**建倉尾段**。本清單抓同一批資料的**早段**：",
        "> 「法人剛開始買、價格仍在低位階」。",
        "",
        f"> **合格條件（四條同時）**：基本面 ∈ {{強化,穩健}} ∧（`base_zone=貼底` 或 距 60 日低"
        f" ≤ {near_low_pct:g}%）∧ 外資尾端連買 {lo}–{hi} 日"
        f" ∧ 20 日外資 ≤ {small_positive_lots:,.0f} 張。",
        "",
        "> ⚠️ **「低位階」是兩種、不是一種**：本 repo 的 `base_zone=貼底` ＝**距季線 MA60"
        " ≤ +10%（未延伸／回到季線 base）**，docs/20 §WS5-①，**不是「貼近 60 日低點」**。"
        "委託書 M4.2 把兩者以 `或` 並聯，故本表同時含：",
        "> ① **距低型**（真的貼近結構低的落難股，＝「價格還在底部」的原意）；"
        "② **僅貼底型**（回檔到季線 base，距 60 日低可能已很遠）。",
        f"> `位階依據` 欄標明每檔靠哪條過，距低型排在前面。本週距低型 {n_near} 檔／"
        f"僅貼底型 {max(qualified.height - n_near, 0)} 檔。**兩者的多空論述不同，不可混用**。",
        "",
        "> ⚠️ **定位＝候選源，不是進場資格，也不是排序主判**。欄位未經前瞻檢驗"
        "（沿 docs/22 §2 flow_turn、docs/24 §3.1 的教訓：直覺 ≠ 證據）。",
        "> 清單只 surfacing 給人**逐檔過**——可判進機會層、可判觀察、也可判不要，"
        "但**不許不看**；**不自動進 picks、不改排序、不改剔除**。",
        "> 與 M1 左側票的分工：M1 要求外資 `轉買`＋貼結構低（≤5%）＋**未破新低**，是"
        "「可小注承接」的進場資格；本清單放寬到距低 ≤10%、不要求未破新低。",
        "",
        f"## 合格清單（{qualified.height} 檔）",
        "",
    ]
    if qualified.is_empty():
        lines += [
            "**本週轉折早段 0 檔合格。**（週報 quota 條款：必須明寫原因，見下方近似清單。）",
            "",
        ]
    else:
        lines += [
            "| 股號 | 名稱 | 主題 | 基本面 | 月營收YoY% | 位階依據 | 距60日低% | 連買天 "
            "| 外資20日(張) | 外資加速度 | 融資減肥 | 收盤 | 60日低 | 距季線% | flags |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in qualified.iter_rows(named=True):
            lines.append(
                f"| {r.get('stock_id')} | {r.get('name') or ''} | {r.get('theme') or ''} "
                f"| {r.get('fundamental_health') or '—'} | {_fmt(r.get('rev_yoy_pct'))} "
                f"| {r.get('位階依據') or '—'} | {_fmt(r.get('dist_low_60d_pct'))} "
                f"| {_fmt(r.get('foreign_inflection_days'))} "
                f"| {_fmt(r.get('foreign_net_lots'), 0)} "
                f"| {_fmt(r.get('foreign_flow_diff_5_20'))} | {_fmt(r.get('margin_slim'))} "
                f"| {_fmt(r.get('close'), 2)} | {_fmt(r.get('low_60d'), 2)} "
                f"| {_fmt(r.get('ma60_dist_pct'))} | {r.get('flags') or ''} |"
            )
        lines.append("")

    lines += [f"## 只差一條（{near_miss.height} 檔・供 quota 交代「差哪一條」）", ""]
    if near_miss.is_empty():
        lines += ["（本週無「只差一條」者。）", ""]
    else:
        # 這段只在「合格 0 檔」時才需要被逐檔讀（quota 條款要一句「最接近者差哪一條」），
        # 全列會有數十行、反而蓋掉上面的合格清單；截斷並註明總數。
        shown = near_miss.head(near_miss_limit) if near_miss_limit > 0 else near_miss
        lines += [
            "| 股號 | 名稱 | 差哪一條 | 基本面 | 距60日低% | 連買天 | 外資20日(張) |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in shown.iter_rows(named=True):
            lines.append(
                f"| {r.get('stock_id')} | {r.get('name') or ''} | **{r.get('差哪一條')}** "
                f"| {r.get('fundamental_health') or '—'} | {_fmt(r.get('dist_low_60d_pct'))} "
                f"| {_fmt(r.get('foreign_inflection_days'))} "
                f"| {_fmt(r.get('foreign_net_lots'), 0)} |"
            )
        if shown.height < near_miss.height:
            lines.append(
                f"\n（僅列前 {shown.height} 檔，另有 {near_miss.height - shown.height} "
                f"檔同為「只差一條」；完整名單見 `inflection_ambush_near_miss.csv`。）"
            )
        lines.append("")
    return "\n".join(lines)
