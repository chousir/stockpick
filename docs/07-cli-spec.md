# 07 — CLI 與 Makefile 規格

## 設計原則

- **Makefile = 一級入口**，所有常用操作都有對應 `make` 指令
- **底層 CLI = Typer 寫的 `tw-screener` 命令**，給進階使用
- 不在 Phase 1 做 GUI / Web Dashboard

## Makefile 完整指令

```makefile
# ─── 環境 ───────────────────────────────────────
init:           ## 初始化資料夾結構（首次 clone 後跑）
sync:           ## uv sync 安裝/更新依賴
clean:          ## 清掉暫存（不刪 reports/）
clean-cache:    ## 清掉 7 天以上的 cache
deep-clean:     ## ⚠️ 清掉所有 data/ 和 cache（保留 reports/）

# ─── 開發 ───────────────────────────────────────
test:           ## 跑全部測試
test-unit:      ## 只跑單元測試（離線、快）
test-integration: ## 跑整合測試（會打網，慢，需注意 rate limit）
lint:           ## ruff check
typecheck:      ## mypy
fmt:            ## ruff format

# ─── 資料 ───────────────────────────────────────
fetch-twse:     ## 增量抓 TWSE 日線、法人、月營收
fetch-stock STOCK_ID=2330:  ## 抓單檔個股完整資料
update-all:     ## 跑 fetch-twse + 補抓本週需要的個股

# ─── 選股 ───────────────────────────────────────
screen STRATEGY=a_breakout: ## 跑單一策略
screen-all:     ## 跑全部三組策略
screen-dry STRATEGY=a_breakout: ## 預演（不打網，只組 URL 給看）

# ─── 分析 ───────────────────────────────────────
group:          ## 跑族群分析，產出 group_analysis.md（觀察段留空）
leaders:        ## 只跑領頭羊判斷

# ─── 報告 ───────────────────────────────────────
report STOCK_ID=2330:        ## 產單檔個股報告（用 Claude API）
report-batch:                ## 讀本週 group_analysis 推薦清單批次產
report-list STOCKS="2330,3034": ## 產指定清單

# ─── 完整流程 ────────────────────────────────────
week:           ## 完整週流程：fetch-twse + screen-all + group
weekend:        ## 等同 week，但會 commit 結果到 git

# ─── 回測 / 績效 ─────────────────────────────────
backtest-strategies: ## 三個月後跑：看三組策略的歷史勝率
review-watchlist:    ## 列出觀察清單目前績效
```

## 典型使用情境

### 情境 A：每週六早上的完整流程

```bash
cd ~/tw-stock-screener
git pull                       # 若用 git 同步多機

make week                      # 一鍵：抓 TWSE + 三策略選股 + 族群分析

# 30 秒後完成，打開：
open reports/2026-W21/group_analysis.md

# 看完族群分析，挑出 5-10 檔
claude                         # 開 Claude Code session
> 分析本週 2330, 3034, 2454, 8069, 6505
# Claude Code 自動產出 5 份報告

# 看報告，決定 watchlist
$EDITOR watchlist/active.md

# commit
git add reports/ watchlist/
git commit -m "W21 weekly analysis"
git push
```

### 情境 B：盤中發現一檔有興趣，深度分析

```bash
make report STOCK_ID=2330
# 或
claude
> 分析 2330
```

### 情境 C：三個月後檢驗策略

```bash
make backtest-strategies
# 輸出：
# reports/_meta/strategy_performance_2026Q2.md
# - 策略 A：勝率 58%，平均 4 週報酬 +3.2%
# - 策略 B：勝率 64%，平均 8 週報酬 +5.8%
# - 策略 C：勝率 71%，平均 24 週報酬 +8.4%（含股息）
```

## CLI（底層）

`tw-screener` 命令支援的子命令：

```bash
tw-screener init
tw-screener data fetch-twse [--from DATE] [--to DATE]
tw-screener data fetch-stock STOCK_ID
tw-screener screen run STRATEGY [--dry-run] [--force]
tw-screener screen run-all
tw-screener analysis group [--week WEEK]
tw-screener analysis leaders [--week WEEK]
tw-screener report stock STOCK_ID [--week WEEK]
tw-screener report batch [--week WEEK] [--top N]
tw-screener cache clean [--days N]
tw-screener backtest strategies [--from DATE] [--to DATE]
```

每個指令都有 `--help` 顯示參數。

## 設定覆寫

`Makefile` 指令支援環境變數覆寫：

```bash
# 強制不讀 cache
FORCE=1 make screen STRATEGY=a_breakout

# 改 sleep 秒數（debug 時）
GOODINFO_INTERVAL=10 make screen STRATEGY=a_breakout

# 用不同 settings 檔
SETTINGS=config/settings.test.yaml make screen STRATEGY=a_breakout
```

## 輸出格式約定

CLI 輸出用 `rich` 美化：

```
$ make screen-all
┌─ 跑策略 a_breakout ────────────────────────┐
│ URL 組裝完成                                │
│ 讀快取... 命中（剩 18h 過期）               │
│ 解析中... 找到 18 檔                        │
│ 寫入 reports/2026-W21/screen_result_a.csv  │
└────────────────────────────────────────────┘

┌─ 跑策略 b_growth_institutional ────────────┐
│ URL 組裝完成                                │
│ Sleep 3.4s (rate limit)                    │
│ HTTP GET... 200 OK (1.2s)                  │
│ 解析中... 找到 12 檔                        │
│ 寫入 reports/2026-W21/screen_result_b.csv  │
└────────────────────────────────────────────┘
...
```

錯誤輸出用紅色，重要警告用黃色，成功用綠色。

## 不做的事（Phase 1）

- ❌ Web Dashboard
- ❌ TUI（terminal UI 互動式選單）
- ❌ Slack/LINE 通知
- ❌ 排程（cron）—— 留給使用者自己設

理由：先把核心流程做穩，這些都是錦上添花，且加上去後維護成本高。
