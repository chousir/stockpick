# 08 — Milestones（給 Claude Code 依序執行）

> **這份是 Claude Code 的執行藍圖。**  
> 一次只做一個 milestone，做完停下等使用者驗收，**不要連續執行**。

每個 milestone 都包含：
- **目標**：這階段要達成什麼
- **成功標準**（Success Criteria）：怎樣算做完
- **可動檔案範圍**：Claude Code 只能改這些（Surgical Changes 原則）
- **驗收指令**：使用者跑哪個指令來驗
- **預期成果物**：產出哪些檔案

---

## M0：專案骨架（預估 30 分鐘）

### 目標
建立可運作的最小骨架：repo 結構、依賴、基礎 CLI、CI 設定。

### 可動檔案範圍
- `pyproject.toml`
- `Makefile`
- `.gitignore`
- `.devcontainer/devcontainer.json`
- `src/tw_screener/__init__.py`
- `src/tw_screener/cli.py`（只放 `hello` 子指令）
- `tests/test_smoke.py`
- `config/settings.yaml`

### 成功標準
- [ ] `uv sync` 無錯誤完成
- [ ] `make test` 通過（至少一個 smoke test）
- [ ] `uv run tw-screener hello` 印出 "Hello from tw-stock-screener"
- [ ] `make lint` 通過
- [ ] `make init` 建立 `data/`、`reports/`、`logs/`、`watchlist/` 資料夾

### 驗收指令
```bash
make sync && make init && make test && make lint && uv run tw-screener hello
```

### 注意
- **不要動 `docs/`**。docs 是規格，由使用者維護。
- **不要動 `CLAUDE.md`**。
- pyproject.toml 的依賴照 `docs/01-environment.md` 列的最小集合，不要加額外的。

---

## M1：證交所 OpenAPI 資料層（預估 1.5 小時）

### 目標
能從證交所 OpenAPI 抓日線、法人買賣、月營收，存成 parquet。

### 可動檔案範圍
- `src/tw_screener/data/twse.py`
- `src/tw_screener/data/cache.py`
- `src/tw_screener/data/models.py`
- `src/tw_screener/cli.py`（加 `data fetch-twse`, `data fetch-stock`）
- `tests/data/test_twse.py`
- `tests/fixtures/twse/`（離線 JSON）

### 成功標準
- [ ] `make fetch-twse` 能成功抓最近 1 個交易日的全市場日線
- [ ] 資料存 `data/cache/twse/daily_YYYYMMDD.parquet`
- [ ] 重複呼叫不重抓（cache hit）
- [ ] `make fetch-stock STOCK_ID=2330` 能抓單檔近 60 天 OHLCV、近 12 月營收、近 20 日法人
- [ ] 測試覆蓋率（data/ 模組）≥ 70%

### 驗收指令
```bash
make fetch-twse
ls -la data/cache/twse/
make fetch-stock STOCK_ID=2330
uv run python -c "import polars as pl; print(pl.read_parquet('data/cache/twse/daily_*.parquet').tail())"
```

### 注意
- 使用 `httpx`，非同步可選但**不要強上 async**（簡單優先）
- 證交所 API 沒 token，但仍要加 User-Agent，間隔 1 秒
- 解析後的 schema 用 Pydantic 定義在 `models.py`

---

## M2：Goodinfo 三件套（預估 3 小時，最費工的 milestone）

### 目標
實作 url_builder、fetcher、parser，能用 YAML 策略觸發 Goodinfo 篩選並解析結果。

### 可動檔案範圍
- `src/tw_screener/screener/goodinfo/url_builder.py`
- `src/tw_screener/screener/goodinfo/fetcher.py`
- `src/tw_screener/screener/goodinfo/parser.py`
- `src/tw_screener/screener/goodinfo/__init__.py`
- `tests/screener/goodinfo/`
- `tests/fixtures/goodinfo/`（**手動先放好真實 HTML**）

### 成功標準
- [ ] 給定 `config/strategies/a_breakout.yaml`，`url_builder` 能組出可在瀏覽器打開且有結果的 URL
- [ ] `fetcher.get(url)` 第一次打網、第二次讀 cache（觀察 log 確認）
- [ ] `fetcher` 遇到「您的瀏覽量異常」HTML 時 raise `GoodinfoBlockedError`
- [ ] `parser.parse_screener_result(html)` 從 fixture 解析出 Polars DataFrame，欄位符合 `docs/04-screener-spec.md`
- [ ] 整合測試（離線）：YAML → URL → HTML（fixture） → DataFrame 全鏈路通
- [ ] 測試覆蓋率（goodinfo/ 模組）≥ 80%

### 驗收指令
```bash
make test-unit                          # 必過
uv run tw-screener screen dry --strategy a_breakout  # 看組出的 URL
# 手動把 URL 貼到瀏覽器確認有結果
# 然後跑實打：
make screen STRATEGY=a_breakout
cat reports/$(date +%Y-W%V)/screen_result_a.csv
```

### 注意（**最重要**）
- **強制遵守 `docs/02-data-sources.md` 的合規限速**
- `fetcher` 必須先實作 cache + rate limit + retry，**再實作打網**。先確保「不會被擋」的機制再去打。
- HTML 結構解析用「中文表頭定位」，**不要用 CSS index**。
- 開發過程中若需要新的 HTML 樣本，**先在瀏覽器手動下載存 fixture，不要重複打網**。

---

## M3：策略 Runner + 三組策略 YAML（預估 1.5 小時）

### 目標
實作 ScreenerRunner，跑出三組策略的 CSV，並支援 Makefile 的 `screen` / `screen-all`。

### 可動檔案範圍
- `config/strategies/a_breakout.yaml`
- `config/strategies/b_growth_institutional.yaml`
- `config/strategies/c_dividend_steady.yaml`
- `src/tw_screener/screener/runner.py`
- `src/tw_screener/cli.py`（加 `screen` 指令）
- `Makefile`（加 `screen`, `screen-all`, `screen-dry` 對應 target）
- `tests/screener/test_runner.py`

### 成功標準
- [ ] 三個 YAML 完全照 `docs/03-strategies.md` 規格
- [ ] `make screen STRATEGY=a_breakout` 產出 `reports/YYYY-Www/screen_result_a.csv`
- [ ] `make screen-all` 跑完三組，產出 3 個 CSV，總耗時 < 5 分鐘
- [ ] CSV 欄位符合 `docs/04-screener-spec.md` 規格
- [ ] `make screen-dry STRATEGY=a_breakout` 只組 URL 不打網

### 驗收指令
```bash
make screen-dry STRATEGY=a_breakout
make screen STRATEGY=a_breakout
make screen-all
ls -la reports/$(date +%Y-W%V)/
head reports/$(date +%Y-W%V)/screen_result_a.csv
```

### 注意
- 三個 YAML 內容必須跟 docs/03-strategies.md 一字不差
- Runner 收到 0 結果不算錯（市場大跌時 A 可能 0 檔），正常產空 CSV
- 結果 > 100 檔時印警告：「條件可能太寬鬆」

---

## M4：族群分析 + 領頭羊（預估 2 小時）

### 目標
讀三組 CSV + TWSE 價量，產出 `group_analysis.md`（觀察段留空，由 Claude 後補）。

### 可動檔案範圍
- `src/tw_screener/analysis/grouping.py`
- `src/tw_screener/analysis/leader.py`
- `src/tw_screener/analysis/indicators/` (MACD/RS 等，pure function)
- `src/tw_screener/report/group_report.py`
- `src/tw_screener/report/templates/group_analysis.md.j2`
- `src/tw_screener/cli.py`（加 `group`, `leaders` 指令）
- `Makefile`
- `tests/analysis/`

### 成功標準
- [ ] `make group` 在有三組 CSV 的前提下產出 `group_analysis.md`
- [ ] 報告結構符合 `docs/05-group-analysis.md` 規格
- [ ] 族群強度分數公式照規格實作，**權重從 settings.yaml 讀**
- [ ] 領頭羊分數公式照規格實作
- [ ] 「## 4. 觀察」段有明確 `<!-- TODO: Claude 補寫 -->` 標記
- [ ] 推薦深度分析優先順序前 5-10 檔正確排序

### 驗收指令
```bash
make group
cat reports/$(date +%Y-W%V)/group_analysis.md
```

### 注意
- 族群歸類錯誤是 data quality 問題，不用 100% 完美
- RS 計算需要 60 天歷史，M1 已有資料
- 不要在這 milestone 做「美股式」的回測，保持簡單

---

## M5：個股報告 Pipeline（預估 2 小時）

### 目標
能用 `make report STOCK_ID=2330` 產出單檔深度報告。Claude Code 互動模式同樣可觸發。

### 可動檔案範圍
- `src/tw_screener/report/data_fetcher.py`
- `src/tw_screener/report/builder.py`
- `src/tw_screener/report/prompts/stock_report.md`
- `src/tw_screener/cli.py`
- `Makefile`
- `tests/report/`

### 成功標準
- [ ] `make report STOCK_ID=2330` 產出 `reports/YYYY-Www/stocks/2330_台積電.md`
- [ ] 報告結構符合 `docs/06-report-spec.md`
- [ ] 報告內容**所有數字都來自抓回的資料**（檢查方式：故意改 cache 資料，重跑，報告數字應變動）
- [ ] 多方論點 3-5 點、空方論點 3-5 點、空方不少於多方
- [ ] 含「資料來源與時間」段
- [ ] 報告中**無**「目標價」、「強烈建議」、「飆股」等禁用字眼

### 驗收指令
```bash
make report STOCK_ID=2330
cat reports/$(date +%Y-W%V)/stocks/2330_台積電.md
# 人工審視：多空是否平衡、數字是否來自資料、有無禁用字眼
```

### 注意
- 這個 milestone 會用到 Anthropic API（呼叫 Claude），API key 從 `.env` 讀
- 失敗的 fallback：若 API 失敗，產出**只有資料區塊、無分析論述**的草稿，標記「待 Claude Code 互動補寫」
- 互動模式（直接在 `claude` session 中說「分析 2330」）由 CLAUDE.md 引導，不需額外 code

---

## M6：完整週流程 + Backtest 骨架（預估 1 小時）

### 目標
`make week` 一鍵跑完前面所有，並建立 backtest 模組骨架（先不實作，預留位置）。

### 可動檔案範圍
- `Makefile`（加 `backtest-strategies`；`week`, `weekend` 在 M5 已加）
- `.gitignore`（移除 `/watchlist/` 以便 `make weekend` 能 commit watchlist）
- `src/tw_screener/backtest/__init__.py`（skeleton）
- `src/tw_screener/backtest/strategies.py`（skeleton + docstring）
- `watchlist/active.md`, `watchlist/waiting.md`, `watchlist/closed.md`（範例）
- `tests/test_e2e.py`

### 成功標準
- [ ] `make week` 一鍵完成：fetch-twse → screen-all → group
- [ ] 整個流程在沒被擋的前提下 < 10 分鐘
- [ ] e2e 測試（用 fixture）通過
- [ ] backtest 模組有 placeholder（function 簽名 + raise NotImplementedError），三個月後才實作
- [ ] `make backtest-strategies` 印出未實作提示並 exit 1

### 驗收指令
```bash
make week
ls -la reports/$(date +%Y-W%V)/
# 應該看到：
#   screen_result_a_breakout.csv
#   screen_result_b_growth_institutional.csv
#   screen_result_c_dividend_steady.csv
#   group_analysis.md
#   stocks/                          # 由 make report 產生
```

### 注意
- 這 milestone 不要實作 backtest 邏輯，只搭骨架。要回測得有 3 個月以上資料才有意義。
- watchlist/ 的範本只放 3-5 個範例條目，使用者會自己維護。

---

## M7：文件最終化 + 第一次完整週使用（預估 30 分鐘）

### 目標
跑一次完整流程、回頭補 README、把實際遇到的坑寫進 docs。

### 可動檔案範圍
- `README.md`（補 quick start）
- `docs/99-troubleshooting.md`（新建，紀錄 M0-M6 遇到的坑）
- 各 docs/ 小修正

### 成功標準
- [ ] README 有可複製貼上的 5 步 quick start
- [ ] `docs/99-troubleshooting.md` 至少有 5 條常見問題與解法
- [ ] 跑一次 `make week`，把所有產出 commit 到 git
- [ ] 開一次 Claude Code session，請它產 3 份個股報告，確認流程順

### 驗收指令
使用者親自跑：
```bash
make week
claude
> 分析本週推薦清單前 3 名
```

### 注意
- 這個 milestone 主要是「真實使用」+「補洞」。
- 不要新增功能，只補文件和小修正。

---

## 整體預估

| Milestone | 估時 | 累計 |
|---|---|---|
| M0 | 30 min | 0.5h |
| M1 | 1.5h | 2h |
| M2 | 3h | 5h |
| M3 | 1.5h | 6.5h |
| M4 | 2h | 8.5h |
| M5 | 2h | 10.5h |
| M6 | 1h | 11.5h |
| M7 | 0.5h | 12h |

**總計約 12 小時**，分散在 3-7 天完成比較合理（每天 1-2 個 milestone）。

## 給 Claude Code 的提醒

- **每個 milestone 之間使用者要驗收**。你不需要主動跳到下一個。
- 遇到規格不清楚 → 問，不要猜。
- 遇到要加新依賴 → 問，不要直接加。
- 遇到要改 docs/ → 除非該 milestone 明確允許，否則先問。
- 完成後給簡潔報告：「改了哪些檔 / 加了哪些測試 / 怎麼驗 / 我認為的下一步」。
