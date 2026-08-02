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


def describe_macro_light(light: MacroLight) -> dict[str, object]:
    """MacroLight → 報表/CLI 共用顯示 dict（docs/25 v2 §4.2 格式）。"""

    def _fmt_reading(r: IndicatorReading) -> dict[str, object]:
        return {
            "series_id": r.series_id,
            "transform": r.transform,
            "as_of": r.observed_date.isoformat() if r.observed_date else None,
            "raw_value": round(r.raw_value, 4) if r.raw_value is not None else None,
            "score_pct": round(r.score * 100, 1) if r.score is not None else None,
            "stale": r.stale,
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
) -> MacroLight:
    """IO 入口：抓序列→計分→append history.parquet。供 CLI／Makefile `make macro` 呼叫。"""
    with open(settings_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    hpath = history_path or Path(cfg["paths"]["data_dir"]) / "macro_regime" / "history.parquet"
    light = compute_market_macro(
        cfg, settings_path, history_path=hpath, force_refresh=force_refresh
    )
    if light.as_of is not None:
        append_history(hpath, light)
    else:
        logger.warning("macro_regime 主訊號無資料，不寫入 history.parquet")
    return light
