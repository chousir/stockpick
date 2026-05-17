# 台股波段選股與分析系統 (TW Stock Screener)

> 半自動化的台股每週選股 → 族群分析 → 個股深度報告流程
> 個人工具，非投資建議。

## 一句話說明

每週把 1800 檔台股 → 30 檔候選 → 5-10 檔個股深度報告 → 你下決策。

## Quick Start（5 步）

```bash
# Step 1：首次 clone 後做一次
make sync && make init

# Step 2：每週跑一次（抓資料 → 三組篩選 → 族群分析）
make week

# Step 3：讀族群分析報告，挑 5-10 檔感興趣的
cat reports/$(date +%Y-W%V)/group_analysis.md

# Step 4：產個股資料草稿（單檔，5-10 秒）
make report STOCK_ID=2330
# 或批次產前 5 檔
make report-batch

# Step 5：把 reports/YYYY-Www/stocks/2330_台積電.md 貼到 Claude 對話
# Claude 補完分析後，貼回覆蓋原檔
```

**每週使用流程詳解 → [docs/10-sop.md](./docs/10-sop.md)**  
**遇到問題（被擋、空資料、未分類等）→ [docs/99-troubleshooting.md](./docs/99-troubleshooting.md)**

## 核心設計原則

1. **半自動，不全自動**：資料抓取、選股、報告骨架自動化；下單決策保留給人。
2. **資料層與分析層分離**：數字由程式抓、由 Polars 算；解讀由 Claude 寫。
3. **三組互補策略**：A 波段啟動、B 法人成長、C 穩健存股。覆蓋攻擊/主力/防守三個象限。
4. **族群 + 領頭羊優先**：台股強族群帶動明顯，找對族群比找對個股重要。
5. **累積式知識庫**：每週結果存 Git，三個月後可回看策略勝率。

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
| [`docs/03-strategies.md`](./docs/03-strategies.md) | 三組策略詳細定義、YAML 規範 |
| [`docs/04-screener-spec.md`](./docs/04-screener-spec.md) | 選股模組規格 |
| [`docs/05-group-analysis.md`](./docs/05-group-analysis.md) | 族群分析、領頭羊判斷 |
| [`docs/06-report-spec.md`](./docs/06-report-spec.md) | 個股深度報告框架與輸出規範 |
| [`docs/07-cli-spec.md`](./docs/07-cli-spec.md) | Makefile 指令、CLI 介面 |
| [`docs/08-milestones.md`](./docs/08-milestones.md) | 7 個 milestone + 各自驗收 criteria |
| [`docs/09-coding-conventions.md`](./docs/09-coding-conventions.md) | 程式碼風格、命名、測試規範 |
| [`docs/10-sop.md`](./docs/10-sop.md) | **每週使用 SOP**（手動 Claude 對話模式、含範本 prompt） |
| [`docs/99-troubleshooting.md`](./docs/99-troubleshooting.md) | 開發與使用過程的常見問題與解法 |

## 給 Claude Code 的使用指示

1. **先讀** `CLAUDE.md` 與 `docs/08-milestones.md`。
2. **每次只做一個 milestone**，做完停下等使用者驗收，**不要連續執行**。
3. **每個 milestone 完成時**：跑驗收指令、確認 success criteria 達標、寫一段「本 milestone 完成清單」給使用者。
4. **不確定的事先問**，不要自行假設（Karpathy 第一原則）。
