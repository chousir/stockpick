"""M2 投降洗盤偵測（委託書 M2）——`market_washout` 反向 flag。

**為什麼要獨立成 flag，不能併進 regime 分數**：現行 V2 regime（趨勢/廣度/資金）在洗盤
底部三分項全深負 → 姿態恰好在「最該警戒反轉」的時點喊最防禦。投降偵測是**反向訊號**
——極端負讀數是「接近落底」的證據，語意與 regime 的線性加權相反，合進去會互相抵銷、
兩個訊號一起消失。故 flag 獨立計算、獨立呈現，**不進 regime_score、不改燈色、不改排序**。

四個子項（委託書 M2.2），**≥2 項同時成立**才觸發：
  1. 融資投降：全市場融資餘額單日或 5 日減幅 z < −2
  2. 廣度 washout：全宇宙站上 MA60 比率 < ~20%
  3. 資金分項極端持續：regime 資金子分 < −0.9 連續 ≥3 週
  4. 指數深負乖離：大盤距自身 MA60 < −7%

**校準狀態＝未校準（委託書「誠實帳」明列）**：歷史面板只有一次 530 億級投降樣本
（2026-07-30），門檻是先驗值不是校準值。依 M2.2 括號要求，**前 4 週僅描述、不判勝負**；
門檻調整一律走 settings diff，不在程式裡調。

**兩個口徑降級，讀數字前必看**：
  - 融資序列＝逐股 `margin_balance` 加總，單位**張、僅上市**（TWSE MI_MARGN 個股表）。
    新聞說的「7/30 融資減 530 億」是**金額**口徑；本模組沒有金額序列，z 建在張數代理上，
    兩者不可互相引用數字。
  - 「大盤」＝ regime 用的**等權指數**（`analysis/regime.py` 明文修正過的口徑），
    不是加權 TAIEX。等權指數對中小型股的投降更敏感，方向一致但幅度不可與大盤指數對表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

import polars as pl
from loguru import logger

# 子項代號（落檔與報表都用這組，別在別處另取名）
HIT_MARGIN = "margin_capitulation"
HIT_BREADTH = "breadth_washout"
HIT_FLOW = "flow_extreme_streak"
HIT_INDEX = "index_deep_deviation"
_ALL_HITS = (HIT_MARGIN, HIT_BREADTH, HIT_FLOW, HIT_INDEX)


@dataclass(frozen=True)
class SubSignal:
    """單一子項的判定結果。`status` 是誠實欄：資料不足時不假裝算得出來。"""

    name: str
    hit: bool
    value: float | None
    threshold: float | None
    status: str  # ok | insufficient_data | missing
    detail: str = ""


@dataclass(frozen=True)
class WashoutResult:
    as_of: date | None
    triggered: bool
    n_hit: int
    n_evaluable: int
    hits: list[str]
    subs: list[SubSignal] = field(default_factory=list)

    @property
    def posture_note(self) -> str:
        """觸發時 regime 姿態要改印的字串（委託書 M2.3）。未觸發回空字串。"""
        if not self.triggered:
            return ""
        return (
            "深跌後段・反轉警戒——極端讀數＝投降特徵，禁止在此區追加減碼"
            f"（{self.n_hit}/{self.n_evaluable} 子項觸發：{'、'.join(self.hits)}）"
        )


def market_margin_series(margin_daily: pl.DataFrame) -> pl.DataFrame:
    """逐股融資 → 全市場逐日序列 (date, total_margin_lots, total_margin_chg_lots)。

    **為什麼要帶 `total_margin_chg_lots` 而不是自己 diff 餘額**：margin 快取是逐日檔、
    但**有缺日**（週跑節奏下 `fetch_margin` 一週只補一天，靠 `fetch-margin-history` 回補
    才補齊）。對「可得日序列」做 `diff(1)` 會把跨假日/跨缺口的多日變化當成單日變化，
    z 分母被灌大、極值被稀釋。TWSE MI_MARGN 的 `margin_chg` 是**該日對前一交易日**的
    真實變化（由來源給、不受本機快取缺日影響），加總即為 gap-proof 的單日變化序列。

    Args:
        margin_daily: 多日 margin 快取合併（需 date／margin_balance／margin_chg）。

    Returns:
        依日期升冪；輸入缺欄或空 → 空表。**單位張、僅上市**（見模組 docstring）。
    """
    schema = {
        "date": pl.Date,
        "total_margin_lots": pl.Int64,
        "total_margin_chg_lots": pl.Int64,
    }
    need = {"date", "margin_balance", "margin_chg"}
    if margin_daily.is_empty() or not need.issubset(margin_daily.columns):
        return pl.DataFrame(schema=schema)
    return (
        margin_daily.group_by("date")
        .agg(
            pl.col("margin_balance").sum().cast(pl.Int64).alias("total_margin_lots"),
            pl.col("margin_chg").sum().cast(pl.Int64).alias("total_margin_chg_lots"),
        )
        .sort("date")
        .select(list(schema))
    )


def margin_capitulation(
    series: pl.DataFrame,
    z_threshold: float = -2.0,
    lookback: int = 250,
    min_samples: int = 60,
    window_5d_max_calendar_days: int = 9,
) -> SubSignal:
    """子項 1：全市場融資餘額單日 / 5 日減幅的 z 分數是否 < 門檻。

    兩窗取**較極端者**（min）——單日崩量式減資與五日連續縮減是同一現象的不同節奏，
    只看其中一個會漏掉另一型。z 建在**變化量**自身的均值/標準差上，不是水位的 z：
    融資餘額在多頭年是趨勢序列，水位百分位/z 會失去鑑別力（docs/26 §5.1 的 DGS20 前例）。

    Args:
        series: `market_margin_series` 輸出。
        z_threshold: z 低於此＝投降（settings，預設 −2）。
        lookback: 算 z 的回看列數（＝可得交易日數）。
        min_samples: 有效樣本低於此 → 該窗不評（不用薄樣本算 z）。
        window_5d_max_calendar_days: 尾端 5 列若橫跨超過此日曆天數＝快取有缺日、
            這 5 列不是連續 5 個交易日 → **5 日窗誠實棄用**，只留單日窗。

    誠實邊界：5 日窗是「尾端 5 列的變化量加總」。歷史段的 rolling(5) 同樣以可得列為單位，
    分佈與當期同構、可比；但若尾端剛好跨缺口，寧可不算也不報一個橫跨兩週的「5 日減幅」。
    """
    if series.is_empty() or series.height < 2:
        return SubSignal(HIT_MARGIN, False, None, z_threshold, "insufficient_data",
                         f"融資序列僅 {series.height} 日，算不出變化量")
    s = series.sort("date").tail(lookback)
    chg_col = "total_margin_chg_lots"
    d5 = s.with_columns(pl.col(chg_col).rolling_sum(5).alias("_d5"))

    def _z(col: str) -> float | None:
        vals = d5[col].drop_nulls()
        if vals.len() < min_samples:
            return None
        mu, sd = vals.mean(), vals.std()
        latest = d5[col].tail(1).item()
        if mu is None or sd is None or not sd or latest is None:
            return None
        return (float(cast(float, latest)) - float(cast(float, mu))) / float(cast(float, sd))

    parts: list[str] = []
    zs: list[float] = []
    z1 = _z(chg_col)
    if z1 is None:
        parts.append(f"單日窗有效樣本 < {min_samples}、不評")
    else:
        zs.append(z1)
        parts.append(f"單日 z={z1:+.2f}")
    # 5 日窗連續性檢核：尾端 5 列若跨缺口就不算
    tail_dates = s.tail(5)["date"].to_list()
    span = (tail_dates[-1] - tail_dates[0]).days if len(tail_dates) == 5 else None
    if span is None or span > window_5d_max_calendar_days:
        parts.append(f"5日窗棄用（尾端 5 列橫跨 {span} 日曆天，快取有缺日）")
    else:
        z5 = _z("_d5")
        if z5 is None:
            parts.append(f"5日窗有效樣本 < {min_samples}、不評")
        else:
            zs.append(z5)
            parts.append(f"5日 z={z5:+.2f}")
    if not zs:
        return SubSignal(HIT_MARGIN, False, None, z_threshold, "insufficient_data",
                         "；".join(parts))
    z = min(zs)
    return SubSignal(
        HIT_MARGIN, z < z_threshold, round(z, 2), z_threshold, "ok",
        f"取較極端者 {z:+.2f}（{'；'.join(parts)}；張數口徑、僅上市）",
    )


def breadth_washout(
    frac_above_ma: float | None,
    max_frac: float = 0.20,
) -> SubSignal:
    """子項 2：全宇宙站上 MA60 的比率是否低於門檻。

    Args:
        frac_above_ma: `regime.compute_breadth_score` 依據裡的 `frac_above_ma` ∈ [0,1]；
            None＝廣度不可信（有效報價股數不足），誠實回 `missing`、不猜。
        max_frac: 低於此＝廣度 washout（settings，預設 0.20）。
    """
    if frac_above_ma is None:
        return SubSignal(HIT_BREADTH, False, None, max_frac, "missing",
                         "regime 廣度分項不可信（有效報價股數不足）")
    return SubSignal(
        HIT_BREADTH, frac_above_ma < max_frac, round(float(frac_above_ma), 3), max_frac, "ok",
        f"站上 MA60 佔比 {frac_above_ma:.1%}",
    )


def flow_extreme_streak(
    flow_history: pl.DataFrame,
    max_score: float = -0.9,
    min_weeks: int = 3,
    trading_days_per_week: int = 5,
) -> SubSignal:
    """子項 3：regime 資金子分是否 < 門檻且連續 ≥ min_weeks 週。

    Args:
        flow_history: (date, flow_score) 逐日 as-of 序列（`research/panel/regime_labels.parquet`
            或累積的 washout 歷史）。缺檔 → `missing`。
        max_score: 資金子分低於此＝極端（settings，預設 −0.9）。
        min_weeks: 需連續幾週。
        trading_days_per_week: 週→交易日換算；「連 N 週」＝尾端連續 N×tdpw 個交易日皆極端。

    誠實邊界：本子項要求**尾端連續**——中間任一日回到門檻之上即中斷。序列長度不足
    N 週 → `insufficient_data`（不用半段序列宣稱「連 3 週」）。
    """
    need = min_weeks * trading_days_per_week
    if flow_history.is_empty() or not {"date", "flow_score"}.issubset(flow_history.columns):
        return SubSignal(HIT_FLOW, False, None, max_score, "missing",
                         "無 regime 資金子分歷史（先跑 make regime-history 或累積 washout 歷史）")
    s = flow_history.sort("date").drop_nulls("flow_score")
    if s.height < need:
        return SubSignal(HIT_FLOW, False, None, max_score, "insufficient_data",
                         f"資金子分序列 {s.height} 日 < 連 {min_weeks} 週所需 {need} 日")
    tail = s.tail(need)["flow_score"].to_list()
    streak_ok = all(float(v) < max_score for v in tail)
    worst = max(float(v) for v in tail)  # 尾段中「最不極端」的一天決定連續是否成立
    return SubSignal(
        HIT_FLOW, streak_ok, round(worst, 3), max_score, "ok",
        f"近 {need} 個交易日資金子分最高 {worst:+.3f}"
        f"（{'全段' if streak_ok else '未全段'} < {max_score}）",
    )


def dense_days(price_history: pl.DataFrame, min_priced: int) -> pl.DataFrame:
    """只留「當日有報價個股數 ≥ min_priced」的交易日——薄覆蓋日不得進指數。

    **為什麼非做不可（2026-08-08 實測）**：本機日線快取的每日宇宙大小極不平均——
    2026-07-27~07-30 每天只有 **10 檔**、07-23 有 175 檔、08-06 有 191 檔，而正常日
    5,000~6,400 檔。等權指數在只有 10 檔的日子等於「這 10 檔的平均」，chain 下去會產生
    憑空的 −20% 級跌幅（實測 2026-07-31 指數 −23%，而當日大盤實際是史上最大單日漲點）。

    `regime.compute_breadth_score` 早就有 `min_priced` 這道閘（薄覆蓋 → 廣度回 None），
    但 `compute_market_index` 沒有——趨勢分項與本子項都吃這個未設防的指數。本函式在
    M2 範圍內自保：**只過濾本子項用的輸入，不動 regime 既有行為**（那是另一個 milestone
    的事，見 docs/27 §風險）。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return price_history
    good = (
        price_history.drop_nulls("close")
        .group_by("date")
        .agg(pl.len().alias("_n"))
        .filter(pl.col("_n") >= min_priced)
        .select("date")
    )
    return price_history.join(good, on="date", how="inner")


def index_deep_deviation(
    market_index: pl.DataFrame,
    ma_window: int = 60,
    max_dist_pct: float = -7.0,
) -> SubSignal:
    """子項 4：等權指數距自身 MA60 的乖離是否深於門檻。

    Args:
        market_index: (date, market_index) 等權指數序列。**呼叫端須先用 `dense_days`
            濾掉薄覆蓋日**，否則指數會被 10 檔的日子污染（見該函式 docstring）。
        ma_window: 均線窗。
        max_dist_pct: 乖離深於此（更負）＝深負乖離（settings，預設 −7%）。
    """
    if market_index.is_empty() or "market_index" not in market_index.columns:
        return SubSignal(HIT_INDEX, False, None, max_dist_pct, "missing", "無等權指數序列")
    idx = market_index.sort("date")["market_index"].drop_nulls()
    if idx.len() < ma_window:
        return SubSignal(HIT_INDEX, False, None, max_dist_pct, "insufficient_data",
                         f"指數序列 {idx.len()} 日 < MA{ma_window}")
    ma = idx.tail(ma_window).mean()
    latest = float(cast(float, idx.tail(1).item()))
    if ma is None or float(cast(float, ma)) <= 0:
        return SubSignal(HIT_INDEX, False, None, max_dist_pct, "insufficient_data", "MA 不可用")
    dist = (latest / float(cast(float, ma)) - 1.0) * 100.0
    return SubSignal(
        HIT_INDEX, dist < max_dist_pct, round(dist, 2), max_dist_pct, "ok",
        f"等權指數距 MA{ma_window} {dist:+.2f}%（等權口徑、非加權 TAIEX）",
    )


def detect_market_washout(subs: list[SubSignal], min_hits: int = 2) -> WashoutResult:
    """≥ min_hits 個子項同時成立 → `market_washout` 觸發（委託書 M2.2）。

    **分母誠實**：`n_evaluable` 只算 `status=ok` 的子項。子項因資料缺而算不出來時，
    分母縮小、分子不變——這使觸發判定保守（少一個子項只會更難觸發），但**不得**把
    「4 項裡 0 項觸發」與「1 項可算且未觸發」印成同一句話（沿 docs/26 §6.2(5) 的
    「已求值 N 項中觸發 M 項」口徑）。
    """
    hits = [s.name for s in subs if s.hit]
    n_eval = sum(1 for s in subs if s.status == "ok")
    as_of = None
    return WashoutResult(
        as_of=as_of,
        triggered=len(hits) >= min_hits,
        n_hit=len(hits),
        n_evaluable=n_eval,
        hits=hits,
        subs=subs,
    )


def compute_market_washout(
    cfg: dict,
    settings_path: Path,
    regime_result: object,
    price_history: pl.DataFrame | None = None,
) -> WashoutResult:
    """IO 便利包裝：載融資快取／資金子分歷史 → 四子項 → `detect_market_washout`。

    比照 `regime.compute_market_regime` 的分工（純函式在上、IO 包裝在下），供 group 報告
    與 `market washout` CLI 共用。純讀本機快取、**不打網**。

    Args:
        cfg: 完整 settings dict（讀 `washout`／`regime`／`paths`）。
        settings_path: 設定檔路徑（建 TWSE client 用）。
        regime_result: `RegimeResult`——取 `evidence["breadth"]["frac_above_ma"]`。
        price_history: 全市場日線；None 則自行載（group 已載過時傳入避免重讀）。

    資金子分歷史來源（依序）：`backtest.regime_history.output_path`（`make regime-history`
    的 as-of 逐日產物）→ 讀不到則本模組自己累積的 washout 歷史都沒有 flow 序列，
    該子項誠實回 `missing`。**不就地重算逐日 regime**——那要跑數分鐘，不該掛在週報路徑上。
    """
    from tw_screener.analysis.regime import compute_market_index
    from tw_screener.analysis.rotation import load_market_history
    from tw_screener.data.twse import create_client

    wcfg = cfg.get("washout", {}) or {}
    rcfg = cfg.get("regime", {}) or {}
    cache_dir = Path(cfg["paths"]["cache_dir"]) / "twse"

    # 子項 1：融資投降
    mcfg = wcfg.get("margin", {}) or {}
    client = create_client(settings_path)
    margin_frame = client.load_margin_frame(n_days=int(mcfg.get("lookback_days", 250)))
    sub_margin = margin_capitulation(
        market_margin_series(margin_frame),
        z_threshold=float(mcfg.get("z_threshold", -2.0)),
        lookback=int(mcfg.get("lookback_days", 250)),
        min_samples=int(mcfg.get("min_samples", 60)),
        window_5d_max_calendar_days=int(mcfg.get("window_5d_max_calendar_days", 9)),
    )

    # 子項 2：廣度 washout（重用 regime 已算好的依據，不重算）
    ev = getattr(regime_result, "evidence", {}) or {}
    breadth_ev = ev.get("breadth", {}) if isinstance(ev, dict) else {}
    frac = breadth_ev.get("frac_above_ma") if isinstance(breadth_ev, dict) else None
    sub_breadth = breadth_washout(
        float(frac) if frac is not None else None,
        max_frac=float((wcfg.get("breadth", {}) or {}).get("max_frac_above_ma", 0.20)),
    )

    # 子項 3：資金分項極端持續
    fcfg = wcfg.get("flow", {}) or {}
    labels_path = Path(
        cfg.get("backtest", {}).get("regime_history", {}).get(
            "output_path", "research/panel/regime_labels.parquet"
        )
    )
    flow_hist = pl.DataFrame()
    if labels_path.exists():
        try:
            flow_hist = pl.read_parquet(labels_path).select("date", "flow_score")
        except Exception as exc:  # noqa: BLE001 — 研究產物壞掉不擋週報，該子項誠實 missing
            logger.warning("讀 {} 失敗（{}），資金子分子項標 missing", labels_path, exc)
    sub_flow = flow_extreme_streak(
        flow_hist,
        max_score=float(fcfg.get("max_flow_score", -0.9)),
        min_weeks=int(fcfg.get("min_weeks", 3)),
        trading_days_per_week=int(fcfg.get("trading_days_per_week", 5)),
    )

    # 子項 4：指數深負乖離（等權指數，口徑同 regime）。**先濾薄覆蓋日**——快取每日宇宙
    # 大小極不平均（實測有只剩 10 檔的日子），不濾會憑空造出 −20% 級假乖離。
    icfg = wcfg.get("index", {}) or {}
    if price_history is None:
        price_history = load_market_history(cache_dir, n_days=int(rcfg.get("history_days", 250)))
    min_priced = int(
        icfg.get("min_priced", (rcfg.get("breadth", {}) or {}).get("min_priced", 200))
    )
    dense = dense_days(price_history, min_priced)
    market_index = compute_market_index(
        dense, clip_daily_return_pct=float(rcfg.get("clip_daily_return_pct", 10.0))
    )
    latest_dense = cast("date | None", dense["date"].max() if not dense.is_empty() else None)
    latest_any = cast(
        "date | None", price_history["date"].max() if not price_history.is_empty() else None
    )
    if latest_dense is None or latest_dense != latest_any:
        sub_index = SubSignal(
            HIT_INDEX, False, None, float(icfg.get("max_dist_pct", -7.0)), "insufficient_data",
            f"最新交易日 {latest_any} 的日線覆蓋 < {min_priced} 檔（薄快取），"
            f"最近一個足量日＝{latest_dense}；不用薄樣本算乖離",
        )
    else:
        ma_window = int(icfg.get("ma_window", 60))
        sub_index = index_deep_deviation(
            market_index,
            ma_window=ma_window,
            max_dist_pct=float(icfg.get("max_dist_pct", -7.0)),
        )
        # 濾掉的薄日不會憑空消失——它們的個股報酬會併進下一個足量日（chain 的性質）。
        # 這使乖離「幅度」不精確（方向仍可讀），如實附註而非假裝濾乾淨了。
        n_all = price_history["date"].n_unique()
        n_dense = dense["date"].n_unique()
        if n_dense < n_all and sub_index.status == "ok":
            sub_index = SubSignal(
                sub_index.name, sub_index.hit, sub_index.value, sub_index.threshold,
                sub_index.status,
                f"{sub_index.detail}；⚠️ 回看窗內 {n_all - n_dense}/{n_all} 個交易日因快取"
                f"覆蓋 < {min_priced} 檔被剔除，其報酬併入次一足量日 → **幅度不精確、"
                f"只讀方向**（densify 快取＝`make backfill-daily-history`）",
            )

    result = detect_market_washout(
        [sub_margin, sub_breadth, sub_flow, sub_index],
        min_hits=int(wcfg.get("min_hits", 2)),
    )
    as_of = getattr(regime_result, "as_of", None)
    return WashoutResult(
        as_of=as_of,
        triggered=result.triggered,
        n_hit=result.n_hit,
        n_evaluable=result.n_evaluable,
        hits=result.hits,
        subs=result.subs,
    )


def render_washout_block(result: WashoutResult, descriptive_only: bool = True) -> list[str]:
    """group_analysis regime 段的 washout 區塊（純揭露，不改燈色、不改排序）。"""
    head = "🔻 **投降洗盤 flag：觸發**" if result.triggered else "投降洗盤 flag：未觸發"
    lines = [
        f"- {head}（已求值 {result.n_evaluable} 項中觸發 {result.n_hit} 項；共 4 個子項）",
    ]
    if result.triggered:
        lines.append(f"  - 姿態改印：**{result.posture_note}**")
        lines.append(
            "  - 效果：恐慌豁免（M3.2）進入可用狀態、持股表逐檔標「豁免適用/不適用」；"
            "左側清單（M1）升級為決策卡必列段。"
        )
    for s in result.subs:
        mark = "✅" if s.hit else ("—" if s.status != "ok" else "·")
        val = "未取得" if s.value is None else f"{s.value}"
        lines.append(f"  - {mark} `{s.name}`：{val}（門檻 {s.threshold}／{s.status}）— {s.detail}")
    if descriptive_only:
        lines.append(
            "  - ⚠️ **未校準、僅描述**：歷史只有一次 530 億級投降樣本（2026-07-30），"
            "門檻為先驗值。依委託書 M2.2，上線前 4 週本 flag **只描述、不判勝負**；"
            "口徑降級見 `analysis/washout.py` docstring（融資＝張數/僅上市、指數＝等權）。"
        )
    return lines


_HISTORY_SCHEMA: dict[str, type[pl.DataType]] = {
    "as_of": pl.Date,
    "triggered": pl.Boolean,
    "n_hit": pl.Int64,
    "n_evaluable": pl.Int64,
    "hits": pl.Utf8,
    "margin_z": pl.Float64,
    "breadth_frac": pl.Float64,
    "flow_worst": pl.Float64,
    "index_dist_pct": pl.Float64,
    "statuses": pl.Utf8,
}


def to_history_row(result: WashoutResult, as_of: date) -> pl.DataFrame:
    """WashoutResult → 一列歷史（供未來回測驗證偵測本身，委託書 M2.3 第 4 點）。"""
    by_name = {s.name: s for s in result.subs}

    def _v(name: str) -> float | None:
        s = by_name.get(name)
        return None if s is None else s.value

    return pl.DataFrame(
        [{
            "as_of": as_of,
            "triggered": result.triggered,
            "n_hit": result.n_hit,
            "n_evaluable": result.n_evaluable,
            "hits": "|".join(result.hits),
            "margin_z": _v(HIT_MARGIN),
            "breadth_frac": _v(HIT_BREADTH),
            "flow_worst": _v(HIT_FLOW),
            "index_dist_pct": _v(HIT_INDEX),
            "statuses": "|".join(f"{n}={by_name[n].status}" for n in _ALL_HITS if n in by_name),
        }],
        schema=_HISTORY_SCHEMA,
    )


def append_washout_history(history_path: Path, result: WashoutResult, as_of: date) -> None:
    """append 一列進 washout_history.parquet。**同日冪等**。

    冪等的理由同 `macro_regime.append_history`：`make week` 同一天可能重跑，沒有這道檢查
    會在累積序列裡埋進同日重複列，往後拿這份歷史回測偵測本身時會被悄悄污染。
    """
    history_path.parent.mkdir(parents=True, exist_ok=True)
    new_row = to_history_row(result, as_of)
    if history_path.exists():
        existing = pl.read_parquet(history_path)
        if not existing.is_empty() and (existing["as_of"] == as_of).any():
            logger.info(f"washout history 已有 {as_of} 這天，跳過重複 append")
            return
        combined = pl.concat([existing, new_row], how="diagonal_relaxed")
    else:
        combined = new_row
    combined.write_parquet(history_path)
    logger.info(f"washout history 寫入 {history_path}（{len(combined)} 列）")
