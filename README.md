# 台股波段選股與分析系統 (TW Stock Screener)

> 半自動化的台股每週選股 → 族群分析 → 個股深度報告流程
> 個人工具，非投資建議。

## 一句話說明

每週把 1800 檔台股 → 30 檔候選 → 5-10 檔個股深度報告 → 你下決策。

## 何時跑 `make week`

- **推薦時段**：交易日收盤後 **15:00 起**（TWSE T86 法人 ~15:00 穩定，Goodinfo 也同步更新完成）
- **週六/日 / 週一早上跑也 OK**：系統會自動對齊「最近一個交易日」（trading_date 錨點），
  報告會落在上週的 `reports/YYYY-Www/`，不會建空週目錄
- **跨日重複跑**：第二次走 cache，不會重抓
- 詳見 [docs/02-data-sources.md](./docs/02-data-sources.md) 的「最早可用時點」表

## Quick Start（5 步）

```bash
# Step 1：首次 clone 後做一次
make sync && make init

# Step 2：每週跑一次（抓資料 → 三組篩選 → 族群分析）
# GROUP 必填，二選一：abc=經典三角 / def=ProPicks 復刻
make week GROUP=abc
# 或
make week GROUP=def

# Step 3：ProPicks 風格全清單分析（推薦，需要 Claude Opus 網頁對話）
#   把 group_analysis.md + 3 個 screen_result_*.csv 貼到 claude.ai
#   配合 docs/11-propicks-analysis.md 的範本 prompt
#   產出 reports/Www/picks.md（5-7 檔進場清單 + 為何入選 + 風險 + 居安思危）
#   或：純人工讀 group_analysis.md 挑股（快但只看 top 10 機械排名）
cat reports/$(date +%Y-W%V)/group_analysis.md

# Step 4：對 picks.md 內每檔產個股深度報告（5-10 秒/檔）
make report STOCK_ID=2330
# 或批次產前 5 檔
make report-batch

# Step 5：完成分析（依是否有 API key 分兩種模式）
#   有 ANTHROPIC_API_KEY → Step 4 已產出完整分析報告，直接讀
#   無 API key → Step 4 產出資料草稿；把整份貼到 Claude 對話，依範本 prompt 補寫後貼回
```

**每週使用流程詳解 → [docs/10-sop.md](./docs/10-sop.md)**  
**遇到問題（被擋、空資料、未分類等）→ [docs/99-troubleshooting.md](./docs/99-troubleshooting.md)**

## 核心設計原則

1. **半自動，不全自動**：資料抓取、選股、報告骨架自動化；下單決策保留給人。
2. **資料層與分析層分離**：數字由程式抓、由 Polars 算；解讀由 Claude 寫。
3. **雙策略體系**：A/B/C 經典三角（學院派、單維度互補）vs D/E/F ProPicks 復刻組
   （AI 派、多因子整合），`make week GROUP=abc/def` 二選一切換（見下方說明）。
4. **族群 + 領頭羊優先**：台股強族群帶動明顯，找對族群比找對個股重要。
5. **累積式知識庫**：每週結果存 Git，三個月後可回看策略勝率。

## 兩組策略體系

每週 `make week` 必須選擇 `GROUP=abc` 或 `GROUP=def`，兩組互斥（無預設值）。

### A/B/C 經典三角（學院派・不重疊）

每組單一維度主導，覆蓋攻擊 / 主力 / 防守三個象限：

| 策略 | 條件概念 | 角色 | 持有時間 |
|---|---|---|---|
| **A 動能突破** | 週 MACD 翻多 + 5/10/20 均線多頭 + 流動性過濾 | 攻擊 | 1–4 週 |
| **B 成長主力** | 累計營收 YoY + 連續 2 季淨利 + 外資連買 | 主力 | 1–3 月 |
| **C 品質價值** | 近 4 季 ROE≥20 + 連續配息 10 年 + 殖利率≥4 | 防守 | 6+ 月 |

### D/E/F ProPicks 復刻組（AI 派・多因子整合）

對標 Investing.com ProPicks 三大主題策略，每組混合財務 / 成長 / 估值 / 動能多個因子；
**三組共用「市值≥100 億」**作為風格識別（A/B/C 是全市場視角）：

| 策略 | 條件概念 | 對標 ProPicks | 持有時間 |
|---|---|---|---|
| **D 品質龍頭** | 市值≥100 億 + ROE≥15 + 配息 8 年 + 連 2 季淨利 | TWCH15 台灣晶片冠軍 | 6+ 月 |
| **E 成長動能** | 市值≥100 億 + 營收 YoY≥20 + 連 2 季淨利 + 均線多頭 | Tech Titans | 1–3 月 |
| **F 價值反彈** | 市值≥100 億 + PER≤15 + 殖利率≥3 + 營收 YoY≥10 | Top Value Stocks | 3–6 月 |

### 兩組並用的優勢

- **不同市況用不同組**：盤整 / 分歧時用 ABC（單維度訊號清楚），趨勢明確時用 DEF（多因子過濾雜訊）
- **跨週交叉驗證**：本週 ABC 入選的股，下週切 DEF 也再次入選 → 雙重訊號 = 最強候選
- **思維風格互補**：A/B/C 是教科書式分工，D/E/F 是 ProPicks AI 從 100+ 因子蒸餾的結果，
  並用可避免單一框架盲點
- **累積後可回測**：兩組各自跑數個月後，可比較同檔股票在不同框架下的勝率與報酬

詳細條件與設計取捨見 [docs/03-strategies.md](./docs/03-strategies.md)。

## 技術棧

- **Python 3.11+**（uv 管理）
- **Polars**（資料處理，Rust 底層、速度足夠）
- **Rust + PyO3**（Phase 2 才導入，先預埋接口）
- **Claude Code**（個股報告產生 + 整個專案的 AI pair programmer）
- **GitHub + 本地開發**（Codespaces 為備援）

## 文件導覽

| 文件 | 內容 |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | Claude Code 行為守則（Karpathy 4 原則 + 專案規則 + 分析師人設） |
| [`docs/00-architecture.md`](./docs/00-architecture.md) | 系統架構、資料流、模組職責 |
| [`docs/01-environment.md`](./docs/01-environment.md) | 環境設定、依賴管理、devcontainer |
| [`docs/02-data-sources.md`](./docs/02-data-sources.md) | Goodinfo 爬蟲規範、證交所 OpenAPI、合規限速 |
| [`docs/03-strategies.md`](./docs/03-strategies.md) | A/B/C + D/E/F 兩組策略定義、GROUP 切換機制、YAML 規範 |
| [`docs/04-screener-spec.md`](./docs/04-screener-spec.md) | 選股模組規格 |
| [`docs/05-group-analysis.md`](./docs/05-group-analysis.md) | 族群分析、領頭羊判斷 |
| [`docs/06-report-spec.md`](./docs/06-report-spec.md) | 個股深度報告框架與輸出規範 |
| [`docs/07-cli-spec.md`](./docs/07-cli-spec.md) | Makefile 指令、CLI 介面 |
| [`docs/08-milestones.md`](./docs/08-milestones.md) | 7 個 milestone + 各自驗收 criteria |
| [`docs/09-coding-conventions.md`](./docs/09-coding-conventions.md) | 程式碼風格、命名、測試規範 |
| [`docs/10-sop.md`](./docs/10-sop.md) | **每週使用 SOP**（手動 Claude 對話模式、含範本 prompt） |
| [`docs/11-propicks-analysis.md`](./docs/11-propicks-analysis.md) | **ProPicks 風格全清單分析**（推薦的 Step 3，含完整 prompt + 流程） |
| [`docs/99-troubleshooting.md`](./docs/99-troubleshooting.md) | 開發與使用過程的常見問題與解法 |

## 給 Claude Code 的使用指示

1. **先讀** `CLAUDE.md` 與 `docs/08-milestones.md`。
2. **每次只做一個 milestone**，做完停下等使用者驗收，**不要連續執行**。
3. **每個 milestone 完成時**：跑驗收指令、確認 success criteria 達標、寫一段「本 milestone 完成清單」給使用者。
4. **不確定的事先問**，不要自行假設（Karpathy 第一原則）。
