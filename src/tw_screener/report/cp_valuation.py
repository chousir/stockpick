"""個股相對估值表（CP 值補漲研究 C1，docs/13-cp-value-research.md §4 Phase C）。

生產軌：把 analysis/valuation.py 的橫斷面相對估值（官方 trailing PE 主、PB 補虧損股）渲染成
cp_valuation.md（人讀）+ .csv（機器讀）。這是 C2 三重濾網「相對便宜」那層的輸入；本身
**不下買/不買結論、不給目標價**（守人設）。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl


def render_valuation_report(
    valuation: pl.DataFrame,
    week_tag: str,
    output_dir: Path,
    meta: dict,
    names: dict[str, str] | None = None,
    max_rows: int = 40,
) -> Path:
    """渲染 cp_valuation.md + 寫 cp_valuation.csv，回傳 md 路徑。

    表列「相對便宜」優先（依次產業百分位遞增）取前 max_rows；空表仍出報告（誠實標）。
    """
    names = names or {}
    output_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# 個股相對估值表（C1，橫斷面・官方 trailing PE/PB） — {week_tag}",
        "",
        f"- 資料日：{meta.get('data_date', '?')}・宇宙：{meta.get('universe', '上市+上櫃')}・"
        f"{meta.get('n_stocks', 0)} 檔",
        f"- 有 PE {meta.get('n_with_pe', 0)} 檔（{meta.get('pe_coverage_pct', 0)}%）・"
        f"無本益比（虧損/無正盈餘，改用 PB） {meta.get('n_no_pe', 0)}",
        f"- 有次產業相對位階 {meta.get('n_with_relative', 0)} 檔"
        f"（其中退用 PB {meta.get('n_via_pb', 0)}）・標『相對便宜』 {meta.get('n_cheap', 0)} 檔",
        "",
        "## 資料現實與誠實但書",
        "",
        *[f"> {n}" for n in meta.get("notes", [])],
        "",
        "## 相對便宜排名（依所屬次產業百分位遞增，0=次產業內最便宜）",
        "",
        "> 欄位：鏡頭＝PE 主／PB 補(虧損股)・值＝該鏡頭比值・次中位＝次產業同儕中位數・"
        "相對%＝(值/次中位−1)・百分位＝次產業內升冪百分位・同儕＝有效同儕數・殖利率%＝官方",
        "",
    ]

    ranked = valuation.filter(pl.col("val_pctile").is_not_null())
    if ranked.is_empty():
        lines.append("（本期無可計算次產業相對位階的個股——同儕有效數不足。）")
    else:
        if max_rows > 0:
            ranked = ranked.head(max_rows)
        lines += [
            "| 股號 | 名稱 | 市場 | 鏡頭 | 值 | 次中位 | 相對% | 百分位 | 同儕 | 殖利率% | 標記 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in ranked.iter_rows(named=True):
            metric = r.get("val_metric") or "—"
            val = r["pbr"] if metric == "PB" else r["pe"]
            val_s = f"{val:.2f}" if val is not None else "—"
            med = f"{r['val_median']:.2f}" if r["val_median"] is not None else "—"
            rel = f"{r['val_vs_subind_pct']:+.1f}" if r["val_vs_subind_pct"] is not None else "—"
            pct = f"{r['val_pctile']:.0f}" if r["val_pctile"] is not None else "—"
            dy = f"{r['dividend_yield']:.2f}" if r["dividend_yield"] is not None else "—"
            lines.append(
                f"| {r['stock_id']} | {names.get(r['stock_id'], '')} | {r['market']} | {metric} "
                f"| {val_s} | {med} | {rel} | {pct} | {r['val_peer_n']} | {dy} "
                f"| {r['cheap_flag'] or '—'} |"
            )
    lines += [
        "",
        "---",
        "",
        "> 這是**相對便宜（trailing 橫斷面）**參考，非前瞻估值、非買/不買結論、不給目標價。",
        "> 便宜可能是市場已反映風險（賺虧本、衰退、治理疑慮），須配合 B 階段資金/位階與個股",
        "> 基本面判讀（守人設「多空並陳、由人決策」）。隨官方日 ratios 累積，未來可補歷史百分位。",
        "",
    ]
    md_path = output_dir / "cp_valuation.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    csv_out = (
        valuation.with_columns(
            pl.col("stock_id")
            .map_elements(lambda s: names.get(s, ""), return_dtype=pl.Utf8)
            .alias("stock_name")
        )
        if valuation.height
        else valuation
    )
    csv_out.write_csv(output_dir / "cp_valuation.csv")
    return md_path
