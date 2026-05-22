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
fetch-twse:     ## 增量抓 TWSE 日線、法人、月營收、產業分類
fetch-stock STOCK_ID=2330:  ## 抓單檔個股完整資料
fetch-candidates-history:   ## 對本週入選股聯集補抓 STOCK_DAY 歷史（動能 + MA60 斜率用，MONTHS=13 預設）

# ─── 選股 ───────────────────────────────────────
screen STRATEGY=a_breakout: ## 跑單一策略
screen-all GROUP=defg: ## 跑指定組策略（defg 主流程 / def / abc legacy）
screen-dry STRATEGY=a_breakout: ## 預演（不打網，只組 URL 給看）

# ─── 分析 ───────────────────────────────────────
group:          ## 跑族群分析，產出 group_analysis.md（觀察段留空）
leaders:        ## 只跑領頭羊判斷

# ─── 報告 ───────────────────────────────────────
report STOCK_ID=2330:        ## 產單檔個股報告（有 API key 完整分析，否則資料草稿）
report-batch:                ## 讀本週 group_analysis 推薦清單前 5 檔批次產

# ─── 完整流程 ────────────────────────────────────
week:           ## 完整週流程：fetch-twse → screen-all → fetch-candidates-history → group
weekend:        ## 等同 week，但會 commit 結果到 git（無新檔時跳過 commit）

# ─── 回測（骨架） ────────────────────────────────
backtest-strategies: ## ⚠️ 三個月後實作，目前 exit 1 + 提示
```

> `update-all`、`report-list`、`review-watchlist`、`tw-screener backtest` CLI 子指令**未實作**，
> 對應功能可用既有指令組合替代（多檔報告 → 多次 `make report STOCK_ID=XXXX`；
> watchlist 維護 → 手動編輯 `watchlist/*.md`）。

## 典型使用情境

### 情境 A：每週六早上的完整流程

```bash
cd ~/tw-stock-screener
git pull                       # 若用 git 同步多機

make week                      # 一鍵：抓 TWSE + 三策略選股 + 族群分析

# 完成後打開：
open reports/2026-W21/group_analysis.md

# 從第 5 節「推薦個股深度分析優先順序」挑 5-10 檔，逐檔產資料草稿
make report STOCK_ID=2330
make report STOCK_ID=3034
# 或一次跑前 5 檔
make report-batch

# 若有 ANTHROPIC_API_KEY：上面指令會直接產完整分析。
# 若無：產資料草稿，依 docs/10-sop.md 手動貼到 Claude 對話補寫。

# 看報告，決定 watchlist
$EDITOR watchlist/active.md

# commit
make weekend                   # 等同：make week + git add + commit + push（已含空 commit 守衛）
```

### 情境 B：盤中發現一檔有興趣，深度分析

```bash
make report STOCK_ID=2330
# 或
claude
> 分析 2330
```

### 情境 C：三個月後檢驗策略（功能尚未實作）

```bash
make backtest-strategies
# 目前：印「尚未實作」提示並 exit 1
# 預計 2026-08（累積 12+ 週歷史後）實作，產出範例：
# reports/_meta/strategy_performance_2026Q3.md
# - 策略 A：勝率 58%，平均 4 週報酬 +3.2%
# - 策略 B：勝率 64%，平均 8 週報酬 +5.8%
# - 策略 C：勝率 71%，平均 24 週報酬 +8.4%（含股息）
```

## CLI（底層）

`tw-screener` 命令支援的子命令：

```bash
tw-screener hello
tw-screener version
tw-screener data fetch-twse
tw-screener data fetch-stock STOCK_ID
tw-screener data fetch-candidates-history [--week WEEK] [--months 13]
tw-screener screen run STRATEGY [--dry-run]
tw-screener screen run-all
tw-screener screen dry STRATEGY
tw-screener analysis group [--week WEEK]
tw-screener analysis leaders [--week WEEK]
tw-screener report stock STOCK_ID [--week WEEK]
tw-screener report batch [--week WEEK] [--top N]
```

> `init`、`cache clean`、`backtest strategies` 子指令未實作（對應功能用 `make init`、
> `make clean-cache`、`make backtest-strategies` 即可）。

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
│ 寫入 reports/2026-W21/screen_result_a_breakout.csv │
└────────────────────────────────────────────┘

┌─ 跑策略 b_growth_institutional ────────────┐
│ URL 組裝完成                                │
│ Sleep 3.4s (rate limit)                    │
│ HTTP GET... 200 OK (1.2s)                  │
│ 解析中... 找到 12 檔                        │
│ 寫入 reports/2026-W21/screen_result_b_growth_institutional.csv │
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
