# 規劃書 05 — 選股結果回饋閉環（Pick-Outcome Loop）

> 對應審查發現：**2026-07-01 分析師實戰回饋審查**（人設角色自評）§真問題#1
> ——「沒有回測閉環，是最致命的洞」。
> 性質：**讓系統回頭驗證它自己每週的「精選 pick」對不對**，把每週從零開始選、
> 從不驗證上週自己的開放迴路，補成可被驗證、會自我修正的閉環。
> 高價值；本份是整個第二波審查中 ROI 最高的單一改動。

---

## 背景與根因

### 已有的 ≠ 缺的
規劃書 03 V1（`backtest/strategies.py`，已實作）回測的是 **raw `screen_result_{d,e,f,g}_*.csv`**
——純 Goodinfo 策略入選快照。它回答的是「D/E/F/G 選股策略本身有沒有 edge」。

但**分析師實際交給人決策的不是那張 raw 清單，是 `pick.md` 裡的「核心層／機會層」精選**
——那是經過四鏡頭交叉、拆窗、因子簇檢核、人工蒸餾後的結果。這一層的命中率
**從未被任何程式追蹤**（`grep` 全 `src/` 無 pick-outcome tracking，V1 只讀 screen_result）。

於是出現審查裡最刺眼的案例：**旺宏 W26 是核心、W27 變「下跌陷阱」**。
到底 W26 把它當核心本來就錯，還是 W26 對、只是該換手？**系統答不出來，因為沒有 hit-rate。**

### 更深的根因：pick 根本沒被可靠地保存
盤點 `reports/2026-W*/`：
- `pick.md`：W21、W22、W23、W25、W27 有。
- `picks.md`（多一個 s）：W24。
- **W26 完全沒有** pick 檔。

也就是說——**閉環的前置條件（歷史精選可被機器讀取）現在就是壞的**。
命名漂移（pick/picks）、整週遺失、且 `pick.md` 是自由格式 Markdown、無結構欄位。
不先把「精選」變成穩定、可解析的產物，命中率無從算起。

---

## PO1 — 精選 pick 的結構化持久化（前置，必做第一步）

### 目標
把每週「核心／機會／補充」精選固化成一份**穩定命名、含結構化欄位**的產物，
讓機器能回讀 entry 基準、分層、當週判讀依據。

### 方案
1. 定義 `reports/<week>/picks.csv`（機器讀）＋沿用 `pick.md`（人讀，仍由分析師產）。
   `picks.csv` 最小 schema：
   `week_tag / stock_id / name / layer(core|opportunity|pool) / entry_zone / stop / thesis_tag / decided_at`。
2. 新增 CLI `tw-screener picks record`——分析師定稿後跑一次，把 `pick.md` 裡的精選
   落成 `picks.csv`（初版可半自動：讀分析師填的簡表 → 驗 schema → 寫檔）。
   **不做 NLP 硬解自由 Markdown**（脆、易錯歸因），改要求定稿時填一張最小結構表。
3. 回填 W21–W27 既有 `pick.md` → `picks.csv`（一次性，人工核對，順手修正 W24 命名、補 W26）。
4. schema／路徑／欄位全進 `config/settings.yaml`。

### 成功標準
- [ ] `reports/<week>/picks.csv` schema 固定、`picks record` 可產出並驗欄位。
- [ ] W21–W27 全數有 `picks.csv`（含補回 W26、統一命名）。
- [ ] `tests/` 有 schema 驗證與 happy-path。

### 可動檔案範圍
`src/tw_screener/report/pick_store.py`（新）、`cli.py`、`config/settings.yaml`、
`reports/2026-W2*/picks.csv`（資料回填，注意 CLAUDE.md 2.6 不得含個人持股）、`tests/`。

### 風險與取捨
- 要分析師定稿時多填一張最小表——這是**刻意的成本**，換來可驗證性；不硬解 Markdown。
- `entry_zone/stop` 屬決策資訊，回填舊週時若原 `pick.md` 未寫則留 null，不臆造。

---

## PO2 — 精選命中率 × α/β 拆解（核心）

### 目標
對 `picks.csv` 算**核心層 vs 機會層**入選後 N 週的勝率／平均・中位報酬／最大回撤，
**並拆出族群 beta 與選股 alpha**——回答「這套方法是有預測力，還是多頭裡隨便挑都賺」。

### 現況可用素材（皆已存在，不需新資料源）
- `data/cache/twse/daily_*.parquet`：**8,849 檔全市場日線**，前進報酬足夠。
- `backtest/strategies.py` 的 `compute_forward_returns`（除權息還原／下市 null／未到期排除
  三類邊界已實作且可測）——**直接複用**，只是輸入從 screen_result 換成 picks。
- `analysis/stock_panel.py` 已有 RS vs 大盤（等權全市場指數）。
- 族群歸屬：`concepts.yaml` 次產業標籤 → 算「該檔所屬次產業同期等權報酬」＝ beta 基準。

### 方案
1. `backtest/picks_outcome.py`（新）：
   - 讀 `picks.csv`，複用 `compute_forward_returns`（entry 一律入選**次一交易日**，禁當日收盤回看）。
   - 三個基準並列：**個股報酬 / 大盤同期 / 所屬次產業同期** → alpha = 個股 − 次產業。
   - 分層彙總：core vs opportunity 各自的 win_rate / median / maxDD / sample_n / vs 大盤 / vs 族群。
   - 多持有窗（2/4/8/12 週）看 edge 衰減（沿用 docs/15 decay 概念）。
2. CLI `tw-screener picks outcome`；產 `research/picks_outcome/summary_<date>.md`（gitignore）。
3. Makefile 加 `pick-outcome`。

### 成功標準
- [ ] 產出 core/opportunity 分層的勝率・報酬・回撤表，且**同時列 vs 大盤、vs 族群兩個超額**。
- [ ] 「這套選股有無 alpha」有數字回答（alpha = 個股 − 所屬次產業，非只 vs 大盤）。
- [ ] 樣本不足（起步僅 6–7 週）誠實標「N 太小、方向性參考」，不假裝顯著。
- [ ] `tests/backtest/test_picks_outcome.py` 用合成 picks.csv + 價格驗算。

### 可動檔案範圍
`src/tw_screener/backtest/picks_outcome.py`（新）、`cli.py`、`Makefile`、`config/settings.yaml`、`tests/`。

### 風險與取捨
- **前視／存活者偏誤**：entry 用入選之後可成交價；下市缺 exit → null 不當 0（同 V1）。
- 樣本稀疏是硬限制：定位「隨週數變厚、每季重算」，與 rotation-calib 同節奏。
- 不掃一堆持有窗挑最好看的講故事——固定全部窗一起報。

---

## PO3 — 翻轉案例 post-mortem（旺宏型「核心→陷阱」解剖）

### 目標
自動找出**上週入核心、本週被降級/標風險**的翻轉標的，把「翻轉前有哪些近端訊號先出現」
攤成一張對照表——讓「核心→陷阱」不再只是事後懊悔，而是可學習的 pattern。

### 方案
1. `picks outcome` 之上加 `--diff`：比對相鄰兩週 `picks.csv`，列出 layer 下降或進 flags 的標的。
2. 對每個翻轉標的，拉入選當週 vs 翻轉當週的 **近端籌碼窗（5d/10d，已存在欄位）、
   價量軌跡（PO 依賴 07 的軌跡欄若已做）、ΔRank** ——回答「翻轉前，哪個訊號其實已經先轉」。
3. 產「翻轉解剖」段進 `research/picks_outcome/`；累積成「翻轉前兆」清單，回饋 docs/11 讀法。

### 成功標準
- [ ] `picks outcome --diff` 列出週對週降級標的與其翻轉前的近端訊號對照。
- [ ] 旺宏 W26→W27 個案可重現解剖（驗證真實案例能被工具還原）。

### 可動檔案範圍
`src/tw_screener/backtest/picks_outcome.py`、`cli.py`、`tests/`。

### 風險
- 翻轉樣本少、結論先當「觀察」不當規則；避免用單一旺宏案例過度歸納。

---

## 驗收（整份規劃書）

```bash
uv run tw-screener picks record --week current   # PO1
make pick-outcome                                # PO2：分層命中率 × α/β
uv run tw-screener picks outcome --diff          # PO3：翻轉解剖
make test && make lint && make typecheck
```

## 執行順序
**PO1 → PO2 → PO3**。PO1 不先做，後兩者無輸入。PO2 是本份主結論來源，PO3 借 PO2 的
報酬序列做個案解剖。**與規劃書 07（軌跡欄）有依賴**：PO3 的翻轉前兆若含價量軌跡，需 07 先落欄，
否則 PO3 先只用既有近端籌碼窗，軌跡欄待 07 完成後補。
