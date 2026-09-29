"""M-Pick3a 保留樣本週快照編排（docs/33 §5；自 cli.py 薄殼呼叫）。

FinMind 日線（2013 起、今日次產業成員）→ 交易日曆／價格因子／趨勢分重建
（intra_pick、rotation_efficacy 純函式，口徑同 M-Pick2）→ 除權息還原總報酬 target
→ regime 降級標籤（成員等權指數趨勢＋成員廣度，法人分項缺席，docs/33 §5 D4）→ 月營收 as-of
→ 2015-01～2021-12 週快照 parquet ＋ 資料品質報告（含 TWSE MI_INDEX 抽樣核價）
→ research/intra_pick_oos/。

**只做資料層**：不算任何因子×target 統計、不數主宇宙（in_main）規模——兩者都在 M-Pick3b
預註冊 commit 之後（docs/33 §5 執行順序 3）。報告裡的覆蓋率只看欄位非 null 比例，
不分組比 target。
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import typer
from rich.console import Console

from tw_screener.backtest import intra_pick as ip
from tw_screener.backtest import intra_pick_oos as oos

console = Console()

#: 報告的欄位覆蓋率清單（只算非 null 比例，不碰 target 值的分布）。
_COVERAGE_COLS: tuple[str, ...] = (
    "ma60_dist_pct", "amount_million", "trend_bucket", "high52_near", "rev_accel", "mom_6_1",
    "r10", "r20", "r40",
)


def _member_price(price_raw: pl.DataFrame, member_ids: set[str]) -> tuple[pl.DataFrame, int]:
    """今日成員且有成交的列 → (date, stock_id, close, volume)。

    比照 TWSE 慣例：無成交日無價、不成列。

    回傳 (價格表, 被排除的無成交列數)。
    """
    px = price_raw.filter(pl.col("stock_id").is_in(sorted(member_ids)))
    traded = px.filter(
        pl.col("close").is_not_null() & (pl.col("close") > 0) & (pl.col("volume").fill_null(0) > 0)
    )
    return (
        traded.select("date", "stock_id", "close", pl.col("volume").cast(pl.Int64)),
        px.height - traded.height,
    )


def build_holdout_stock_weeks(
    cfg_all: dict[str, Any], settings: Path, cfg: ip.IntraPickConfig, hcfg: oos.HoldoutConfig
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """IO：組出保留樣本 (date, stock_id) 週快照寬表與 meta（meta 內含報告要用的中間表）。"""
    from tw_screener.analysis.concepts import load_themes
    from tw_screener.analysis.rotation import compute_subindustry_baskets
    from tw_screener.analysis.sector_universe import list_subindustries
    from tw_screener.backtest import rotation_efficacy as eff
    from tw_screener.backtest.panel import build_price_panel
    from tw_screener.backtest.regime_history import build_regime_history
    from tw_screener.data.finmind import (
        load_dividend_result_history,
        load_month_revenue_history,
        load_stock_price_history,
    )
    from tw_screener.report.shortlist import order_members_by_concepts

    finmind_dir = Path(cfg_all["paths"]["cache_dir"]) / "finmind"
    concepts_path = settings.parent / "concepts.yaml"
    members = list_subindustries(concepts_path=concepts_path)
    if members.is_empty():
        console.print("[red]缺 concepts.yaml 次產業成員[/red]")
        raise typer.Exit(1)
    member_ids = set(members["stock_id"].cast(pl.Utf8).to_list())

    console.print("[bold]載入 FinMind 日線（全部 price_*.parquet）...[/bold]")
    price_raw = load_stock_price_history(finmind_dir)
    if price_raw.is_empty():
        console.print("[red]無 FinMind 日線快取——先跑 make backfill-finmind-price[/red]")
        raise typer.Exit(1)
    price, n_no_trade = _member_price(price_raw, member_ids)
    calendar = ip.trading_calendar(price, cfg.calendar_min_names)
    if not calendar:
        console.print("[red]FinMind 日線無可用交易日[/red]")
        raise typer.Exit(1)
    price = price.filter(pl.col("date").is_in(calendar))

    console.print("[bold]價格因子／趨勢分重建（intra_pick、rotation_efficacy 純函式）...[/bold]")
    feats = ip.price_features(price, calendar, cfg)
    primary = ip.primary_sector(order_members_by_concepts(members, load_themes(concepts_path)))
    baskets = compute_subindustry_baskets(members, price)
    trend = eff.trend_score_series(price, members, baskets)

    console.print("[bold]regime 降級標籤（空法人輸入 → 趨勢＋廣度按權重正規化）...[/bold]")
    regime = build_regime_history(price, pl.DataFrame(), cfg_all.get("regime") or {})

    console.print("[bold]除權息還原總報酬 target...[/bold]")
    divs = load_dividend_result_history(finmind_dir)
    covered = {f.stem.removeprefix("dividend_") for f in finmind_dir.glob("dividend_*.parquet")}
    events = oos.adjustment_events(divs)
    horizons = sorted({cfg.horizon, *cfg.disclose_horizons})
    targets = oos.total_return_targets(price, events, horizons, covered_ids=covered)
    # 位階（ma60_dist_pct）與 regime join 直接用面板同一函式（ETF 過濾、去重、滾動窗口徑零漂移）；
    # 面板自算的純價差 r{h} 丟掉，改用上面的總報酬 target。
    base = build_price_panel(
        price, horizons=tuple(horizons), ma_windows=(60,),
        regime=regime.select("date", "regime_label"),
    ).select("date", "stock_id", "ma60_dist_pct", "regime")

    weeks = oos.holdout_week_dates(calendar, hcfg.start, hcfg.end)
    buckets = ip.sector_bucket_table(
        trend, baskets, weeks, cfg.min_sector_members, cfg.shortlist.trend_buckets
    )
    sw = (
        base.filter(pl.col("date").is_in(weeks))
        .join(targets, on=["date", "stock_id"], how="left")
        .join(feats.drop("close"), on=["date", "stock_id"], how="left")
        .join(primary, on="stock_id", how="inner")
        .join(
            buckets.select("date", "sub_industry", "trend_score", "trend_bucket"),
            on=["date", "sub_industry"],
            how="left",
        )
        .with_columns(pl.col("stock_id").is_in(sorted(covered)).alias("div_covered"))
    )
    rev = load_month_revenue_history(finmind_dir)
    revf = ip.revenue_asof(
        sw.select("date", "stock_id"), ip.revenue_accel_table(rev), cfg.revenue_available_day
    )
    sw = ip.mark_universe(sw.join(revf, on=["date", "stock_id"], how="left"), cfg).sort(
        "date", "stock_id"
    )
    meta: dict[str, Any] = {
        "calendar": (calendar[0], calendar[-1], len(calendar)),
        "weeks": weeks,
        "horizons": horizons,
        "member_ids": member_ids,
        "n_primary": primary.height,
        "price_ids": set(price_raw["stock_id"].unique().to_list()),
        "n_no_trade": n_no_trade,
        "price_member": price,
        "price_raw": price_raw,
        "divs": divs,
        "events": events,
        "covered": covered,
        "rev": rev,
        "regime": regime,
        "targets": targets,
    }
    return sw, meta


# ─── 核價 ─────────────────────────────────────────────────────────────────────


def sample_reconcile_dates(
    calendar: list[date], years: tuple[int, int], per_year: int, seed: int
) -> list[date]:
    """每年從交易日曆抽 per_year 天（每年獨立種子 seed+年，換年份範圍不影響他年抽樣）。"""
    out: list[date] = []
    for y in range(years[0], years[1] + 1):
        days = [d for d in calendar if d.year == y]
        if days:
            out += sorted(random.Random(seed + y).sample(days, min(per_year, len(days))))
    return out


def reconcile_frames(
    fm: pl.DataFrame, ref: pl.DataFrame, tol_pct: float, fm_ids: set[str]
) -> tuple[pl.DataFrame, dict[str, int]]:
    """FinMind (date, stock_id, close, volume) vs 參考源 (date, stock_id, close_ref, volume_ref)。

    收盤走 panel.reconcile_close（同一容差定義）；另報成交股數完全一致數與差 < 1 張（1,000 股）數
    （H1 用量；本地上櫃 stock_day_* 源自 TPEX 仟股單位、已 ×1000，只能到張的精度），以及
    FinMind 缺列數。
    fm_ids＝FinMind 有日線檔的全部股票（不是 fm 當日有列者——否則「有檔但當日缺列」會被漏算）；
    「FinMind 缺列」只算參考源當日有、且在 fm_ids 內的股。反方向（FinMind 有、參考源無）不報：
    MI_INDEX 只含上市，上櫃股必然「缺」，數字無意義。
    """
    from tw_screener.backtest.panel import reconcile_close

    recon = reconcile_close(fm.select("date", "stock_id", "close"), ref, tol_pct=tol_pct)
    vol = fm.select("date", "stock_id", "volume").join(
        ref.select("date", "stock_id", "volume_ref"), on=["date", "stock_id"], how="inner"
    )
    ref_in = ref.filter(pl.col("stock_id").is_in(sorted(fm_ids)))
    missing_fm = ref_in.join(fm.select("date", "stock_id"), on=["date", "stock_id"], how="anti")
    stats = {
        "n_close": recon.height,
        "n_close_ok": int(recon["within_tol"].sum()) if recon.height else 0,
        "n_vol": vol.height,
        "n_vol_exact": int((vol["volume"] == vol["volume_ref"]).sum()) if vol.height else 0,
        "n_vol_lot": (
            int(((vol["volume"] - vol["volume_ref"]).abs() < 1000).sum()) if vol.height else 0
        ),
        "n_vol_fm_gt": int((vol["volume"] - vol["volume_ref"] >= 1000).sum()) if vol.height else 0,
        "n_vol_ref_gt": (
            int((vol["volume_ref"] - vol["volume"] >= 1000).sum()) if vol.height else 0
        ),
        "missing_in_finmind": missing_fm.height,
    }
    return recon, stats


def _mi_index_reference(
    settings: Path, hcfg: oos.HoldoutConfig, dates: list[date]
) -> pl.DataFrame:
    """抽樣日 TWSE MI_INDEX（上市全市場，一日一請求）→ (date, stock_id, close_ref, volume_ref)。

    快取寫獨立目錄（hcfg.reconcile_cache_dir）：2014–2021 的 daily_* 不混入生產 data/cache/twse/
    （避免被 load_market_history／prune-cache 誤讀誤刪）；已存在的日子不重打網。
    """
    from tw_screener.data.twse import create_client

    client = create_client(settings)
    client.cache_dir = Path(hcfg.reconcile_cache_dir)
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for d in dates:
        df = client.fetch_daily_all_historical(d)
        if not df.is_empty():
            frames.append(
                df.select(
                    "date", pl.col("stock_id").cast(pl.Utf8),
                    pl.col("close").alias("close_ref"),
                    pl.col("trade_volume").cast(pl.Int64).alias("volume_ref"),
                )
            )
    if not frames:
        return pl.DataFrame(
            schema={"date": pl.Date, "stock_id": pl.Utf8, "close_ref": pl.Float64,
                    "volume_ref": pl.Int64}
        )
    return pl.concat(frames).drop_nulls("close_ref").filter(pl.col("close_ref") > 0)


def _local_reference(cache_twse: Path, pattern: str) -> pl.DataFrame:
    """既有 TWSE／TPEX 本地日快取（零新請求）→ (date, stock_id, close_ref, volume_ref)。"""
    from tw_screener.analysis.rotation import load_market_history

    px = load_market_history(cache_twse, n_days=5000, patterns=(pattern,))
    return px.select(
        "date", "stock_id", pl.col("close").alias("close_ref"),
        pl.col("volume").cast(pl.Int64).alias("volume_ref"),
    ).drop_nulls("close_ref").filter(pl.col("close_ref") > 0)


def local_reconcile(
    fm: pl.DataFrame,
    listed_ref: pl.DataFrame,
    stock_day_ref: pl.DataFrame,
    holdout_end: date,
    tol_pct: float,
    fm_ids: set[str],
    otc_daily_ref: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """次要對照：本地官方快取 vs FinMind，分來源 × 期間（保留樣本重疊段／之後）。

    上市＝daily_*（MI_INDEX／STOCK_DAY_ALL 全市場日檔）；上櫃＝stock_day_* 個股月檔中從未出現在
    daily_* 的股（daily_* 每日涵蓋全體上市股，不在其中者即上櫃）；另可給 otc_daily_*（TPEX OpenAPI
    日收盤行情，上櫃官方日總量，只有近期）當上櫃量的仲裁源。回 {f"{來源}_{期間}": 結果}。
    """
    listed_ids = set(listed_ref["stock_id"].unique().to_list())
    otc_ref = stock_day_ref.filter(~pl.col("stock_id").is_in(sorted(listed_ids)))
    sources = [("listed", listed_ref), ("otc", otc_ref)]
    if otc_daily_ref is not None and not otc_daily_ref.is_empty():
        sources.append(("otcdaily", otc_daily_ref))
    out: dict[str, Any] = {}
    for mkt, ref in sources:
        for period, cond in (
            ("holdout", pl.col("date") <= holdout_end),
            ("after", pl.col("date") > holdout_end),
        ):
            r = ref.filter(cond)
            if r.is_empty():
                continue
            dates = r["date"].unique().to_list()
            recon, stats = reconcile_frames(
                fm.filter(pl.col("date").is_in(dates)), r, tol_pct, fm_ids
            )
            span = (recon["date"].min(), recon["date"].max()) if recon.height else (None, None)
            out[f"{mkt}_{period}"] = {"span": span, "recon": recon, "stats": stats}
    return out


def adjustment_validation(
    cfg_all: dict[str, Any],
    meta: dict[str, Any],
    cfg: ip.IntraPickConfig,
    hcfg: oos.HoldoutConfig,
) -> dict[str, Any]:
    """除權息還原驗證（IO；零新請求）：三條腿（判準門檻見 settings，事前寫定）。

    (1) 全期自洽：FinMind「除息前收盤價」vs 價格資料前一交易列收盤——涵蓋整個保留樣本，
        不需官方對照期。
    (2) 事件層級：FinMind 事件 vs TWSE 除權息預告表（dividend_calendar_*，2026-05-19 起滾動累積）。
    (3) 窗層級：還原 r+h vs 「TWSE 現金股利線性加回」——用面板同一函式 build_price_panel、同一批
        FinMind 價格列，只換股利來源＝TWSE 預告表；不用 research/panel（其 2026-06 起列數稀疏，
        對多數股票不是同一批價格列，不能當參考）。
    (2)(3) 缺預告表 → 標未執行。(1b)(3′) 是看過 (3) 的結果後才加的診斷（(3) 的線性參考對除息後
    股價漲幅敏感）——只描述、不設判準。
    """
    from tw_screener.backtest.panel import build_price_panel
    from tw_screener.data.twse import load_recent_dividends

    out: dict[str, Any] = {
        "before": None, "ratio": None, "events": None, "windows": None, "windows_ratio": None,
    }
    out["before"] = oos.before_price_consistency(
        meta["divs"], meta["price_member"], hcfg.adj_before_price_tol_pct
    )
    out["ratio"] = oos.ratio_rounding_error(meta["divs"])
    official = load_recent_dividends(Path(cfg_all["paths"]["cache_dir"]) / "twse", date(2000, 1, 1))
    start = official["ex_date"].min() if not official.is_empty() else None
    end: date = meta["calendar"][1]
    if not isinstance(start, date):
        return out
    ids = meta["member_ids"] & meta["covered"]
    out["events"] = {
        "start": start, "end": end, "n_ids": len(ids),
        **oos.reconcile_events(official, meta["divs"], start, end, ids),
    }
    h = cfg.horizon
    ref = (
        build_price_panel(
            meta["price_member"],
            dividends=official.select("stock_id", "ex_date", "cash_dividend"),
            horizons=(h,),
            ma_windows=(60,),
        )
        .filter(pl.col("div_coverage"))
        .select("date", "stock_id", f"r{h}")
    )
    raw = oos.total_return_targets(meta["price_member"], oos.adjustment_events(pl.DataFrame()), [h])
    stock_ids = set(
        official.filter(pl.col("type").str.contains("權"))["stock_id"].to_list()
    ) | set(
        meta["divs"]
        .filter(pl.col("event_type").str.contains("權") & (pl.col("ex_date") >= start))[
            "stock_id"
        ]
        .to_list()
    )
    out["windows"] = {
        "h": h, "since": start,
        **oos.return_agreement(meta["targets"], raw, ref, h, stock_ids),
    }
    # (3′) 事後補充：比值版官方參考（前收／(前收−官方現金股利)，同 FinMind 的比值公式）→ 去除二階項
    ref_ratio = (
        oos.total_return_targets(
            meta["price_member"], oos.official_ratio_events(official, meta["price_member"]), [h]
        )
        .filter(pl.col("date") >= start)
        .select("date", "stock_id", f"r{h}")
    )
    out["windows_ratio"] = {
        "h": h, **oos.return_agreement(meta["targets"], raw, ref_ratio, h, stock_ids),
    }
    return out


# ─── 報告 ─────────────────────────────────────────────────────────────────────


def _pct(n: int, d: int) -> str:
    return f"{n / d:.2%}" if d else "—"


def _num(v: object) -> float:
    """polars 聚合回傳值 → float（型別上可能是任意 Python literal；非數值 → nan）。"""
    return float(v) if isinstance(v, (int, float)) else float("nan")


def _coverage_by_year(sw: pl.DataFrame) -> pl.DataFrame:
    """逐年：股週數、檔數、各欄非 null 比例（全成員股週，不限主宇宙、不分 target 值）。"""
    return (
        sw.with_columns(pl.col("date").dt.year().alias("year"))
        .group_by("year")
        .agg(
            pl.len().alias("rows"),
            pl.col("stock_id").n_unique().alias("stocks"),
            pl.col("date").n_unique().alias("weeks"),
            *[pl.col(c).is_not_null().mean().alias(c) for c in _COVERAGE_COLS if c in sw.columns],
        )
        .sort("year")
    )


def _target_null_reasons(sw: pl.DataFrame, meta: dict[str, Any], h: int) -> dict[str, int]:
    """r{h} null 的成因計數（互斥、依序歸因）：未到期／列不足 → 無除權息檔 → 窗內無效事件 → 窗內
    原始收盤不連續（fwd_disc，mark_universe 作廢）。"""
    px = meta["price_member"].sort("stock_id", "date").with_columns(
        pl.col("close").shift(-(1 + h)).over("stock_id").is_not_null().alias("_matured")
    )
    df = sw.join(px.select("date", "stock_id", "_matured"), on=["date", "stock_id"], how="left")
    null = df.filter(pl.col(f"r{h}").is_null())
    unmatured = null.filter(~pl.col("_matured").fill_null(False))
    rest = null.filter(pl.col("_matured").fill_null(False))
    no_div = rest.filter(~pl.col("div_covered"))
    rest = rest.filter(pl.col("div_covered"))
    bad = rest.filter(pl.col(f"bad_evt_{h}").fill_null(False))
    rest = rest.filter(~pl.col(f"bad_evt_{h}").fill_null(False))
    disc = rest.filter(pl.col(f"fwd_disc_{h}").fill_null(False))
    return {
        "rows": df.height,
        "null": null.height,
        "unmatured": unmatured.height,
        "no_div_file": no_div.height,
        "bad_event": bad.height,
        "fwd_disc": disc.height,
        "other": rest.height - disc.height,
    }


def render_report(
    sw: pl.DataFrame,
    meta: dict[str, Any],
    cfg: ip.IntraPickConfig,
    hcfg: oos.HoldoutConfig,
    recon: dict[str, Any],
    out_files: dict[str, str],
    adj_val: dict[str, Any] | None = None,
) -> list[str]:
    """資料品質報告 markdown（無任何因子×target 統計）。"""
    cal0, cal1, ncal = meta["calendar"]
    weeks: list[date] = meta["weeks"]
    price_member: pl.DataFrame = meta["price_member"]
    events: pl.DataFrame = meta["events"]
    divs: pl.DataFrame = meta["divs"]
    rev: pl.DataFrame = meta["rev"]
    regime: pl.DataFrame = meta["regime"]
    covered: set[str] = meta["covered"]
    price_ids: set[str] = meta["price_ids"]
    h_main = cfg.horizon

    in_hold = price_member.filter(pl.col("date").is_between(hcfg.start, hcfg.end))
    per_year = (
        in_hold.group_by(pl.col("date").dt.year().alias("year"))
        .agg(
            pl.col("stock_id").n_unique().alias("stocks"),
            (pl.len() / pl.col("date").n_unique()).alias("avg_names"),
        )
        .sort("year")
    )
    ev_hold = events.filter(pl.col("ex_date").is_between(hcfg.start, hcfg.end))
    n_bad_ev = int(ev_hold["adj_factor"].is_null().sum())
    disc_ratio = 1 / (1 - cfg.disc_pct / 100)
    n_big_ev = int((ev_hold["adj_factor"] >= disc_ratio).sum())
    fac = ev_hold["adj_factor"].drop_nulls()
    n_div_files = len(covered)
    n_div_with = divs["stock_id"].n_unique() if not divs.is_empty() else 0
    rev_first = (
        rev.group_by("stock_id").agg(pl.col("year").min().alias("y0"))
        if not rev.is_empty()
        else pl.DataFrame(schema={"stock_id": pl.Utf8, "y0": pl.Int64})
    )
    n_rev_2013 = int((rev_first["y0"] <= 2013).sum())
    member_ids: set[str] = meta["member_ids"]
    no_price = sorted(member_ids - price_ids)
    # 同 panel._non_etf_expr：恰 4 位數字且非 00 開頭才是普通股
    # （TDR 91xxxx 等被面板排除，同 M-Pick2）
    non_common = sorted(
        s for s in member_ids if not re.fullmatch(r"\d{4}", s) or s.startswith("00")
    )
    hold_ids = set(in_hold["stock_id"].unique().to_list())
    no_hold = sorted((member_ids & price_ids) - hold_ids - set(non_common))

    lines = [
        "# M-Pick3a 保留樣本週快照：資料品質報告（docs/33）",
        "",
        f"- 產出日：{date.today()}；保留樣本快照 {hcfg.start}～{hcfg.end}，"
        f"共 {len(weeks)} 週（{weeks[0] if weeks else '—'}～{weeks[-1] if weeks else '—'}）。",
        f"- 交易日曆（FinMind 成員日線、當日 ≥ {cfg.calendar_min_names} 檔有價）：{cal0}～{cal1}，"
        f"{ncal} 日。",
        "- 口徑：價格因子／趨勢分／gate／主次產業重用 M-Pick2 純函式（intra_pick、"
        "rotation_efficacy、shortlist）；只換價格來源（FinMind）與 target"
        "（除權息還原總報酬，entry＝次一交易日收盤）。",
        "- **本報告不含任何因子×target 統計，也不數主宇宙（in_main）規模**——兩者皆待 M-Pick3b "
        "預註冊 commit 之後（docs/33 §5）。以下覆蓋率只看欄位非 null 比例。",
        "",
        "## 1. 資料來源與覆蓋",
        "",
        f"- 今日次產業成員 {len(member_ids)} 檔（有主次產業 {meta['n_primary']} 檔）；"
        f"FinMind 日線有檔的成員 {len(member_ids & price_ids)} 檔；無日線 {len(no_price)} 檔"
        + (f"（{'、'.join(no_price[:15])}{'…' if len(no_price) > 15 else ''}）" if no_price else "")
        + "。",
        f"- 非普通股代號（面板慣例排除，同 M-Pick2）：{len(non_common)} 檔"
        + (f"（{'、'.join(non_common)}）" if non_common else "")
        + f"；保留樣本期間無價（上市櫃晚於 {hcfg.end.year} 年等）：{len(no_hold)} 檔。",
        f"- 排除無成交列（量 0 或價缺，比照 TWSE 慣例無成交日不成列）：{meta['n_no_trade']:,} 列。",
        "",
        "| 年 | 有價成員數 | 日均有價檔數 |",
        "|---|---|---|",
        *[
            f"| {r['year']} | {r['stocks']} | {r['avg_names']:.0f} |"
            for r in per_year.iter_rows(named=True)
        ],
        "",
        f"- 除權息結果：檔 {n_div_files} 個（有事件 {n_div_with}、確認無事件 "
        f"{n_div_files - n_div_with}）；保留樣本期事件 {ev_hold.height} 筆，無效（前後價缺）"
        f"{n_bad_ev} 筆；還原比值 min／中位／max ＝ "
        + (
            f"{_num(fac.min()):.4f}／{_num(fac.median()):.4f}／{_num(fac.max()):.4f}"
            if fac.len()
            else "—"
        )
        + f"；比值 ≥ {disc_ratio:.3f}（原始收盤跌幅會超過不連續門檻 {cfg.disc_pct:.0f}%，"
        f"該窗 target 被 fwd_disc 作廢）{n_big_ev} 筆。",
        f"- 月營收：{rev['stock_id'].n_unique() if not rev.is_empty() else 0} 檔；"
        f"最早年份 ≤ 2013 者 {n_rev_2013} 檔（F4 需 18 個月回看）。",
        "",
        "## 2. 欄位覆蓋（保留樣本全成員股週；非 null 比例）",
        "",
        "| 年 | 股週 | 檔數 | 週數 | " + " | ".join(_COVERAGE_COLS) + " |",
        "|---|---|---|---|" + "---|" * len(_COVERAGE_COLS),
    ]
    for r in _coverage_by_year(sw).iter_rows(named=True):
        lines.append(
            f"| {r['year']} | {r['rows']:,} | {r['stocks']} | {r['weeks']} | "
            + " | ".join(f"{r[c]:.1%}" for c in _COVERAGE_COLS)
            + " |"
        )
    lines += ["", "### target null 成因（互斥、依序歸因）", ""]
    lines += [
        "| 窗 | 股週 | null | 未到期/列不足 | 無除權息檔 | 窗內無效事件 "
        "| 窗內原始收盤不連續 | 其他 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in meta["horizons"]:
        t = _target_null_reasons(sw, meta, h)
        lines.append(
            f"| r+{h} | {t['rows']:,} | {t['null']:,} | {t['unmatured']:,} | "
            f"{t['no_div_file']:,} | {t['bad_event']:,} | {t['fwd_disc']:,} | {t['other']:,} |"
        )
    last_weeks = [w for w in weeks if w.year == hcfg.end.year and w.month == 12]
    lines += [
        "",
        f"- 注意：{hcfg.end.year}-12 的 {len(last_weeks)} 個快照，r+{h_main} 出場日落在 "
        f"{hcfg.end.year + 1} 年初（與 M-Pick2 面板 2022-01 起的報酬期間重疊 ≤ 8 週，"
        "但快照日不重疊）——是否剔除由 M-Pick3b 預註冊決定。",
        "",
        "## 3. regime 降級標籤（docs/33 §5 D4）",
        "",
        "- 定義：`build_regime_history`（V2 引擎逐日 as-of）吃**成員**日線、法人輸入為空 → "
        "trend（成員等權指數均線鏈）＋ breadth（成員站上 MA60 比例＋指數位階）按權重正規化；"
        "與 2022+ 面板（全市場＋法人三分項）**定義不同**，只供 C4 切片判讀。",
        f"- flow_score 非 null：{int(regime['flow_score'].is_not_null().sum())} 日"
        f"（應為 0＝降級路徑生效）。",
        "",
        "| 年 | " + " | ".join(sorted(regime["regime_label"].unique().to_list())) + " |",
        "|---|" + "---|" * regime["regime_label"].n_unique(),
    ]
    labels = sorted(regime["regime_label"].unique().to_list())
    wk = regime.filter(pl.col("date").is_in(weeks)).with_columns(
        pl.col("date").dt.year().alias("year")
    )
    for y in sorted(wk["year"].unique().to_list()):
        g = wk.filter(pl.col("year") == y)
        lines.append(
            f"| {y} | "
            + " | ".join(str(int((g["regime_label"] == lb).sum())) for lb in labels)
            + " |"
        )
    lines += ["", "## 4. 核價（FinMind vs TWSE 官方）", ""]
    lines += _recon_lines(recon, hcfg)
    lines += _adjustment_lines(adj_val, hcfg)
    lines += [
        "",
        "## 5. 已知偏誤與限制（照列，M-Pick3b 預註冊須引用）",
        "",
        "- membership＝今日 concepts.yaml 回套 2015–2021（非 point-in-time，比 2022+ 更重，"
        "如 AI 伺服器類主題當年不存在）。",
        "- 倖存者偏誤：只抓今日成員 → 2015–2021 間下市者缺席（方向對 H1 不明）。",
        "- 價格因子（F2 52 週高點、偏好帶距離、成交額）沿用 M-Pick2 定義：**原始收盤**、未還原；"
        "只有 target 是總報酬。窗內原始收盤單日變動 > 不連續門檻者因子／target 作廢（同 M-Pick2），"
        "大比例配股（見 §1 計數）會因此少量排除。",
        "- 成交額＝收盤×成交股數（price_features 同式），非 FinMind 成交金額欄。",
        "- **上櫃成交股數口徑**：FinMind＝TPEX 官方日收盤行情總量（§4 otc_daily_* 對照）；M-Pick2 "
        "面板的上櫃量取自 stock_day_*（TPEX 個股日成交資訊，仟股單位、少數股日偏低 ≥ 1 張，見 §4 "
        "stock_day_* 列的「FinMind 較大」計數）——H1 用單日成交額，兩樣本上櫃量口徑在這些股日不同。",
        "- regime 標籤定義降級（§3）。",
        "- 上櫃價格無 2014–2021-05 官方核價（MI_INDEX 僅上市、TPEX 歷史 bulk 端點未驗證可用）；"
        "只在本地 stock_day_* 涵蓋的 2021-06 起對照（§4 次要對照）。",
        "",
        "## 6. 產物",
        "",
        *[f"- {k}：`{v}`" for k, v in out_files.items()],
        "",
    ]
    return lines


def _recon_lines(recon: dict[str, Any], hcfg: oos.HoldoutConfig) -> list[str]:
    tol, pass_rate = hcfg.reconcile_tol_pct, hcfg.reconcile_pass_rate
    lines: list[str] = []
    main = recon.get("mi_index")
    if main is None:
        lines.append("> MI_INDEX 抽樣核價未執行（--no-reconcile）——如實標註，核價判定缺席。")
    else:
        s = main["stats"]
        rate = s["n_close_ok"] / s["n_close"] if s["n_close"] else 0.0
        verdict = "PASS" if s["n_close"] and rate >= pass_rate else "FAIL"
        lines += [
            f"- **主核價（上市，TWSE MI_INDEX {len(main['dates'])} 個抽樣日："
            f"{', '.join(str(d) for d in main['dates'])}）：{verdict}**——收盤差 <{tol}%："
            f"{s['n_close_ok']:,}/{s['n_close']:,}（{rate:.2%}，通過線 {pass_rate:.1%}，"
            "同 docs/22）。",
            f"- 成交股數完全一致：{s['n_vol_exact']:,}/{s['n_vol']:,}"
            f"（{_pct(s['n_vol_exact'], s['n_vol'])}）；差 < 1 張："
            f"{_pct(s['n_vol_lot'], s['n_vol'])}；MI_INDEX 有列、FinMind 有檔卻缺列 "
            f"{s['missing_in_finmind']:,}。",
        ]
        # FinMind 收盤 null（官方有價）→ within_tol null：計入失敗，也要出現在逐筆表
        worst = main["recon"].filter(~pl.col("within_tol").fill_null(False)).head(10)
        if worst.height:
            lines += [
                "",
                "| date | stock_id | FinMind | MI_INDEX | diff% |",
                "|---|---|---|---|---|",
                *[
                    f"| {r['date']} | {r['stock_id']} | {r['close_panel']} | {r['close_ref']} | "
                    f"{r['diff_pct']:.3f} |"
                    for r in worst.iter_rows(named=True)
                ],
            ]
    titles = {
        "listed_holdout": "上市 daily_*・保留樣本重疊段",
        "otc_holdout": "上櫃 stock_day_*・保留樣本重疊段",
        "listed_after": "上市 daily_*・保留樣本之後",
        "otc_after": "上櫃 stock_day_*・保留樣本之後",
        "otcdaily_holdout": "上櫃 otc_daily_*（TPEX OpenAPI 日收盤行情）・保留樣本重疊段",
        "otcdaily_after": "上櫃 otc_daily_*（TPEX OpenAPI 日收盤行情）・保留樣本之後",
    }
    for key, title in titles.items():
        o = recon.get(key)
        if o is None:
            continue
        s = o["stats"]
        d0, d1 = o["span"]
        lines.append(
            f"- 次要對照（{title} {d0}～{d1}，本地官方快取、零新請求，僅揭露）："
            f"收盤差 <{tol}% {s['n_close_ok']:,}/{s['n_close']:,}"
            f"（{_pct(s['n_close_ok'], s['n_close'])}）；股數完全一致 "
            f"{_pct(s['n_vol_exact'], s['n_vol'])}、差 < 1 張 {_pct(s['n_vol_lot'], s['n_vol'])}"
            f"（差 ≥ 1 張：FinMind 較大 {s['n_vol_fm_gt']:,}、參考源較大 {s['n_vol_ref_gt']:,}）；"
            f"FinMind 有檔卻缺列 {s['missing_in_finmind']:,}。"
        )
    return lines


def _verdict(ok: bool | None) -> str:
    return "未執行" if ok is None else ("PASS" if ok else "FAIL")


def _adjustment_lines(av: dict[str, Any] | None, hcfg: oos.HoldoutConfig) -> list[str]:
    """除權息還原驗證段（三條腿；判準門檻讀 settings，事前寫定）。"""
    lines = ["", "### 除權息還原驗證（三條腿；判準事前寫進 settings）", ""]
    if not av or av.get("before") is None:
        return lines + ["> 未執行。"]
    b = av["before"]
    rate_b = b["n_ok"] / b["n_checked"] if b["n_checked"] else None
    lines += [
        "**(1) 全期自洽（涵蓋保留樣本；FinMind 除息前收盤價 vs 價格資料前一交易列收盤）**",
        "",
        f"- 事件 {b['n_events']:,} 筆（無效＝前後價缺 {b['n_invalid_before']:,}、事件前無價格列 "
        f"{b['n_no_prev_row']:,}）；可檢查 {b['n_checked']:,} 筆，其中價差 ≤ "
        f"{hcfg.adj_before_price_tol_pct}% 者 {b['n_ok']:,}（{_pct(b['n_ok'], b['n_checked'])}）、"
        f"分毫不差（≤ 0.005 元）{b['n_exact']:,}（{_pct(b['n_exact'], b['n_checked'])}）；"
        f"停牌致事件日順延到下一交易列 {b['n_realigned']:,} 筆。"
        f"**{_verdict(None if rate_b is None else rate_b >= hcfg.adj_before_price_min_rate)}**"
        f"（門檻 ≥ {hcfg.adj_before_price_min_rate:.0%}）",
    ]
    if b["worst"].height:
        lines += [
            "", "差距最大的事件（前 8）：", "",
            "| stock_id | ex_date | before_price | 前一列收盤 | 差% |", "|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['stock_id']} | {r['ex_date']} | {r['before_price']} | {r['prev_close']} | "
            f"{r['diff_pct']:.2f} |"
            for r in b["worst"].iter_rows(named=True)
        ]
    r = av.get("ratio")
    if r and r["n_checked"]:
        lines += [
            "",
            "**(1b) 事後補充（看過 (3) 後才加、只描述不設判準）：還原比值的誤差量級**",
            "",
            f"- 純現金事件 {r['n_checked']:,} 筆（全期，含保留樣本）：FinMind 比值 before/after "
            f"相對精確值 before/(before−D) 的誤差＝中位 {_fmt_pp(r['err_median'])}、"
            f"|誤差| p99 {_fmt_pp(r['err_abs_p99'])}、最大 {_fmt_pp(r['err_abs_max'])}"
            "——即含該事件之窗 target 的還原誤差量級（after_price 為四捨五入到分的參考價）。",
        ]
    lines += ["", "**(2)(3) 獨立官方對照（TWSE 除權息預告表；只有 2026-05-19 起有）**", ""]
    if av.get("events") is None:
        return lines + ["> 無 TWSE 除權息預告表快取——(2)(3) 未執行，如實標註。"]
    e = av["events"]
    rate = e["n_matched"] / e["n_official"] if e["n_official"] else None
    cash_rate = e["n_cash_ok"] / e["n_cash_pairs"] if e["n_cash_pairs"] else None
    lines += [
        f"- (2) 事件層級（成員且有除權息檔 {e['n_ids']} 檔、{e['start']}～{e['end']}）：官方 "
        f"{e['n_official']} 筆、FinMind {e['n_finmind']} 筆、同股同日配對 {e['n_matched']} 筆"
        f"（占官方 {_pct(e['n_matched'], e['n_official'])}）。"
        f"**{_verdict(None if rate is None else rate >= hcfg.adj_event_match_min_rate)}**"
        f"（門檻 ≥ {hcfg.adj_event_match_min_rate:.0%}）",
        f"- 僅官方有 {e['official_only'].height} 筆（其中 FinMind 有同股 ±3 天內事件＝日期不一致 "
        f"{e['n_official_only_near']} 筆）；僅 FinMind 有 {e['finmind_only'].height} 筆"
        "（官方預告表是滾動快照聯集、涵蓋不保證完整，這項無法裁決誰多誰漏，只計數）。",
        f"- 純現金配對事件金額（差 ≤ 0.01 元）：{e['n_cash_ok']}/{e['n_cash_pairs']}"
        f"（{_pct(e['n_cash_ok'], e['n_cash_pairs'])}）。"
        f"**{_verdict(None if cash_rate is None else cash_rate >= hcfg.adj_cash_amount_min_rate)}**"
        f"（門檻 ≥ {hcfg.adj_cash_amount_min_rate:.0%}）",
    ]
    for title, df, cols in (
        ("僅官方有（前 8 筆）", e["official_only"],
         ["stock_id", "ex_date", "type", "cash_dividend"]),
        ("現金金額不一致（前 8 筆）", e["cash_bad"],
         ["stock_id", "ex_date", "cash_dividend", "dividend_value"]),
    ):
        if df.height:
            lines += ["", f"{title}：", "", "| " + " | ".join(cols) + " |",
                      "|" + "---|" * len(cols)]
            lines += [
                "| " + " | ".join(str(r[c]) for c in cols) + " |"
                for r in df.head(8).iter_rows(named=True)
            ]
    w = av.get("windows")
    if w is None:
        return lines
    p99 = w["diff_abs_p99"]
    ok_diff = None if p99 is None else p99 <= hcfg.adj_window_diff_p99_max_pp
    lines += [
        "",
        f"- (3) 窗層級（r+{w['h']}；參考＝TWSE 預告表現金股利線性加回，同一批 FinMind 價格列，"
        f"{w['since']} 起共同窗 {w['n_windows']:,} 個）：兩方都無事件 {w['n_none']:,}；"
        f"兩方都看見 {w['n_both']:,}（其中含配股股票 {w['n_both_stock_div']:,}，參考不還原配股、"
        f"必然不同，另列）；僅 FinMind 看見 {w['n_fm_only']:,}（多半是官方預告表漏事件，見上）；"
        f"僅官方看見 {w['n_ref_only']:,}。",
        f"- 純現金、兩方都看見的窗 {w['n_cash']:,} 個：差值（還原 − 線性加回）中位 "
        f"{_fmt_pp(w['diff_median'])}、|差| p99 {_fmt_pp(w['diff_abs_p99'])}、最大 "
        f"{_fmt_pp(w['diff_abs_max'])}。**{_verdict(ok_diff)}**"
        f"（門檻 p99 ≤ {hcfg.adj_window_diff_p99_max_pp}pp，事前寫定、未因結果調整）。"
        "兩種還原方式的差＝二階項 (D/entry)·(exit/after−1)，隨除息後股價漲幅放大——"
        "下表最大的窗若集中在少數大漲股即屬此因，見 (3′)。",
    ]
    worst = w["worst"]
    if worst.height:
        lines += ["", "|差| 最大的窗（前 8）：", "",
                  "| date | stock_id | 還原 r | 線性加回 r | 差 |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['date']} | {r['stock_id']} | {r['adj']:.3f} | {r['ref']:.3f} | "
            f"{r['diff']:+.3f} |"
            for r in worst.iter_rows(named=True)
        ]
    wr = av.get("windows_ratio")
    if wr is not None:
        lines += [
            "",
            "**(3′) 事後補充（只描述不設判準）：改用比值版官方參考（前收／(前收−官方現金股利)，"
            "與 FinMind 同一比值公式，去除二階項）**",
            "",
            f"- 純現金、兩方都看見的窗 {wr['n_cash']:,} 個（僅 FinMind 看見 {wr['n_fm_only']:,}、"
            f"僅官方看見 {wr['n_ref_only']:,}）：差值（FinMind 還原 − 官方比值版）中位 "
            f"{_fmt_pp(wr['diff_median'])}、|差| p99 {_fmt_pp(wr['diff_abs_p99'])}、最大 "
            f"{_fmt_pp(wr['diff_abs_max'])}。",
        ]
    return lines


def _fmt_pp(v: object) -> str:
    return f"{float(v):+.3f}pp" if isinstance(v, (int, float)) else "—"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_intra_pick_holdout(settings: Path, out_dir: Path | None, reconcile: bool = True) -> None:
    """M-Pick3a：保留樣本週快照 parquet＋資料品質報告（含核價）。不做任何因子評估。"""
    import yaml

    with open(settings, encoding="utf-8") as f:
        cfg_all = yaml.safe_load(f)
    cfg = ip.IntraPickConfig.from_settings(cfg_all)
    hcfg = oos.HoldoutConfig.from_settings(cfg_all)
    out = out_dir or Path(hcfg.output_dir)

    sw, meta = build_holdout_stock_weeks(cfg_all, settings, cfg, hcfg)

    recon: dict[str, Any] = {}
    fm = meta["price_raw"].select("date", "stock_id", "close", "volume")
    if reconcile:
        cal = sorted(meta["price_member"]["date"].unique().to_list())
        dates = sample_reconcile_dates(
            cal, hcfg.reconcile_years, hcfg.reconcile_dates_per_year, hcfg.reconcile_seed
        )
        console.print(f"[bold]核價：TWSE MI_INDEX 抽樣 {len(dates)} 日（一日一請求）...[/bold]")
        ref = _mi_index_reference(settings, hcfg, dates)
        r, s = reconcile_frames(
            fm.filter(pl.col("date").is_in(dates)), ref, hcfg.reconcile_tol_pct, meta["price_ids"]
        )
        recon["mi_index"] = {"dates": dates, "recon": r, "stats": s}
    cache_twse = Path(cfg_all["paths"]["cache_dir"]) / "twse"
    console.print("[bold]次要對照：本地官方日快取（daily_*／stock_day_*，零新請求）...[/bold]")
    recon.update(
        local_reconcile(
            fm,
            _local_reference(cache_twse, "daily_*.parquet"),
            _local_reference(cache_twse, "stock_day_*.parquet"),
            hcfg.end,
            hcfg.reconcile_tol_pct,
            meta["price_ids"],
            otc_daily_ref=_local_reference(cache_twse, "otc_daily_*.parquet"),
        )
    )

    console.print("[bold]除權息還原驗證（前收自洽＋TWSE 預告表，零新請求）...[/bold]")
    adj_val = adjustment_validation(cfg_all, meta, cfg, hcfg)

    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    pq = out / f"holdout_stockweeks_{tag}.parquet"
    sw.write_parquet(pq)
    reg_pq = out / f"holdout_regime_labels_{tag}.parquet"
    meta["regime"].write_parquet(reg_pq)
    out_files = {
        "週快照寬表": str(pq),
        "週快照 SHA-256（M-Pick3b 預註冊釘版用）": _sha256(pq),
        "regime 降級標籤（逐日）": str(reg_pq),
    }
    if "mi_index" in recon:
        rc = out / f"holdout_reconcile_{tag}.csv"
        recon["mi_index"]["recon"].write_csv(rc)
        out_files["MI_INDEX 核價逐筆"] = str(rc)
    report = out / f"holdout_data_report_{tag}.md"
    report.write_text(
        "\n".join(render_report(sw, meta, cfg, hcfg, recon, out_files, adj_val)),
        encoding="utf-8",
    )
    console.print(f"[green]週快照：{pq}（{sw.height:,} 列、{sw['date'].n_unique()} 週）[/green]")
    console.print(f"[green]報告：{report}[/green]")
