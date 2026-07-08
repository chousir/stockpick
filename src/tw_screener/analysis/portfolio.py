"""analysis/portfolio.py — 組合層風控（規劃書 03 V3）。

設計意圖（規劃書 03 §V3）：
  把 docs/14 D4 目前只在 prompt 層的「人工因子簇檢核」（docs/11:202）落成**可計算**的模組，
  對一組持股（holdings_enriched，可選併入候選）揭露三類隱性集中：

    1. 標籤集中度：同一次產業/主題標籤押了幾檔（多標籤 aware，一檔可計入多標籤）。
    2. 報酬相關簇：近 N 日「日報酬」兩兩 Pearson 相關 ≥ 門檻者連通成簇（隱性共動）。
    3. 因子簇曝險：預定義因子簇（settings，如「利率敏感＝銀行＋建材營造＋產險＋壽險」）
       命中幾檔、佔比多少、是否超上限。

關鍵約定：
  - 純函式計算，IO（讀價量/讀 holdings CSV）由呼叫端（cli.py）負責，沿用既有 loader。
  - 所有視窗、門檻、簇定義由 settings.portfolio 傳入，不寫死。
  - 定位＝**風險揭露**，非硬約束（規劃書風險段＋ CLAUDE.md Part 3「由人決策」）。
  - holdings_enriched **無部位大小欄** → 所有「合計%」皆為**等權檔數佔比近似**，誠實標註。
  - 資料不足（持股 < 2、無價格、重疊天數不足）時誠實回空/標註，不假裝有結論。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import cast

import polars as pl

# holdings/candidates_enriched 帶 industry（TWSE 大分類）＋ theme（次產業/概念，多標籤以「、」串）
_LABEL_COLS = ("industry", "theme")
_LABEL_SEP = "、"


@dataclass(frozen=True)
class PortfolioCheckResult:
    """組合體檢結果（標籤集中度＋報酬相關簇＋因子簇曝險＋透明依據）。"""

    as_of: date | None
    n_holdings: int
    label_concentration: list[dict[str, object]] = field(default_factory=list)
    corr_clusters: list[dict[str, object]] = field(default_factory=list)
    factor_clusters: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _labels_of(industry: object, theme: object) -> set[str]:
    """單一持股的標籤集合＝industry ＋ theme 多標籤拆解（去空白、去空字串）。"""
    labels: set[str] = set()
    for raw in (industry, theme):
        if raw is None:
            continue
        for part in str(raw).split(_LABEL_SEP):
            part = part.strip()
            if part and part not in {"-", "—", '""'}:
                labels.add(part)
    return labels


def _holdings_labels(members: pl.DataFrame) -> list[tuple[str, set[str]]]:
    """members → [(stock_id, 標籤集合), ...]，缺欄位以空字串容錯。"""
    if members.is_empty() or "stock_id" not in members.columns:
        return []
    cols = ["stock_id"] + [c for c in _LABEL_COLS if c in members.columns]
    rows = members.select(cols).to_dicts()
    out: list[tuple[str, set[str]]] = []
    for r in rows:
        sid = str(r.get("stock_id"))
        out.append((sid, _labels_of(r.get("industry"), r.get("theme"))))
    return out


def compute_label_concentration(
    members: pl.DataFrame, min_count: int, min_share: float
) -> list[dict[str, object]]:
    """標籤集中度：逐標籤統計持有檔數／佔比，標記達 min_count 或 min_share 者為集中。

    多標籤 aware（一檔可計入多個標籤）。回傳依 count 降冪的全標籤列表（flagged 標記集中）。
    """
    holdings = _holdings_labels(members)
    n = len(holdings)
    if n == 0:
        return []
    counts: dict[str, list[str]] = {}
    for sid, labels in holdings:
        for lab in labels:
            counts.setdefault(lab, []).append(sid)
    out: list[dict[str, object]] = []
    for lab, sids in counts.items():
        c = len(sids)
        share = c / n
        out.append(
            {
                "label": lab,
                "count": c,
                "share": round(share, 3),
                "stock_ids": sorted(sids),
                "flagged": c >= min_count or share >= min_share,
            }
        )
    out.sort(key=lambda d: (-cast(int, d["count"]), cast(str, d["label"])))
    return out


def _daily_returns_wide(
    price_history: pl.DataFrame, stock_ids: list[str], window: int, clip_pct: float
) -> pl.DataFrame:
    """價格史 → 近 window 交易日「日報酬」寬表（index=date、各欄=stock_id 的日報酬）。

    只取 stock_ids；夾限日報酬（台股漲跌停）防未還原事件毒化相關。資料不足回空表。
    """
    if price_history.is_empty() or not {"date", "stock_id", "close"}.issubset(
        price_history.columns
    ):
        return pl.DataFrame()
    df = (
        price_history.filter(pl.col("stock_id").is_in(stock_ids))
        .select(["date", "stock_id", "close"])
        .drop_nulls(["close"])
        .sort(["stock_id", "date"])
    )
    if df.is_empty():
        return pl.DataFrame()
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("stock_id") - 1.0).alias("ret")
    ).drop_nulls(["ret"])
    if clip_pct > 0:
        bound = clip_pct / 100
        df = df.with_columns(pl.col("ret").clip(-bound, bound))
    # 取每股最近 window 個交易日的日報酬，再轉寬表（date × stock_id）
    df = df.sort(["stock_id", "date"]).group_by("stock_id", maintain_order=True).tail(window)
    return df.pivot(values="ret", index="date", on="stock_id").sort("date")


def compute_correlation_clusters(
    price_history: pl.DataFrame,
    stock_ids: list[str],
    window: int,
    min_overlap: int,
    threshold: float,
    clip_daily_return_pct: float,
) -> tuple[list[dict[str, object]], list[str]]:
    """近 window 日 日報酬兩兩 Pearson 相關 ≥ threshold → 連通成簇（union-find）。

    回傳 (clusters, notes)。clusters＝[{stock_ids, size, pairs:[{a,b,rho}]}]；單檔不成簇者不列。
    重疊有效交易日 < min_overlap 的對跳過（標進 notes 計數）。純函式。
    """
    notes: list[str] = []
    ids = sorted(set(stock_ids))
    if len(ids) < 2:
        return [], notes
    wide = _daily_returns_wide(price_history, ids, window, clip_daily_return_pct)
    if wide.is_empty():
        notes.append("無足夠價格史計算相關")
        return [], notes
    present = [c for c in ids if c in wide.columns]
    missing = [c for c in ids if c not in wide.columns]
    if missing:
        notes.append(f"{len(missing)} 檔無價格史、未納入相關：{', '.join(missing)}")

    # union-find
    parent: dict[str, str] = {c: c for c in present}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        parent[_find(a)] = _find(b)

    pairs: list[dict[str, object]] = []
    skipped = 0
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            a, b = present[i], present[j]
            pair = wide.select([a, b]).drop_nulls()
            if pair.height < min_overlap:
                skipped += 1
                continue
            rho = pair.select(pl.corr(a, b)).item()
            if rho is None:
                continue
            if abs(rho) >= threshold:
                pairs.append({"a": a, "b": b, "rho": round(float(rho), 3)})
                _union(a, b)
    if skipped:
        notes.append(f"{skipped} 對因重疊交易日 < {min_overlap} 跳過")

    # 連通分量 → 簇（size ≥ 2）
    groups: dict[str, list[str]] = {}
    for c in present:
        groups.setdefault(_find(c), []).append(c)
    clusters: list[dict[str, object]] = []
    for root, members_ in groups.items():
        if len(members_) < 2:
            continue
        member_set = set(members_)
        cluster_pairs = [
            p for p in pairs if p["a"] in member_set and p["b"] in member_set
        ]
        clusters.append(
            {
                "stock_ids": sorted(members_),
                "size": len(members_),
                "pairs": sorted(cluster_pairs, key=lambda p: -abs(cast(float, p["rho"]))),
            }
        )
    clusters.sort(key=lambda d: -cast(int, d["size"]))
    return clusters, notes


def compute_factor_cluster_exposure(
    members: pl.DataFrame, clusters_cfg: list[dict]
) -> list[dict[str, object]]:
    """預定義因子簇曝險：每簇命中幾檔（標籤 industry/theme 任一命中 labels）／佔比／是否超上限。

    超上限＝命中檔數 > max_count 或 佔比 > max_share（檔數佔比＝等權近似，無部位大小）。
    回傳每個定義簇一列（命中 0 檔也列，flagged=False），供報表/CLI 完整揭露。
    """
    holdings = _holdings_labels(members)
    n = len(holdings)
    if n == 0 or not clusters_cfg:
        return []
    out: list[dict[str, object]] = []
    for spec in clusters_cfg:
        name = str(spec.get("name", "?"))
        labels = {str(x) for x in spec.get("labels", []) or []}
        max_count = spec.get("max_count")
        max_share = spec.get("max_share")
        hit = [sid for sid, lbls in holdings if lbls & labels]
        c = len(hit)
        share = c / n
        over_count = max_count is not None and c > int(max_count)
        over_share = max_share is not None and share > float(max_share)
        out.append(
            {
                "name": name,
                "labels": sorted(labels),
                "count": c,
                "share": round(share, 3),
                "stock_ids": sorted(hit),
                "max_count": max_count,
                "max_share": max_share,
                "flagged": bool(over_count or over_share),
            }
        )
    return out


def _merge_etf_exposure(members: pl.DataFrame, exposure_cfg: dict) -> pl.DataFrame:
    """把 ETF 手標曝險 labels（settings.portfolio.etf_exposure）併入該檔 theme 欄。

    ETF 無 industry/theme 標籤 → 集中度/因子簇看不到其曝險（docs/21 §1.2 稀釋失真）。
    手標 labels 以「、」併入 theme，下游 _labels_of 原樣拆解；非 ETF 或無設定者不動。
    """
    if members.is_empty() or not exposure_cfg or "stock_id" not in members.columns:
        return members
    if "theme" not in members.columns:
        members = members.with_columns(pl.lit(None, dtype=pl.Utf8).alias("theme"))
    label_map = {
        str(sid): _LABEL_SEP.join(str(x) for x in (spec.get("labels") or []))
        for sid, spec in exposure_cfg.items()
        if isinstance(spec, dict) and spec.get("labels")
    }
    if not label_map:
        return members
    extra = pl.col("stock_id").cast(pl.Utf8).replace_strict(label_map, default=None)
    theme = pl.col("theme").cast(pl.Utf8)
    return members.with_columns(
        pl.when(extra.is_null())
        .then(theme)
        .when(theme.is_null() | (theme.str.strip_chars() == ""))
        .then(extra)
        .otherwise(theme + pl.lit(_LABEL_SEP) + extra)
        .alias("theme")
    )


def compute_portfolio_check(
    members: pl.DataFrame, price_history: pl.DataFrame, cfg: dict
) -> PortfolioCheckResult:
    """合成組合體檢（標籤集中度＋報酬相關簇＋因子簇曝險）。

    cfg＝settings.portfolio（corr / label_concentration / factor_clusters / etf_exposure）。
    members＝持股（holdings_enriched，可含併入候選），須有 stock_id；industry/theme 缺則容錯。
    純函式，IO 由呼叫端載入。
    """
    notes: list[str] = []
    exposure_cfg = cfg.get("etf_exposure") or {}
    if exposure_cfg and not members.is_empty() and "stock_id" in members.columns:
        hit = sorted(
            {str(s) for s in members["stock_id"].to_list()} & set(exposure_cfg)
        )
        if hit:
            members = _merge_etf_exposure(members, exposure_cfg)
            notes.append(f"ETF 曝險為手標估計（{'、'.join(hit)}；主動 ETF 依公開月報）")
    stock_ids = (
        [str(x) for x in members["stock_id"].to_list()]
        if not members.is_empty() and "stock_id" in members.columns
        else []
    )
    n = len(stock_ids)
    as_of: date | None = None
    if not price_history.is_empty() and "date" in price_history.columns:
        as_of = price_history.sort("date")["date"].tail(1).item()

    if n == 0:
        notes.append("無持股資料")
        return PortfolioCheckResult(as_of=as_of, n_holdings=0, notes=notes)

    lc_cfg = cfg.get("label_concentration", {})
    label_conc = compute_label_concentration(
        members,
        min_count=int(lc_cfg.get("min_count", 3)),
        min_share=float(lc_cfg.get("min_share", 0.4)),
    )

    corr_cfg = cfg.get("corr", {})
    corr_clusters, corr_notes = compute_correlation_clusters(
        price_history,
        stock_ids,
        window=int(corr_cfg.get("window", 60)),
        min_overlap=int(corr_cfg.get("min_overlap", 40)),
        threshold=float(corr_cfg.get("threshold", 0.7)),
        clip_daily_return_pct=float(corr_cfg.get("clip_daily_return_pct", 10.0)),
    )
    notes.extend(corr_notes)

    factor_clusters = compute_factor_cluster_exposure(
        members, list(cfg.get("factor_clusters", []) or [])
    )

    return PortfolioCheckResult(
        as_of=as_of,
        n_holdings=n,
        label_concentration=label_conc,
        corr_clusters=corr_clusters,
        factor_clusters=factor_clusters,
        notes=notes,
    )


def describe_portfolio_check(r: PortfolioCheckResult) -> dict[str, object]:
    """把 PortfolioCheckResult 轉成報表/CLI 共用顯示 dict（摘要行＋三段＋警示計數）。"""
    flagged_labels = [d for d in r.label_concentration if d.get("flagged")]
    flagged_factors = [d for d in r.factor_clusters if d.get("flagged")]
    n_alerts = len(flagged_labels) + len(r.corr_clusters) + len(flagged_factors)
    line = (
        f"組合體檢：{r.n_holdings} 檔｜集中標籤 {len(flagged_labels)}・"
        f"高相關簇 {len(r.corr_clusters)}・因子簇超限 {len(flagged_factors)}"
    )
    return {
        "n_holdings": r.n_holdings,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "line": line,
        "n_alerts": n_alerts,
        "label_concentration": r.label_concentration,
        "flagged_labels": flagged_labels,
        "corr_clusters": r.corr_clusters,
        "factor_clusters": r.factor_clusters,
        "flagged_factors": flagged_factors,
        "notes": r.notes,
    }
