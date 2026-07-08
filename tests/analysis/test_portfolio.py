"""組合層風控計算層測試（規劃書 03 V3）：標籤集中度/報酬相關簇/因子簇曝險，全離線合成。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from tw_screener.analysis.portfolio import (
    _labels_of,
    compute_correlation_clusters,
    compute_factor_cluster_exposure,
    compute_label_concentration,
    compute_portfolio_check,
    describe_portfolio_check,
)

# 合成持股：3 銀行 + 2 半導體（其中一檔多標籤）
MEMBERS = pl.DataFrame(
    {
        "stock_id": ["2801", "2809", "2812", "2330", "2454"],
        "industry": ["金融保險", "金融保險", "金融保險", "半導體業", "半導體業"],
        "theme": ["銀行", "銀行", "銀行", "晶圓代工、AI人工智慧", "AI人工智慧"],
    }
)

CLUSTERS_CFG = [
    {
        "name": "利率敏感",
        "labels": ["銀行", "建材營造", "產險", "壽險"],
        "max_count": 2,
        "max_share": 0.5,
    }
]

PCFG = {
    "label_concentration": {"min_count": 3, "min_share": 0.4},
    "corr": {"window": 60, "min_overlap": 3, "threshold": 0.7, "clip_daily_return_pct": 10.0},
    "factor_clusters": CLUSTERS_CFG,
}


def _price_history() -> pl.DataFrame:
    """A、B 收盤完全相同（ρ=1）；C 收盤恆定（日報酬恆 0 → corr 為 null、不入簇）。"""
    closes = [100.0, 101.0, 103.0, 102.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0]
    rows: list[dict] = []
    for i, c in enumerate(closes):
        d = date(2026, 1, 1) + timedelta(days=i)
        rows.append({"date": d, "stock_id": "A", "close": c})
        rows.append({"date": d, "stock_id": "B", "close": c})  # 與 A 同步 → ρ=1
        rows.append({"date": d, "stock_id": "C", "close": 50.0})  # 恆定 → 報酬 0
    return pl.DataFrame(rows)


# ── 標籤拆解 ──────────────────────────────────────────────────────────────────
def test_labels_of_splits_multi_label_and_drops_empty() -> None:
    assert _labels_of("半導體業", "晶圓代工、AI人工智慧") == {
        "半導體業",
        "晶圓代工",
        "AI人工智慧",
    }
    assert _labels_of("金融保險", "") == {"金融保險"}
    assert _labels_of(None, '""') == set()


# ── 標籤集中度 ────────────────────────────────────────────────────────────────
def test_label_concentration_flags_and_multilabel() -> None:
    # min_share 0.5：半導體業 2/5=0.4 < 0.5 且 2 < min_count → 不 flag，隔離 count 路徑
    out = compute_label_concentration(MEMBERS, min_count=3, min_share=0.5)
    by_label = {d["label"]: d for d in out}
    # 銀行 3 檔（≥ min_count）→ flagged；半導體業 2 檔不 flag
    assert by_label["銀行"]["count"] == 3
    assert by_label["銀行"]["flagged"] is True
    assert by_label["金融保險"]["flagged"] is True
    assert by_label["半導體業"]["count"] == 2
    assert by_label["半導體業"]["flagged"] is False
    # 多標籤：2330 同時計入 晶圓代工 / AI人工智慧
    assert by_label["AI人工智慧"]["count"] == 2  # 2330 + 2454
    assert by_label["晶圓代工"]["count"] == 1


def test_label_concentration_min_share_path() -> None:
    # min_count 很大、靠 min_share 命中：半導體業 2/5=0.4 ≥ 0.4 → flagged
    out = compute_label_concentration(MEMBERS, min_count=99, min_share=0.4)
    semi = next(d for d in out if d["label"] == "半導體業")
    assert semi["flagged"] is True


def test_label_concentration_empty() -> None:
    assert compute_label_concentration(pl.DataFrame(), 3, 0.4) == []


# ── 因子簇曝險 ────────────────────────────────────────────────────────────────
def test_factor_cluster_exposure_over_limit() -> None:
    out = compute_factor_cluster_exposure(MEMBERS, CLUSTERS_CFG)
    assert len(out) == 1
    d = out[0]
    assert d["name"] == "利率敏感"
    assert d["count"] == 3  # 3 銀行
    assert d["stock_ids"] == ["2801", "2809", "2812"]
    assert abs(float(d["share"]) - 0.6) < 1e-9
    assert d["flagged"] is True  # 3 > max_count 2 且 0.6 > max_share 0.5


def test_factor_cluster_zero_hit_not_flagged() -> None:
    members = pl.DataFrame(
        {"stock_id": ["2330"], "industry": ["半導體業"], "theme": ["晶圓代工"]}
    )
    out = compute_factor_cluster_exposure(members, CLUSTERS_CFG)
    assert out[0]["count"] == 0
    assert out[0]["flagged"] is False


# ── 報酬相關簇 ────────────────────────────────────────────────────────────────
def test_correlation_clusters_groups_correlated_pair() -> None:
    clusters, notes = compute_correlation_clusters(
        _price_history(), ["A", "B", "C"], window=60, min_overlap=3, threshold=0.7,
        clip_daily_return_pct=10.0,
    )
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl["stock_ids"] == ["A", "B"]  # C 恆定報酬 → corr null → 不入簇
    assert cl["size"] == 2
    assert cl["pairs"][0]["rho"] == 1.0


def test_correlation_clusters_skips_low_overlap() -> None:
    clusters, notes = compute_correlation_clusters(
        _price_history(), ["A", "B", "C"], window=60, min_overlap=50, threshold=0.7,
        clip_daily_return_pct=10.0,
    )
    assert clusters == []
    assert any("跳過" in n for n in notes)


def test_correlation_clusters_single_stock_noop() -> None:
    clusters, notes = compute_correlation_clusters(
        _price_history(), ["A"], window=60, min_overlap=3, threshold=0.7,
        clip_daily_return_pct=10.0,
    )
    assert clusters == []


# ── 合成 + 顯示 ───────────────────────────────────────────────────────────────
def test_compute_portfolio_check_orchestration() -> None:
    # 持股 ids 對到價格 A/B/C → 相關簇也算得出
    members = pl.DataFrame(
        {
            "stock_id": ["A", "B", "C"],
            "industry": ["金融保險", "金融保險", "半導體業"],
            "theme": ["銀行", "銀行", "晶圓代工"],
        }
    )
    result = compute_portfolio_check(members, _price_history(), PCFG)
    assert result.n_holdings == 3
    assert result.as_of == date(2026, 1, 10)
    assert len(result.corr_clusters) == 1  # {A, B}
    # 銀行 2 檔（< min_count 3 且 share 0.67 ≥ 0.4）→ flagged via share
    banks = next(d for d in result.label_concentration if d["label"] == "銀行")
    assert banks["flagged"] is True


def test_compute_portfolio_check_empty_holdings() -> None:
    result = compute_portfolio_check(pl.DataFrame(), _price_history(), PCFG)
    assert result.n_holdings == 0
    assert result.label_concentration == []
    assert any("無持股" in n for n in result.notes)


def test_compute_portfolio_check_no_price_history() -> None:
    # 無價格 → 相關簇空、label/factor 仍算（報告段路徑）
    result = compute_portfolio_check(MEMBERS, pl.DataFrame(), PCFG)
    assert result.corr_clusters == []
    assert any(d["flagged"] for d in result.factor_clusters)
    assert any(d["flagged"] for d in result.label_concentration)


def test_describe_portfolio_check_line_and_alerts() -> None:
    result = compute_portfolio_check(MEMBERS, pl.DataFrame(), PCFG)
    desc = describe_portfolio_check(result)
    assert "組合體檢" in str(desc["line"])
    # 集中標籤(銀行/金融保險=2) + 相關簇(0) + 因子簇超限(1) = 3
    assert desc["n_alerts"] == len(desc["flagged_labels"]) + len(desc["flagged_factors"])
    assert len(desc["flagged_factors"]) == 1


# ── ETF 手標曝險（docs/21 M-ETF1）────────────────────────────────────────────
def test_compute_portfolio_check_merges_etf_exposure_labels() -> None:
    """etf_exposure 手標 labels 併入 theme → 集中度計數含 ETF、notes 揭露手標估計。"""
    members = pl.concat(
        [
            MEMBERS,
            pl.DataFrame(
                {"stock_id": ["0050"], "industry": ["ETF"], "theme": [""]}
            ),
        ],
        how="vertical",
    )
    cfg = {
        **PCFG,
        "etf_exposure": {"0050": {"labels": ["半導體業", "台股大盤β"], "note": "台積電權重過半"}},
    }
    result = compute_portfolio_check(members, pl.DataFrame(), cfg)
    labels = {d["label"]: d for d in result.label_concentration}
    assert labels["半導體業"]["count"] == 3  # 2330 + 2454 + 0050（手標）
    assert "0050" in labels["半導體業"]["stock_ids"]
    assert labels["台股大盤β"]["count"] == 1
    assert any("手標" in str(n) for n in result.notes)


def test_compute_portfolio_check_etf_exposure_no_hit_noop() -> None:
    """etf_exposure 設定的 id 不在持股 → 標籤/notes 皆不變（純查表、不虛增）。"""
    cfg = {**PCFG, "etf_exposure": {"9999": {"labels": ["不存在標籤"]}}}
    result = compute_portfolio_check(MEMBERS, pl.DataFrame(), cfg)
    assert "不存在標籤" not in {d["label"] for d in result.label_concentration}
    assert all("手標" not in str(n) for n in result.notes)
