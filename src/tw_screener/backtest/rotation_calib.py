"""起漲點回測校準（rotation 引擎 R2 研究軌，docs/12-sector-rotation.md §2.4）。

從歷史資料回推：哪些資金流向訊號（analysis/rotation.py 輸出）在次產業起漲點
前/當下觸發的命中率最高、誤報最低，產出校準報告供生產軌（R3）採用門檻。

方法記錄（誠實標明取捨）：
- 起漲點＝低基期（距 M 日低點 ≤ tol%）且 N 日內籃子前瞻漲幅 ≥ X% 的「首個合格日」。
  首合格日可能略早於實際價格啟動（仍在打底），這正合用途——資金先進、價未動。
  偵測需滿 M 日歷史（吃掉樣本前段，誠實 > 樣本數）。
- 訊號門檻用「次產業自身滾動 z 分數」（≈站上自身歷史高位的百分位概念）：
  各次產業法人淨額規模不可比，z 分數可比；滾動視窗避免全樣本前視偏誤。
  breadth 已是 [0,1] 可比 → 用絕對門檻。
- 觸發＝向上穿越（昨日 ≤ 門檻、今日 > 門檻），連續高於只算一次。
- 命中＝觸發後 lead_window 個交易日內（含當日）有起漲點；起漲後 occupy 窗內
  的觸發剔除（已在波段中，非領先）；歷史尾端不足 lead_window 的觸發剔除。
- 同時報 precision（命中率）、recall（episode 覆蓋率）、lift（vs 隨機基率）——
  單看命中率會選出「一年只響一次」的無用訊號。
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date

import polars as pl

_EPISODE_SCHEMA: dict[str, type[pl.DataType]] = {
    "sub_industry": pl.Utf8,
    "start_date": pl.Date,
    "base_index": pl.Float64,
    "fwd_return_pct": pl.Float64,
}

_NON_SIGNAL_COLS = frozenset({"sub_industry", "date", "members"})


def detect_breakouts(
    baskets: pl.DataFrame,
    x_pct: float = 10.0,
    n_days: int = 15,
    m_days: int = 60,
    low_base_tol_pct: float = 3.0,
    cooldown_days: int = 15,
) -> pl.DataFrame:
    """對每個次產業籃子指數偵測起漲點（episodes）。

    起漲點＝同時滿足（a）低基期：當日指數 ≤ 近 M 日最低 ×(1+tol%)；
    （b）前瞻：未來 N 日內指數最高點漲幅 ≥ X%。連續合格日取首日，
    之後 cooldown_days 內不再計（避免同波段重複）。需滿 M 日歷史。
    """
    if baskets.is_empty():
        return pl.DataFrame(schema=_EPISODE_SCHEMA)
    rows: list[dict] = []
    for sub_df in baskets.sort(["sub_industry", "date"]).partition_by(
        "sub_industry", maintain_order=True
    ):
        sub = sub_df["sub_industry"][0]
        idx = sub_df["basket_index"].to_list()
        dates = sub_df["date"].to_list()
        n = len(idx)
        i = m_days - 1  # 需滿 M 日歷史
        while i < n - 1:
            fwd_max = max(idx[i + 1 : min(i + 1 + n_days, n)])
            fwd_ret = fwd_max / idx[i] - 1.0
            low = min(idx[i - m_days + 1 : i + 1])
            if idx[i] <= low * (1 + low_base_tol_pct / 100) and fwd_ret >= x_pct / 100:
                rows.append(
                    {
                        "sub_industry": sub,
                        "start_date": dates[i],
                        "base_index": idx[i],
                        "fwd_return_pct": fwd_ret * 100,
                    }
                )
                i += cooldown_days + 1
            else:
                i += 1
    if not rows:
        return pl.DataFrame(schema=_EPISODE_SCHEMA)
    return pl.DataFrame(rows, schema=_EPISODE_SCHEMA).sort(["sub_industry", "start_date"])


def standardize_signals(
    flows: pl.DataFrame,
    z_window: int = 60,
    z_min_periods: int = 30,
) -> pl.DataFrame:
    """對非 breadth 訊號欄加 {col}_z（次產業自身滾動 z 分數）。

    breadth 欄（[0,1] 可比）不轉換。std=0 或 warmup 期 → null（不觸發）。
    """
    sig_cols = [
        c
        for c in flows.columns
        if c not in _NON_SIGNAL_COLS and "breadth" not in c
    ]
    out = flows.sort(["sub_industry", "date"])
    exprs = []
    for c in sig_cols:
        mean = pl.col(c).rolling_mean(z_window, min_samples=z_min_periods).over("sub_industry")
        std = pl.col(c).rolling_std(z_window, min_samples=z_min_periods).over("sub_industry")
        exprs.append(
            pl.when(std > 0).then((pl.col(c) - mean) / std).otherwise(None).alias(f"{c}_z")
        )
    return out.with_columns(exprs)


def compute_triggers(
    flows_z: pl.DataFrame,
    col: str,
    threshold: float,
    require_momentum: bool = False,
) -> pl.DataFrame:
    """訊號觸發（向上穿越）→ (sub_industry, date)。

    觸發＝今日 col > threshold 且昨日 ≤ threshold（null 視為未達標）。
    require_momentum：另要求當日 flow_momentum > 0（docs/12 §2.4 範例組合）。
    """
    cond = pl.col(col) > threshold
    if require_momentum:
        cond = cond & (pl.col("flow_momentum") > 0)
    df = (
        flows_z.sort(["sub_industry", "date"])
        .with_columns(cond.fill_null(False).alias("_hit"))
        .with_columns(
            (pl.col("_hit") & ~pl.col("_hit").shift(1, fill_value=False).over("sub_industry"))
            .alias("_trigger")
        )
    )
    return df.filter(pl.col("_trigger")).select(["sub_industry", "date"])


def compute_base_rate(
    episodes: pl.DataFrame,
    calendar: list[date],
    keys: set[str] | list[str],
    lead_window: int = 15,
    occupy_days: int = 15,
    warmup_pos: int = 0,
    key_col: str = "sub_industry",
) -> float:
    """隨機基率：keys 範圍內合格日中「起漲點落在其後 lead_window 內」的比率。

    null model＝在這群 key 的所有合格（非占用、非尾端）交易日裡隨機挑一天、
    剛好落在某起漲點前 lead_window 內的機率。lift = 訊號命中率 / 此基率。
    keys 給「全宇宙」（個股層 B2 = 全上市股）時，基率與單一訊號的觸發分布無關、
    可一次算好重複用，且避免「廣訊號因觸發無起漲股而灌大 lift」。
    """
    pos = {d: i for i, d in enumerate(calendar)}
    max_eval = len(calendar) - 1 - lead_window
    ep_by_key: dict[str, list[int]] = {}
    for k, d in episodes.select([key_col, "start_date"]).iter_rows():
        if d in pos:
            ep_by_key.setdefault(k, []).append(pos[d])
    for v in ep_by_key.values():
        v.sort()
    eligible = 0
    hit_days = 0
    for k in keys:
        eps = ep_by_key.get(k, [])
        for p in range(warmup_pos, max_eval + 1):
            if any(e < p <= e + occupy_days for e in eps):
                continue
            eligible += 1
            j = bisect_left(eps, p)
            if j < len(eps) and eps[j] - p <= lead_window:
                hit_days += 1
    return hit_days / eligible if eligible else 0.0


def evaluate_triggers(
    triggers: pl.DataFrame,
    episodes: pl.DataFrame,
    calendar: list[date],
    lead_window: int = 15,
    occupy_days: int = 15,
    warmup_pos: int = 0,
    key_col: str = "sub_industry",
    base_rate: float | None = None,
) -> dict:
    """觸發 vs 起漲點的命中統計（pooled 跨 key_col）。

    命中＝觸發後 [0, lead_window] 交易日內有起漲點；起漲後 (start, start+occupy]
    的觸發、與歷史尾端不足 lead_window 的觸發剔除。
    key_col：分群鍵——族群層＝"sub_industry"，個股層（B2）＝"stock_id"，其餘邏輯共用。
    base_rate：可傳預先算好的隨機基率（個股層掃多訊號時一次算好全宇宙基率重複用，
    省去逐訊號重算；None 則沿用族群層行為，就 episodes∪觸發 key 範圍即時算）。
    回傳 n_triggers / hits / false_positives / hit_rate / recall / lift /
    median_lead_days / n_episodes。
    """
    pos = {d: i for i, d in enumerate(calendar)}
    max_eval = len(calendar) - 1 - lead_window

    ep_by_sub: dict[str, list[int]] = {}
    for sub, d in episodes.select([key_col, "start_date"]).iter_rows():
        if d in pos:
            ep_by_sub.setdefault(sub, []).append(pos[d])
    for v in ep_by_sub.values():
        v.sort()

    def _in_occupied(sub: str, p: int) -> bool:
        for e in ep_by_sub.get(sub, []):
            if e < p <= e + occupy_days:
                return True
        return False

    def _next_episode_dist(sub: str, p: int) -> int | None:
        eps = ep_by_sub.get(sub, [])
        j = bisect_left(eps, p)
        if j < len(eps) and eps[j] - p <= lead_window:
            return eps[j] - p
        return None

    hits = 0
    fps = 0
    leads: list[int] = []
    covered: set[tuple[str, int]] = set()
    for sub, d in triggers.select([key_col, "date"]).iter_rows():
        p = pos.get(d)
        if p is None or p < warmup_pos or p > max_eval or _in_occupied(sub, p):
            continue
        dist = _next_episode_dist(sub, p)
        if dist is not None:
            hits += 1
            leads.append(dist)
            eps = ep_by_sub[sub]
            covered.add((sub, eps[bisect_left(eps, p)]))
        else:
            fps += 1

    # 隨機基率：未預先給定則就 episodes∪觸發 key 範圍即時算（族群層行為）
    if base_rate is None:
        subs = set(ep_by_sub) | set(triggers[key_col].unique().to_list())
        base_rate = compute_base_rate(
            episodes, calendar, subs, lead_window, occupy_days, warmup_pos, key_col
        )

    n_eval = hits + fps
    n_episodes = sum(len(v) for v in ep_by_sub.values())
    hit_rate = hits / n_eval if n_eval else 0.0
    leads.sort()
    return {
        "n_triggers": n_eval,
        "hits": hits,
        "false_positives": fps,
        "hit_rate": hit_rate,
        "recall": len(covered) / n_episodes if n_episodes else 0.0,
        "base_rate": base_rate,
        "lift": (hit_rate / base_rate) if base_rate > 0 else None,
        "median_lead_days": leads[len(leads) // 2] if leads else None,
        "n_episodes": n_episodes,
    }


def scan_signals(
    flows: pl.DataFrame,
    episodes: pl.DataFrame,
    z_window: int = 60,
    z_min_periods: int = 30,
    z_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0),
    breadth_thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8),
    lead_window: int = 15,
    occupy_days: int = 15,
) -> pl.DataFrame:
    """掃描（訊號 × 門檻）→ 命中統計表（docs/12 §2.4 校準目標）。

    z 訊號（流向/動能/集中度）掃 z_thresholds；breadth 掃絕對門檻；
    另對 *_flow_* z 訊號加「+mom」變體（且 flow_momentum > 0）。
    """
    if flows.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    flows_z = standardize_signals(flows, z_window, z_min_periods)
    calendar = sorted(
        set(flows["date"].unique().to_list())
        | set(episodes["start_date"].unique().to_list())
    )
    warmup_pos = min(z_min_periods, len(calendar) - 1)

    jobs: list[tuple[str, str, float, bool]] = []  # (label, col, threshold, require_mom)
    for c in flows_z.columns:
        if c.endswith("_z"):
            base = c[:-2]
            for t in z_thresholds:
                jobs.append((f"{base} (z>{t})", c, t, False))
                if "_flow_" in base:
                    jobs.append((f"{base} (z>{t}) +mom", c, t, True))
        elif "breadth" in c:
            for t in breadth_thresholds:
                jobs.append((f"{c} (>{t})", c, t, False))

    rows = []
    for label, col, thr, req_mom in jobs:
        triggers = compute_triggers(flows_z, col, thr, require_momentum=req_mom)
        stats = evaluate_triggers(
            triggers, episodes, calendar, lead_window, occupy_days, warmup_pos
        )
        rows.append({"signal": label, **stats})
    out = pl.DataFrame(rows)
    # F1（precision=hit_rate, recall）綜合排序
    return out.with_columns(
        pl.when((pl.col("hit_rate") + pl.col("recall")) > 0)
        .then(2 * pl.col("hit_rate") * pl.col("recall") / (pl.col("hit_rate") + pl.col("recall")))
        .otherwise(0.0)
        .alias("f1")
    ).sort("f1", descending=True)


def render_calibration_report(
    scan: pl.DataFrame,
    episodes: pl.DataFrame,
    params: dict,
    data_range: tuple[date, date],
    min_triggers: int = 8,
    min_lift: float = 1.5,
    top_n: int = 10,
) -> str:
    """校準報告 markdown（含建議寫入 settings.yaml.rotation 的數值）。"""
    lines = [
        "# 次產業起漲點回測校準報告",
        "",
        f"- 資料區間：{data_range[0]} ~ {data_range[1]}",
        f"- 起漲定義：低基期（距 {params['m_days']} 日低點 ≤ {params['low_base_tol_pct']}%）"
        f"＋ {params['n_days']} 日內籃子漲幅 ≥ {params['x_pct']}%"
        f"・冷卻 {params['cooldown_days']} 日",
        f"- 領先視窗：{params['lead_window']} 交易日・z 視窗 {params['z_window']} 日",
        f"- 起漲點樣本：{episodes.height} 個"
        f"（{episodes['sub_industry'].n_unique() if not episodes.is_empty() else 0} 個次產業）",
        "",
        "## 起漲點清單",
        "",
        "| 次產業 | 起漲日 | 前瞻漲幅 |",
        "|---|---|---|",
    ]
    for sub, d, _, fwd in episodes.iter_rows():
        lines.append(f"| {sub} | {d} | +{fwd:.1f}% |")

    base_rate = scan["base_rate"][0] if not scan.is_empty() else 0.0
    lines += [
        "",
        f"## 訊號掃描（隨機基率 {base_rate:.1%}；lift = 命中率/基率）",
        "",
        "| 訊號 | 觸發 | 命中 | 誤報 | 命中率 | recall | lift | 中位領先(日) | F1 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in scan.iter_rows(named=True):
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        lead = str(r["median_lead_days"]) if r["median_lead_days"] is not None else "—"
        lines.append(
            f"| {r['signal']} | {r['n_triggers']} | {r['hits']} | {r['false_positives']} "
            f"| {r['hit_rate']:.1%} | {r['recall']:.1%} | {lift} | {lead} | {r['f1']:.3f} |"
        )

    qualified = scan.filter(
        (pl.col("n_triggers") >= min_triggers)
        & (pl.col("lift").is_not_null())
        & (pl.col("lift") >= min_lift)
    ).head(top_n)
    lines += [
        "",
        f"## 建議名單（觸發 ≥{min_triggers}・lift ≥{min_lift}・F1 排序前 {top_n}）",
        "",
    ]
    if qualified.is_empty():
        lines.append("（無訊號同時滿足樣本數與 lift 門檻——調整起漲定義或放寬條件後重跑）")
    else:
        for i, r in enumerate(qualified.iter_rows(named=True), 1):
            lines.append(
                f"{i}. **{r['signal']}**：命中率 {r['hit_rate']:.0%}・recall {r['recall']:.0%}"
                f"・lift {r['lift']:.1f}・中位領先 {r['median_lead_days']} 日"
                f"（{r['hits']}/{r['n_triggers']} 觸發命中）"
            )
        best = qualified.row(0, named=True)
        lines += [
            "",
            "## 建議寫入 settings.yaml `rotation.entry_signal`",
            "",
            "```yaml",
            "  entry_signal:",
            f"    # R2 校準（資料 {data_range[0]} ~ {data_range[1]}）：{best['signal']}",
            f"    signal: {best['signal'].split(' ')[0]}",
            f"    mode: {'abs' if 'breadth' in best['signal'] else 'z'}",
            f"    threshold: {best['signal'].split('>')[1].split(')')[0]}",
            f"    require_momentum: {str('+mom' in best['signal']).lower()}",
            f"    lead_window: {params['lead_window']}",
            "```",
        ]
    lines += [
        "",
        "---",
        "",
        "> 注意：樣本約 1 年、起漲點數十個，統計力有限；建議門檻當「有依據的起點」，",
        "> 隨資料累積每季重跑校準。起漲首合格日可能仍在打底（略早於實際噴出），",
        "> 此偏向正合「資金先進、價未動」的偵測用途。",
        "",
    ]
    return "\n".join(lines)
