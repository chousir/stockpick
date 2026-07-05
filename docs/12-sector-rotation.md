# 12 — 次產業資金流向輪動圖（規劃書 / Fable 5 大開發藍圖）

> **這份文件的定位**：給 Claude **Fable 5** 執行的「次產業資金流向輪動」開發藍圖。
> 沿用 `CLAUDE.md` Part 2.1 的 milestone 紀律——**一次只做一個 milestone，做完跑驗收、
> 停下等使用者說「下一個」，不要連續執行**。
>
> 本檔由 Opus 在 `feature/sector-rotation` 分支撰寫；Fable 5 接手逐 milestone 實作。
> 規格不清楚 → 問，不要猜；要加新依賴 → 問，不要直接加。

---

## 0. 一句話目標

在現有 D/E/F/G 選股 + 族群分析之上，新增一層 **「次產業資金流向輪動」**：
**先用歷史資料把參數回測校準（從起漲點回推），再用校準參數產出輪動地圖**
（對標 [cryptocity 台股 sector rotation map](https://www.cryptocity.tw/news/taiwan-stock-sector-rotation-map)，
核心鏡頭是**法人資金流向**，不是漲幅）。Phase 1 輸出文字 / CSV，UI 留 Phase 2。

「做完」的定義（success criteria，總綱）：
- 能回答「**本週法人資金正流入哪些次產業、哪些在流出**」，且門檻是歷史校準出來的、不是拍腦袋。
- 能回答「**我的命中 / 觀察 / 持有清單，落在資金流向象限的哪裡**」（有沒有參與到下一棒）。
- 全程可離線重跑（吃本地快取），不違反 Goodinfo 合規限速。

---

## 1. 為什麼做、跟現有東西的差異

### 1.1 對標 cryptocity：資金流向是「領先鏡頭」
漲幅是**落後鏡頭**（已經漲完才看得到）。法人資金淨流入通常**領先價格**，
所以 rotation map 的箭頭畫的是「資金往哪流」，用來抓**下一棒**，不是追已經噴的。

### 1.2 與現有 Section 2.8 輪動雷達的關鍵差異
| 面向 | 現有 `grouping.py` 的 `lead_score`（2.8 雷達） | 本規劃的輪動引擎 |
|---|---|---|
| 宇宙 | **只看本週被篩中的候選股** → 選擇偏誤 | **全次產業成員籃子**（無偏） |
| 時間 | 單週快照 | **法人資金時間序列**（近 1 年） |
| 訊號 | 外資/量能 breadth（家數比） | **法人淨額時間序列**（流入動能 / 力度 / ΔRank） |
| 參數 | 權重手設 | **歷史起漲點回測校準** |
| 定位 | — | 校準後的參數**回頭升級 2.8 雷達**（補強，不取代） |

> **設計原則（呼應 CLAUDE.md Simplicity / Surgical）**：不重寫 `grouping.py`。
> 新引擎是獨立模組，產出校準參數；2.8 雷達的整合放最後一個 milestone，且以「並列對照 →
> 再決定是否替換」的方式漸進，不一次動到主流程。

---

## 2. 方法論

### 2.1 分析單位（兩層 + 一個選配）
1. **主：次產業**＝`config/concepts.yaml` 的手標 label（**排除** `concept_themes` 清單裡的概念主題）。
   例：記憶體模組、IC 設計、晶圓代工、網通設備組件、散熱、CCL…。這是使用者要的「次產業」粒度。
2. **輔（粗層交叉驗證）：TWSE 28 類**＝`industry_YYYYMM.parquet` + `otc_industry_YYYYMM.parquet`（全市場覆蓋）。
   當次產業籃子成員太少（< 門檻）時，退回 28 類看資金流向是否一致。
3. **選配：概念主題**＝`concept_themes`（5G / AI / 衛星…）。第二層，**首期不做**，列入 R6。

### 2.2 次產業籃子建構
- **成員**＝該 label 的所有 `stock_id`（用 `is_etf_or_warrant` 過濾 ETF/權證，沿用 `grouping.py` 既有判斷）。
- **籃子報酬**＝成員**等權**日報酬序列（首期等權；市值加權需全市場歷史市值，目前無 → 列已知限制）。
- **資料源**＝`data/cache/twse/daily_all_*.parquet`（`date, stock_id, close, volume`，全市場 ~252 日）。

### 2.3 資金流向訊號（**主訊號**）
每個次產業、每個交易日，對成員加總法人淨額（`institutional_*` 上市 + `institutional_otc_*` 上櫃合併，
欄位 `foreign_net / trust_net / dealer_net`）：

| 衍生訊號 | 定義 | 直覺 |
|---|---|---|
| `net_flow_5d / 20d` | 近 N 日成員法人淨額加總 | 資金近期淨流入規模 |
| `flow_momentum` | 本週淨流入 − 前週淨流入 | 資金**在加速**還是減速 |
| `flow_breadth` | 成員中法人淨買超的家數 / 成員數 | 是雨露均霑還是單檔灌水 |
| `flow_concentration` | 次產業淨流入 / 籃子成交額 | **資金力度**（小產業同額流入 = 力道更猛，避免大產業稀釋） |
| `flow_rank_delta`（ΔRank） | 次產業按 `net_flow` 排名的週對週變化 | 沿用既有 `attach_rank_delta` 機制 |
| 外資 / 投信 拆分 | 上述各項分 `foreign` / `trust`（自營雜訊大，僅參考） | 外資＝趨勢、投信＝短打 |

> 主判斷邏輯（cryptocity 式）：**資金流入 × 價格未漲 ＝ 輪動下一棒候選**；
> **資金流出 × 價格仍高 ＝ 背離**（舊稱「出貨警訊」；2026-07 校準：前瞻超額仍為正、非賣訊）。象限化見 §5。

### 2.4 起漲點定義（回測校準用・R2 的核心）
對每個次產業的**等權報酬指數**：
- **起漲點**＝從近 `M` 日低基期起算、`N` 日內籃子累積漲幅 ≥ `X%`（`X / N / M` 為**待校準參數**）。
- **校準目標**：找出「在起漲點**前 / 當下**，哪些資金流向訊號（§2.3）的**哪個門檻**，
  **領先命中率最高、誤報最低**」。例如：「外資 `net_flow_5d` 站上次產業自身近 1 年的 80 百分位，
  且 `flow_momentum > 0`，平均領先起漲 X 個交易日、命中率 Y%、誤報 Z%」。
- **產出**：每個訊號的最佳門檻 + 領先天數分布 + 命中/誤報統計 →
  寫進 `config/settings.yaml` 的 `rotation` 段，供生產軌（R3）使用。

### 2.5 為什麼以法人資金為主、漲幅為輔
呼應使用者指示與 cryptocity：漲幅當**確認/驗證**（起漲點的定義），
法人資金當**預警/主訊號**（找下一棒）。兩者分工，不混為一談。

---

## 3. 資料盤點（已具備 / 缺口）

| 資料 | 路徑 | 狀態 | 用途 |
|---|---|---|---|
| 全市場日線 | `daily_all_*.parquet`（close/volume・~252 日） | ✅ | 籃子報酬、起漲點 |
| 三大法人（上市） | `institutional_*.parquet`（foreign/trust/dealer net） | ✅ | 資金流向主訊號 |
| 三大法人（上櫃） | `institutional_otc_*.parquet`（同 schema） | ✅ | 同上，需與上市合併 |
| 全市場產業 | `industry_YYYYMM` + `otc_industry_YYYYMM` | ✅ | 28 類粗層交叉驗證 |
| 次產業成員 | `config/concepts.yaml`（1893 行手標） | ✅ | 次產業籃子 membership |
| 候選股 13 月日線 | `stock_day_*.parquet`（2752 檔×月） | ✅ | 補洞、個股位階 |

**缺口 / 限制（誠實標明）**：
- **全市場歷史市值**：無 → 籃子**首期等權**，市值加權列已知限制（R3 旁註）。
- **篩選命中史只有 4 週**（W21–W24）：所以「1 年內命中策略」沒有 1 年的*篩選*史；
  **價量 / 法人史可回推 ~1 年**，命中宇宙首期較小（隨週累積變厚）。
- **EPS / 毛利率未快取**：與本功能無關，不在此處理。
- **未提交前置**：working tree 有一支 `src/tw_screener/data/twse.py` 的 `load_candidate_history`
  schema 對齊修正（`daily_all_*` + `stock_day_*` 合併）。本功能會用到多源歷史載入，
  **R0 把它收進來並補測試**。

---

## 4. 「測試用 / 實作用」兩套（對應決策 Q2）

| 軌 | 宇宙 | 參數 | 輸出 | 進主流程？ |
|---|---|---|---|---|
| **研究/校準軌（測試用）** | **全次產業成員** | 掃描 / 搜尋中 | 校準報告 + 建議門檻 | ❌ 放 `research/`，不進 `make week` |
| **生產軌（實作用）** | 全成員算流向 + **命中/觀察/持有疊圖** | R2 校準好的固定值 | `sector_rotation.md / .csv` | ✅ 由 `make rotation` 產出 |

兩軌**共用同一套籃子 / 資金流向計算純函式**（`analysis/rotation.py`），差別只在「宇宙範圍」與
「參數來源」。這樣校準改了，生產自動受惠，不會兩份邏輯走鐘。

---

## 5. 輸出規格（Phase 1：文字 / CSV）

`reports/YYYY-Www/sector_rotation.md`：
1. **資金流入 Top N 次產業**：`net_flow_5d/20d`、`flow_momentum`、`flow_breadth`、
   `flow_concentration`、ΔRank、籃子 5 日報酬、價量位階（距均線）。
2. **資金流出 / 出貨警訊次產業**：流出 × 仍在高位的。
3. **輪動象限**（資金×位階的**狀態切片、非多空判詞**——2026-07 校準實測，見下）：
   - 流入 × 未漲 ＝ **起漲攔截區**（舊稱「下一棒」；含 **⚡貼低**精確變體＝距低 ≤
     `quadrant.next_precision_low_pct`，校準顯示 precision 顯著較高、代價觸發少領先短）
   - 流入 × 已漲 ＝ 主升續勢（CSV `quadrant` 值不變；**校準：前瞻超額最強象限**）
   - 流出 × 已漲 ＝ 背離（舊稱「出貨警訊」；**校準：前瞻超額仍為正、非賣訊**，不得單憑此欄剔除）
   - 流出 × 未漲 ＝ 冷卻 / 觀望
   - **排序 vs 象限分工（M1）**：Section 1 主表以**趨勢分**排序（強弱）、**不再列象限欄**
     （與「淨流/距低」冗餘、曾自打臉「趨勢#1 卻標出貨警訊」）；象限只活在 Section 3（狀態圖
     ＋校準註記）。★＝校準事件訊號。三軸分工＝**排序講強弱、象限講狀態、★講事件**。
   - **四象限可信度校準**：`make rotation-calib` 產「四象限可信度」段（各象限前瞻 10/20 日
     籃子報酬＋vs 大盤超額＋起漲攔截 lift；現行門檻 vs ⚡貼低變體）。結論寫回
     `settings.rotation.quadrant.calib_note`（報表 Section 3 印出）、每季重跑覆核。門檻
     `position_low_pct` / `next_precision_low_pct` 皆在 settings、不寫死。
   - **`flow_turn`（修法 3・資金轉向覆蓋）**：象限 x 軸只看 `net_flow_20d` 正負，會把
     「20 日累積仍負、但近 5 日已轉買」誤標純出貨、把「20 日正、但近 5 日轉賣」誤標純主升。
     故獨立標長/短窗符號背離：**🔺資金回流**（20d<0 且 5d>0，出貨警訊**可能緩解**，逐檔複核）／
     **退潮**（20d>0 且 5d<0，主升近端轉弱，留意）。出貨警訊段同時揭露 `5日淨流/動能`。
     與 `freshness`（加速度＝5 日 vs 前 5 日）區分：flow_turn 看「近端實際買賣方向」。
4. **「我的參與度」疊圖**：`watchlist/holdings.csv` + 本週命中股，標出各自落在哪個象限、
   所屬次產業資金方向（我有沒有參與到正在流入的次產業）。

`reports/YYYY-Www/sector_rotation.csv`：機器可讀，供下週算 ΔRank、未來 UI 吃。

**Phase 2 UI（R6・選配）— 已由投資戰情室 dashboard 吸收（2026-06-24 結案）**：
原規劃「吃 `sector_rotation.csv` 的本地靜態 HTML」已被 docs/17 投資戰情室 dashboard
（FastAPI + React/Vite，族群輪動頁讀 `sector_rotation.csv`/`theme_strength.csv`）取代，
**不再另做靜態 HTML**。R6 剩下的選配項僅「把輪動結論餵進 doc 11 挑股流程（picks 自動化）」與
概念主題第二層輪動，兩者仍待估、非必做。

---

## 6. Milestones（給 Fable 5 逐一執行）

> 每個 milestone 格式：**目標 / 可動檔案範圍 / 成功標準 / 驗收指令 / 預期產物**。
> 做完停下等驗收。R = Rotation，避開既有 M0–M7 編號。

### R0：前置與資料層對齊（預估 1h）
- **目標**：把未提交的 `twse.py` 修正收進來並補測試；建立「次產業成員 / 全市場產業 / 法人合併」三個讀取 API。
- **可動檔案**：`src/tw_screener/data/twse.py`（既有修正）、`src/tw_screener/analysis/sector_universe.py`（新增：
  concepts 次產業 membership + 28 類 mapping 載入）、`src/tw_screener/data/twse.py` 的法人合併 helper、
  `tests/analysis/test_sector_universe.py`、`tests/fixtures/`（離線小樣本）。
- **成功標準**：
  - [ ] `load_candidate_history` 修正有 happy-path 測試（daily_all + stock_day 合併不 ShapeError）。
  - [ ] `sector_universe.list_subindustries()` 回每個次產業 → 成員 stock_id（已濾 ETF/權證）。
  - [ ] `load_institutional_merged(date_range)` 回上市+上櫃合併、欄位 `foreign/trust/dealer_net`。
  - [ ] 測試覆蓋率（新模組）≥ 80%，全離線。
- **驗收指令**：`make test` + `uv run tw-screener sector universe --list | head`
- **產物**：可被 R1 呼叫的乾淨資料 API。

### R1：次產業籃子 + 資金流向計算（純函式，full universe）（預估 2h）
- **目標**：實作 §2.2 籃子報酬 + §2.3 資金流向所有衍生訊號，**純函式、可測**。
- **可動檔案**：`src/tw_screener/analysis/rotation.py`（新增）、`config/settings.yaml`（`rotation` 參數段骨架）、
  `tests/analysis/test_rotation.py`、`tests/fixtures/rotation/`。
- **成功標準**：
  - [ ] `compute_subindustry_baskets(...)` 回每次產業等權報酬日序列。
  - [ ] `compute_fund_flows(...)` 回 `net_flow_5d/20d / flow_momentum / flow_breadth / flow_concentration`（外資/投信拆分）。
  - [ ] ΔRank 復用既有 `attach_rank_delta`，不另寫一套。
  - [ ] 所有參數從 `settings.yaml` 讀，無寫死。
  - [ ] 純函式測試（用 fixture）通過。
- **驗收指令**：`make test` + `uv run tw-screener sector flows --week current --dry`（印前 10 流入次產業）
- **產物**：可被 R2（校準）與 R3（生產）共用的計算層。

### R2：起漲點回測校準（研究軌・**「先自己分析」那一步**）（預估 3h）
- **目標**：實作 §2.4，掃描參數，找出資金流向訊號的**最佳門檻 + 領先天數 + 命中/誤報**，產校準報告。
- **可動檔案**：`src/tw_screener/backtest/rotation_calib.py`（取代既有 skeleton 的一部分）、
  `src/tw_screener/cli.py`（`sector calibrate`）、`Makefile`（`make rotation-calib`）、
  `tests/backtest/test_rotation_calib.py`、輸出 `research/rotation/`（gitignore）。
- **成功標準**：
  - [ ] 能對 ~1 年歷史、每個次產業偵測起漲點（`X/N/M` 可調）。
  - [ ] 對每個資金流向訊號掃門檻，輸出（訊號, 門檻）→（領先天數中位, 命中率, 誤報率, 樣本數）表。
  - [ ] 產 `research/rotation/calibration_YYYYMMDD.md` + 建議寫入 `settings.yaml.rotation` 的數值。
  - [ ] **不**進 `make week` 主流程（研究軌獨立）。
- **驗收指令**：`make rotation-calib` → 人工審視校準報告是否合理（領先訊號 > 隨機、誤報可接受）。
- **產物**：校準報告 + 一組有依據的生產參數。**這是規劃書裡使用者要求「先自己用歷史資料分析」的落點。**

### R3：生產輪動引擎 + 報表（實作軌）（預估 2h）
- **目標**：用 R2 校準參數，每週產出 §5 的 `sector_rotation.md / .csv`（含四象限）。
- **可動檔案**：`src/tw_screener/report/rotation_report.py`（新增）、模板 `report/templates/sector_rotation.md.j2`、
  `src/tw_screener/cli.py`（`sector rotation`）、`Makefile`（`make rotation`）、`tests/report/test_rotation_report.py`。
- **成功標準**：
  - [ ] `make rotation` 產出 `sector_rotation.md` + `.csv`，象限分類正確。
  - [ ] 所有數字來自快取資料（改 cache 重跑、數字應變動）。
  - [ ] 報告**不下單一結論、不給目標價、不用禁用字眼**（沿用 CLAUDE.md Part 3.5）。
  - [ ] ΔRank 需上週 `sector_rotation.csv`，首週 null（誠實標 `*`）。
- **驗收指令**：`make rotation` + `cat reports/$(date +%Y-W%V)/sector_rotation.md`
- **產物**：可每週重跑的輪動地圖（文字/CSV）。

### R4：命中 / 觀察 / 持有疊圖 + 串接（預估 1.5h）
- **目標**：把 §5.4「我的參與度」疊上去，並接進流程。
- **可動檔案**：`rotation_report.py`（疊圖段）、`Makefile`（`make week` 末尾選配掛 `make rotation`）、
  `tests/report/`。
- **成功標準**：
  - [ ] `watchlist/holdings.csv` + `watchlist/watchlist.csv` + 本週命中股，逐檔標象限 + 次產業資金方向。
  - [ ] 沒維護 watchlist/holdings 時優雅略過該段（不報錯）。
  - [ ] `make week GROUP=defg` 後可選 `make rotation`，或合一指令。
- **驗收指令**：維護一份範例 watchlist → `make rotation` → 檢查疊圖段。
- **產物**：實作軌完成——「我有沒有參與到下一棒」一眼看到。

### R5：與現有 2.8 雷達整合（並列 → 漸進）（預估 1.5h）✅ 已完成
- **目標**：用 R2 校準參數升級 `grouping.py` 的 `lead_score`，**先並列對照**，確認再決定是否替換。
- **實作方式（與原案差異）**：不動 `grouping.py` 的 lead_score 計算（避免重算/重複邏輯），
  改由 `group_report.py` 讀**本週 `sector_rotation.csv`**（R3 產物）在 Section 2.8 末尾並列
  「輪動Rank」「資金象限」兩欄；`make week` 順序調整為 rotation → group。
- **兩鏡頭差異與使用時機（驗收紀錄）**：

  | | 2.8 雷達 lead_score（舊） | 輪動 Rank / 象限（新・R3） |
  |---|---|---|
  | 宇宙 | 本週篩中候選股（**有選擇偏誤**） | 全次產業成員（無偏、含未入選股） |
  | 訊號 | 外資/量能 breadth（單週快照） | 20 日法人資金流時間序列＋位階象限 |
  | 校準 | 權重手設 | R2 起漲點回測（★＝投信流訊號，lift 1.3-1.5） |
  | 用法 | 「**候選股**中誰要起跑」 | 「**全市場**資金往哪流」 |

  判讀組合：**兩者同強＝最強確認；雷達強/輪動弱＝只有篩中股動（窄；防單檔灌水）；
  輪動強/雷達弱＝資金已進但候選未跟上（更早期，回 candidates 找未入選成員）**。
- **沒先跑 `make rotation`** → 兩欄顯示 `—`（優雅降級）；概念股題材不在輪動宇宙、亦顯示 `—`。
- **是否完全替換 lead_score**：保留並列，由使用者長期對照後拍板（暫不替換）。

### R6（選配）：UI / picks 自動化（預估待估）
- **目標**：~~`sector_rotation.csv` → 本地靜態 HTML / dashboard~~；或把輪動結論餵進 doc 11 的挑股流程。
- **狀態（2026-06-24 更新）**：**UI 半已結案**——投資戰情室 dashboard（docs/17）已吸收
  「輪動視覺化」需求，不再做靜態 HTML。**剩餘選配**＝(a) picks 自動化（輪動結論餵 doc 11）、
  (b) 概念主題（5G/AI/衛星）第二層輪動；兩者仍待估、非必做。

---

## 7. 工程約束（沿用 CLAUDE.md，重申幾條本功能特別相關的）
- Polars / `httpx` / `loguru` / type hints / 純函式可測 / fixtures 離線。
- **所有參數進 `config/settings.yaml` 的 `rotation` 段**，不寫死門檻 / 路徑 / 次產業名。
- **不擅自加依賴**（畫圖 / UI 套件要先問）。
- 本功能**多半吃本地快取**，幾乎不打網；若要補抓歷史法人，沿用既有限速與快取。
- **不做**：市值加權（首期）、UI（首期）、自動下單、目標價 / 漲幅預測、單一結論。

## 8. 給 Fable 5 的執行提醒
- **一次一 milestone**，做完跑驗收、列完成清單、**停下等「下一個」**，不要連續執行。
- 規格不清 → 問；要加依賴 / 改既有 docs → 先問。
- 完成後給：「改了哪些檔 / 加了哪些測試 / 怎麼驗 / 我認為的下一步」。
- 起點：**R0**。R2 是「先自己用歷史資料分析、把參數跑出來」的核心步驟，別跳過直接套預設門檻。

---

> 撰寫：Opus（`feature/sector-rotation`）。執行：Fable 5。
> 本規劃書若與 `CLAUDE.md` 衝突，以 `CLAUDE.md` 為準。
</content>
</invoke>
