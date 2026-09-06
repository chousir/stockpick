# 07 — CLI 與 Makefile 規格

> 本檔描述**現行**指令面（與 `Makefile`、`src/tw_screener/cli.py` 對齊）。
> 各功能的規格與方法論見對應文件：輪動 docs/12、CP 補漲 docs/13、Dashboard docs/17-dashboard-spec、
> 回測/閉環 docs/proposals/03・05。

## 設計原則

- **Makefile = 一級入口**，所有常用操作都有對應 `make` 指令
- **底層 CLI = Typer 寫的 `tw-screener` 命令**，給進階使用（`uv run tw-screener <sub-app> <cmd>`）
- Dashboard（本機讀 `reports/` 的 HUD）與 cron 排程（`scripts/fetch_cron.sh`）為**選配**，
  已落地——早期「Phase 1 不做」清單已成歷史，見 docs/17-dashboard-spec.md 與 README §12

## Makefile 指令總覽

```makefile
# ─── 主要（每週真的在用的三個；其餘進階，help 不列）──
help:            ## 列主要三指令（裸打 make 即顯示；week / pick-outcome / dash-dev）

# ─── 環境 ───────────────────────────────────────
init:            ## 初始化資料夾結構（首次 clone 後跑）
sync:            ## uv sync 安裝/更新依賴
clean:           ## 清掉暫存（__pycache__/.pytest_cache 等，不刪 reports/）
clean-cache:     ## 清掉 7 天以上的 data/cache、data/raw 檔案
deep-clean:      ## ⚠️ 清掉所有 data/cache 和 data/raw（保留 reports/）

# ─── 開發 ───────────────────────────────────────
test:            ## 跑全部測試（離線，fixtures/合成資料）
test-unit:       ## 排除 integration 標記
lint:            ## ruff check
typecheck:       ## mypy
fmt:             ## ruff format

# ─── 資料 ───────────────────────────────────────
fetch-twse:      ## 增量抓 TWSE 日線、法人、月營收、產業別、官方估值比
fetch-stock STOCK_ID=2330:        ## 抓單檔個股完整資料
fetch-tdcc:      ## TDCC 集保戶股權分散表（大戶持股比，規劃書 02 D3）
fetch-candidates-history:         ## 對本週入選股補抓 STOCK_DAY 歷史（MONTHS=13 預設）
fetch-institutional-history:      ## 回補近 N 日上市＋上櫃三大法人（DAYS=20 預設）
backfill-universe-history:        ## ⏳ 一次性回補全部次產業成員日線（~1500 檔，8-12 小時）
build-themes:    ## 爬 Yahoo 概念股 merge 進 config/concepts.yaml（DRY=1 預演）

# ─── 選股 ───────────────────────────────────────
screen STRATEGY=d_quality_leader: ## 跑單一策略
screen-all GROUP=defg:            ## 手動跑 Goodinfo D/E/F/G（2026-08-28 起不再被 week 自動呼叫，docs/31 §20.6）
screen-dry STRATEGY=…:            ## 預演（不打網，只組 URL；＝ screen run --dry-run）
doctor:          ## Goodinfo 健康檢查（被擋/改版→exit 1；規劃書 02 D1）

# ─── 分析 ───────────────────────────────────────
group:           ## 族群分析 → group_analysis.md ＋ candidates_enriched.csv
audit-concepts:  ## 清查 concepts.yaml 無價成員（只報告不改檔）

# ─── 報告 ───────────────────────────────────────
report STOCK_ID=2330:  ## 單檔個股報告（有 API key 完整分析，否則資料草稿）
week-check:      ## 產物完整性檢查（規劃書 05 F4；week 已內含，可單獨重跑）

# ─── 完整流程 ────────────────────────────────────
week GROUP=defg:   ## 完整週流程（十步，見下節）
weekend GROUP=defg: ## week ＋ git commit/push 結果（無新檔跳過 commit）

# ─── 回測 / 研究軌 ───────────────────────────────
backtest-strategies: ## 回測 D/E/F/G 入選後勝率/報酬/回撤 vs 大盤（規劃書 03 V1）
pick-outcome:        ## pick 閉環：分層命中率×α＋偽陰性帳＋停損延遲帳（規劃書 05 F1、委託書 M3.1）
rotation:            ## 次產業資金輪動報表（docs/12）
rotation-calib:      ## R2 起漲點回測校準（研究軌，docs/12 §2.4）
cp-value-candidates: ## B3 個股 CP 補漲候選（生產軌，docs/13）
cp-value-calib:      ## B2 個股起漲事件回測（研究軌，docs/13）
cp-value-valuation:  ## C1 個股相對 PE 估值表（docs/13 §C1）

# ─── Dashboard（docs/17-dashboard-spec）──────────
dash-install:    ## 裝前後端依賴（uv sync ＋ npm install，首次一次）
dash-dev:        ## FastAPI(:8000) ＋ Vite(:5173) 開發模式
dash-build:      ## 前端 build → frontend/dist
dash:            ## 單一 FastAPI 服務 build 後前端＋API（:8000）
dash-test:       ## 後端 webapp 路由 pytest
```

## `make week` 十步流程

```
① fetch-twse                    日線/法人/月營收/產業別/官方估值比 增量入快取
② fetch-institutional-history   回補近 20 日上市＋上櫃法人（斷檔自動補齊）
③ fetch-tdcc                    集保大戶持股比（容錯：TDCC 異常不擋，大戶欄退化 null）
④ doctor                        Goodinfo 健康檢查（只診斷不擋；2026-08-28 起預設流程不呼叫 Goodinfo）
⑤ screen-f-local + screen-redesign-local   F（本地等價，source=local）＋ F2'/G1/G2/G4/G5/L6（本地未驗證式）→ screen_result_*.csv。舊 Goodinfo D/E/G 軟退場（docs/31 §20.6）
⑥ fetch-candidates-history      對命中股聯集補抓 13 個月日線
⑦ rotation                      次產業輪動（容錯：失敗不擋主流程）
⑧ cp-value-candidates           個股 CP 補漲候選（容錯：失敗不擋）
⑨ group                         族群分析 → group_analysis.md ＋ candidates_enriched.csv
⑩ week-check                    產物完整性檢查：容錯步驟若無聲失敗，這裡點名 WARNING
```

## CLI（底層）

`tw-screener` 命令：root 指令 ＋ 10 個 sub-app。

```bash
# root
tw-screener hello / version
tw-screener serve [--reload]          # Dashboard 後端（docs/17-dashboard-spec）

# data — 資料抓取與快取維護
tw-screener data fetch-twse / fetch-tdcc / fetch-stock STOCK_ID
tw-screener data fetch-institutional-history [--days 20]
tw-screener data fetch-candidates-history [--week WEEK] [--months 13]
tw-screener data backfill-otc-history / backfill-universe-history [--months 13]
tw-screener data build-themes [--dry-run]
tw-screener data prune-cache          # 依 settings.cache 保留窗清舊快取（無對應 make target）

# screen — 選股
tw-screener screen run STRATEGY [--dry-run]
tw-screener screen run-all --group defg
tw-screener screen doctor
tw-screener screen run-local STRATEGY # 不打 Goodinfo，純官方資料本地篩選（f_value_rebound=source=local；g1/g2/g4/g5/l6/f2=docs/31 §4/§7.2 新設計候選，source=local_unvalidated，docs/02）

# analysis — 族群分析
tw-screener analysis group / leaders [--week WEEK]

# report — 個股報告與產物檢查
tw-screener report stock STOCK_ID [--week WEEK]
tw-screener report batch              # 多檔批次
tw-screener report check              # ＝ make week-check

# sector — 次產業輪動（docs/12）
tw-screener sector universe [--list|--audit]
tw-screener sector flows --week current [--dry]
tw-screener sector rotation / calibrate

# cp — 個股 CP 補漲（docs/13）
tw-screener cp candidates / calibrate / valuation

# backtest / picks — 驗證閉環（規劃書 03 V1、05 F1）
tw-screener backtest strategies
tw-screener picks sync --week 2026-Www          # 解析 pick.md 尾端區塊整批落底帳（主流程）
tw-screener picks record --week 2026-Www --stock XXXX --layer core   # 單檔補記
tw-screener picks outcome [--diff]

# market / portfolio — 大盤 regime 與組合體檢（規劃書 03 V2、V3）
tw-screener market regime
tw-screener portfolio check [--include-candidates]

# market washout — M2 投降洗盤偵測（docs/27 §1）：反向 flag，不進 regime 分數、不改燈色/排序
tw-screener market washout            # 印四子項讀數＋「已求值 N 項中觸發 M 項」
tw-screener market washout --save     # 另 append 一列進 washout_history.parquet（同日冪等）

# market macro-risk — M8 宏觀窄橋（docs/27 §2）：讀輸入包的 macro_risk_latest.yaml
tw-screener market macro-risk                  # 最新週；印三態＋patch-6 消費結論＋披露 yaml 片段
tw-screener market macro-risk --week 2026-W32  # 指定週
# 三態：ok（依 gate 影響姿態/新倉）／missing／stale／invalid（後三者一律降級為註記、不擋流程）

# market macro — 總經燈號：外生風險水位（docs/25 M-Macro1）
tw-screener market macro [--refresh]   # ＝ make macro；--refresh 略過快取強抓 FRED
```

每個指令都有 `--help` 顯示參數。

## 典型使用情境

### 情境 A：每週的完整流程

```bash
make week GROUP=defg           # 十步一鍵跑完
open reports/2026-Www/group_analysis.md

# 把 6 類報告貼給 Claude（docs/11 prompt）→ pick.md（含尾端機器可讀區塊）
# pick.md 定稿後整批寫底帳（餵 pick 閉環；單檔補記用 picks record）：
uv run tw-screener picks sync --week 2026-Www

make report STOCK_ID=2330      # 對 picks 每檔產深度報告
make weekend GROUP=defg        # 或：week ＋ commit/push（已含空 commit 守衛）
```

### 情境 B：盤中發現一檔有興趣

```bash
make report STOCK_ID=2330
```

### 情境 C：每季校準與回測（皆已實作）

```bash
make backtest-strategies       # 策略層：D/E/F/G 勝率/報酬/回撤 → research/strategy_backtest/
make pick-outcome              # pick 層：分層命中率×α＋偽陰性帳＋停損延遲帳 → research/pick_outcome/
make rotation-calib            # 輪動訊號門檻重校準 → research/rotation/
make cp-value-calib            # 個股起漲事件回測 → research/cp_value/
```

## 參數覆寫

- **Makefile 變數**（`make` 命令列傳入）：`GROUP`、`STRATEGY`、`STOCK_ID`、`DAYS`、`MONTHS`、`DRY=1`。
- **環境變數**：`ANTHROPIC_API_KEY`（設了 → `make report` 直接產完整分析；沒設 → 資料草稿）。
- 其餘參數（限速、快取 TTL、門檻、路徑）一律改 `config/settings.yaml`，**不支援**環境變數覆寫。

## 輸出格式約定

CLI 輸出用 `rich` 美化：錯誤紅色、警告黃色、成功綠色；進度逐策略/逐步驟印出。
報表產物一律落在 `reports/YYYY-Www/`（gitignore，個人分析產物留本地）。
