"""backtest/regime_slice.py — WS-H.4b：校準器主結果表的 regime 切片機制。

CP lift（stock_calib.py）與象限/起漲點校準（rotation_calib.py）的主結果表都建立在
「訊號觸發（入選事件）vs 起漲事件（episodes）」的命中判定上（rotation_calib.
evaluate_triggers）。本模組把同一套判定逐 trigger 展開（trigger_outcomes，鏡射
evaluate_triggers 的剔除規則，不改動該函式本身），按**事件日**（觸發日）join
regime 標籤（research/panel/regime_labels.parquet，settings 鍵
backtest.regime_history.output_path）分桶（進攻/中性/防禦；另「資料不足」＝regime
引擎當日判不出、「未標」＝regime 檔缺或 join 不到），逐桶重算命中率與 lift——
lift 的分母（隨機基率）也逐桶重算（regime_base_rates，鏡射 compute_base_rate 但
按合格日的 regime 分桶），避免「防禦期起漲本來就少 → 沿用全樣本基率灌高/壓低桶內
lift」的失真。CI 用 factor_lab.moving_block_bootstrap_ci 對桶內 per-date 命中率
序列（同日多事件先取當日均值）算 95% CI，除以該桶基率換算為 lift CI（線性變換
不改百分位邊界）；block 長＝校準器 horizon+1 交易日（週頻 ceil(h/5)+1）。

升降級標籤（docs/23 §1c 語彙）：qualified 桶（進攻/中性/防禦且 n≥30 事件）中
≥2 桶 lift 同側（同 >1 或同 <1）→「跨 regime 穩健」；僅進攻桶 lift>1 →
「bull-only」；無 qualified 桶 →「樣本不足」（照列不裁決）。

誠實邊界：regime 檔缺 → load_regime_labels 回空表 → 事件全落「未標」桶照列、
不裁決、不炸；桶內 <10 個交易日 → CI 誠實回 —（moving_block_bootstrap_ci 門檻）。
純函式＋一個 IO 讀檔函式（load_regime_labels），供 stock_calib.py／
rotation_calib.py 兩邊共用，避免各自兜一套判定邏輯造成漂移。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import polars as pl
from loguru import logger

from tw_screener.backtest.factor_lab import inference_footer, moving_block_bootstrap_ci

REGIME_ORDER: tuple[str, ...] = ("進攻", "中性", "防禦")
INSUFFICIENT_REGIME = "資料不足"
UNLABELED = "未標"
DISPLAY_ORDER: tuple[str, ...] = (*REGIME_ORDER, INSUFFICIENT_REGIME, UNLABELED)
MIN_REGIME_N = 30

_LABELS_SCHEMA: dict[str, type[pl.DataType]] = {"date": pl.Date, "regime_label": pl.Utf8}
_SLICE_SCHEMA: dict[str, type[pl.DataType]] = {
    "regime": pl.Utf8,
    "n_events": pl.Int64,
    "n_dates": pl.Int64,
    "hit_rate": pl.Float64,
    "base_rate": pl.Float64,
    "lift": pl.Float64,
    "bs_ci95_lo": pl.Float64,
    "bs_ci95_hi": pl.Float64,
}


def block_len_for_horizon(horizon_days: int, weekly: bool = False) -> int:
    """block 長＝校準器 horizon 對應交易日 +1；週頻資料 ceil(h/5)+1（WS-H.4b 規格）。"""
    h = max(1, int(horizon_days))
    return math.ceil(h / 5) + 1 if weekly else h + 1


def load_regime_labels(path: Path) -> pl.DataFrame:
    """讀 date/regime_label parquet；缺檔、壞檔或欄不符 → 空表（誠實跳過，呼叫端不炸）。"""
    if not path.exists():
        return pl.DataFrame(schema=_LABELS_SCHEMA)
    try:
        df = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — 研究軌讀檔容錯：壞檔誠實跳過整段，不中斷校準
        logger.warning(f"regime 標籤讀取失敗（{path}）：{exc}")
        return pl.DataFrame(schema=_LABELS_SCHEMA)
    if not {"date", "regime_label"}.issubset(df.columns):
        logger.warning(f"regime 標籤檔缺 date/regime_label 欄（{path}）")
        return pl.DataFrame(schema=_LABELS_SCHEMA)
    return df.select(["date", "regime_label"])


def trigger_outcomes(
    triggers: pl.DataFrame,
    episodes: pl.DataFrame,
    calendar: list[date],
    lead_window: int = 15,
    occupy_days: int = 15,
    warmup_pos: int = 0,
    key_col: str = "sub_industry",
) -> pl.DataFrame:
    """逐 trigger 命中/誤報（rotation_calib.evaluate_triggers 判定邏輯的事件級鏡射）。

    回每筆「有效」trigger（非 occupy 期、非尾端、非 warmup 前）一列 key_col/date/hit；
    聚合值（n_triggers/hit_rate/lift）刻意不在此重算——單一真相仍在 evaluate_triggers，
    本函式只把同一判定拆到事件級，供 regime_slice_table 逐桶重聚合。觸發為空回空表。
    """
    schema: dict[str, type[pl.DataType]] = {key_col: pl.Utf8, "date": pl.Date, "hit": pl.Boolean}
    if triggers.is_empty():
        return pl.DataFrame(schema=schema)
    pos = {d: i for i, d in enumerate(calendar)}
    max_eval = len(calendar) - 1 - lead_window

    ep_by_key: dict[str, list[int]] = {}
    for k, d in episodes.select([key_col, "start_date"]).iter_rows():
        if d in pos:
            ep_by_key.setdefault(k, []).append(pos[d])
    for v in ep_by_key.values():
        v.sort()

    rows: list[dict] = []
    for k, d in triggers.select([key_col, "date"]).iter_rows():
        p = pos.get(d)
        if p is None or p < warmup_pos or p > max_eval:
            continue
        eps = ep_by_key.get(k, [])
        if any(e < p <= e + occupy_days for e in eps):
            continue
        j = bisect_left(eps, p)
        hit = j < len(eps) and eps[j] - p <= lead_window
        rows.append({key_col: k, "date": d, "hit": hit})
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema)


def regime_base_rates(
    episodes: pl.DataFrame,
    calendar: list[date],
    keys: set[str] | list[str],
    regime_labels: pl.DataFrame,
    lead_window: int = 15,
    occupy_days: int = 15,
    warmup_pos: int = 0,
    key_col: str = "sub_industry",
) -> dict[str, float]:
    """隨機基率的逐 regime 桶版（rotation_calib.compute_base_rate 的分桶鏡射）。

    合格日（非占用、非尾端）按該日 regime 標籤分桶，各桶內算「起漲點落在其後
    lead_window 內」的比率。regime_labels 空 → 全部合格日落「未標」桶（＝全域基率）。
    回 {regime: base_rate}（僅含有合格日的桶）。
    """
    label_map: dict[date, str] = (
        dict(regime_labels.select(["date", "regime_label"]).iter_rows())
        if not regime_labels.is_empty()
        else {}
    )
    pos = {d: i for i, d in enumerate(calendar)}
    max_eval = len(calendar) - 1 - lead_window
    ep_by_key: dict[str, list[int]] = {}
    for k, d in episodes.select([key_col, "start_date"]).iter_rows():
        if d in pos:
            ep_by_key.setdefault(k, []).append(pos[d])
    for v in ep_by_key.values():
        v.sort()
    eligible: dict[str, int] = {}
    hit_days: dict[str, int] = {}
    for k in keys:
        eps = ep_by_key.get(k, [])
        for p in range(warmup_pos, max_eval + 1):
            if any(e < p <= e + occupy_days for e in eps):
                continue
            regime = label_map.get(calendar[p], UNLABELED)
            eligible[regime] = eligible.get(regime, 0) + 1
            j = bisect_left(eps, p)
            if j < len(eps) and eps[j] - p <= lead_window:
                hit_days[regime] = hit_days.get(regime, 0) + 1
    return {r: hit_days.get(r, 0) / n for r, n in eligible.items() if n}


def _slice_verdict(rows: list[dict], min_n: int) -> str:
    """跨 regime 升降級標籤（docs/23 §1c）：qualified＝進攻/中性/防禦、n≥min_n、lift 可算。"""
    qualified = [
        r
        for r in rows
        if r["regime"] in REGIME_ORDER and r["n_events"] >= min_n and r["lift"] is not None
    ]
    pos = [r["regime"] for r in qualified if r["lift"] > 1.0]
    neg = [r["regime"] for r in qualified if r["lift"] < 1.0]
    if len(pos) >= 2 or len(neg) >= 2:
        same_up = len(pos) >= 2
        tag = "（lift>1 同向）" if same_up else "（lift<1 同向＝跨 regime 否證）"
        minority = neg if same_up else pos
        return f"跨 regime 穩健{tag}" + (
            f"；惟 {'、'.join(minority)} 反向，照列" if minority else ""
        )
    if pos == ["進攻"]:
        return "bull-only"
    if not qualified:
        return "樣本不足"
    if len(pos) == 1:
        return f"單一 regime 有效（{pos[0]}，非多頭桶——仍缺跨 regime 對照，列候補）"
    return "無 regime 桶 lift>1（方向未成立）"


def regime_slice_table(
    outcomes: pl.DataFrame,
    regime_labels: pl.DataFrame,
    base_rates: Mapping[str, float],
    horizon_days: int,
    weekly: bool = False,
    min_n: int = MIN_REGIME_N,
) -> tuple[pl.DataFrame, str]:
    """逐桶重算命中率/lift＋bs_CI95，回 (表, 跨 regime 升降級標籤)。

    outcomes：trigger_outcomes() 輸出（date/hit）。regime_labels：load_regime_labels()
    輸出；空表 → 全數落「未標」桶（照列不裁決）。base_rates：regime_base_rates() 輸出。
    lift 與 CI 皆以「桶內 per-date 命中率序列」算：同日多事件先取當日均值，再對序列
    做 moving_block_bootstrap_ci（block=block_len_for_horizon(horizon_days)），CI 邊界
    除以該桶基率換算為 lift CI。桶基率缺或為 0 → lift/CI 誠實回 None。
    """
    if outcomes.is_empty():
        return pl.DataFrame(schema=_SLICE_SCHEMA), "樣本不足"

    if regime_labels.is_empty():
        labeled = outcomes.with_columns(pl.lit(UNLABELED).alias("regime_label"))
    else:
        labeled = outcomes.join(regime_labels, on="date", how="left").with_columns(
            pl.col("regime_label").fill_null(UNLABELED)
        )
    block_len = block_len_for_horizon(horizon_days, weekly)

    found = set(labeled["regime_label"].unique().to_list())
    rows: list[dict] = []
    for regime in [r for r in DISPLAY_ORDER if r in found]:
        sub = labeled.filter(pl.col("regime_label") == regime)
        daily = (
            sub.group_by("date")
            .agg(pl.col("hit").cast(pl.Float64).mean().alias("hit_mean"))
            .sort("date")
        )
        values = daily["hit_mean"].to_list()
        hit_rate = sum(values) / len(values) if values else None
        ci_lo, ci_hi = moving_block_bootstrap_ci(values, block_len) if values else (None, None)
        br = base_rates.get(regime)
        lift: float | None = None
        ci_lift_lo: float | None = None
        ci_lift_hi: float | None = None
        if hit_rate is not None and br is not None and br > 0:
            lift = hit_rate / br
            ci_lift_lo = ci_lo / br if ci_lo is not None else None
            ci_lift_hi = ci_hi / br if ci_hi is not None else None
        rows.append(
            {
                "regime": regime,
                "n_events": sub.height,
                "n_dates": len(values),
                "hit_rate": hit_rate,
                "base_rate": br,
                "lift": lift,
                "bs_ci95_lo": ci_lift_lo,
                "bs_ci95_hi": ci_lift_hi,
            }
        )
    return pl.DataFrame(rows, schema=_SLICE_SCHEMA), _slice_verdict(rows, min_n)


def regime_dist_desc(table: pl.DataFrame) -> str:
    """regime 分布摘要文字（供 inference_footer 第一行沿用）。"""
    if table.is_empty():
        return "regime 標籤未附或無可用觸發事件"
    return "、".join(f"{r['regime']} n={r['n_events']}" for r in table.iter_rows(named=True))


def render_regime_slice_table(
    table: pl.DataFrame, verdict: str, min_n: int = MIN_REGIME_N
) -> list[str]:
    """切片表主體 markdown（表格＋升降級標籤行）；不含 heading/footer，供組段落者複用。"""
    lines = [
        "| regime | n 事件 | n 日 | 命中率 | lift | bs_CI95(lift) | 備註 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in table.iter_rows(named=True):
        hr = f"{r['hit_rate']:.1%}" if r["hit_rate"] is not None else "—"
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        ci = (
            f"[{r['bs_ci95_lo']:.2f}, {r['bs_ci95_hi']:.2f}]"
            if r["bs_ci95_lo"] is not None
            else "—（<10 日不可重抽）"
        )
        if r["regime"] == UNLABELED:
            note = "regime 檔缺或事件日 join 不到"
        elif r["regime"] in REGIME_ORDER and r["n_events"] < min_n:
            note = f"樣本不足（<{min_n} 事件），照列不裁決"
        else:
            note = ""
        lines.append(
            f"| {r['regime']} | {r['n_events']} | {r['n_dates']} | {hr} | {lift} | {ci} | {note} |"
        )
    lines += ["", f"- 跨 regime 升降級：**{verdict}**（docs/23 §1c 語彙）"]
    return lines


def slice_method_desc(horizon_days: int, weekly: bool = False) -> str:
    """inference_footer 第二行（推論方法）標準文字。"""
    freq = "週頻" if weekly else "日頻"
    return (
        f"moving-block bootstrap（block={block_len_for_horizon(horizon_days, weekly)}"
        f"・{freq} horizon={horizon_days}・B=1000・seed=42）對桶內 per-date 命中率序列"
        "（同日多事件先取當日均值）算 CI95，除以該桶基率換算為 lift CI"
    )


def render_regime_slice_section(
    heading: str,
    table: pl.DataFrame,
    verdict: str,
    sample_span: str,
    horizon_days: int,
    membership_desc: str,
    weekly: bool = False,
) -> list[str]:
    """完整切片段落：heading＋表＋升降級標籤＋inference_footer 三行；空表誠實跳過。"""
    lines = ["", heading, ""]
    if table.is_empty():
        lines.append("（regime 標籤缺席或無可用觸發事件——本段誠實跳過，不影響上方既有結果。）")
        return lines
    lines += render_regime_slice_table(table, verdict)
    lines += inference_footer(
        sample_span,
        regime_dist_desc(table),
        slice_method_desc(horizon_days, weekly),
        membership_desc,
    )
    return lines
