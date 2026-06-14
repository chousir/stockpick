"""個股 CP 值補漲候選清單（CP 值補漲研究 B3，docs/13-cp-value-research.md §4 Phase B）。

生產軌，與 backtest/stock_calib.py（B2 研究軌）對偶——就像 report/rotation_report.py
（生產）對偶 backtest/rotation_calib.py（研究）。拿 B2 校準勝出的因子組合，掃個股
特徵面板（B1 analysis/stock_panel.py）的**最新一筆快照**，產「本週 CP 候選清單」。

定位（守 docs/13 §0 / CLAUDE.md Part 3 人設）：
- 產出是「**把勝率疊高的觀察清單**」，不是預測、不下買/不買結論、不給目標價。
- 因子組合來自 B2 校準（每季重校），不寫死在程式裡——規則進 settings.cp_value.candidate。

三種 label 型態（B2 裁決全上線；門檻全在 settings）：
  L1 埋伏  ：資金 z 高 ＋ 價貼低（+low）——B2 lift 1.6–1.8、領先 ~7 日。
  L2 追突破：資金 z 高 ＋ 價貼低（剛離低也涵蓋在 +low≤position_low_pct 帶）。
  L3 反轉  ：資金 z 高 ＋ 加速中（+mom）＋ **深跌情境**（距高 ≤ −drawdown%）
            ＋ **確認非單日 spike**（近 confirm_days 個交易日法人合計連續淨買）。
            docs/13 §3 第三關：單日資金 flip 接刀率最高，故反轉須過確認門檻才進清單。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from loguru import logger

# label key → 報表顯示型態名
_LABEL_NAMES: dict[str, str] = {"ambush": "埋伏", "breakout": "追突破", "reversal": "反轉"}
# 資金 z 欄字首 → 其加速度欄（B1 stock_panel 命名；與 stock_calib 一致）
_MOM_BY_PREFIX: dict[str, str] = {
    "net_flow": "flow_momentum",
    "foreign_flow": "foreign_momentum",
    "trust_flow": "trust_momentum",
}


def _prefix_col(df: pl.DataFrame, prefix: str) -> str | None:
    """panel 隨 position_window 命名的位階欄（如 above_low_60d_pct）。取首見。"""
    for c in df.columns:
        if c.startswith(prefix) and c.endswith("d_pct"):
            return c
    return None


def _mom_col(z_col: str) -> str:
    """資金 z 欄 → 對應加速度欄（trust_flow_20d_z → trust_momentum）。"""
    for pfx, mom in _MOM_BY_PREFIX.items():
        if z_col.startswith(pfx):
            return mom
    return "flow_momentum"


def latest_snapshot(panel: pl.DataFrame) -> pl.DataFrame:
    """每檔取自身最新一筆（各股以其最後有資料日為準，非全宇宙同一日）。"""
    if panel.is_empty():
        return panel
    return panel.filter(pl.col("date") == pl.col("date").max().over("stock_id"))


def recent_buy_confirm(
    panel: pl.DataFrame, institutional: pl.DataFrame, confirm_days: int = 2
) -> pl.DataFrame:
    """L3 反轉確認（docs/13 §3 第三關）：近 confirm_days 個交易日法人合計**連續淨買**。

    對齊 panel 交易日曆（缺紀錄＝當日淨額 0，沿用補 0 慣例），逐股取其最後一日的
    近 confirm_days 日淨額最小值 > 0 即確認（過濾單日資金 spike）。
    回傳 (stock_id, confirm_buy)；暖機不足（資料 < confirm_days）→ 不確認。
    """
    if panel.is_empty():
        return pl.DataFrame(schema={"stock_id": pl.Utf8, "confirm_buy": pl.Boolean})
    keys = panel.select(["date", "stock_id"]).unique()
    if institutional.is_empty():
        nets = keys.with_columns(pl.lit(0, dtype=pl.Int64).alias("total_net"))
    else:
        daily = institutional.select(["date", "stock_id", "total_net"]).unique(["date", "stock_id"])
        nets = keys.join(daily, on=["date", "stock_id"], how="left").with_columns(
            pl.col("total_net").fill_null(0)
        )
    nets = nets.sort(["stock_id", "date"]).with_columns(
        pl.col("total_net")
        .rolling_min(confirm_days, min_samples=confirm_days)
        .over("stock_id")
        .alias("_min_net")
    )
    last = nets.filter(pl.col("date") == pl.col("date").max().over("stock_id"))
    return last.select(
        "stock_id", (pl.col("_min_net") > 0).fill_null(False).alias("confirm_buy")
    )


def build_cp_candidates(
    snapshot: pl.DataFrame,
    confirm: pl.DataFrame,
    rules: list[dict],
    position_low_pct: float = 15.0,
    drawdown_pct: float = 20.0,
    cp_ceiling: float = 60.0,
    max_candidates: int = 40,
) -> pl.DataFrame:
    """套 B2 勝出規則於最新快照，產標註後的 CP 候選表（docs/13 B3）。

    Args:
        snapshot: latest_snapshot 輸出（每檔一列、含 B1 特徵）
        confirm: recent_buy_confirm 輸出（stock_id, confirm_buy）
        rules: settings.cp_value.candidate.rules；每條 dict：
            name / label(ambush|breakout|reversal) / flow_z_col / z_threshold /
            modifier(low|mom|none) / require_confirm(僅 reversal 用)
        position_low_pct: +low 修飾＝距低 ≤ 此值（價貼低）
        drawdown_pct: reversal 深跌情境＝距高 ≤ −此值
        cp_ceiling: cp_score 的位階剩餘空間 room 天花板（同 A1）
        max_candidates: **每個主型態**各取前 N（≤0 不截）——三型態各自獵場，避免
            貼低高分的埋伏把離低的反轉全擠掉

    Returns:
        每檔一列：stock_id/date/primary_label/labels/rules/cp_score/flow_z_col/flow_z/
        freshness/above_low_pct/above_high_pct/confirm_buy，依 cp_score 遞減。空輸入 → 空表。
    """
    if snapshot.is_empty() or not rules:
        return pl.DataFrame()
    low_col = _prefix_col(snapshot, "above_low_")
    high_col = _prefix_col(snapshot, "above_high_")
    snap = snapshot.join(confirm, on="stock_id", how="left").with_columns(
        pl.col("confirm_buy").fill_null(False)
    )
    room = (
        (1 - pl.col(low_col) / cp_ceiling).clip(0.0, 1.0)
        if low_col
        else pl.lit(None, dtype=pl.Float64)
    )

    active: list[tuple[int, dict]] = []
    for i, r in enumerate(rules):
        zc = str(r["flow_z_col"])
        if zc not in snap.columns:  # 規則引用面板沒有的欄 → 誠實略過
            logger.warning("CP 候選規則 {} 引用不存在欄 {}，略過", r.get("name"), zc)
            continue
        cond = pl.col(zc) > float(r["z_threshold"])
        mod = str(r.get("modifier", "none"))
        if mod == "low" and low_col:
            cond = cond & (pl.col(low_col) <= position_low_pct)
        elif mod == "mom":
            cond = cond & (pl.col(_mom_col(zc)) > 0)
        if r["label"] == "reversal":
            if high_col:
                cond = cond & (pl.col(high_col) <= -drawdown_pct)
            if r.get("require_confirm", True):
                cond = cond & pl.col("confirm_buy")
        cond = cond.fill_null(False)
        snap = snap.with_columns(
            cond.alias(f"_hit{i}"),
            pl.when(cond).then((pl.col(zc) * room).round(3)).otherwise(None).alias(f"_score{i}"),
        )
        active.append((i, r))
    if not active:
        return pl.DataFrame()

    cand = snap.filter(pl.any_horizontal([pl.col(f"_hit{i}") for i, _ in active]))
    if cand.is_empty():
        return pl.DataFrame()

    rows: list[dict] = []
    for rec in cand.iter_rows(named=True):
        hit_rules: list[str] = []
        labels: list[str] = []
        best: dict | None = None
        best_score: float | None = None
        for i, r in active:
            if not rec[f"_hit{i}"]:
                continue
            hit_rules.append(str(r["name"]))
            labels.append(_LABEL_NAMES.get(str(r["label"]), str(r["label"])))
            sc = rec[f"_score{i}"]
            if sc is not None and (best_score is None or sc > best_score):
                best_score, best = sc, r
        if best is None:  # 全 hit 規則 score 皆 null（room 缺）→ 取首條命中規則
            best = next(r for i, r in active if rec[f"_hit{i}"])
        zc = str(best["flow_z_col"])
        mom = rec.get("flow_momentum")
        zval = rec.get(zc)
        rows.append(
            {
                "stock_id": rec["stock_id"],
                "date": rec["date"],
                "primary_label": str(best["label"]),  # 最高分規則的型態（分組／排序用）
                "labels": "/".join(dict.fromkeys(labels)),
                "rules": "/".join(dict.fromkeys(hit_rules)),
                "cp_score": best_score,
                "flow_z_col": zc,
                "flow_z": round(zval, 2) if zval is not None else None,
                "freshness": "加速" if (mom or 0) > 0 else ("放緩" if (mom or 0) < 0 else None),
                "above_low_pct": (
                    round(rec[low_col], 1) if low_col and rec.get(low_col) is not None else None
                ),
                "above_high_pct": (
                    round(rec[high_col], 1) if high_col and rec.get(high_col) is not None else None
                ),
                "confirm_buy": bool(rec["confirm_buy"]),
            }
        )
    # 以 flow_z 為次序鍵：反轉股多離低 → room=0 → cp_score 全 0，靠資金 z 才排得開。
    sort_keys, sort_desc = ["cp_score", "flow_z"], [True, True]
    out = pl.DataFrame(rows).sort(sort_keys, descending=sort_desc, nulls_last=True)
    # 每個主型態各取前 max_candidates：三型態是各自獵場（docs/13 §3），全域排名會讓
    # 「貼低高分」的埋伏淹沒「離低」的反轉，故分型態截斷確保各型態都看得到。
    if max_candidates > 0:
        out = out.group_by("primary_label", maintain_order=True).head(max_candidates)
        out = out.sort(sort_keys, descending=sort_desc, nulls_last=True)
    return out


def attach_subind_quadrant(
    candidates: pl.DataFrame, membership: pl.DataFrame, rotation_csv: Path
) -> pl.DataFrame:
    """疊上每檔所屬次產業象限（讀本週 sector_rotation.csv；無則誠實降級「—」）。

    一檔多標籤 → 列出各「次產業(象限)」；象限缺（成員不足未列輪動排名）→ 標「榜外」。
    """
    if candidates.is_empty():
        return candidates.with_columns(pl.lit("—").alias("subind_quadrant"))
    quad_map: dict[str, str] = {}
    if rotation_csv.exists():
        try:
            rt = pl.read_csv(rotation_csv, infer_schema_length=0)
            if {"sub_industry", "quadrant"} <= set(rt.columns):
                quad_map = dict(zip(rt["sub_industry"].to_list(), rt["quadrant"].to_list()))
        except Exception as exc:  # noqa: BLE001 — 壞快照不該擋候選清單
            logger.warning("讀 sector_rotation.csv 失敗（象限降級）：{}", exc)
    subs_by_stock: dict[str, list[str]] = {}
    for sub, sid in membership.iter_rows():
        subs_by_stock.setdefault(sid, []).append(sub)

    vals: list[str] = []
    for sid in candidates["stock_id"].to_list():
        subs = subs_by_stock.get(sid, [])
        if not subs:
            vals.append("未標次產業")
        elif not quad_map:
            vals.append("—")  # 無輪動快照可對
        else:
            vals.append("; ".join(f"{s}({quad_map.get(s, '榜外')})" for s in subs))
    return candidates.with_columns(pl.Series("subind_quadrant", vals))


def attach_valuation(
    candidates: pl.DataFrame, valuation: pl.DataFrame, cheap_pctile: float = 30.0
) -> pl.DataFrame:
    """疊 C1 估值層 → 三重濾網第三道「相對便宜」（docs/13 §C2）。

    candidates 已是 B3 命中＝**錢進來（資金 z）＋型態/位階（前兩道濾網）**；本函式 join
    C1 的次產業相對 PE，標第三道。三態（triple_filter）：
      ✓三重 ＝有相對位階且次產業 PE 百分位 ≤ cheap_pctile（錢進＋沒漲＋相對便宜全過）
      估值缺＝無相對位階（缺 EPS／同儕不足）——誠實標未知，**不當貴亦不剔除候選**
      —    ＝有相對位階但不在便宜帶
    新增欄：pe / pe_subind_pctile / triple_filter。
    """
    if candidates.is_empty():
        return candidates
    if valuation.is_empty():
        v = candidates.select("stock_id").with_columns(
            pl.lit(None, dtype=pl.Float64).alias("pe"),
            pl.lit(None, dtype=pl.Float64).alias("pe_subind_pctile"),
        )
    else:
        v = valuation.select("stock_id", "pe", "pe_subind_pctile")
    return candidates.join(v, on="stock_id", how="left").with_columns(
        pl.when(pl.col("pe_subind_pctile").is_null())
        .then(pl.lit("估值缺"))
        .when(pl.col("pe_subind_pctile") <= cheap_pctile)
        .then(pl.lit("✓三重"))
        .otherwise(pl.lit("—"))
        .alias("triple_filter")
    )


def tag_holding_status(
    candidates: pl.DataFrame,
    holdings: list[str],
    watch: list[str],
    hits: list[str],
) -> pl.DataFrame:
    """疊庫存/觀察/本週命中（docs/13 B3「與庫存/觀察/本週命中疊圖」）→ watch_status 欄。"""
    if candidates.is_empty():
        return candidates.with_columns(pl.lit("").alias("watch_status"))
    hset, wset, hitset = set(holdings), set(watch), set(hits)
    vals: list[str] = []
    for sid in candidates["stock_id"].to_list():
        s = []
        if sid in hset:
            s.append("庫存")
        if sid in wset:
            s.append("觀察")
        if sid in hitset:
            s.append("本週命中")
        vals.append("/".join(s))
    return candidates.with_columns(pl.Series("watch_status", vals))


def render_cp_candidates_report(
    candidates: pl.DataFrame,
    week_tag: str,
    output_dir: Path,
    params: dict,
    coverage: dict,
    rules: list[dict],
    names: dict[str, str] | None = None,
    data_date: str = "",
) -> Path:
    """渲染 cp_candidates.md（人讀）+ 寫 cp_candidates.csv（機器讀），回傳 md 路徑。

    candidates 須已過 attach_subind_quadrant + tag_holding_status。空清單仍出報告
    （誠實標「本週無候選」），不靜默。
    """
    names = names or {}
    output_dir.mkdir(parents=True, exist_ok=True)

    n_total = candidates.height
    n_hold = candidates.filter(pl.col("watch_status").str.contains("庫存")).height if n_total else 0
    n_watch = (
        candidates.filter(pl.col("watch_status").str.contains("觀察")).height if n_total else 0
    )
    n_hit = (
        candidates.filter(pl.col("watch_status").str.contains("本週命中")).height if n_total else 0
    )
    has_val = "triple_filter" in candidates.columns
    n_triple = (
        candidates.filter(pl.col("triple_filter") == "✓三重").height if (n_total and has_val) else 0
    )

    rule_lines = []
    for r in rules:
        mod = str(r.get("modifier", "none"))
        mod_desc = {"low": "＋價貼低", "mom": "＋加速中", "none": ""}.get(mod, "")
        extra = ""
        if r["label"] == "reversal":
            dd = params.get("drawdown_pct", 20)
            cd = params.get("confirm_days", 2)
            extra = f"＋深跌(距高≤−{dd:g}%)＋連{cd}日續買確認"
        rule_lines.append(
            f"- **{r['name']}**（{_LABEL_NAMES.get(str(r['label']), r['label'])}）："
            f"{r['flow_z_col']} z>{r['z_threshold']}{mod_desc}{extra}"
        )

    lines = [
        f"# 個股 CP 值補漲候選清單 — {week_tag}",
        "",
        f"- 資料日：{data_date or coverage.get('date_max', '?')}・"
        f"宇宙：{coverage.get('universe', 'listed')}・{coverage.get('n_stocks', 0)} 檔",
        f"- 法人覆蓋 {coverage.get('inst_coverage_pct', 0)}%・"
        f"次產業標記覆蓋 {coverage.get('subind_coverage_pct', 0)}%",
        f"- 本週候選 **{n_total}** 檔"
        + (
            f"（庫存 {n_hold}・觀察 {n_watch}・本週命中 {n_hit}"
            + (f"・三重濾網全過 {n_triple}" if has_val else "")
            + "）"
            if n_total
            else ""
        ),
        "",
        "## 因子規則（B2 校準勝出組合；門檻見 settings.cp_value.candidate）",
        "",
        *rule_lines,
        "",
        "## 候選清單（分型態，各依 cp_score＝資金 z × 位階剩餘空間 遞減）",
        "",
        "> 三型態是各自獵場，分開列；同股若多型態命中，歸最高分型態、規則欄保留全部。",
        "> 欄位：CP＝cp_score（資金 z × 位階空間）・z＝資金 z・距低/距高＝距 60 日低/高 %",
        *(
            [
                "> 三重＝C2 三重濾網（錢進＋沒漲＋相對便宜）：✓三重＝次產業 PE 百分位 ≤ 便宜門檻；"
                "估值缺＝缺 EPS/同儕不足；次位＝次產業 PE 升冪百分位（0=最便宜，C1 單季年化代理）。"
            ]
            if has_val
            else []
        ),
        "",
    ]
    if n_total == 0:
        lines.append("（本週無個股同時滿足任一規則——錢進且價貼低/深跌反轉的標的暫缺。）")
    else:
        head = "| 股號 | 名稱 | 規則 | CP | z | 新鮮 | 距低% | 距高% | 確認 | 象限 | 持有 |"
        sep = "|---|---|---|---|---|---|---|---|---|---|---|"
        if has_val:
            head += " PE | 次位 | 三重 |"
            sep += "---|---|---|"
        for key in ("ambush", "breakout", "reversal"):
            grp = candidates.filter(pl.col("primary_label") == key)
            lines += [f"### {_LABEL_NAMES[key]}（{grp.height} 檔）", ""]
            if grp.is_empty():
                lines += ["（本型態本週無候選。）", ""]
                continue
            lines += [head, sep]
            for r in grp.iter_rows(named=True):
                cp = f"{r['cp_score']:.2f}" if r["cp_score"] is not None else "—"
                fz = f"{r['flow_z']:.2f}" if r["flow_z"] is not None else "—"
                al = f"{r['above_low_pct']:.1f}" if r["above_low_pct"] is not None else "—"
                ah = f"{r['above_high_pct']:.1f}" if r["above_high_pct"] is not None else "—"
                confirm = "✓" if r["confirm_buy"] else "—"
                row = (
                    f"| {r['stock_id']} | {names.get(r['stock_id'], '')} | {r['rules']} "
                    f"| {cp} | {fz} | {r['freshness'] or '—'} | {al} | {ah} "
                    f"| {confirm} | {r['subind_quadrant']} | {r['watch_status'] or '—'} |"
                )
                if has_val:
                    pe = f"{r['pe']:.1f}" if r.get("pe") is not None else "—"
                    pct = (
                        f"{r['pe_subind_pctile']:.0f}"
                        if r.get("pe_subind_pctile") is not None
                        else "—"
                    )
                    row += f" {pe} | {pct} | {r['triple_filter']} |"
                lines.append(row)
            lines.append("")

    lines += [
        "",
        "---",
        "",
        "> 這是**觀察清單**，不是買/不買結論、不給目標價。個股持有決策仍須個股層籌碼／",
        "> 基本面判讀（守人設「多空並陳、由人決策」）。",
        "> 因子來自 B2 回測（現實天花板 lift ~1.3–1.5，疊高勝率非預測）；隨資料累積每季重校。",
        "> L3 反轉為三型態中確定性最低（真反轉與逃命反彈當下難分），已套深跌情境＋",
        "> 連續續買確認，仍需謹慎。資金 z＝個股自身 60 日滾動 z；新鮮度＝法人合計加速度正負。",
        *(
            [
                "> 三重濾網「相對便宜」最適用埋伏/追突破；反轉本就深跌、便宜常與深跌同源，"
                "非獨立加成。估值＝C1 單季年化代理（非真 TTM）、橫斷面相對非前瞻，「便宜」",
                "亦可能是市場已反映風險，須配合個股基本面判讀。",
            ]
            if has_val
            else []
        ),
        "",
    ]
    md_path = output_dir / "cp_candidates.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    csv_out = (
        candidates.with_columns(
            pl.col("stock_id")
            .map_elements(lambda s: names.get(s, ""), return_dtype=pl.Utf8)
            .alias("stock_name")
        )
        if n_total
        else candidates
    )
    csv_out.write_csv(output_dir / "cp_candidates.csv")
    return md_path
