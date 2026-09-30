"""backtest/intra_pick_ledger.py — M-Pick3c 前瞻台帳（純函式；docs/35，docs/33 §2B）。

M-Pick2／M-Pick3b 在「重建的」shortlist 可入選池裡檢驗族群內挑檔因子；本模組是確認軌：
`make week` 尾段把當週**真實** shortlist 池（reports/<週>/shortlist.csv，不是重建池）連同
凍結當下的因子值寫進底帳，供 2026-W40 起的乾淨樣本外評估。

底帳只記「當週看得到的東西」：池成員與 tier／rank／gate_reason、F1–F4 因子值、偏好帶距離；
**不含任何報酬欄**——r+20 一律事後 join，底帳因此可以 append-only、已凍結的列不會被事後補欄動到。

口徑（全部沿用，不新增定義）：
- 因子＝`intra_pick` 的純函式（price_features／eps_asof／revenue_asof；docs/32 §3 的定義、
  價格用原始收盤、point-in-time 期限規則），窗長與可得期限讀 `backtest.intra_pick`。
- 偏好帶距離＝`shortlist._band_dist`（與生產排序同一函式），凍結記錄以免日後偏好帶改動污染舊列。
- 成交額（H1 的 log 成交額）與距季線位階＝shortlist.csv 自帶的生產值，不另算。

IO（讀 shortlist.csv、載日線／FinMind 快取、印摘要）由 `intra_pick_ledger_runner` 負責。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from tw_screener.backtest import intra_pick as ip
from tw_screener.report.shortlist import (
    TIER_ALT,
    TIER_CAPPED,
    TIER_TOP,
    ShortlistConfig,
    _band_dist,
)

#: 合格成員＝過 gate 者（top／alt 入選，capped 為過 gate 但被同次產業／因子簇／名額上限擋下）。
#: 族群內比較的母體（docs/32 §2 的 in_main 在真實池的對應；n_g ≥ 2 由評估端再套）。
MAIN_TIERS: tuple[str, ...] = (TIER_TOP, TIER_ALT, TIER_CAPPED)

LEDGER_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "recorded_at": pl.Utf8,  # 該週列最後一次寫入時間（UTC，秒）——稽核用，不進任何統計
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "source": pl.Utf8,  # candidate／watchlist／holding
    "sub_industry": pl.Utf8,  # 主次產業（生產口徑，concepts.yaml 手標第一個）
    "tier": pl.Utf8,  # top／alt／capped／gated／held（生產 shortlist 當週結果）
    "rank": pl.Int64,  # top／alt 的機器排序名次，其餘 null
    "gate_reason": pl.Utf8,  # gated／capped 的原因，top／alt 為 null
    "trend_score": pl.Float64,
    "trend_rank": pl.Int64,
    "trend_n": pl.Int64,
    "trend_bucket": pl.Int64,
    "close": pl.Float64,
    "ma60_dist_pct": pl.Float64,
    "amount_million": pl.Float64,  # 單日成交額（百萬；H1 的 log 成交額由此衍生）
    "band_dist": pl.Float64,  # 與偏好帶 ext_band_pct 的距離（帶內＝0；H2 的 −偏好帶距離由此衍生）
    "mom_6_1": pl.Float64,  # F1
    "high52_near": pl.Float64,  # F2
    "eps_accel": pl.Float64,  # F3
    "rev_accel": pl.Float64,  # F4
    # F3／F4 當週查找的期別（如 2026Q2、2026-08）：因子為 null 時，判讀是快取落後或該檔缺資料
    "eps_quarter": pl.Utf8,
    "rev_month": pl.Utf8,
}
LEDGER_COLUMNS: tuple[str, ...] = tuple(LEDGER_SCHEMA)

#: 由程式算出（不從 shortlist.csv 拷貝）的台帳欄；其餘欄原樣取自當週 shortlist.csv
_COMPUTED_COLS: frozenset[str] = frozenset(
    {
        "recorded_at",
        "band_dist",
        "mom_6_1",
        "high52_near",
        "eps_accel",
        "rev_accel",
        "eps_quarter",
        "rev_month",
    }
)
SHORTLIST_SOURCE_COLS: tuple[str, ...] = tuple(c for c in LEDGER_COLUMNS if c not in _COMPUTED_COLS)

#: 台帳記錄的因子欄（docs/32 §3 四因子；順序＝報告列序）
FACTOR_COLS: tuple[str, ...] = ip.FACTORS

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


class LedgerFrozenError(RuntimeError):
    """該週已過寫入期限（data_date + rewrite_days）或無法判定期限：首次寫入與重寫一律拒絕。"""


# ─── 週次閘與期別標籤 ─────────────────────────────────────────────────────────


def is_week_tag(value: str) -> bool:
    """是否為 YYYY-Www 週次標籤（固定寬度，字串序＝時間序）。"""
    return bool(_WEEK_RE.match(value))


def is_before_start(week: str, start_week: str) -> bool:
    """week 早於乾淨樣本起點（start_week）→ True。兩者都必須是 YYYY-Www，否則 ValueError。"""
    for label, value in (("week", week), ("start_week", start_week)):
        if not is_week_tag(value):
            raise ValueError(f"{label}={value!r} 不是 YYYY-Www 格式")
    return week < start_week


def _quarter_label(qi: int) -> str:
    """季別索引（year·4+quarter−1）→ 2026Q2。"""
    return f"{qi // 4}Q{qi % 4 + 1}"


def _month_label(mi: int) -> str:
    """月別索引（year·12+month−1）→ 2026-08。"""
    return f"{mi // 12}-{mi % 12 + 1:02d}"


def _rev_month_index(d: date, cfg: ip.IntraPickConfig) -> int:
    """d 當日 F4 應查的營收月索引；規則鏡射 intra_pick.revenue_asof
    （d 日 ≥ revenue_available_day → 上個月，否則上上個月），測試釘住兩者一致。"""
    return d.year * 12 + d.month - 1 - (1 if d.day >= cfg.revenue_available_day else 2)


def eps_quarter_label(d: date, cfg: ip.IntraPickConfig) -> str:
    """d 當日 F3 應查的財報季標籤（如 2026Q2）；與 intra_pick.eps_asof 用同一個查找函式。"""
    return _quarter_label(ip.latest_available_quarter(d, cfg.eps_available_from))


def rev_month_label(d: date, cfg: ip.IntraPickConfig) -> str:
    """d 當日 F4 應查的營收月標籤（如 2026-08）。"""
    return _month_label(_rev_month_index(d, cfg))


def cache_staleness(
    fin: pl.DataFrame, rev: pl.DataFrame, d: date, cfg: ip.IntraPickConfig
) -> list[str]:
    """FinMind 財報／月營收快取的最新期別是否落後 d 當週應查的期別（快取層級判斷、非逐檔）。

    落後 → F3／F4 整週會是 null（as-of 規則不回退舊期）；回傳可操作的提示句，空 list＝快取新鮮。
    """
    notes: list[str] = []
    need_q = ip.latest_available_quarter(d, cfg.eps_available_from)
    have_q = (
        None
        if fin.is_empty()
        else fin.select(
            (pl.col("year").cast(pl.Int64) * 4 + pl.col("quarter").cast(pl.Int64) - 1).max()
        ).item()
    )
    if have_q is None or have_q < need_q:
        have = "為空" if have_q is None else f"最新季 {_quarter_label(have_q)}"
        notes.append(
            f"FinMind 財報快取{have}，本週 F3 應查 {_quarter_label(need_q)}"
            "——跑 make backfill-finmind-financials"
        )
    need_m = _rev_month_index(d, cfg)
    have_m = (
        None
        if rev.is_empty()
        else rev.select(
            (pl.col("year").cast(pl.Int64) * 12 + pl.col("month").cast(pl.Int64) - 1).max()
        ).item()
    )
    if have_m is None or have_m < need_m:
        have = "為空" if have_m is None else f"最新月 {_month_label(have_m)}"
        notes.append(
            f"FinMind 月營收快取{have}，本週 F4 應查 {_month_label(need_m)}"
            "——跑 make backfill-finmind-revenue"
        )
    return notes


# ─── 組列 ────────────────────────────────────────────────────────────────────


def build_ledger_rows(
    shortlist: pl.DataFrame,
    feats: pl.DataFrame,
    eps: pl.DataFrame,
    rev: pl.DataFrame,
    *,
    sl_cfg: ShortlistConfig,
    ip_cfg: ip.IntraPickConfig,
    recorded_at: str,
) -> pl.DataFrame:
    """真實 shortlist 全表（每個被考慮的股票一列，含 gated／held）＋凍結因子值 → 底帳列。

    feats＝`intra_pick.price_features` 輸出；eps／rev＝`eps_asof`／`revenue_asof`
    對 (data_date, 池內股票) 的輸出。三者都只取 shortlist 的 data_date 那一日；
    該日無資料的股票因子為 null（不補值、不回退舊日）。
    shortlist 須為單一週次、單一 data_date（reports/<週>/shortlist.csv 的天然形狀）。
    欄位缺漏或型別漂移（如數值欄出現字串）一律 raise，不靜默轉 null：
    凍結證據寧可少一週、不可悄悄變形。
    """
    missing = [c for c in SHORTLIST_SOURCE_COLS if c not in shortlist.columns]
    if missing:
        raise ValueError(f"shortlist 缺欄 {missing}——上游 shortlist.csv 欄位與台帳規格不符")
    # load_shortlist 讀 CSV 不解析日期（data_date 是 "2026-09-24" 字串）；已是 Date 則原樣通過
    shortlist = shortlist.with_columns(pl.col("data_date").cast(pl.Date, strict=False))
    weeks = shortlist["week"].unique().to_list()
    dates = shortlist["data_date"].unique().to_list()
    if len(weeks) != 1 or len(dates) != 1 or dates[0] is None:
        raise ValueError(
            f"shortlist 須為單一週次且 data_date 非空：weeks={weeks} data_dates={dates}"
        )
    data_date: date = dates[0]

    def at_date(df: pl.DataFrame, cols: Sequence[str]) -> pl.DataFrame:
        return df.filter(pl.col("date") == data_date).select("stock_id", *cols)

    base = shortlist.with_columns(
        pl.col("stock_id").cast(pl.Utf8),
        _band_dist(sl_cfg).alias("band_dist"),
        pl.lit(recorded_at, dtype=pl.Utf8).alias("recorded_at"),
        pl.lit(eps_quarter_label(data_date, ip_cfg), dtype=pl.Utf8).alias("eps_quarter"),
        pl.lit(rev_month_label(data_date, ip_cfg), dtype=pl.Utf8).alias("rev_month"),
    )
    joined = (
        base.join(at_date(feats, ("mom_6_1", "high52_near")), on="stock_id", how="left")
        .join(at_date(eps, ("eps_accel",)), on="stock_id", how="left")
        .join(at_date(rev, ("rev_accel",)), on="stock_id", how="left")
    )
    out = joined.select([pl.col(c).cast(t) for c, t in LEDGER_SCHEMA.items()]).sort(
        "week", "stock_id"
    )
    if out.height != shortlist.height:
        raise ValueError(
            f"台帳列數 {out.height} ≠ shortlist 列數 {shortlist.height}——join 產生重複鍵"
        )
    return out


# ─── 底帳 IO ─────────────────────────────────────────────────────────────────


def read_ledger(path: Path) -> pl.DataFrame:
    """讀既有底帳；全欄位明帶 schema（同 g1_g2_g5_watch 修法），全 null 的數值欄不會變字串。"""
    if not path.exists():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    return pl.read_csv(path, schema_overrides=LEDGER_SCHEMA, try_parse_dates=True).select(
        LEDGER_COLUMNS
    )


def rewrite_deadline(data_date: date, rewrite_days: int) -> date:
    """某週列可寫入（含首次寫入與重寫）的最後一天（含）。"""
    return data_date + timedelta(days=rewrite_days)


def check_write_window(
    existing: pl.DataFrame,
    week: str,
    new_data_date: date | None,
    *,
    now: datetime,
    rewrite_days: int,
) -> None:
    """對 week 的寫入（首次或重寫）是否仍在期限內；逾期 raise LedgerFrozenError。

    期限＝「基準日 + rewrite_days」（含當日）；基準日＝該週既有列的 data_date（已有列時，最早者），
    否則為新列的 new_data_date。無法判定基準日（既有列 data_date 全缺，或新列無 data_date）
    → 視為已凍結（失敗時關閉）。runner 用它在載入快取之前先 fail-fast，upsert_ledger 再做權威檢查。
    """
    old = existing.filter(pl.col("week") == week)
    if old.is_empty():
        kind = "首次寫入"
        ref_dates = [] if new_data_date is None else [new_data_date]
    else:
        kind = "重寫"
        ref_dates = [d for d in old["data_date"].unique().to_list() if d is not None]
    if not ref_dates:
        raise LedgerFrozenError(f"{week} {kind}無法判定期限（data_date 全缺）——視為已凍結")
    today = now.astimezone(UTC).date()
    deadline = rewrite_deadline(min(ref_dates), rewrite_days)
    if today > deadline:
        raise LedgerFrozenError(
            f"{week} {kind}已逾期：data_date={min(ref_dates)}，寫入期限 {deadline}"
            f"（rewrite_days={rewrite_days}），今日 {today}——本週不入乾淨樣本"
        )


def upsert_ledger(
    path: Path, new_rows: pl.DataFrame, *, now: datetime, rewrite_days: int
) -> pl.DataFrame:
    """把本週列併入底帳（以 week 為單位整週替換，冪等）。

    寫入期限（凍結規則，見 check_write_window）：對某一週的**任何**寫入——首次寫入或重寫——
    都要求 now（UTC 日期）不晚於「基準日 + rewrite_days」。逾期 → raise LedgerFrozenError、
    底帳不動。目的：乾淨樣本外證據不得在結果可見之後被改寫，也不得事後才決定要記哪幾週
    （選擇偏誤）；期限內（同週補跑、快取補齊後重跑）允許整週替換。其他週的列一律不碰。
    先寫暫存再換名，不留半套檔。
    """
    existing = read_ledger(path)
    if new_rows.is_empty():
        return existing
    weeks = new_rows["week"].unique().to_list()
    if len(weeks) != 1:
        raise ValueError(f"一次只 upsert 一週：{weeks}")
    week = weeks[0]
    new_dates = [d for d in new_rows["data_date"].unique().to_list() if d is not None]
    check_write_window(
        existing,
        week,
        min(new_dates) if new_dates else None,
        now=now,
        rewrite_days=rewrite_days,
    )
    merged = pl.concat(
        [existing.filter(pl.col("week") != week), new_rows.select(LEDGER_COLUMNS)],
        how="vertical",
    ).sort("week", "stock_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    merged.write_csv(tmp)
    tmp.replace(path)
    return merged


# ─── 摘要（runner 印給人看；不是統計裁決）─────────────────────────────────────


def week_summary(rows: pl.DataFrame, min_coverage: float) -> dict[str, object]:
    """本週列的規模與因子覆蓋率（合格成員內非 null 比例）。

    低於 min_coverage 的因子列入 low_coverage。合格成員＝MAIN_TIERS；
    n_groups_ge2＝合格成員 ≥ 2 檔的主次產業數（族群內比較實際有得比的組）。
    """
    main = rows.filter(pl.col("tier").is_in(MAIN_TIERS))
    n_main = main.height
    coverage: dict[str, float] = {}
    n_groups = n_groups_ge2 = 0
    if n_main:
        coverage = {c: 1.0 - main[c].null_count() / n_main for c in FACTOR_COLS}
        per_group = main.filter(pl.col("sub_industry").is_not_null()).group_by("sub_industry").len()
        n_groups = per_group.height
        n_groups_ge2 = per_group.filter(pl.col("len") >= 2).height
    return {
        "n_rows": rows.height,
        "n_main": n_main,
        "n_top": rows.filter(pl.col("tier") == TIER_TOP).height,
        "n_groups": n_groups,
        "n_groups_ge2": n_groups_ge2,
        "coverage": coverage,
        "low_coverage": sorted(c for c, v in coverage.items() if v < min_coverage),
    }
