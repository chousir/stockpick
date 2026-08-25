"""backtest/macro_grid_search_runner.py — docs/31 §23.4 Part 4 IO 編排（自 cli.py 薄殼呼叫）。

讀 research/macro_regime_screening/raw/（既有4指標FRED快取，非本輪新抓）→ macro_regime_validate
（build_level_pct_series/build_speed_pct_series，重用production純函式）→ macro_grid_search
（episode/命中/false positive純函式）→ 160組grid結果CSV，寫 research/macro_regime_screening/
round6_grid/（gitignored，研究軌一次性，不掛make week，同macro_regime_validate慣例）。

單指標144組×多指標組合16組共160組，每組獨立算：3事件是否命中（§23.4命中定義）、
fire_rate、false_positive_episodes、是否通過候選天花板（§23.4三項）。distinct分數序列只算
一次（48條：4指標×(3個level_pct lookback + 3×3個speed_pct lookback/delta組合)）、快取後
給144個threshold重用，避免per-combo重算（§23.4效能提醒）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
from rich.console import Console

from tw_screener.backtest.macro_grid_search import (
    combine_fired,
    count_false_positive_episodes,
    fire_rate,
    first_hit_in_window,
    merge_fired_episodes,
    nth_trading_day_after,
    nth_trading_day_before,
)
from tw_screener.backtest.macro_regime_validate import (
    build_level_pct_series,
    build_speed_pct_series,
)

console = Console()


@dataclass(frozen=True)
class EventSpec:
    name: str
    anchors: tuple[date, ...]


def _load_events(raw: list[dict]) -> list[EventSpec]:  # type: ignore[type-arg]
    return [
        EventSpec(e["name"], tuple(date.fromisoformat(a) for a in e["anchors"])) for e in raw
    ]


def _event_windows(
    events: list[EventSpec], calendar: list[date], before_td: int, after_td: int
) -> list[tuple[date, date]]:
    """每個事件每個錨點的[錨點-before_td, 錨點+after_td]視窗，union（供false positive排除用）。"""
    windows = []
    for ev in events:
        for a in ev.anchors:
            windows.append(
                (
                    nth_trading_day_before(calendar, a, before_td),
                    nth_trading_day_after(calendar, a, after_td),
                )
            )
    return windows


def _hit_windows(events: list[EventSpec], calendar: list[date], min_td: int, max_td: int) -> dict:
    """事件name → 該事件每個錨點的命中視窗[錨點-max_td, 錨點-min_td]列表。"""
    out: dict[str, list[tuple[date, date]]] = {}
    for ev in events:
        out[ev.name] = [
            (
                nth_trading_day_before(calendar, a, max_td),
                nth_trading_day_before(calendar, a, min_td),
            )
            for a in ev.anchors
        ]
    return out


def _score_to_fired(
    scores: pl.DataFrame, threshold: float, analysis_start: date, analysis_end: date
) -> pl.DataFrame:
    return (
        scores.filter((pl.col("date") >= analysis_start) & (pl.col("date") <= analysis_end))
        .with_columns((pl.col("score") >= threshold).alias("fired"))
        .select("date", "fired")
    )


def _evaluate_combo(
    label: str,
    fired: pl.DataFrame,
    events: list[EventSpec],
    calendar: list[date],
    gap_td: int,
    min_lead_td: int,
    max_lead_td: int,
    fp_windows: list[tuple[date, date]],
    fire_rate_ceiling: float,
    fp_ceiling: int,
) -> dict[str, object]:
    episodes = merge_fired_episodes(fired, gap_td)
    hit_windows = _hit_windows(events, calendar, min_lead_td, max_lead_td)
    hits = {}
    for ev in events:
        hit = None
        for w0, w1 in hit_windows[ev.name]:
            found = first_hit_in_window(episodes, w0, w1)
            if found is not None:
                hit = found
                break
        hits[ev.name] = hit
    n_hits = sum(1 for v in hits.values() if v is not None)
    fr = fire_rate(fired)
    fp = count_false_positive_episodes(episodes, fp_windows)
    candidate = n_hits == len(events) and fr < fire_rate_ceiling and fp <= fp_ceiling
    row: dict[str, object] = {
        "combo": label,
        "n_episodes": len(episodes),
        "fire_rate": round(fr, 4) if fr == fr else None,  # nan 自比較恆 False
        "false_positive_episodes": fp,
        "n_events_hit": n_hits,
        "n_events_total": len(events),
        "candidate": candidate,
    }
    for ev in events:
        ev_hit = hits[ev.name]
        row[f"hit_{ev.name}"] = ev_hit.start.isoformat() if ev_hit is not None else None
    return row


def run_macro_grid_search(settings: Path) -> Path:
    """docs/31 §23.4：160組grid，寫round6_grid/results.csv，回傳輸出路徑。"""
    import yaml

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    gs = cfg.get("backtest", {}).get("macro_grid_search", {})
    raw_dir = Path(gs.get("raw_dir", "research/macro_regime_screening/raw"))
    indicator_files: dict[str, str] = gs.get("indicator_files", {})
    calendar_file = raw_dir / gs.get("calendar_file", "BAA10Y.parquet")
    analysis_start = date.fromisoformat(gs.get("analysis_start", "2019-01-01"))
    analysis_end = date.fromisoformat(gs.get("analysis_end", "2026-07-31"))
    lookback_grid = [int(x) for x in gs.get("lookback_grid", [252, 504, 756])]
    delta_grid = [int(x) for x in gs.get("delta_grid", [10, 20, 40])]
    threshold_grid = [float(x) for x in gs.get("threshold_grid", [0.80, 0.90, 0.95])]
    min_obs = int(gs.get("min_obs", 30))
    gap_td = int(gs.get("gap_td", 10))
    min_lead_td = int(gs.get("min_lead_td", 5))
    max_lead_td = int(gs.get("max_lead_td", 60))
    fp_before_td = int(gs.get("fp_before_td", 60))
    fp_after_td = int(gs.get("fp_after_td", 20))
    fire_rate_ceiling = float(gs.get("fire_rate_ceiling", 0.20))
    fp_ceiling = int(gs.get("false_positive_ceiling", 5))
    events = _load_events(gs.get("events", []))
    combo_fixed: dict[str, dict] = gs.get("combo_fixed_config", {})  # type: ignore[type-arg]
    out_dir = Path(gs.get("output_dir", "research/macro_regime_screening/round6_grid"))

    for name, fname in indicator_files.items():
        if not (raw_dir / fname).exists():
            console.print(f"[red]找不到 {raw_dir / fname}（指標 {name}）[/red]")
            raise FileNotFoundError(str(raw_dir / fname))

    console.print("[bold]載入4指標raw parquet＋統一交易日曆…[/bold]")
    # 只保留warmup buffer內的歷史（最長lookback=756交易日≈3年，用1500日曆天margin涵蓋假日/
    # 週末保守估計），避免每個series逐日呼叫compute_level_pct/speed_pct時掃過整段1962起的
    # 全歷史——這是純效能優化，不影響結果（compute_level_pct本身只看as_of以前的lookback窗）。
    from datetime import timedelta

    buffer_days = max(lookback_grid) * 2 + 100
    warmup_start = analysis_start - timedelta(days=buffer_days)
    raw_frames = {
        name: pl.read_parquet(raw_dir / fname).filter(pl.col("date") >= warmup_start)
        for name, fname in indicator_files.items()
    }
    for name, df in raw_frames.items():
        console.print(f"  {name}：{df.height} 列（warmup起自 {warmup_start}）")
    calendar_df = pl.read_parquet(calendar_file)
    calendar = [
        d for d in calendar_df.sort("date")["date"].to_list() if analysis_start <= d <= analysis_end
    ]
    console.print(f"  交易日曆（{calendar_file.name}，分析窗內）{len(calendar)} 天")

    fp_windows = _event_windows(events, calendar, fp_before_td, fp_after_td)

    # ── 建48條distinct分數序列（indicator×transform×lookback[×delta]），只算一次 ──
    console.print("[bold]建分數序列（level_pct×3窗、speed_pct×3窗×3delta，每指標各1次）…[/bold]")
    series_cache: dict[tuple, pl.DataFrame] = {}  # type: ignore[type-arg]
    for name, df in raw_frames.items():
        for lb in lookback_grid:
            series_cache[(name, "level_pct", lb, None)] = build_level_pct_series(df, lb, min_obs)
        for lb in lookback_grid:
            for dd in delta_grid:
                series_cache[(name, "speed_pct", lb, dd)] = build_speed_pct_series(
                    df, lb, dd, min_obs
                )
    console.print(f"  分數序列共 {len(series_cache)} 條")

    rows: list[dict[str, object]] = []

    # ── 144組單指標 ──
    for (name, transform, lb, dd), scores in series_cache.items():
        for th in threshold_grid:
            fired = _score_to_fired(scores, th, analysis_start, analysis_end)
            label = f"{name}|{transform}|lb={lb}" + (f"|delta={dd}" if dd else "") + f"|th={th}"
            rows.append(
                _evaluate_combo(
                    label, fired, events, calendar, gap_td, min_lead_td, max_lead_td,
                    fp_windows, fire_rate_ceiling, fp_ceiling,
                )
            )

    # ── 16組多指標組合（固定單指標設定，§23.4避免二次挑選） ──
    console.print("[bold]多指標組合（6 pair×2規則＋4個全指標規則）…[/bold]")
    fixed_fired: dict[str, pl.DataFrame] = {}
    for name, conf in combo_fixed.items():
        key = (name, conf["transform"], int(conf["lookback_days"]), conf.get("delta_days"))
        scores = series_cache[key]
        fixed_fired[name] = _score_to_fired(
            scores, float(conf["threshold"]), analysis_start, analysis_end
        )

    names = list(combo_fixed.keys())
    import itertools

    for a, b in itertools.combinations(names, 2):
        for rule, tag in (("union", "union"), ("count_ge", "intersect")):
            min_count = 2 if rule == "count_ge" else 1
            combined = combine_fired([fixed_fired[a], fixed_fired[b]], rule, min_count)
            label = f"combo|{a}+{b}|{tag}"
            rows.append(
                _evaluate_combo(
                    label, combined, events, calendar, gap_td, min_lead_td, max_lead_td,
                    fp_windows, fire_rate_ceiling, fp_ceiling,
                )
            )

    all_frames = [fixed_fired[n] for n in names]
    for rule, min_count, tag in (
        ("union", 1, "any1of4"),
        ("count_ge", 2, "ge2of4"),
        ("count_ge", 3, "ge3of4"),
        ("count_ge", 4, "all4"),
    ):
        combined = combine_fired(all_frames, rule, min_count)
        label = f"combo|all4|{tag}"
        rows.append(
            _evaluate_combo(
                label, combined, events, calendar, gap_td, min_lead_td, max_lead_td,
                fp_windows, fire_rate_ceiling, fp_ceiling,
            )
        )

    result = pl.DataFrame(rows)
    console.print(f"  共 {result.height} 組（pre-registration §23.4寫死N=160）")
    candidates = result.filter(pl.col("candidate"))
    console.print(f"  通過候選天花板：{candidates.height} 組")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "results.csv"
    result.write_csv(out_path)
    console.print(f"[green]寫入 {out_path}[/green]")
    return out_path
