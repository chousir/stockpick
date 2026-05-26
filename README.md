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

# Step 2：每週跑一次（抓資料 → 篩選 → 族群分析）
# 主流程：GROUP=defg（D/E/F/G ProPicks 復刻組＋成長拉回）
make week GROUP=defg
# （def=D/E/F 不含 G；abc=A/B/C 經典三角，已列 legacy）

# Step 3：ProPicks 風格全清單分析（推薦，需要 Claude Opus 網頁對話）
#   貼 group_analysis.md + candidates_enriched.csv + holdings/watchlist_enriched.csv + 4 個 screen CSV
#   配合 docs/11-propicks-analysis.md 的範本 prompt
#   產出 reports/Www/picks.md（任務0 庫存/觀察決策 + 精選進場清單 + 風險 + 居安思危）
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

## 維護 `config/concepts.yaml`（多標籤主題：次產業 + 概念股）

`concepts.yaml` 給每檔股票額外的主題標籤（**並存**於 TWSE 主族群、不取代）。`make week` 最後一步
`group` 會讀它，在 `group_analysis.md` 產出 **2.6 電子次產業 / 2.7 概念股強度排名** + 個股「主題」欄，
並請 Claude 判斷同主題叢集是否輪動領漲。**半自動維護，分兩部分：**

### A. 電子次產業 — 手動
- 直接編 `concepts.yaml` 的 `concepts:`：`"股號": 次產業` 或多標籤 `"股號": [次產業A, 次產業B]`。
- 為何手動：Yahoo 每類只給前 ~30 檔，會把成分 >30 的次產業（IC設計服務 ~115…）截斷，故維持手動、完整。

### B. 概念股題材 — 自動（一個指令）
```bash
make build-themes DRY=1   # 預覽 → config/concepts.candidate.yaml（不覆蓋正式檔）
make build-themes         # 正式 → merge 進 config/concepts.yaml（先自動備份 .bak）
```
- 從 Yahoo 抓「概念股」成分（衛星/5G/AI…）**merge 進 concepts.yaml**；**手動次產業原封不動**
  （靠檔內 `concept_themes` 清單分辨自動概念股，重跑只清舊換新）。
- **挑要哪些題材**：編 `config/settings.yaml` 的 `themes_build.concept_whitelist`
  （留空＝全部 101 個）。目前預設 15 個熱門趨勢（衛星/5G/AI/電動車/功率半導體…）；
  全部可選名單見 [docs/02-data-sources.md](./docs/02-data-sources.md)。
- **建議每月跑一次**（概念股成分會變動，太久不更新會失準）。白名單 15 個 ≈ 45 秒；全 101 個 ≈ 5–7 分。
- 限制：每題材取 Yahoo 前約 30 檔（領頭觀察、非全量）。

## 維護庫存／觀察清單（部位管理・每次必分析）

選股流程只找「新標的」；你**已持有的部位**與**私人觀察股**靠這兩份清單納入分析——
不受篩選宇宙限制、即使沒命中任何策略也會被逐檔分析。

```bash
# 編兩個檔（範例列改成你的）：
#   watchlist/holdings.csv   股號,買入價,股數,備註   ← 庫存（含成本，已 gitignore 不外流）
#   watchlist/watchlist.csv  股號,備註                ← 觀察清單
make group   # （或 make week）末尾自動 enrich，產出下列 2 檔（清單股無快取會自動抓 TWSE）
```

- 產出 `reports/Www/holdings_enriched.csv`（技術/籌碼/估值/月營收YoY/flags ＋ **報酬率%、現值、MA60停損價**）
  與 `watchlist_enriched.csv`（同欄、無買入價）。
- Step 3 把這 2 檔一起貼給 Claude → prompt 的**任務 0（必做）**：庫存給**續抱/加碼/減碼/停利/停損**、
  觀察給**進場時機**。
- 隱私：`holdings.csv`（含買入價）已在 `.gitignore`，不進 git；`watchlist.csv` 只有股號。

## 核心設計原則

1. **半自動，不全自動**：資料抓取、選股、報告骨架自動化；下單決策保留給人。
2. **資料層與分析層分離**：數字由程式抓、由 Polars 算；解讀由 Claude 寫。
3. **主策略組 D/E/F/G**：ProPicks 復刻組（多因子整合）＋成長拉回 G（E 的逆勢孿生），
   `make week GROUP=defg`；A/B/C 經典三角為早期 legacy（保留可跑、不再維護）。
4. **族群 + 領頭羊優先**：台股強族群帶動明顯，找對族群比找對個股重要。
5. **累積式知識庫**：每週結果存 Git，三個月後可回看策略勝率。

## 策略體系

現行主流程 `make week GROUP=defg`，跑 **D/E/F/G** 四組（無預設值，GROUP 必填）。

### D/E/F/G ProPicks 復刻組（現行主力）

D/E/F 對標 Investing.com ProPicks 三大主題策略，每組混合財務 / 成長 / 估值 / 動能多個因子；
**共用「市值≥100 億」**作為風格識別。**G 是 E 的逆勢孿生**，補抓 D/E/F 漏看的「回踩季線」優質成長股：

| 策略 | 條件概念 | 對標 / 角色 | 持有時間 |
|---|---|---|---|
| **D 品質龍頭** | 市值≥100 億 + ROE≥15 + 配息 8 年 + 連 2 季淨利 | TWCH15 台灣晶片冠軍 | 6+ 月 |
| **E 成長動能** | 市值≥100 億 + 營收 YoY≥20 + 連 2 季淨利 + 均線多頭 | Tech Titans（順勢） | 1–3 月 |
| **F 價值反彈** | 市值≥100 億 + PER≤15 + 殖利率≥3 + 營收 YoY≥10 | Top Value Stocks | 3–6 月 |
| **G 成長拉回** | 同 E 基本面 + 季線上揚回踩（乖離 −5%~+10%）+ 量縮 | E 的逆勢孿生（低接） | 1–3 月 |

> **E 順勢、G 逆勢**：E 抓已突破/均線多頭的強勢成長股，G 抓暫時失守均線、回踩上揚季線的同類股，
> 兩者技術狀態近乎互斥、共同覆蓋各種市況。G 的拉回過濾（季線上揚＋乖離帶＋量縮）在分析層用快取的
> MA60／量比計算，CSV 為基本面宇宙、有效拉回命中見 `group_analysis.md`。

### A/B/C 經典三角（legacy）

早期實驗、已停用（保留檔案、`GROUP=abc` 仍可跑，新功能不再接）：A 動能突破（MACD＋均線多頭）、
B 成長主力（營收＋淨利＋外資連買）、C 品質價值（ROE＋配息＋殖利率）。

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
| [`docs/03-strategies.md`](./docs/03-strategies.md) | D/E/F/G 主策略 + A/B/C legacy 定義、GROUP 切換機制、YAML 規範 |
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
