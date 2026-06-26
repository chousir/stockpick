# 規劃書 03 — 量化驗證閉環

> 對應審查發現：§4#1（個股策略回測尚未實作）、§4#2（缺大盤 regime 總控）、
> §4#6（缺組合層風控）。
> 性質：**把「看起來合理」變成「資料證明有 edge」，並補上市場層剎車與組合風控**。
> 高價值；V1 是整份審查中 ROI 最高的單一改動。

---

## 背景

系統已有兩條回測軌：
- `rotation-calib`（次產業資金訊號）— **已實作**，用 34 起漲點校準出 ★ 進場訊號。
- `cp-value-calib`（個股 CP 因子）— 研究軌，在 `backtest/stock_calib.py`。

但**最核心的主張「D/E/F/G 選股策略本身有效」從未被回測**：
[backtest/strategies.py](../../src/tw_screener/backtest/strategies.py) 三個函式全是
`raise NotImplementedError`，`make backtest-strategies` 直接 `exit 1`（Makefile:128–130）。
原註記「需累積 3 個月、預計 2026-08 後」——**現已累積 5 週 reports/（W21–W26），
到 2026-08 可正式啟動**。沒有這個，整套系統無法自我證明。

此外，所有訊號都在個股/族群層，**缺市場層的剎車**（空頭期照推 breakout 危險），
且 picks 缺組合層的集中度控管（五檔可能都是同一題材）。

---

## V1 — 個股策略回測閉環（最高優先）

### 目標
把 `backtest/strategies.py` 三函式實作出來，量化 D/E/F/G 入選後 N 週的
**勝率 / 平均報酬 / 中位報酬 / 最大回撤 / 樣本數**，並與大盤同期對比（超額報酬）。

### 現況可用素材
- `reports/2026-W*/screen_result_{d,e,f,g}_*.csv`：每週各策略入選快照（純 Goodinfo，未被後處理改寫）。
- `data/cache/twse/daily_*.parquet` + `stock_day_*`：價格歷史（前進報酬計算用）。
- 既有 `analysis/momentum.py`（報酬計算）、`rotation.load_market_history`（大盤基準）可重用。

### 方案（實作三函式，介面已預定義）
1. `load_historical_screens(reports_dir)`：掃所有 `screen_result_*.csv`，
   合併成 `week_tag / stock_id / name / strategy_id` 長表（已有 schema 註解）。
2. `compute_forward_returns(screens, price_history, hold_weeks)`：
   - 入選週的「次一交易日開盤或當週收盤」為 entry，持有 `hold_weeks` 後為 exit。
   - **明確處理**：除權息還原（沿用 `momentum.compute_dividend_addback` 思路）、
     下市/停牌（缺 exit 價 → 標 null 不當 0）、尚未到期的近週（前進窗不足 → 排除不污染）。
   - 同時算同期大盤（等權全市場指數，與 rotation/panel 同法）→ 超額報酬。
3. `strategy_summary(returns)`：每策略 win_rate / avg / median / max_drawdown / sample_count
   ＋ vs 大盤超額；另切「多窗持有期」（如 2/4/8/12 週）看 edge 隨持有衰減（沿用
   docs/15 robustness 的 decay 概念）。
4. 產 `research/strategy_backtest/summary_{YYYYMMDD}.md`（gitignore，本地研究產物）。
5. Makefile `backtest-strategies` 改成真的跑（移除 exit 1）。

### 成功標準
- [ ] `make backtest-strategies` 產出各策略勝率/報酬/回撤表（不再 exit 1）。
- [ ] 報酬計算對「除權息／下市／未到期」三類邊界有明確、可測的處理。
- [ ] 與大盤超額並列（避免「大盤漲所以策略賺」的錯歸因）。
- [ ] 樣本不足時誠實標「樣本 N 太小、僅供參考」，不假裝顯著。
- [ ] `tests/backtest/test_strategies.py` 以合成 reports/ + 價格驗算邏輯。

### 可動檔案範圍
`src/tw_screener/backtest/strategies.py`、`cli.py`、`Makefile`、
`tests/backtest/test_strategies.py`、`tests/fixtures/`（合成週快照）。

### 風險與取捨
- **存活者偏誤/前視偏誤**：entry 一律用入選「之後」可成交價，禁用入選當日收盤回看。
- **樣本稀疏**：5 週起步，結論先當「方向性」；隨週數變厚每季重算（與 rotation-calib 同節奏）。
- 不做最佳化過擬合（不掃一堆持有期挑最好的講故事）——固定報告全部持有期。

---

## V2 — 大盤 regime 總控閘門

### 目標
加一個**市場層多空/位階訊號**，在 picks 階段做倉位建議的剎車——空頭/高位期自動收緊。

### 方案
1. `analysis/regime.py`（新）：用既有等權全市場指數算
   - 趨勢：指數 vs MA20/MA60/MA120 的多空排列。
   - 廣度：全市場 breadth（上漲家數比、距低位階）。
   - 資金：全市場三大法人淨流方向（已有法人快取可加總）。
   合成 `regime ∈ {進攻 / 中性 / 防禦}` ＋ 連續分數，全參數進 settings。
2. 接進 `group_analysis.md` Section 0（事件/姿態）與 `sector_rotation.md` 表頭，
   作為 picks「姿態建議＋倉位上限」的依據（docs/11 prompt 已有「姿態」概念，補上量化來源）。

### 成功標準
- [ ] `uv run tw-screener market regime` 印出當前 regime 與分項依據。
- [ ] group/rotation 報表頭顯示 regime；空頭期報表明確提示「降低總曝險」。
- [ ] 參數全 settings 化、純函式可離線測試。

### 可動檔案範圍
`src/tw_screener/analysis/regime.py`（新）、`report/group_report.py`、
`report/rotation_report.py`、`cli.py`、`config/settings.yaml`、`tests/`。

### 風險
regime 本身也該回測（V1 完成後可順帶驗「regime 防禦期是否真的少賠」）；上線初期定位為
「輔助姿態」、不硬性 gate 掉訊號。

---

## V3 — 組合層風控（因子簇／相關性集中度）

### 目標
落實 [docs/14](../14-entry-ladder-portfolio-fix.md) 已提的「因子簇上限／前重後輕分批／
停損脫鉤」，避免 picks 五檔其實是同一題材的隱性集中。

### 方案
1. `analysis/portfolio.py`（新）：對一組 picks（或 holdings＋picks）計算
   - 次產業/概念股標籤集中度（同 `concepts.yaml` 多標籤）。
   - 近 60 日報酬相關矩陣（價格已有）→ 找高相關簇。
   - 因子簇上限檢核（同簇曝險超過門檻 → 警示）。
2. 產「組合體檢」段落進 picks 流程（docs/11）或 dashboard 持股頁。
3. 分批/停損脫鉤的具體規則沿用 docs/14，落成可計算的輔助欄。

### 成功標準
- [ ] `uv run tw-screener portfolio check --week current` 列出集中度與高相關簇警示。
- [ ] 與 holdings_enriched 整合，dashboard 持股頁可顯集中度。
- [ ] 純函式、可離線測試。

### 可動檔案範圍
`src/tw_screener/analysis/portfolio.py`（新）、`cli.py`、`report/*`、
（選）`frontend/` 持股頁、`tests/`。

### 風險
相關性會隨市況變、非穩定 → 定位為「風險揭露」而非硬約束（守人設「由人決策」）。

---

## 驗收（整份規劃書）

```bash
make backtest-strategies                    # V1：產勝率/報酬/回撤表
uv run tw-screener market regime            # V2
uv run tw-screener portfolio check --week current  # V3
make test && make lint && make typecheck
```

## 執行順序
**V1 → V2 → V3**。V1 一旦有了報酬序列，V2/V3 都能借它驗證自身價值（regime 是否少賠、
組合風控是否降波動），形成真正的閉環。
</content>
