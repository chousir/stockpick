# 台股波段選股與分析系統 (TW Stock Screener)

> 半自動化的台股每週流程：選股 → 次產業資金輪動 → 族群分析 → AI 挑股 → 個股深度報告
> 個人工具，非投資建議。

## 一句話說明

每週把 1800 檔台股 → 篩成 ~150 檔候選 → 疊上「全市場資金往哪流」的輪動地圖 → AI 蒸餾出 5-10 檔
＋進場價/停損 → **你下決策**。

---

## 系統怎麼運作（全貌）

```
                    ┌─────────────── 資料層（全部本地 parquet 快取）───────────────┐
  TWSE/TPEX OpenAPI │ 日線 daily_*  法人 institutional_*(上市+上櫃)  月營收  產業別  官方估值比 valuation_ratios_* │
  Goodinfo（限速爬蟲）│ 策略篩選結果（YAML 條件 → URL → HTML → CSV）                │
  Yahoo 概念股       │ config/concepts.yaml 主題標籤（手標次產業＋自動爬概念股）      │
                    └──────────────────────────┬───────────────────────────────┘
                                               ▼
 make week GROUP=defg ＝ 以下五步串起來：
 ① fetch-twse                日線/法人/月營收/產業別/官方估值比(PE/PB/殖利率) 增量入快取
 ② screen-all GROUP=defg     Goodinfo 跑 D/E/F/G 四策略 → screen_result_*.csv（純快照）
 ③ fetch-candidates-history  對命中股聯集補抓 13 個月個股日線（MA60/量比/動能用）
 ④ rotation                  ★ 次產業資金流向輪動（全市場宇宙）→ sector_rotation.md/csv
 ④' cp-value-candidates      個股 CP 補漲候選＋C2 三重濾網 → cp_candidates.md（group Section 6 要讀）
 ⑤ group                     族群分析（候選股宇宙）→ group_analysis.md ＋ candidates_enriched.csv
                                               ▼
 手動：把報告貼給 Claude（docs/11 prompt）→ picks.md（精選進場清單）
 手動：make report STOCK_ID=XXXX → 個股深度報告
```

兩個分析宇宙刻意不同、互相校驗：

| | ⑤ 族群分析（group_analysis.md） | ④ 資金輪動（sector_rotation.md） |
|---|---|---|
| 宇宙 | **本週篩中的候選股**（精、有選擇偏誤） | **全次產業成員**（無偏、含未入選股） |
| 鏡頭 | 漲幅/breadth/法人（候選股之間比強弱） | 20 日法人資金流時間序列＋位階象限 |
| 回答 | 「篩中的股裡，哪群在跑、誰帶頭」 | 「全市場資金往哪流、下一棒可能是誰」 |

兩邊在 `group_analysis.md` **Section 2.8 並列對照**（雷達 lead_score × 輪動 Rank/象限）：
**同強＝最強確認；雷達強輪動弱＝只有篩中股在動（防單檔灌水）；輪動強雷達弱＝資金已進但
候選未跟上（更早期訊號）**。

## 何時跑 `make week`

- **推薦時段**：交易日收盤後 **15:00 起**（TWSE T86 法人 ~15:00 穩定，Goodinfo 也同步更新完成）
- **週六/日 / 週一早上跑也 OK**：自動對齊「最近一個交易日」，報告落在正確的 `reports/YYYY-Www/`
- **跨日重複跑**：第二次走 cache，不會重抓
- 詳見 [docs/02-data-sources.md](./docs/02-data-sources.md) 的「最早可用時點」表

## Quick Start（5 步）

```bash
# Step 1：首次 clone 後做一次
make sync && make init

# Step 2：每週跑一次（抓資料 → 篩選 → 資金輪動 → 族群分析）
make week GROUP=defg
# （def=D/E/F 不含 G；abc=A/B/C 經典三角，已列 legacy）

# Step 3：ProPicks 風格全清單分析（推薦，Claude 網頁對話）
#   貼 group_analysis.md + sector_rotation.md + candidates_enriched.csv
#   + holdings/watchlist_enriched.csv + 4 個 screen CSV
#   配合 docs/11-propicks-analysis.md 的範本 prompt → 產出 reports/Www/picks.md
cat reports/$(date +%Y-W%V)/group_analysis.md

# Step 4：對 picks.md 內每檔產個股深度報告（5-10 秒/檔）
make report STOCK_ID=2330

# Step 5：完成分析（依是否有 API key 分兩種模式）
#   有 ANTHROPIC_API_KEY → Step 4 已產出完整分析報告，直接讀
#   無 API key → Step 4 產出資料草稿；貼到 Claude 對話依範本補寫後貼回
```

**每週使用流程詳解 → [docs/10-sop.md](./docs/10-sop.md)**
**遇到問題（被擋、空資料、未分類等）→ [docs/99-troubleshooting.md](./docs/99-troubleshooting.md)**

---

## 指令總覽

| 指令 | 做什麼 | 何時用 |
|---|---|---|
| `make week GROUP=defg` | 完整週流程 ①~⑤ | **每週一次（主入口）** |
| `make weekend GROUP=defg` | week ＋ git commit/push 結果 | 想自動存檔時 |
| `make rotation` | 次產業資金輪動報表（單獨重跑） | 盤後想單看資金流向 |
| `make group` | 族群分析（單獨重跑，吃既有 CSV） | 改 concepts.yaml 後重產報告 |
| `make report STOCK_ID=2330` | 單檔個股深度報告 | picks 選出後逐檔深掘 |
| `make screen STRATEGY=d_quality_leader` | 跑單一策略 | 調策略 YAML 後測試 |
| `make screen-dry STRATEGY=…` | 只組 Goodinfo URL 不打網 | 驗證 YAML 條件 |
| `make rotation-calib` | ★ 起漲點回測校準（研究軌） | 每季重校準訊號門檻 |
| `make fetch-twse` | 增量抓日線/法人/月營收 | 通常不必單獨跑（week 含） |
| `make fetch-stock STOCK_ID=2330` | 抓單檔完整資料 | 臨時看一檔沒快取的股 |
| `make fetch-institutional-history DAYS=20` | 回補近 N 日法人 | 法人快取斷檔時 |
| `make build-themes` | 爬 Yahoo 概念股更新 concepts.yaml | 每月或新題材出現時（`DRY=1` 預演） |
| `make audit-concepts` | 清查 concepts.yaml 無價成員（不改檔） | 久久檢查興櫃/下市/誤標 |
| `bash scripts/fetch_cron.sh` | 盤後抓全市場資料（cron 用，見 §12） | 每交易日（排程或手動） |
| `make test` / `make lint` / `make typecheck` | 測試 / ruff / mypy | 開發時 |
| `uv run tw-screener sector universe --list` | 列出次產業宇宙與成員 | 檢查 concepts.yaml 覆蓋 |
| `uv run tw-screener sector universe --audit` | 列出近日無價的次產業成員 | 清 concepts.yaml 前先看 |
| `uv run tw-screener sector flows --week current --dry` | 終端機直接印資金流排名 | 不想開報表、快速看 |

---

## 功能詳解

### 1. 次產業資金流向輪動（`make rotation`）

對標[台股資金輪動圖](https://www.cryptocity.tw/news/taiwan-stock-sector-rotation-map)的核心功能
（[docs/12-sector-rotation.md](./docs/12-sector-rotation.md)）。與選股無關地掃**全市場**：
每個次產業（`concepts.yaml` 手標、46 個）的全部成員，加總上市+上櫃三大法人淨額，算出：

- **資金訊號**：5/20 日淨流（張）、flow_momentum（資金加速度）、breadth（淨買超成員比）、
  力度（法人淨買股數/成交股數＝集中度）、週對週 ΔRank
- **四象限**（資金軸＝20 日淨流正負 × 價格軸＝籃子距 60 日低點位階）：
  - 🟢 **下一棒**（流入×未漲）＝重點觀察
  - 🔵 主升續勢（流入×已漲）　🔴 出貨警訊（流出×已漲）　⚪ 冷卻觀望（流出×未漲）
- **★ 校準進場訊號**：投信 20 日資金流 z>1 且動能>0——不是拍腦袋，是用 1 年歷史、34 個
  起漲點回測校準出來的（觸發後 15 日內起漲命中率 ≈ 隨機 1.3-1.5 倍、中位領先 8-10 日）
- **我的參與度**：自動把 `watchlist/holdings.csv`、`watchlist.csv`、本週命中股逐檔標上
  所屬次產業的象限與資金方向——「我有沒有參與到下一棒」一眼看到

輸出 `reports/Www/sector_rotation.md`（人讀）＋ `.csv`（下週 ΔRank 與未來 UI 用）。
`make week` 已內含；單獨跑只要快取在就行（不打 Goodinfo）。

```bash
make rotation                                      # 產本週輪動報表
uv run tw-screener sector flows --week current --dry   # 終端機快速看前 10 流入
uv run tw-screener sector universe --list          # 次產業成員清單與 28 類對照覆蓋率
```

### 2. 起漲點回測校準（`make rotation-calib`・研究軌）

輪動訊號的門檻**從歷史資料回推**，不是手設（docs/12 §2.4）：

1. 對每個次產業籃子偵測「起漲點」＝低基期（距 60 日低 ≤3%）＋ 15 日內漲 ≥10%
2. 掃描所有資金訊號 × 門檻組合，統計：命中率（precision）、episode 覆蓋率（recall）、
   **lift（vs 隨機基率）**、領先天數
3. 產出 `research/rotation/calibration_YYYYMMDD.md`（gitignore，本地研究產物）
   ＋建議寫入 `settings.yaml` `rotation.entry_signal` 的數值

```bash
make rotation-calib                          # 用 settings 預設起漲定義
uv run tw-screener sector calibrate --x-pct 12   # 敏感度測試：只算強波段
```

**建議每季資料累積後重跑一次**，把新建議值更新進 `settings.yaml`（含校準日期註記）。
目前校準結論（2026-06）：`trust_flow_20d (z>1.0)+momentum` 最穩健；對照組敏感度
X=8% 時全訊號 lift→1.0，證明訊號只領先「有意義的波段」、不領先雜訊。

### 3. 選股篩選（`make screen-all GROUP=defg`）

YAML 驅動的 Goodinfo 條件篩選：`config/strategies/*.yaml` 定義條件 → 組 URL → 限速爬蟲
（≥3 秒間隔＋抖動、24h 快取、指數退避、concurrency=1，**合規底線見 docs/02**）→ 解析成 CSV。
新增策略只要寫 YAML 不用寫 Python（流程見 docs/03「新增策略的流程」）。

### 4. 族群分析（`make group`）

讀本週篩選 CSV ＋價量/法人快取，產 `group_analysis.md`：
Section 0 策略代號/除權息/總經事件、1 入選分布、2 族群強度排名（2.5 跨族群強勢股、
2.6 次產業強度、2.7 概念股題材、**2.8 輪動雷達＋全宇宙輪動並列**）、3 各族群前 3 名、
4 觀察、5 Claude 次產業深度分析請求（輪動雷達驅動）、6 Claude CP 補漲候選分析請求
（個股層・讀同夾 cp_candidates.md）；同時產 `candidates_enriched.csv`
（全候選股 × 技術/籌碼/估值/flags 排雷欄，**AI 挑股的主要宇宙**）。

### 5. AI 挑股（手動・docs/11）

跑完 week 後把報告貼給 Claude 網頁版，用 [docs/11-propicks-analysis.md](./docs/11-propicks-analysis.md)
的範本 prompt 產 `picks.md`：執行摘要（姿態＋可動作 3-5 檔＋分批進場價/MA60 停損）→
庫存/觀察決策 → 精選清單 → 訊號交集 → 市場節奏。**多空並陳、不下單一結論**。

### 6. 個股深度報告（`make report STOCK_ID=…`）

10 段固定框架（基本面/籌碼/技術/多方/空方/進場條件/不進場情境/族群定位/資料來源），
空方論點不得少於多方、禁目標價（CLAUDE.md Part 3）。有 `ANTHROPIC_API_KEY` 全自動；
沒有則產資料草稿、貼 Claude 對話補寫。

### 7. 主題模型維護（`config/concepts.yaml`）

每檔股票的「次產業＋概念股」多標籤（並存於 TWSE 官方分類）。**半自動**：
- **次產業（手標）**：電子細分（記憶體/記憶體模組/IC設計/封測/晶圓代工…）＋金融＋航運，
  直接編 `concepts:` 段。Yahoo 每主題只給 ~30 檔會截斷大次產業，故手標維持完整。
  **勿用外部批次匯入整碗覆蓋**（粗分類會併掉細桶）。
- **概念股（自動）**：`make build-themes` 爬 Yahoo（5G/AI/衛星…15 主題），只動概念股標籤、
  不動手標次產業；`DRY=1` 先看 candidate 再覆蓋。
- `make group` 末尾會列出「電子股未標次產業」提醒清單，增量補標。

### 8. 庫存／觀察清單（每次必分析）

```bash
# watchlist/holdings.csv   股號,買入價,股數,備註  ← 已 gitignore，不外流
# watchlist/watchlist.csv  股號,備註
```
維護後 `make week`（或 `make group`＋`make rotation`）自動：
- enrich 成 `holdings_enriched.csv`（＋報酬率/現值/MA60 停損價）、`watchlist_enriched.csv`
- 在 `sector_rotation.md`「我的參與度」逐檔標象限與資金方向
- Step 3 貼給 Claude 時走 prompt 任務 0：庫存給續抱/加碼/減碼/停利/停損、觀察給進場時機

### 9. 總經行事曆（`config/macro_calendar.yaml`）

FOMC/CPI/台股結算/法說等市場級事件 → `group_analysis.md` Section 0.6 → picks 的事件閘門
（事件落地前控倉）。內建排程全標 `verified: false`，**請依官方公告校對後改 true**、過期清掉。

### 10. 策略回測（未實作・骨架）

`make backtest-strategies` 目前印提示後 exit 1——需累積 3 個月以上 `reports/` 歷史
（預計 2026-08 後實作 D/E/F/G 入選後 N 週報酬/勝率統計）。注意這與 `rotation-calib` 不同：
後者回測的是**次產業資金訊號**（已實作），前者回測**個股策略**（待累積資料）。

### 11. PoC：主動式 ETF 持股（`poc/active_etf/`）

候選新訊號源（主動式 ETF 每日持股異動）。資料公開但後端 geo-fence 台灣，完整抓取需在
台灣本機跑，目前擱置、與主流程隔離（見 `poc/active_etf/README.md`）。

### 12. 每日資料排程（cron · 上櫃法人不可回補）

上櫃法人（TPEX）只供最新交易日、**缺日不可回補**（TPEX 端無歷史日期參數）。沒在當天抓那天的
上櫃資金流就永久缺、rotation 上櫃籃子被低估。`make week` 只在你想分析時跑、無法保證每交易日
都抓到，故用 cron 每交易日盤後固定跑 `fetch-twse`：

`scripts/fetch_cron.sh` 已備好（解析專案路徑、補 cron 精簡 PATH、`flock` 防重入、寫
`logs/cron_fetch.log`）。T86 法人收盤後約 90 分鐘、**15:00 起穩定**（docs/02），故排 18:00 穩妥；
crontab 加一行（交易日 18:00，盤後法人/月營收都已公布；依系統時區）：

```bash
crontab -e
# ↓ 路徑換成你的絕對路徑
0 18 * * 1-5  /bin/bash /path/to/stockpick/scripts/fetch_cron.sh
```

**WSL2 注意**：cron 預設不自動啟動，三選一——
① 每次開 WSL 後 `sudo service cron start`（關 WSL 就停）；
② `/etc/wsl.conf` 加 `[boot]` 段、設 `systemd=true` 後用 systemd 管 cron（重開 WSL 生效）；
③ 用 Windows 工作排程器呼叫 `wsl.exe -d <distro> -- /bin/bash /path/to/stockpick/scripts/fetch_cron.sh`（WSL 沒開機也會被喚起，最穩）。

漏抓自我檢查：`make rotation` / `sector flows` 會印「上櫃法人快取落後上市 N 個交易日」警告；
看到就手動 `make fetch-twse`（但只能補到最新日，更早的缺口補不回）。

---

## 報表產物導覽（`reports/YYYY-Www/`）

| 檔案 | 誰產的 | 內容 / 用途 |
|---|---|---|
| `screen_result_{d,e,f,g}_*.csv` | ② screen-all | 各策略入選快照（純 Goodinfo 12 欄，不被後處理改寫） |
| `screen_log.md` | ② screen-all | 各策略檔數＋交集統計 |
| `sector_rotation.md` / `.csv` | ④ rotation | **資金輪動地圖**：排名/四象限/★訊號/我的參與度；CSV 供下週 ΔRank |
| `cp_candidates.md` / `.csv` | ④' cp-value-candidates | 個股 CP 補漲候選＋C2 三重濾網（官方 trailing PE/PB；group Section 6 要讀）|
| `group_analysis.md` | ⑤ group | 族群分析主報告（Section 0-6） |
| `candidates_enriched.csv` | ⑤ group | 全候選股 × 完整欄位＝**AI 挑股主宇宙** |
| `holdings_enriched.csv` / `watchlist_enriched.csv` | ⑤ group | 庫存/觀察 enrich（有維護才產） |
| `theme_strength.csv` | ⑤ group | 2.8 雷達快照（供下週 ΔRank，不必貼給 Claude） |
| `picks.md` | 手動 Step 3 | AI 精選進場清單 |
| `stocks/XXXX_名稱.md` | make report | 個股深度報告 |

`reports/` 與 `research/`（校準報告）皆 gitignore——個人分析產物留本地。

---

## 策略體系

現行主流程 `make week GROUP=defg`，跑 **D/E/F/G** 四組（GROUP 必填、無預設）。

### D/E/F/G ProPicks 復刻組（現行主力）

D/E/F 對標 Investing.com ProPicks，共用「市值≥100 億」；**G 是 E 的逆勢孿生**：

| 策略 | 條件概念 | 對標 / 角色 | 持有時間 |
|---|---|---|---|
| **D 品質龍頭** | 市值≥100 億 + ROE≥15 + 配息 8 年 + 連 2 季淨利 | TWCH15 台灣晶片冠軍 | 6+ 月 |
| **E 成長動能** | 市值≥100 億 + 營收 YoY≥20 + 連 2 季淨利 + 均線多頭 | Tech Titans（順勢） | 1–3 月 |
| **F 價值反彈** | 市值≥100 億 + PER≤15 + 殖利率≥3 + 營收 YoY≥10 | Top Value Stocks | 3–6 月 |
| **G 成長拉回** | 同 E 基本面 + 季線上揚回踩（乖離 −5%~+10%）+ 量縮 | E 的逆勢孿生（低接） | 1–3 月 |

> **E 順勢、G 逆勢**：G 的拉回過濾在分析層用快取 MA60/量比計算；G 的 CSV 是基本面宇宙，
> 有效拉回命中見 `group_analysis.md` 標 G 者。

### A/B/C 經典三角（legacy）

早期實驗、已停用（檔案保留、`GROUP=abc` 可跑、新功能不接）。
詳細條件與設計取捨見 [docs/03-strategies.md](./docs/03-strategies.md)。

## 核心設計原則

1. **半自動，不全自動**：資料抓取、選股、輪動、報告骨架自動化；下單決策保留給人。
2. **資料層與分析層分離**：數字由程式抓、Polars 算；解讀由 Claude 寫；**缺資料標「未取得」不編造**。
3. **兩宇宙互相校驗**：候選股鏡頭（精）× 全市場資金鏡頭（無偏），Section 2.8 並列防選擇偏誤。
4. **參數有依據**：輪動訊號門檻由起漲點回測校準（lift/recall/領先天數），每季重校。
5. **累積式知識庫**：每週快取與快照累積，ΔRank、回測、策略勝率都靠時間變厚。

## 開發與測試

```bash
make test        # 全部測試（~350 個，全離線：fixtures/合成資料，不打網）
make test-unit   # 排除 integration 標記
make lint        # ruff
make typecheck   # mypy
```

- 模組對應：`src/tw_screener/{data,screener,analysis,report,backtest}/`，
  測試鏡像在 `tests/`。HTML 解析測試用 `tests/fixtures/` 離線樣本。
- 慣例：Polars（不用 pandas）、httpx、loguru、type hints、參數進 `config/settings.yaml`
  不寫死（[docs/09-coding-conventions.md](./docs/09-coding-conventions.md)）。

## 技術棧

**Python 3.11+**（uv）・**Polars**・**httpx**・**jinja2**（報表模板）・**typer**（CLI）・
**Claude**（個股報告生成＋全清單挑股＋本專案的 pair programmer）

## 文件導覽

| 文件 | 內容 |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | Claude Code 行為守則（工程原則 + 專案規則 + 分析師人設） |
| [`docs/00-architecture.md`](./docs/00-architecture.md) | 系統架構、資料流、模組職責 |
| [`docs/01-environment.md`](./docs/01-environment.md) | 環境設定、依賴管理、devcontainer |
| [`docs/02-data-sources.md`](./docs/02-data-sources.md) | Goodinfo 爬蟲規範、證交所 OpenAPI、合規限速 |
| [`docs/03-strategies.md`](./docs/03-strategies.md) | D/E/F/G 主策略 + A/B/C legacy、GROUP 機制、YAML 規範 |
| [`docs/04-screener-spec.md`](./docs/04-screener-spec.md) | 選股模組規格 |
| [`docs/05-group-analysis.md`](./docs/05-group-analysis.md) | 族群分析、族群內排名 |
| [`docs/06-report-spec.md`](./docs/06-report-spec.md) | 個股深度報告框架與輸出規範 |
| [`docs/07-cli-spec.md`](./docs/07-cli-spec.md) | Makefile 指令、CLI 介面 |
| [`docs/08-milestones.md`](./docs/08-milestones.md) | 建置期 M0-M7 milestones（已完成） |
| [`docs/09-coding-conventions.md`](./docs/09-coding-conventions.md) | 程式碼風格、命名、測試規範 |
| [`docs/10-sop.md`](./docs/10-sop.md) | **每週使用 SOP**（手動 Claude 對話模式、含範本 prompt） |
| [`docs/11-propicks-analysis.md`](./docs/11-propicks-analysis.md) | **ProPicks 全清單分析**（Step 3 完整 prompt + 流程） |
| [`docs/12-sector-rotation.md`](./docs/12-sector-rotation.md) | **次產業資金輪動**規劃書＋方法論（R0-R6、起漲點校準、四象限） |
| [`docs/99-troubleshooting.md`](./docs/99-troubleshooting.md) | 常見問題與解法 |

## 給 Claude Code 的使用指示

1. **先讀** `CLAUDE.md`；輪動相關開發另讀 `docs/12-sector-rotation.md`。
2. **每次只做一個 milestone**，做完停下等使用者驗收，**不要連續執行**。
3. **每個 milestone 完成時**：跑驗收指令、確認 success criteria、給「完成清單」。
4. **不確定的事先問**，不要自行假設。
