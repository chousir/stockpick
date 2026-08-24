"""backtest/l6_g4_watch.py — docs/31 §9 item4：L6／G4 前瞻累積軌的每週快照＋底帳。

背景（不要重寫這段推導，讀 docs/31 §9「G1/G4/F2' 深度查核」與 §5.2 複核）：G4（成長股候選）
與 L6（左側反彈候選）本輪都做不出回溯統計驗證——G4 是月營收 OpenAPI 純快照、physically
無法回查 2022 空頭；L6 唯一的歷史數字（25.5%／31.7%）樣本量級只有「兩個重疊週」，經不起
再壓榨。兩者剩下的路只有**等時間**：每週記錄一次「誰在資料日符合門檻」，累積到不重疊的
新週次夠多，才有資格重新跑統計檢定（那個檢定本身不在本檔範圍，是未來的獨立里程碑）。

本檔只做「記錄」，不做「裁決」——原因見上：現在還沒有夠格判讀的樣本，先把記分板建起來，
硬跑檢定只會產出「樣本不足」的空殼。

**為什麼要現在存這些欄位，而不是等以後再回頭算**：`pe_ratio`／`cum_rev_yoy_pct`／
`rev_yoy_pct`／`rev_yoy_delta`／`trust_net_5d` 這五個欄位的資料源
（`load_latest_valuation_ratios`／`fetch_revenue`）**只回傳當下快照**，沒有歷史查詢
（docs/31 §9 已實測證實 `t187ap05_L` 官方 swagger 無 `parameters` 欄位）。今天不記，
這週的門檻判定就永久遺失、無法回頭補。相對地，「落難週」四維技術面（距MA60/距60日低/
前8週報酬）與「後4週報酬進同池前20%」的結果標籤全部是價格衍生欄，可以在未來任何時間點
用日線快取重建——本檔刻意不算這兩塊，避免現在寫死的口徑跟未來重建時的口徑不一致。

**L6 門檻的一個未解問題（不要自作主張選一個）**：docs/31 §4.2 定義 L6＝
`YoY≥20 ∧ PE≤25 ∧ 投信近5日淨買>0 ∧ 市值≥100億`（四條件），但 §5.2 實際做統計檢定的
只有「YoY≥20且PE≤25」兩條件（n=55/41/42，25.5%/31.7%/33.3%）——「投信近5日淨買>0」
在原表是獨立測的一列（n=89，27.0%），從未跟YoY/PE聯集驗證過。§9 item4 又只寫「相同判準
(YoY≥20∧PE≤25)」。本檔對兩種讀法都存旗標（`l6_2cond`／`l6_4cond`），不擅自二選一，
留給未來重新登記檢定時決定要對哪個口徑算命中率。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

LEDGER_SCHEMA: dict[str, type[pl.DataType]] = {
    "week": pl.Utf8,
    "data_date": pl.Date,
    "stock_id": pl.Utf8,
    "name": pl.Utf8,
    "market_cap_billion": pl.Float64,
    "pe_ratio": pl.Float64,
    "rev_yoy_pct": pl.Float64,          # 單月 YoY（G4 的 yoy_pct）
    "cum_rev_yoy_pct": pl.Float64,      # 累計 YoY（L6 的 YoY、G4 的 cum_yoy_pct）
    "rev_yoy_delta": pl.Float64,        # 本月 YoY − 上月 YoY（G4 的 rev_yoy_delta）
    "revenue_year_month": pl.Utf8,      # 營收數字所屬報告月（前視偏誤查核用，見 docstring）
    "revenue_preview_risk": pl.Boolean,  # True＝該報告月在 data_date 當下可能尚未公告
    "trust_net_5d": pl.Float64,         # 投信近5日淨買超（股，同 institutional 原始單位）
    "l6_2cond": pl.Boolean,             # YoY≥20 ∧ PE≤25（§5.2 實測的口徑、§9 item4 字面判準）
    "l6_4cond": pl.Boolean,             # 上式 ∧ 投信近5日淨買>0 ∧ 市值≥100億（§4.2 完整定義）
    "g4": pl.Boolean,                   # yoy_pct>cum_yoy_pct ∧ cum_yoy_pct≥0 ∧ rev_yoy_delta≥0
}


def revenue_disclosure_date(year_month: str) -> date | None:
    """月營收公告日估計＝次月10日（MOPS/TWSE公告慣例，非法定期限）。

    `year_month` 格式 "YYYYMM"（如 "202607"）→ 估計公告日 2026-08-10。格式不合法回 None。
    只用於粗篩「這個數字在 data_date 當下有沒有可能已公開」，不是精確法定期限——
    ±1、2 天的公告延遲本函式量不到，這是刻意的粗估，供旗標而非精確裁決用。
    """
    if not year_month or len(year_month) != 6 or not year_month.isdigit():
        return None
    year, month = int(year_month[:4]), int(year_month[4:6])
    if not (1 <= month <= 12):
        return None
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
    try:
        return date(next_year, next_month, 10)
    except ValueError:
        return None


def build_l6_g4_snapshot(
    universe: pl.DataFrame,
    revenue: pl.DataFrame,
    yoy_deltas: dict[str, tuple[float | None, float | None]],
    trust_net_5d: dict[str, float | None],
    week: str,
    data_date: date,
    l6_yoy_min: float = 20.0,
    l6_pe_max: float = 25.0,
    l6_mktcap_min_billion: float = 100.0,
) -> pl.DataFrame:
    """把全市場當週快照併成 L6/G4 判準列，只回傳至少命中一式的股票（不記全市場）。

    Args:
        universe: `screener.local.universe.build_local_universe()` 輸出（需
            stock_id/name/market_cap_billion/pe_ratio/cum_rev_yoy_pct）。
        revenue: `client.fetch_revenue()` 當月輸出（需 stock_id/year_month/yoy_pct）——
            `cum_rev_yoy_pct` 已在 universe 裡，這裡只借 revenue 的單月 yoy_pct+年月。
        yoy_deltas: `client.load_revenue_yoy_deltas()` 輸出 {stock_id: (delta, delta_prev)}。
        trust_net_5d: {stock_id: 近5日投信淨買超}（呼叫端算好傳入，見 runner）。
        week / data_date: 本週 tag 與資料日（供底帳去重鍵與後續回填用）。

    Returns:
        LEDGER_SCHEMA；`l6_2cond`/`l6_4cond`/`g4` 三者皆 False 的列不出現。
    """
    need = {"stock_id", "name", "market_cap_billion", "pe_ratio", "cum_rev_yoy_pct"}
    if universe.is_empty() or not need.issubset(universe.columns):
        return pl.DataFrame(schema=LEDGER_SCHEMA)

    rev_cols = revenue.select("stock_id", "year_month", "yoy_pct") if (
        not revenue.is_empty() and {"stock_id", "year_month", "yoy_pct"}.issubset(revenue.columns)
    ) else pl.DataFrame(schema={"stock_id": pl.Utf8, "year_month": pl.Utf8, "yoy_pct": pl.Float64})

    base = universe.select(
        "stock_id", "name", "market_cap_billion", "pe_ratio", "cum_rev_yoy_pct"
    ).join(rev_cols, on="stock_id", how="left")

    rows: list[dict] = []
    for r in base.iter_rows(named=True):
        sid = r["stock_id"]
        cum_yoy = r["cum_rev_yoy_pct"]
        pe = r["pe_ratio"]
        yoy = r["yoy_pct"]
        ym = r["year_month"]
        delta, _delta_prev = yoy_deltas.get(sid, (None, None))
        tn5 = trust_net_5d.get(sid)

        disclosure = revenue_disclosure_date(ym) if ym else None
        preview_risk = bool(disclosure is not None and disclosure > data_date)

        l6_2cond = (
            cum_yoy is not None and pe is not None
            and cum_yoy >= l6_yoy_min and pe <= l6_pe_max
        )
        l6_4cond = bool(
            l6_2cond
            and tn5 is not None and tn5 > 0
            and r["market_cap_billion"] is not None
            and r["market_cap_billion"] >= l6_mktcap_min_billion
        )
        g4 = bool(
            yoy is not None and cum_yoy is not None and delta is not None
            and yoy > cum_yoy and cum_yoy >= 0 and delta >= 0
        )
        if not (l6_2cond or l6_4cond or g4):
            continue
        rows.append(
            {
                "week": week,
                "data_date": data_date,
                "stock_id": sid,
                "name": r["name"],
                "market_cap_billion": r["market_cap_billion"],
                "pe_ratio": pe,
                "rev_yoy_pct": yoy,
                "cum_rev_yoy_pct": cum_yoy,
                "rev_yoy_delta": delta,
                "revenue_year_month": ym,
                "revenue_preview_risk": preview_risk,
                "trust_net_5d": tn5,
                "l6_2cond": bool(l6_2cond),
                "l6_4cond": l6_4cond,
                "g4": g4,
            }
        )
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


def select_l6_candidates(snapshot: pl.DataFrame) -> pl.DataFrame:
    """從 `build_l6_g4_snapshot()` 輸出篩 `l6_2cond==True` 的列（docs/31 §5.2實測
    讀法：YoY≥20∧PE≤25，`screen run-local l6`用）。

    刻意只用`l6_2cond`、不含`l6_4cond`多出的投信近5日淨買/市值≥100億兩條件——
    那兩條件從未獨立驗證過（docs/31 §9 記載的L6讀法未解問題），不可擅自升級成
    四條件版當「更嚴謹」，避免引入未經檢驗的額外篩選。**統計驗證仍未過關**
    （§20：L6目前仍在「累積中」，現有歷史數字樣本量級僅兩個重疊週），呼叫端必須
    把`source`標成`local_unvalidated`。
    """
    if snapshot.is_empty() or "l6_2cond" not in snapshot.columns:
        return pl.DataFrame(schema={"stock_id": pl.Utf8, "name": pl.Utf8})
    return snapshot.filter(pl.col("l6_2cond")).select("stock_id", "name").unique("stock_id")


@dataclass(frozen=True)
class L6G4Inputs:
    """`build_l6_g4_snapshot()` 需要的三個輸入（見 `build_l6_g4_inputs()`）。"""

    revenue: pl.DataFrame
    yoy_deltas: dict[str, tuple[float | None, float | None]]
    trust_net_5d: dict[str, float | None]


def build_l6_g4_inputs(client, cfg: dict, data_date: date) -> L6G4Inputs:  # noqa: ANN001 — TWSEClient，避免循環 import
    """組 `build_l6_g4_snapshot()` 需要的三個輸入（純讀既有快取，不打網）。

    抽出自原本寫死在 `l6_g4_watch_runner.run_l6_g4_watch` 裡的邏輯，供該 CLI runner
    與 `report/group_runner.py`（docs/31 使用者要求把 L6/G4 揭露進
    `candidates_enriched.csv` 後新增）共用，避免兩處各自維護一份等價邏輯。
    """
    wc = cfg.get("backtest", {}).get("l6_g4_watch", {})
    flow_window = int(wc.get("trust_flow_window_td", 5))

    revenue = client.fetch_revenue()
    yoy_deltas = client.load_revenue_yoy_deltas()

    inst = client.load_institutional_history(n_days=flow_window, as_of=data_date)
    trust_net_5d: dict[str, float | None] = {}
    if not inst.is_empty() and {"stock_id", "trust_net"}.issubset(inst.columns):
        agg = inst.group_by("stock_id").agg(pl.col("trust_net").sum().alias("_s"))
        trust_net_5d = {
            str(r["stock_id"]): (float(r["_s"]) if r["_s"] is not None else None)
            for r in agg.iter_rows(named=True)
        }

    return L6G4Inputs(revenue=revenue, yoy_deltas=yoy_deltas, trust_net_5d=trust_net_5d)


def _read_ledger(path: Path) -> pl.DataFrame:
    """讀既有底帳 CSV，全欄位明帶 `schema_overrides=LEDGER_SCHEMA`。

    2026-08-23 修正：原本只對 `stock_id` 指定 dtype，其餘欄位交給 `pl.read_csv` 自行
    推斷——當某週命中列的某數值欄（如 `rev_yoy_delta`，OTC 股常見）**全部是 null**時，
    推斷會把該欄判成 `Utf8`。下一步 `pl.concat(..., how="diagonal_relaxed")` 會把
    **合併後**的欄位（含先前所有週的真實浮點值）一併靜默升型成字串——底帳是唯一資料源，
    這個污染永久發生、無法回頭修。全欄位明帶 schema 讓 polars 直接把全 null 欄讀成
    該型別的 null，不會落回字串推斷。
    """
    if not path.exists():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    return pl.read_csv(path, try_parse_dates=True, schema_overrides=LEDGER_SCHEMA)


def upsert_l6_g4_ledger(path: Path, new_rows: pl.DataFrame) -> pl.DataFrame:
    """把本週快照併入底帳（以 (week, stock_id) 去重、冪等——同週重跑覆寫舊列）。

    底帳是唯一資料源（`pe_ratio`/`cum_rev_yoy_pct` 等欄位的資料源皆為當下快照，
    見模組 docstring）——`research/` 目錄本身 gitignored，本檔不備份，遺失即
    永久遺失，不能重建。
    """
    if new_rows.is_empty():
        return _read_ledger(path)
    existing = _read_ledger(path)
    if not existing.is_empty():
        week = new_rows["week"][0]
        existing = existing.filter(pl.col("week") != week)
    merged = pl.concat([existing, new_rows], how="diagonal_relaxed").sort(["week", "stock_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_csv(path)
    return merged


def ledger_progress_summary(ledger: pl.DataFrame) -> dict[str, object]:
    """底帳累積進度一行摘要（runner 印給人看，不是統計裁決）。"""
    if ledger.is_empty():
        return {"n_weeks": 0, "weeks": [], "n_l6_2cond": 0, "n_l6_4cond": 0, "n_g4": 0}
    return {
        "n_weeks": ledger["week"].n_unique(),
        "weeks": sorted(ledger["week"].unique().to_list()),
        "n_l6_2cond": int(ledger["l6_2cond"].fill_null(False).sum()),
        "n_l6_4cond": int(ledger["l6_4cond"].fill_null(False).sum()),
        "n_g4": int(ledger["g4"].fill_null(False).sum()),
        "n_preview_risk": int(ledger["revenue_preview_risk"].fill_null(False).sum()),
    }
