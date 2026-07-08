# 21 — ETF 持股整合（holdings ETF 輕量列＋組合曝險手標 規劃書）

> 起因：使用者將 4 檔 ETF（0050／00981A／00988A／00687B）加入 `watchlist/holdings.csv`，
> 問「能不能分析、怎麼分析、有沒有幫助」。本檔先誠實劃界——**哪些分析對 ETF 沒用、不做**；
> 再把有實證的兩個真問題（持股無聲消失＋組合體檢稀釋）接進生產。

---

## 0. 一句話目標

讓 ETF 持股在**持股追蹤層**（報酬率/位階/現值）與**組合曝險層**（集中度/因子簇）誠實可見，
**不把 ETF 塞進個股 alpha 框架**（族群輪動/位階起漲/基本面/籌碼訊號對 ETF 無意義）。

---

## 1. 研究實證（2026-07-08，W28 實際輸出）——為什麼要動

### 1.1 問題一：持股無聲消失（已實證）

- `holdings.csv` 有 10 檔（6 個股＋4 ETF），今日 11:25 產出的 `reports/2026-W28/holdings_enriched.csv`
  **只有 6 檔**——4 檔 ETF 全數無聲消失，無任何警告。
- 消失點＝[`grouping.py` `is_etf_or_warrant`](../src/tw_screener/analysis/grouping.py)：`group_stocks`
  一律跳過「00」開頭代號。這對**選股宇宙**是正確設計（維持不變），但 holdings enrich 也走同一條路，
  導致持股 ETF 連「買入價/報酬率%/現值」都沒有。
- 實際痛點：00988A 買 22.65、現價 19.62＝**−13.4%**，系統完全沒看到。
- 資料可用性已驗證：0050/00981A/00988A 日線快取 100 列完整（daily＋stock_day）、法人買賣超快取有資料；
  基本面/族群標籤**無**（ETF 天然沒有）。

### 1.2 問題二：組合體檢稀釋失真（已實證）

W28 `group_analysis.md` 組合體檢段：

> 組合體檢：10 檔｜半導體業：5 檔（**50%**）

- 分母含 4 檔無標籤 ETF → 個股層實際集中度 5/6＝**83%** 被稀釋成 50%。
- 更失真的方向：0050（台積電權重過半）＋00981A（主動台股成長）其實是**加碼**半導體 β，
  真實半導體曝險 ≈ 7/10 檔，體檢卻反向顯示變分散。
- 00687B（美債長天期）是組合唯一利率久期部位，因子簇完全看不到（現有「利率敏感」簇＝
  銀行/壽險股權鏈，方向相反，**不可**把債券 ETF 塞進去）。
- 以買入價粗估，ETF 4 檔 ≈ 組合市值 **30%**——這 30% 目前在所有風控揭露中隱形。

### 1.3 附帶發現

- **代號疑義（需使用者確認）**：holdings.csv 寫「00687B 元大美債20年」，但 00687B＝**國泰**20年美債；
  元大美債20年＝**00679B**。且快取價 28.16 vs 買入價 33 對不上。整合設計對兩個代號同樣適用，
  但價格追蹤會追錯標的——請使用者修正 CSV。
- 00687B 日線只有 otc_daily 零星 9 列（近 18 個交易日）——資料源本就稀疏，位階/MA 欄會誠實 null。
- 診斷雷達（docs/19 WS2）`build_market_screens` 事件宇宙＝當日全市場、**未濾 ETF**；ETF 進 holdings
  後會以「held」身分進五態雷達。雷達對照的是 screener 宇宙（已排除 ETF），宇宙須對齊。

---

## 2. 誠實劃界：對 ETF 沒用、不做的事

| 不做 | 為什麼沒用 |
|---|---|
| ETF 個股報告（docs/11 十段框架） | 基本面全空（無營收/EPS/毛利率）、無族群歸屬、籌碼語義不同（ETF 法人買賣含申贖/造市機制性流量，非選股訊號） |
| 00988A 標的層分析 | 持倉＝全球股票（海外），驅動因子完全不在本系統資料範圍 |
| 00687B/00679B 標的層分析 | 驅動＝美國長天期利率＋匯率，系統無此資料源；技術面殘影無決策價值 |
| 00981A 持股內容當訊號源 | 已有 `poc/active_etf/` PoC，資料後端 geo-fence 台灣本機；屬另一條研究軌，不混入本 milestone |
| 組合體檢改價值加權 | holdings_enriched 已有 market_value_k，技術上可做；但 V3 等權設計動一發牽全身，列**未來選配**，本次不動 |

---

## 3. 設計（生產／一顆 bubble）

### 3.1 ETF 輕量列（修問題一）

- `group_stocks(..., skip_etf: bool = True)`：新增參數，預設 True＝現行為（選股宇宙不變）；
  只有 `enrich_named_list`（holdings/watchlist enrich）傳 False。
- ETF 列產出內容＝**價格可算欄**（close/change/momentum/ret_10d/MA 距離/低高點/量比/報酬率/現值）
  ＋法人買賣欄（資料存在，揭露；讀法警語見 §3.4）；基本面/估值/族群欄誠實 null。
- `industry` 欄對 ETF 列標 `"ETF"`（不混入「未分類」）；enriched CSV 新增 `asset_type` 欄
  （`etf`/`stock`），三份 CSV（candidates/watchlist/holdings）同欄位超集慣例。
- dashboard 免改：`EnrichedRow` 全 Optional＋`extra="allow"`，已驗證相容。

### 3.2 ETF 手標曝險表（修問題二）

`config/settings.yaml` `portfolio.etf_exposure`（手標、非資料源；比照 factor_clusters 純設定慣例）：

```yaml
portfolio:
  etf_exposure:            # ETF 手標曝險：labels 併入該檔標籤集合參與 集中度/因子簇
    "0050":   {labels: [半導體業, 台股大盤β], note: 台積電權重過半}
    "00981A": {labels: [半導體業, 台股大盤β], note: 主動台股成長；持股依公開月報估計}
    "00988A": {labels: [全球科技, 美元資產], note: 主動全球創新；海外持倉}
    "00687B": {labels: [美債長天期, 美元資產], note: 20年期美國公債；利率久期部位}
```

- 注入點＝`compute_portfolio_check` 開頭：members 的 `theme` 欄對命中 stock_id 併入手標 labels
  （單點修改，報告段與 `portfolio check` CLI 兩條路自動生效）。
- 半導體 label 用與 industry 完全一致的字串「半導體業」→ 集中度自動合併計數（預期 7/10＝70%）。
- **不**把 00687B 塞進「利率敏感」因子簇（該簇＝升息受惠股權鏈，債券方向相反）；
  美元資產/美債久期以 label 揭露即可（1 檔不構成集中，價值在**可見**）。
- 揭露 note：組合體檢段加一行「ETF 曝險為手標估計（主動 ETF 依公開月報）」。

### 3.3 診斷雷達宇宙對齊（修 §1.3 第三點）

`build_market_screens` 濾 `is_etf_or_warrant`——雷達宇宙鏡射 screener 宇宙（本就不選 ETF，
「漏抓 ETF 起漲」不是本系統的雷達職責）。

### 3.4 docs/11 讀法同步（純 prompt）

ETF 列（`asset_type=etf`）讀法三原則：只看 報酬率/位階/組合曝險；法人欄含申贖與造市
機制性流量、僅供參考不當籌碼訊號；不套個股多空/基本面框架。

---

## 4. 不變式（surgical 界線）

- 選股宇宙、候選清單、picks 底帳、CP 候選：**零行為變化**（skip_etf 預設 True）。
- 組合體檢等權近似、V3 三段結構：不動。
- 不加資料源、不加依賴；00687B 資料稀疏就讓欄位 null，不去補抓。

---

## 5. 驗收

1. `make test` 全綠（新增：grouping skip_etf 迴歸＋ETF 收列、portfolio etf_exposure 併標籤、
   enriched rows asset_type、診斷宇宙濾 ETF）。
2. 重生 W28 `holdings_enriched.csv`：10 列（6 股＋4 ETF），ETF 列有 close/return_pct/market_value_k
   ＋`asset_type=etf`＋`industry=ETF`，基本面欄 null。
3. `uv run tw-screener portfolio check`：n=10、半導體業計入 0050/00981A（7 檔）、
   00687B/00988A 曝險 label 可見。
4. 候選端迴歸：candidates 產出流程無 ETF 列（宇宙不變）。

> 報表正文（group_analysis.md 組合體檢段）W29 重跑生效；本次只重生 holdings_enriched.csv。

---

> 建立：2026-07-08。狀態：使用者「若有用先規劃書再執行」授權下開工。
