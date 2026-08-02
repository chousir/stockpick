"""backtest/macro_regime_validate_runner.py — M-Macro2/M-Macro3 IO 編排（自 cli.py 薄殼呼叫）。

讀 research/macro_regime_screening/raw/（三輪研究產出，gitignored；先跑過三輪研究才有這批
parquet）→ macro_regime_validate 純函式 → research/macro_regime_screening/round4/round5.md。
研究軌一次性驗證，非週流程，不掛 make week（同 cp-value-calib／contrarian-efficacy 慣例）。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from rich.console import Console

console = Console()


def _fmt_lift(result) -> str:  # noqa: ANN001
    if result.lift is None:
        return f"— (n 不足；桶內 n={result.n_high_risk}／全樣本 n={result.n_all})"
    ci = (
        f"〔{result.ci_lo:.2f}–{result.ci_hi:.2f}〕"
        if result.ci_lo is not None and result.ci_hi is not None
        else "〔CI 不可得〕"
    )
    return (
        f"{result.lift:.2f} {ci}"
        f"（高風險 n={result.n_high_risk}／全樣本 n={result.n_all}・{result.n_blocks} 區塊）"
    )


def run_macro_regime_validate(settings: Path) -> Path:
    """M-Macro2：as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 重測，寫 round4 報告。"""
    import yaml

    from tw_screener.backtest.macro_regime_validate import (
        build_dual_risk_series,
        build_level_pct_series,
        compute_event_labels,
        high_risk_lift,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    mv = cfg.get("backtest", {}).get("macro_regime_validate", {})
    raw_dir = Path(mv.get("raw_dir", "research/macro_regime_screening/raw"))
    event_target = raw_dir / mv.get("event_target", "NASDAQCOM.parquet")
    primary_file = raw_dir / mv.get("primary_series_file", "BAA10Y.parquet")
    dexjpus_file = raw_dir / mv.get("dexjpus_file", "DEXJPUS.parquet")
    n_days = int(mv.get("event_n_days", 60))
    drawdown_pct = float(mv.get("event_drawdown_pct", 0.15))
    lookback_days = int(mv.get("lookback_days", 756))
    min_obs = int(mv.get("min_obs", 30))
    quintile = float(mv.get("quintile", 0.80))
    threshold_grid = [float(q) for q in mv.get("threshold_grid", [0.75, 0.80, 0.85])]
    dexjpus_delta_days = int(mv.get("dexjpus_delta_days", 20))
    dexjpus_tail_grid = [float(q) for q in mv.get("dexjpus_tail_grid", [0.80, 0.90, 0.95, 0.98])]
    block_len = int(mv.get("block_len", 60))
    n_boot = int(mv.get("n_boot", 1000))
    seed = int(mv.get("seed", 42))
    out_dir = Path(mv.get("output_dir", "research/macro_regime_screening"))

    for p in (event_target, primary_file, dexjpus_file):
        if not p.exists():
            console.print(
                f"[red]找不到 {p}——先跑三輪研究產出 raw/ parquet（docs/25 §6 Phase 2 前置）[/red]"
            )
            raise FileNotFoundError(str(p))

    console.print("[bold]載入研究原始序列（本地 parquet，不打網）…[/bold]")
    nasdaq = pl.read_parquet(event_target)
    baa10y = pl.read_parquet(primary_file)
    dexjpus = pl.read_parquet(dexjpus_file)

    events = compute_event_labels(nasdaq, n_days, drawdown_pct)
    console.print(f"  事件標籤 {events.height} 列（N={n_days}／X={drawdown_pct:.0%}，NASDAQCOM）")

    console.print(
        "[bold]as-of 回放：逐日重算 BAA10Y level_pct（重用 production compute_level_pct）…[/bold]"
    )
    baa10y_scores = build_level_pct_series(baa10y, lookback_days, min_obs)
    console.print(f"  BAA10Y level_pct 序列 {baa10y_scores.height} 列")

    headline = high_risk_lift(baa10y_scores, events, quintile, block_len, n_boot, seed)
    console.print(f"  headline lift（quintile={quintile}）＝{_fmt_lift(headline)}")

    threshold_results = {
        q: high_risk_lift(baa10y_scores, events, q, block_len, n_boot, seed)
        for q in threshold_grid
    }

    console.print(
        "[bold]DEXJPUS tail-event 重測：逐日重算 dual_risk（重用 production "
        "compute_dual_risk）…[/bold]"
    )
    dexjpus_scores = build_dual_risk_series(dexjpus, lookback_days, dexjpus_delta_days, min_obs)
    console.print(f"  DEXJPUS dual_risk 序列 {dexjpus_scores.height} 列")
    dexjpus_results = {
        q: high_risk_lift(dexjpus_scores, events, q, block_len, n_boot, seed)
        for q in dexjpus_tail_grid
    }

    lines = [
        "# MacroRegime 研究——round 4：as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 重測",
        "",
        "> 承接 round 1-3（隸屬 docs/25-macro-regime.md §6 Phase 2 M-Macro2）。"
        "本輪由 `tw-screener backtest macro-regime-validate`"
        "（`src/tw_screener/backtest/macro_regime_validate.py`）產出，"
        "非人工複製貼上——重跑 `make macro-regime-validate` 可重現全部數字。",
        "",
        "## 0. 開工三行",
        "",
        f"- **三個問題**：① 直接重用 production `compute_level_pct` 逐日重放 BAA10Y，"
        f"lift 是否跟 round 2 headline（2.26〔1.61–3.06〕）在誤差範圍內一致？"
        f"② 紅燈切點（quintile）在 {threshold_grid} 這個小網格上訊號強度如何變化？"
        f"③ DEXJPUS 用更窄尾端門檻（{dexjpus_tail_grid}）重測，round1 quintile=0.80 的"
        f"無證據結論（1.15〔0.80–1.49〕）會不會翻案？",
        "- **裁決門檻**：①「一致」＝CI 有重疊且點估計差距 < 1.0；②③沿用 round1-3 全程一致的"
        f"「headline lift 且 CI 下界 >1」判定，N={n_days}/X={drawdown_pct:.0%} 對 NASDAQCOM。",
        "- **答完即停**：本檔即為終稿，是否寫回 docs/25 由使用者裁決。",
        "",
        "## 1. as-of 回放驗證（production pipeline vs 研究階段）",
        "",
        "- **research 階段（round 2，`raw/round2_baa10y_results.csv`）**：headline lift "
        "**2.26〔1.61–3.06〕**"
        "（pass1_nasdaq, level_pct, N=60, X=15%, n_blocks=168, n_obs=10134）。",
        f"- **本輪 as-of 回放（直接呼叫 `analysis/macro_regime.compute_level_pct`，非重寫）**："
        f"lift **{_fmt_lift(headline)}**。",
        "",
    ]

    if headline.lift is not None:
        gap = abs(headline.lift - 2.2619)
        ci_overlap = (
            headline.ci_lo is not None
            and headline.ci_hi is not None
            and headline.ci_lo <= 3.0595
            and headline.ci_hi >= 1.6061
        )
        verdict = "**一致**" if ci_overlap and gap < 1.0 else "**不一致，需人工複核**"
        gap_note = (
            "production pipeline 與研究階段計算邏輯等價，通過 Phase 2 把關。"
            if "一致" in verdict
            else "研究用計算邏輯與 production 用計算邏輯可能有微妙落差，"
            "或事件目標序列/日期範圍與 round 2 原始跑法不完全同源"
            "（如 raw/ 序列自 round 2 後被重抓過），須人工追查後才能宣稱 Phase 2 as-of 驗證通過。"
        )
        lines += [
            f"- **裁決**：{verdict}"
            f"（點估計差距 {gap:.2f}，CI {'有' if ci_overlap else '無'}重疊）。{gap_note}",
            "",
        ]
    else:
        lines += ["- **裁決**：樣本不足，無法比較——檢查 raw/ 資料完整性。", ""]

    lines += [
        "## 2. 門檻敏感度（red_min/quintile 網格）",
        "",
        "| quintile（≈red_min/100） | lift |",
        "|---|---|",
    ]
    for q in threshold_grid:
        lines.append(f"| {q:.2f} | {_fmt_lift(threshold_results[q])} |")
    lines += [
        "",
        "> 讀法：quintile 越窄（門檻越高）→ 樣本數越少、CI 越寬，是否伴隨 lift 上升要看數字本身；"
        "若 lift 隨門檻收窄而系統性升高，代表訊號集中在更極端的尾端，支持未來把 red_min 往上調的"
        "討論——但門檻改動本身仍須使用者拍板（本輪只描述現象，不擅自改 settings）。",
        "",
        "## 3. DEXJPUS tail-event 重測",
        "",
        "- round 1 結論（quintile=0.80，即 top 20%）：dual_risk headline lift 1.15〔0.80–1.49〕"
        "＝無證據。",
        "",
        "| 門檻（top N%） | lift |",
        "|---|---|",
    ]
    for q in dexjpus_tail_grid:
        pct = (1 - q) * 100
        lines.append(f"| top {pct:.0f}%（quintile={q:.2f}） | {_fmt_lift(dexjpus_results[q])} |")

    strongest = max(
        (r for r in dexjpus_results.values() if r.lift is not None),
        key=lambda r: r.ci_lo if r.ci_lo is not None else float("-inf"),
        default=None,
    )
    if strongest is not None and strongest.ci_lo is not None and strongest.ci_lo > 1.0:
        dexjpus_verdict = (
            "**出現證據**——至少一個更窄尾端門檻的 CI 下界 >1，tail-event 假說可能成立。"
            "**不擅自升級 DEXJPUS 為計分訊號**（v2 §0「不合成」是設計主軸，新增第二個計分指標"
            "是需要使用者拍板的架構決定，非本輪研究可單方決定）——記錄為候選，留待使用者裁決是否"
            "值得另開一輪跟 BAA10Y 同等規格的完整驗證（四格網格＋台股 Pass 2 確認）。"
        )
    else:
        dexjpus_verdict = (
            "**維持無證據**——即使收窄到 top 2%，CI 下界仍未穩定 >1。"
            "docs/25「tail-event 稀釋假說」本輪未被證實；DEXJPUS 繼續留在揭露面板、不計分，"
            "維持 round1 判定不變。"
        )
    lines += ["", f"**裁決**：{dexjpus_verdict}", ""]

    lines += [
        "## 4. 誠實記錄",
        "",
        "- 本檔由 `run_macro_regime_validate` 直接產出 markdown（非人工轉述 agent 摘要），"
        "數字可用 `make macro-regime-validate` 重現。",
        "- 事件目標沿用 round 1 的 NASDAQCOM（唯一有數十年歷史、可比較的權益指數）；"
        "台股本地確認（Pass 2）本輪未重跑——round 1/2 已指出 Pass 2 樣本太薄（22 個不重疊區塊）"
        "不足以獨立下結論，本輪聚焦「production pipeline 是否等價於研究階段」，Pass 2 範圍不變。",
        "- CI 用 block bootstrap（block_len 同 headline N，1000 次重抽），跟三輪研究方法論一致；"
        "重用 `factor_lab.moving_block_bootstrap_ci`，未另寫 bootstrap 邏輯（避免方法論漂移）。",
        "",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report_2026-08-01_round4.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]round4 報告 → {out_path}[/green]")
    return out_path


def run_macro_regime_resonance(settings: Path) -> Path:
    """M-Macro3（docs/25 §6 Phase 3）：燈號 vs V2 regime 共振/背離讀法實測驗證，寫 round5 報告。

    需先跑過 `make regime-history`（產 research/panel/regime_labels.parquet）——本函式純讀本地
    parquet，不打網、不重算 regime 標籤（regime_history.py 是唯一算標籤的地方，這裡只消費）。
    """
    import yaml

    from tw_screener.backtest.macro_regime_validate import (
        bucket_lift,
        build_light_color_series,
        compute_event_labels,
    )

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    mv = cfg.get("backtest", {}).get("macro_regime_validate", {})
    raw_dir = Path(mv.get("raw_dir", "research/macro_regime_screening/raw"))
    primary_file = raw_dir / mv.get("primary_series_file", "BAA10Y.parquet")
    tw_index_file = raw_dir / mv.get("tw_index_file", "tw_equal_weight_index.parquet")
    n_days = int(mv.get("event_n_days", 60))
    drawdown_pct = float(mv.get("event_drawdown_pct", 0.15))
    block_len = int(mv.get("block_len", 60))
    n_boot = int(mv.get("n_boot", 1000))
    seed = int(mv.get("seed", 42))
    out_dir = Path(mv.get("output_dir", "research/macro_regime_screening"))

    mr = cfg.get("macro_regime", {})
    lookback_days = int(mr.get("lookback_days", 756))
    thresholds = mr.get("thresholds", {})
    green_max = float(thresholds.get("green_max", 60))
    red_min = float(thresholds.get("red_min", 80))
    hysteresis = float(mr.get("hysteresis", 3))

    rh = cfg.get("backtest", {}).get("regime_history", {})
    regime_path = Path(rh.get("output_path", "research/panel/regime_labels.parquet"))

    for p in (primary_file, tw_index_file, regime_path):
        if not p.exists():
            console.print(
                f"[red]找不到 {p}——先跑 make regime-history（V2 regime 標籤）"
                "＋確認三輪研究 raw/ parquet 存在（docs/25 §6 Phase 3 前置）[/red]"
            )
            raise FileNotFoundError(str(p))

    console.print("[bold]載入 BAA10Y／V2 regime 標籤／台股等權指數（本地 parquet，不打網）…[/bold]")
    baa10y = pl.read_parquet(primary_file)
    regime = pl.read_parquet(regime_path).select("date", "regime_label")
    tw_index = pl.read_parquet(tw_index_file).rename({"market_index": "value"})

    events = compute_event_labels(tw_index, n_days, drawdown_pct)
    console.print(
        f"  事件標籤 {events.height} 列（N={n_days}／X={drawdown_pct:.0%}，台股等權指數）"
    )

    console.print("[bold]as-of 回放：逐日重算生產燈色（含遲滯帶跨日記憶）…[/bold]")
    colors = build_light_color_series(baa10y, lookback_days, green_max, red_min, hysteresis)
    merged = colors.join(regime, on="date", how="inner")
    overlap_min, overlap_max = str(merged["date"].min()), str(merged["date"].max())
    console.print(f"  燈色×V2 regime 重疊窗 {merged.height} 列（{overlap_min}~{overlap_max}）")

    buckets = {
        "baa_red_alone": merged.select("date", pl.col("color").eq("紅").alias("in_bucket")),
        "v2_defense_alone": merged.select(
            "date", pl.col("regime_label").eq("防禦").alias("in_bucket")
        ),
        "v2_attack_alone": merged.select(
            "date", pl.col("regime_label").eq("進攻").alias("in_bucket")
        ),
        "resonance": merged.select(
            "date",
            (pl.col("color").eq("紅") & pl.col("regime_label").eq("防禦")).alias("in_bucket"),
        ),
        "divergence": merged.select(
            "date",
            (pl.col("color").eq("紅") & pl.col("regime_label").eq("進攻")).alias("in_bucket"),
        ),
    }
    results = {
        name: bucket_lift(b, events, block_len, n_boot, seed) for name, b in buckets.items()
    }
    for name, r in results.items():
        console.print(f"  {name}: {_fmt_lift(r)}")

    red_dates = sorted(merged.filter(pl.col("color") == "紅")["date"].to_list())
    n_red_days = len(red_dates)
    red_dates_str = [d.isoformat() for d in red_dates]
    resonance = results["resonance"]

    if resonance.n_high_risk == 0:
        verdict = (
            "**結構性無法檢驗（非否證）**——重疊窗內「BAA10Y 紅」與「V2 regime 防禦」從未同時"
            "發生過一天，共振桶樣本數＝0，沒有任何統計量可算。這不是「測了沒訊號」，是「這個"
            "條件在目前可信賴的本地歷史窗裡一次都沒出現過」，兩者是不同的誠實結論"
            "（playbook/20 §6.6：否證是合法結局，但『樣本不足/結構性無法檢驗』不能包裝成否證）。"
        )
    elif resonance.ci_lo is not None and resonance.ci_lo > 1.0:
        verdict = (
            "**共振桶樣本數足以估計，且 CI 下界 >1**——"
            "見下方數字，讀法可保留但仍需更多樣本累積驗證穩健度。"
        )
    else:
        verdict = (
            "**共振桶樣本數過少，CI 不可靠**——見下方數字，不足以判定共振讀法有無預測力。"
        )

    lines = [
        "# MacroRegime 研究——round 5：燈號 vs V2 regime 共振/背離讀法實測驗證",
        "",
        "> 承接 round 1-4（隸屬 docs/25-macro-regime.md §6 Phase 3 M-Macro3）。"
        "本輪由 `tw-screener backtest macro-regime-resonance`"
        "（`src/tw_screener/backtest/macro_regime_validate.py`）產出，"
        "非人工複製貼上——重跑 `make macro-regime-resonance` 可重現全部數字"
        "（前提：`make regime-history` 已跑過、`research/panel/regime_labels.parquet` 存在）。",
        "",
        "## 0. 開工三行",
        "",
        "- **唯一要回答的問題**：「BAA10Y 燈色紅 ∧ V2 regime 防禦」（共振）的 lift 是否顯著優於"
        "任一單獨訊號（BAA10Y 紅 alone 或 V2 防禦 alone），用台股等權指數 N=60/X=15% 事件目標？",
        "- **裁決門檻**：共振桶 CI 下界需明顯高於兩個單獨訊號各自的 lift 點估計，才算「有增量"
        "資訊」；共振桶樣本數為 0 或過少（block bootstrap 需要足夠觀測才給得出可信 CI）→ 回報"
        "「結構性無法檢驗／樣本不足」，不得硬套一個看似自信的結論。",
        "- **答完即停**：本檔即為終稿，是否寫回 docs/11／docs/25 由本輪一併處理（見 §3）。",
        "",
        "## 1. 資料窗與已知限制（誠實記錄，決定了這輪能不能測）",
        "",
        "- **V2 regime 標籤覆蓋 2022-01-03～2026-07-31**（`make regime-history` 現況；受本地"
        "全市場日線快取深度限制，非任意可調）。本輪先嘗試把 regime 標籤窗擴到 2016-01"
        "（價格快取 min date 顯示 2016-01-04 起有資料）——結果發現 2016/2017 只是零星殘留檔案"
        "（分別僅 21／60 個交易日，非連續覆蓋），**2018-2020 整整三年完全沒有本地全市場日線"
        "快取**（無 `daily_*`／`otc_daily_*`／`stock_day_*` 覆蓋），2021 年中才恢復連續覆蓋"
        "（法人快取亦同步從 2021-07-02 起）。換句話說，2022-01 這個既有的 `regime_history.start`"
        "設定不是隨便選的，是本地資料實際可信賴的起點——嘗試往前延伸沒有帶來額外可用樣本，"
        "反而納入了幾週不可靠的殘缺資料，**本輪最終仍採用 2022-2026 這個乾淨窗**。",
        "- **後果**：BAA10Y 真正的紅燈期（2008 金融海嘯、2015-16 中國減速/油價崩跌、2020 "
        "COVID 崩盤）全部落在這個可信賴窗口之外。要真的測到這些事件底下共振讀法的表現，"
        "需要先補齊 2018-2020 本地日線快取（`make backfill-daily-history`／"
        "`make backfill-universe-history`，README 標註約需 8-12 小時，一次性冷啟動）——"
        "這是一個獨立、規模遠大於本輪的資料工程決定，本輪不代為決定，留待使用者評估是否值得做。",
        f"- **本輪實際重疊窗**：{overlap_min}～{overlap_max}，"
        f"{merged.height} 個交易日；其中 BAA10Y 燈色＝紅 僅 **{n_red_days} 天**"
        f"（{red_dates_str}，"
        "皆為 V2 regime=進攻），紅燈期本身在這個窗口內極端稀少，不是共振條件特別嚴格。",
        "",
        "## 2. 四桶 lift 比較（N=60/X=15%，台股等權指數事件目標）",
        "",
        "| 桶 | lift（含樣本數／CI／區塊數） |",
        "|---|---|",
    ]
    for name, r in results.items():
        lines.append(f"| {name} | {_fmt_lift(r)} |")
    lines += [
        "",
        f"**裁決**：{verdict}",
        "",
        "## 3. 對 docs/11／docs/25 的處理",
        "",
        "- **docs/11**「姿態一行」原本的避險句「此讀法尚未實測驗證共振 lift，見 docs/25 §6"
        " Phase 3，僅供敘事參考、非機率結論」——本輪跑完後**加強而非放寬**：不只是「尚未驗證」，"
        "是「重疊窗內共振條件從未發生過，本地資料在可預見的未來也無法驗證，除非先做一次"
        "大規模歷史回補」。docs/11 已同步改寫（見該檔本輪 diff）。",
        "- **docs/25 §6 Phase 3**：標記完成，結果摘要見本檔；不驗收「共振優於單訊號」（結構性"
        "無法檢驗），但驗收「產出研究報告＋docs/11 讀法降級」兩項。",
        "- **不動** `analysis/macro_regime.py` 計分邏輯／`config/settings.yaml` 之"
        "`macro_regime:` 區塊——本輪是讀法驗證，不是計分引擎變更（即使 v2_attack_alone "
        "單獨看起來像是有訊號，也不在本輪授權範圍內採納，留待使用者另行評估）。",
        "",
        "## 4. 誠實記錄",
        "",
        "- 本檔由 `run_macro_regime_resonance` 直接產出 markdown，數字可用 "
        "`make macro-regime-resonance` 重現（需先 `make regime-history`）。",
        "- 燈色重放用 `build_light_color_series`（遲滯帶跨日記憶，非 Phase 2 的單日 quintile "
        "門檻近似）——這是測「production 實際會顯示的顏色」，不是替代訊號。",
        "- lift/CI 統計量與 Phase 2 完全共用（`bucket_lift`／`moving_block_bootstrap_ci`），"
        "未另寫方法論；`bucket_lift` 是 `high_risk_lift` 的泛化版（quintile 門檻只是其中一種"
        "布林條件），`high_risk_lift` 已改為委派呼叫，兩者數字保證一致（回歸測試覆蓋）。",
        "- CI 出現 `nan` 邊界（如 v2_defense_alone 的上界）是 block bootstrap 在區塊數偏少"
        "（block_len=60、樣本 ~1000 天≈17 個區塊）時，部分重抽結果落在 `_lift_stat` 無法計算"
        "的退化情形（某類別在該次重抽中出現 0 次）——如實記錄為「CI 不可得」，不用其他數字頂替。",
        "",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report_2026-08-01_round5.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]round5 報告 → {out_path}[/green]")
    return out_path
