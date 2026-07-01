# 規劃書 08 — 主動 vs 被動法人流（Active-vs-Passive Flow）

> 對應審查發現：**2026-07-01 分析師實戰回饋審查** §真問題#4
> ——「法人淨額 ≠ 主動看好；外資買可能是 MSCI 調權重、ETF 申購、季底作帳的被動流入，
> 我把 foreign_net_lots 當 conviction 訊號，分不出 alpha flow 與機械 flow」。
> 性質：**用可得代理指標，把「被動配置疑慮」標出來**——不假裝能乾淨切割主動/被動。
> 中價值；**受公開資料本質限制，只能做 proxy，本份對此誠實**。

---

## 背景與根因（含可行性邊界）

現況 `foreign_net_lots / inst_net_lots` 直接被當 conviction 訊號排序、標「外資主導」。
但大型金控（國泰金、台新新光金）正是最容易被指數/ETF 被動流帶動的——它們的「外資大買」
有多少是真看好、多少是機械配置，**現在沒有任何欄位分辨**（`grep` 全 `src/`：僅
`is_etf_or_warrant` 做「把 ETF 本身排除出宇宙」，**沒有任何法人流的主動/被動分解**）。

**可行性邊界（必須先講清楚，否則會做一個假的東西）**：
TWSE OpenAPI 的三大法人買賣超**不帶「為什麼買」的標記**——沒有欄位告訴你這筆是
MSCI 調權、ETF 申贖、還是主動選股。**乾淨的主動/被動拆分，公開資料做不到。**
（真要拆需 ETF 每日申贖/成分明細，落在 `poc/active_etf/`，且後端 geo-fence 到台灣本機，
見記憶 `project_active_etf_poc`——非本份範圍。）

**所以本份只做 proxy**：不宣稱能分辨主動/被動，只把「這筆流很可能是被動/機械」的**疑慮標出來**，
供分析師對大型權值股的「外資主導」標籤打折。

---

## AP1 — 周轉調整流 + 被動疑慮旗標（proxy）

### 目標
加兩個代理欄，讓分析師看到「這檔的法人流，相對它的體量到底算不算強、以及是否落在
最易被指數流帶動的族群」——把 raw 淨額的 conviction 解讀打上該有的折扣。

### 現況可用素材
- `foreign_net_5d/10d_lots`、`inst_net_*`、`volume_lots_today`、`amount_million`（enriched 既有）。
- `daily_*.parquet` 可算流通量/均量基準。
- `concepts.yaml` 次產業標籤可辨識「指數權值密集」族群（金控/大型電子權值）。

### 方案
1. `analysis/grouping.py`（或 stock_panel）加派生欄：
   - `foreign_net_turnover_adj`：外資近 N 日淨流 / 同期均量（或 / 流通股數）
     ——**周轉調整後**，把「大到看起來很多、但相對體量其實很小」的被動型買盤壓下來。
   - `passive_flow_suspect`（bool/分級）：落在指數權值密集族群（金控等）**且** 周轉調整流偏低
     **且** 近端無主動加速 → 標「被動疑慮」。門檻進 settings。
2. 報告層（docs/11 籌碼面段）：對 `passive_flow_suspect` 的檔，把「外資主導」讀法自動加註
   「疑被動配置、conviction 打折」，避免大型金控的漂亮淨額被高估。
3. 全門檻/族群清單進 `config/settings.yaml`（不寫死 stock_id）。

### 成功標準
- [ ] enriched 出現 `foreign_net_turnover_adj / passive_flow_suspect` 欄，純函式可測。
- [ ] 國泰金/台新新光金這類指數權值金控，能被標「被動疑慮」；中小型主動買盤標的不被誤標——可測。
- [ ] docs/11 補讀法：**旗標＝把 conviction 打折的揭露，不是扣分 gate**。
- [ ] 文件明寫「這是 proxy、非真主動/被動拆分」，不誤導。

### 可動檔案範圍
`src/tw_screener/analysis/grouping.py`（或 stock_panel）、`report/group_report.py`、
`config/settings.yaml`、`docs/11-propicks-analysis.md`、`tests/`。

### 風險與取捨
- **這是 proxy，會有偽陽/偽陰**——定位「疑慮揭露」而非硬篩，判斷權在人（守人設）。
- 周轉調整分母的選擇（均量 vs 流通股數）影響結果——先取一種、進 settings 可換，別過度工程。
- 真要主動/被動硬拆得靠 `poc/active_etf/`（台灣本機、非本份）——本份不假裝能做到。

---

## AP2（選配）— proxy 的 edge 回測

### 目標
用規劃書 05 回測驗「被動疑慮」標的入選後是否真的比「乾淨主動流」標的弱——
確認這個 proxy 有沒有預測力，避免加一個好看但沒用的欄。

### 方案
`picks_outcome` 按 `passive_flow_suspect` 分桶比較前進報酬與命中率。

### 成功標準
- [ ] 兩桶報酬對比（樣本少誠實標）；無 edge 則明確標「僅揭露、不進排序」。

### 可動檔案範圍
`src/tw_screener/backtest/picks_outcome.py`、`tests/`。

---

## 驗收

```bash
make week GROUP=defg     # AP1：enriched 應含 turnover_adj / passive_flow_suspect
make pick-outcome        # AP2（選配）：proxy edge 對比
make test && make lint && make typecheck
```

## 執行順序
**AP1（可獨立，與 06/07 平行）→ AP2（選配，依賴規劃書 05）**。
本份刻意排在第二波較後——因為它 proxy 性質最強、edge 最不確定，值得先做完 05 有回測後再驗它值不值得。
