"""analysis/macro_regime.py — 總經避險層總控（docs/25 v2）。

設計意圖（docs/25 v2，三輪 research/macro_regime_screening/ block-bootstrap 篩選實證後改版）：
  單一主訊號（BAA10Y level_pct）決定燈色；其餘指標（DGS20/VIXCLS/DCOILWTICO/STLFSI4/
  DGS10/DEXTAUS/DEXJPUS）降為輔助揭露欄位，不進計分——round 3 已證明加權合成本身會
  稀釋訊號而非放大，v2 因此不做群組合成，維持「不合成、並列揭露」鐵則往內層再套一次。

關鍵約定：
  - 評分為純函式（level_pct/speed_pct/dual_risk/classify_light/compute_macro_light）；
    IO（讀 FRED 快取、讀寫 history.parquet）集中在 compute_market_macro/run_macro 便利包裝，
    比照 regime.py 的純函式/IO 分離。
  - 所有窗長／門檻／遲滯帶由 settings.macro_regime 傳入，不寫死（鐵律 5）。
  - level_pct/speed_pct 嚴格因果：只用 as_of（含）以前的資料，這是三輪研究有效性的前提，
    production 端沿用同一份不變（look-ahead 迴歸測試見 tests/analysis/test_macro_regime.py）。
  - 主訊號 stale／未取得 → 燈號「資料不足」（灰）；揭露面板個別指標 stale → 該欄「未取得」，
    不影響燈色判定（誠實原則：沒資料不是沒風險，也不拿舊值裝新鮮）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

GREEN = "綠"
YELLOW = "黃"
RED = "紅"
INSUFFICIENT = "資料不足"

_ADVICE = {
    RED: "系統性風險水位高，評估分批降低曝險／暫緩新進場。",
    YELLOW: "風險水位中等，維持觀察，無需立即調整。",
    GREEN: "風險水位偏低，維持標準流程。",
    INSUFFICIENT: "資料不足，無法判定總經燈號（請確認 FRED 快取／主訊號序列）。",
}

# 窗口/差分序列樣本過薄時百分位沒有統計意義（純數值穩定性防呆，非商業門檻，不進 settings）。
_MIN_OBS = 30


@dataclass(frozen=True)
class IndicatorReading:
    """單一序列的讀值（原始值＋依 transform 算出的風險分數 ∈[0,1]，raw transform 無 score）。"""

    series_id: str
    transform: str  # level_pct / speed_pct / dual_risk / raw
    observed_date: date | None
    raw_value: float | None
    score: float | None  # None＝未取得或 raw transform（純揭露不計分）
    stale: bool


@dataclass(frozen=True)
class MacroLight:
    """總經燈號判定結果（主訊號燈色＋分數＋主訊號讀值＋揭露面板＋上週顏色）。"""

    as_of: date | None
    color: str
    risk_score: float | None  # 主訊號 score×100 ∈[0,100]；None＝資料不足
    primary: IndicatorReading
    disclosure: list[IndicatorReading]
    prev_color: str | None


@dataclass(frozen=True)
class PanelDelta:
    """單一序列相對「上一次不同 run」的變化（docs/26 §7.1，純揭露、不進計分）。

    arrow＝"↑"/"↓"/"→"/`NO_PREV`（無前次或本次未取得）。docs/26 §5.1 的動機：水位百分位在
    趨勢序列上會常駐高位（DGS20 兩年內 34% 週次 ≥p90），「水位高」本身鑑別力低，
    「水位在變」才有資訊——所以面板要能區分慢磨與急衝。
    """

    series_id: str
    prev_run_as_of: date | None
    prev_as_of: date | None
    prev_raw_value: float | None
    prev_score_pct: float | None
    delta_raw_value: float | None
    delta_score_pct: float | None
    arrow: str


# 無前次可比（首次跑／本次未取得）的箭頭佔位符——誠實顯示「不知道」，不用單列硬算變化。
NO_PREV = "—"


def describe_macro_light(
    light: MacroLight, deltas: dict[str, PanelDelta] | None = None
) -> dict[str, object]:
    """MacroLight → 報表/CLI 共用顯示 dict（docs/25 v2 §4.2 格式）。

    deltas 給定時（docs/26 A案）每個讀值附一個 `delta` 子 dict；None＝不顯示變化欄
    （向後相容：panel_history 尚未累積或呼叫端不需要變化欄時的原行為）。
    """

    def _fmt_delta(series_id: str) -> dict[str, object] | None:
        if deltas is None:
            return None
        d = deltas.get(series_id)
        if d is None:
            return {"arrow": NO_PREV, "prev_as_of": None, "score_pct": None, "raw_value": None}
        return {
            "arrow": d.arrow,
            "prev_as_of": d.prev_as_of.isoformat() if d.prev_as_of else None,
            "score_pct": round(d.delta_score_pct, 1) if d.delta_score_pct is not None else None,
            "raw_value": round(d.delta_raw_value, 4) if d.delta_raw_value is not None else None,
        }

    def _fmt_reading(r: IndicatorReading) -> dict[str, object]:
        return {
            "series_id": r.series_id,
            "transform": r.transform,
            "as_of": r.observed_date.isoformat() if r.observed_date else None,
            "raw_value": round(r.raw_value, 4) if r.raw_value is not None else None,
            "score_pct": round(r.score * 100, 1) if r.score is not None else None,
            "stale": r.stale,
            "delta": _fmt_delta(r.series_id),
        }

    score_str = f"{light.risk_score:.0f}/100" if light.risk_score is not None else "—"
    line = f"總經燈號：{light.color} {score_str}"
    change_line = None
    if light.prev_color and light.prev_color != INSUFFICIENT and light.prev_color != light.color:
        change_line = f"{light.prev_color} → {light.color}"
    return {
        "color": light.color,
        "risk_score": light.risk_score,
        "as_of": light.as_of.isoformat() if light.as_of else None,
        "primary": _fmt_reading(light.primary),
        "disclosure": [_fmt_reading(r) for r in light.disclosure],
        "prev_color": light.prev_color,
        "change_line": change_line,
        "advice": _ADVICE.get(light.color, ""),
        "line": line,
    }


def _sorted_nonnull(df: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """篩到 date<=as_of、value 非 null、依 date 排序（causal 計算的共用基礎）。"""
    if df.is_empty() or not {"date", "value"}.issubset(df.columns):
        return pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})
    return df.filter((pl.col("date") <= as_of) & pl.col("value").is_not_null()).sort("date")


def latest_value(df: pl.DataFrame, as_of: date) -> tuple[float | None, date | None]:
    """as_of（含）以前最新一筆非 null 觀測值＋其日期；無資料回 (None, None)。"""
    hist = _sorted_nonnull(df, as_of)
    if hist.is_empty():
        return None, None
    row = hist.tail(1)
    return float(row["value"].item()), row["date"].item()


def compute_level_pct(
    df: pl.DataFrame, as_of: date, lookback_days: int, min_obs: int = _MIN_OBS
) -> float | None:
    """當前值在近 lookback_days 觀測分布中的百分位 ∈[0,1]（含自身，嚴格因果）。

    percentile = (窗內 ≤ 當前值的觀測數) / 窗內觀測數。樣本 < min_obs → None（未取得）。
    """
    hist = _sorted_nonnull(df, as_of)
    if hist.height < min_obs:
        return None
    values = hist.tail(lookback_days)["value"].to_list()
    current = values[-1]
    n = len(values)
    le = sum(1 for v in values if v <= current)
    return le / n


def compute_speed_pct(
    df: pl.DataFrame,
    as_of: date,
    lookback_days: int,
    delta_days: int,
    min_obs: int = _MIN_OBS,
) -> float | None:
    """近 delta_days 變化量，在同窗「所有 delta_days 變化量」分布中的百分位 ∈[0,1]（嚴格因果）。"""
    hist = _sorted_nonnull(df, as_of)
    if hist.height < delta_days + min_obs:
        return None
    values = hist["value"].to_list()
    deltas = [values[i] - values[i - delta_days] for i in range(delta_days, len(values))]
    if len(deltas) < min_obs:
        return None
    window = deltas[-lookback_days:]
    current = window[-1]
    n = len(window)
    le = sum(1 for v in window if v <= current)
    return le / n


def compute_dual_risk(
    df: pl.DataFrame,
    as_of: date,
    lookback_days: int,
    delta_days: int,
    min_obs: int = _MIN_OBS,
) -> float | None:
    """雙尾變速風險：2×|speed_pct − 0.5|（急動即高，方向不論；水位不計分）。"""
    sp = compute_speed_pct(df, as_of, lookback_days, delta_days, min_obs)
    if sp is None:
        return None
    return 2 * abs(sp - 0.5)


def compute_indicator_reading(
    df: pl.DataFrame,
    series_id: str,
    transform: str,
    today: date,
    lookback_days: int,
    delta_days: int,
    stale_days: int,
) -> IndicatorReading:
    """單一指標讀值（依 transform 決定計分方式；raw＝純揭露不計分）。

    stale 判定用「最新觀測日距 today 的日曆天數」，today 為執行當下（或 Phase 2 回放時的
    重放日），與序列自身時滯（如 DEX* 約一週）無關——序列本身時滯已反映在 observed_date。
    """
    raw_value, observed_date = latest_value(df, today)
    stale = observed_date is None or (today - observed_date).days > stale_days
    if stale or observed_date is None:
        return IndicatorReading(series_id, transform, observed_date, raw_value, None, True)

    score: float | None
    if transform == "level_pct":
        score = compute_level_pct(df, observed_date, lookback_days)
    elif transform == "speed_pct":
        score = compute_speed_pct(df, observed_date, lookback_days, delta_days)
    elif transform == "dual_risk":
        score = compute_dual_risk(df, observed_date, lookback_days, delta_days)
    else:  # "raw"：純揭露，不計分
        score = None
    return IndicatorReading(series_id, transform, observed_date, raw_value, score, False)


def classify_light(
    risk_score: float | None,
    prev_light: str | None,
    green_max: float,
    red_min: float,
    hysteresis: float,
) -> str:
    """risk_score → 燈色，含遲滯帶（docs/25 v2 §3.2）。risk_score=None → 資料不足（灰）。

    遲滯帶：換色需突破門檻 ±hysteresis；帶內維持上一輪顏色。prev_light 為 None／資料不足
    （無記憶可依附）時直接用基準門檻判定，不套遲滯。
    """
    if risk_score is None:
        return INSUFFICIENT
    if prev_light is None or prev_light == INSUFFICIENT:
        if risk_score < green_max:
            return GREEN
        if risk_score >= red_min:
            return RED
        return YELLOW

    if prev_light == GREEN:
        if risk_score >= green_max + hysteresis:
            return RED if risk_score >= red_min + hysteresis else YELLOW
        return GREEN
    if prev_light == RED:
        if risk_score < red_min - hysteresis:
            return GREEN if risk_score < green_max - hysteresis else YELLOW
        return RED
    # prev_light == YELLOW
    if risk_score >= red_min + hysteresis:
        return RED
    if risk_score < green_max - hysteresis:
        return GREEN
    return YELLOW


def compute_macro_light(
    series_data: dict[str, pl.DataFrame],
    cfg: dict,
    today: date,
    prev_color: str | None,
) -> MacroLight:
    """合成總經燈號（純函式）。cfg＝settings.macro_regime。IO 由呼叫端載入 series_data。"""
    lookback_days = int(cfg.get("lookback_days", 756))
    delta_days = int(cfg.get("delta_days", 20))
    stale_days = int(cfg.get("stale_days", 10))
    primary_id = cfg["primary_series"]
    primary_transform = cfg.get("primary_transform", "level_pct")
    thresholds = cfg.get("thresholds", {})
    green_max = float(thresholds.get("green_max", 60))
    red_min = float(thresholds.get("red_min", 80))
    hysteresis = float(cfg.get("hysteresis", 3))

    empty = pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})
    primary_reading = compute_indicator_reading(
        series_data.get(primary_id, empty),
        primary_id,
        primary_transform,
        today,
        lookback_days,
        delta_days,
        stale_days,
    )
    risk_score = primary_reading.score * 100 if primary_reading.score is not None else None
    color = classify_light(risk_score, prev_color, green_max, red_min, hysteresis)

    disclosure_cfg: dict[str, str] = cfg.get("disclosure_series", {})
    disclosure = [
        compute_indicator_reading(
            series_data.get(sid, empty), sid, transform, today, lookback_days, delta_days,
            stale_days,
        )
        for sid, transform in disclosure_cfg.items()
    ]

    return MacroLight(
        as_of=primary_reading.observed_date,
        color=color,
        risk_score=risk_score,
        primary=primary_reading,
        disclosure=disclosure,
        prev_color=prev_color,
    )


def to_detail_frame(light: MacroLight) -> pl.DataFrame:
    """MacroLight → per-指標明細 DataFrame（原值/score/as-of/來源）。

    供 reports/Wxx/macro_regime.csv 落地。
    """
    rows = [
        {
            "role": role,
            "series_id": r.series_id,
            "transform": r.transform,
            "as_of": r.observed_date,
            "raw_value": r.raw_value,
            "score_pct": r.score * 100 if r.score is not None else None,
            "stale": r.stale,
            "source": "FRED",
        }
        for role, r in [("primary", light.primary)] + [("disclosure", d) for d in light.disclosure]
    ]
    return pl.DataFrame(rows)


# panel_history.parquet 的欄位順序（long format，docs/26 §7.1）。
# run_as_of＝本次計算的主訊號觀測日（等同 history.parquet 的 as_of，用來識別「哪一次跑」並做冪等）；
# as_of＝該序列自身的最新觀測日（週頻/時滯序列會落後 run_as_of，這是兩個不同概念，刻意分兩欄）。
_PANEL_HISTORY_COLUMNS = [
    "run_as_of",
    "role",
    "series_id",
    "transform",
    "as_of",
    "raw_value",
    "score_pct",
    "stale",
]


def to_panel_history_frame(light: MacroLight) -> pl.DataFrame:
    """MacroLight → panel_history.parquet 的 long format 列（明細欄沿用 to_detail_frame）。"""
    return (
        to_detail_frame(light)
        .drop("source")
        .with_columns(pl.lit(light.as_of, dtype=pl.Date).alias("run_as_of"))
        .select(_PANEL_HISTORY_COLUMNS)
    )


def compute_panel_deltas(
    panel_history: pl.DataFrame,
    light: MacroLight,
    deadband_pct: float,
    deadband_rel: float,
) -> dict[str, PanelDelta]:
    """本次面板 vs「上一次不同 run」的逐序列變化（純函式，docs/26 §7.1）。

    比較基準＝panel_history 裡 `run_as_of` **嚴格早於** 本次 light.as_of 的最大那一輪——
    嚴格早於才不會把本次自己當成前次（append 與本函式的呼叫順序因此不影響結果），
    也不會拿更晚的重放列當基準。

    箭頭優先用 score_pct（風險量尺本身）；raw transform 無 score 時退用相對變化。
    deadband 內＝「→」（持平）；本次 stale／無前次／單位無從比較 → NO_PREV，不猜。
    """
    readings = [light.primary] + list(light.disclosure)
    empty_result = {
        r.series_id: PanelDelta(r.series_id, None, None, None, None, None, None, NO_PREV)
        for r in readings
    }
    if light.as_of is None or panel_history.is_empty():
        return empty_result
    if not {"run_as_of", "series_id"}.issubset(panel_history.columns):
        return empty_result

    earlier = panel_history.filter(pl.col("run_as_of") < light.as_of)
    if earlier.is_empty():
        return empty_result
    prev_run = earlier["run_as_of"].max()
    if not isinstance(prev_run, date):  # 欄位型別意外（手改檔/舊格式）→ 不猜，當作無前次
        return empty_result
    prev_rows = {
        row["series_id"]: row
        for row in earlier.filter(pl.col("run_as_of") == prev_run).iter_rows(named=True)
    }

    result: dict[str, PanelDelta] = {}
    for r in readings:
        prev = prev_rows.get(r.series_id)
        cur_score = r.score * 100 if r.score is not None else None
        if prev is None or r.stale:
            result[r.series_id] = empty_result[r.series_id]
            continue
        prev_score = prev.get("score_pct")
        prev_raw = prev.get("raw_value")
        d_score = (
            cur_score - prev_score
            if (cur_score is not None and prev_score is not None)
            else None
        )
        d_raw = (
            r.raw_value - prev_raw
            if (r.raw_value is not None and prev_raw is not None)
            else None
        )
        result[r.series_id] = PanelDelta(
            series_id=r.series_id,
            prev_run_as_of=prev_run,
            prev_as_of=prev.get("as_of"),
            prev_raw_value=prev_raw,
            prev_score_pct=prev_score,
            delta_raw_value=d_raw,
            delta_score_pct=d_score,
            arrow=_delta_arrow(d_score, d_raw, prev_raw, deadband_pct, deadband_rel),
        )
    return result


def _delta_arrow(
    d_score: float | None,
    d_raw: float | None,
    prev_raw: float | None,
    deadband_pct: float,
    deadband_rel: float,
) -> str:
    """變化 → 箭頭。score_pct 用絕對百分位點 deadband；raw 退用相對變化 deadband。"""
    if d_score is not None:
        if abs(d_score) < deadband_pct:
            return "→"
        return "↑" if d_score > 0 else "↓"
    if d_raw is not None and prev_raw is not None and prev_raw != 0:
        rel = d_raw / abs(prev_raw)
        if abs(rel) < deadband_rel:
            return "→"
        return "↑" if d_raw > 0 else "↓"
    return NO_PREV


# ─── IO 便利包裝（比照 regime.py 的 compute_market_regime 形狀）───────────────────


def load_prev_color(history_path: Path) -> str | None:
    """讀 history.parquet 最後一列的 color（供遲滯帶判斷「上一輪顏色」）；無檔/空表回 None。"""
    if not history_path.exists():
        return None
    df = pl.read_parquet(history_path)
    if df.is_empty() or "color" not in df.columns:
        return None
    return str(df.sort("as_of").tail(1)["color"].item())


def append_history(history_path: Path, light: MacroLight) -> None:
    """把本次 MacroLight 結果 append 一列進 history.parquet（point-in-time）。

    供 Phase 2 as-of 回放使用。冪等：同一 `as_of` 已存在則跳過，不重複寫入
    （`make macro`/`make week` 同一天可能因重試或手動多跑一次；沒有這道檢查
    會在累積序列裡埋進同日重複列，往後的 as-of 回放/敏感度分析會被悄悄污染，
    見 `data/valuation_history.append_valuation_history` 同一冪等模式）。
    """
    row = {
        "as_of": light.as_of,
        "color": light.color,
        "risk_score": light.risk_score,
        "primary_series": light.primary.series_id,
        "primary_raw_value": light.primary.raw_value,
    }
    new_row = pl.DataFrame([row])
    history_path.parent.mkdir(parents=True, exist_ok=True)
    if history_path.exists():
        existing = pl.read_parquet(history_path)
        if not existing.is_empty() and (existing["as_of"] == light.as_of).any():
            logger.info(f"macro_regime history 已有 {light.as_of} 這天，跳過重複 append")
            return
        combined = pl.concat([existing, new_row], how="diagonal_relaxed")
    else:
        combined = new_row
    combined.write_parquet(history_path)
    logger.info(f"macro_regime history 寫入 {history_path}（{len(combined)} 列）")


def append_panel_history(panel_history_path: Path, light: MacroLight) -> None:
    """把本次面板（主訊號＋揭露面板）逐指標 append 進 panel_history.parquet（docs/26 A案）。

    冪等：同一 `run_as_of` 已存在則整批跳過，比照 `append_history` 與
    `data/valuation_history.append_valuation_history` 同一模式——沒有這道檢查，同日重跑會在
    long format 裡埋進整組重複列，之後的變化追蹤會拿「本次 vs 本次」算出假持平。
    """
    if light.as_of is None:
        logger.warning("macro_regime 主訊號無資料，不寫入 panel_history.parquet")
        return
    new_rows = to_panel_history_frame(light)
    panel_history_path.parent.mkdir(parents=True, exist_ok=True)
    if panel_history_path.exists():
        existing = pl.read_parquet(panel_history_path)
        if not existing.is_empty() and (existing["run_as_of"] == light.as_of).any():
            logger.info(f"macro_regime panel_history 已有 {light.as_of} 這輪，跳過重複 append")
            return
        combined = pl.concat([existing, new_rows], how="diagonal_relaxed")
    else:
        combined = new_rows
    combined.write_parquet(panel_history_path)
    logger.info(
        f"macro_regime panel_history 寫入 {panel_history_path}"
        f"（{combined['run_as_of'].n_unique()} 輪／{len(combined)} 列）"
    )


def resolve_panel_history_path(cfg: dict) -> Path:
    """settings 的 macro_regime.panel_history_path（缺 → data_dir 下的預設檔名）。"""
    mr_cfg = cfg.get("macro_regime", {})
    configured = mr_cfg.get("panel_history_path")
    if configured:
        return Path(str(configured))
    return Path(cfg["paths"]["data_dir"]) / "macro_regime" / "panel_history.parquet"


def load_panel_deltas(
    panel_history_path: Path, light: MacroLight, cfg: dict
) -> dict[str, PanelDelta]:
    """IO 便利包裝：讀 panel_history.parquet → compute_panel_deltas。無檔＝全 NO_PREV。"""
    mr_cfg = cfg.get("macro_regime", {})
    deadband_pct = float(mr_cfg.get("delta_arrow_deadband_pct", 2.0))
    deadband_rel = float(mr_cfg.get("delta_arrow_deadband_rel", 0.005))
    history = (
        pl.read_parquet(panel_history_path)
        if panel_history_path.exists()
        else pl.DataFrame(schema={"run_as_of": pl.Date, "series_id": pl.String})
    )
    return compute_panel_deltas(history, light, deadband_pct, deadband_rel)


def compute_market_macro(
    cfg: dict,
    settings_path: Path,
    history_path: Path | None = None,
    force_refresh: bool = False,
) -> MacroLight:
    """IO 便利包裝：載入 FRED 快取（主訊號＋揭露面板）後呼叫純函式 compute_macro_light。

    供 group 報告與 market macro CLI 共用。history_path 給定時自動讀最後一列當 prev_color；
    None 則不套遲滯（視為首次計算）。force_refresh=True（CLI `--refresh`）→ 略過快取強抓。
    """
    from tw_screener.data.fred import create_client

    mr_cfg = cfg.get("macro_regime", {})
    primary_id = mr_cfg["primary_series"]
    disclosure_ids = list(mr_cfg.get("disclosure_series", {}).keys())
    all_ids = [primary_id] + [sid for sid in disclosure_ids if sid != primary_id]

    client = create_client(settings_path)
    series_data = client.fetch_all(all_ids, force=force_refresh)
    prev_color = load_prev_color(history_path) if history_path is not None else None
    return compute_macro_light(series_data, mr_cfg, date.today(), prev_color)


def run_macro(
    settings_path: Path = Path("config/settings.yaml"),
    history_path: Path | None = None,
    force_refresh: bool = False,
    panel_history_path: Path | None = None,
) -> tuple[MacroLight, dict[str, PanelDelta]]:
    """IO 入口：抓序列→計分→append history/panel_history。供 CLI／Makefile `make macro` 呼叫。

    回傳 (燈號, 逐序列變化)。變化在 append 之前算（本次尚未進 panel_history）——
    `compute_panel_deltas` 本身也只看嚴格早於本次的輪次，兩道保險都指向同一件事：
    比較基準必須是上一輪，不能是自己。
    """
    with open(settings_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    hpath = history_path or Path(cfg["paths"]["data_dir"]) / "macro_regime" / "history.parquet"
    ppath = panel_history_path or resolve_panel_history_path(cfg)
    light = compute_market_macro(
        cfg, settings_path, history_path=hpath, force_refresh=force_refresh
    )
    deltas = load_panel_deltas(ppath, light, cfg)
    if light.as_of is not None:
        append_history(hpath, light)
        append_panel_history(ppath, light)
    else:
        logger.warning("macro_regime 主訊號無資料，不寫入 history.parquet")
    return light, deltas
