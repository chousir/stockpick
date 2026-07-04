# 08 — Milestones（給 Claude Code 依序執行）

> **這份是 Claude Code 的執行藍圖。**  
> 一次只做一個 milestone，做完停下等使用者驗收，**不要連續執行**。
>
> ⚠️ **完成狀態以各 milestone 的「收官」敘事段為準**——checkbox（`[ ]`/`[x]`）未回頭維護，
> 不可靠（例：M0-M7 成功標準全未勾但建置期早已完成）。M-Dash 0–4 在 docs/17-dashboard-spec.md。

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
uv run tw-screener screen run a_breakout --dry-run  # 看組出的 URL
# 手動把 URL 貼到瀏覽器確認有結果
# 然後跑實打：
make screen STRATEGY=a_breakout
cat reports/$(date +%Y-W%V)/screen_result_a_breakout.csv
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
- [ ] `make screen STRATEGY=a_breakout` 產出 `reports/YYYY-Www/screen_result_a_breakout.csv`
- [ ] `make screen-all` 跑完三組，產出 3 個 CSV，總耗時 < 5 分鐘
- [ ] CSV 欄位符合 `docs/04-screener-spec.md` 規格
- [ ] `make screen-dry STRATEGY=a_breakout` 只組 URL 不打網

### 驗收指令
```bash
make screen-dry STRATEGY=a_breakout
make screen STRATEGY=a_breakout
make screen-all
ls -la reports/$(date +%Y-W%V)/
head reports/$(date +%Y-W%V)/screen_result_a_breakout.csv
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
- `src/tw_screener/analysis/indicators/` (MACD/RS 等，pure function)　※歷史紀錄：此目錄最終未建立，指標改以 Polars 向量化內嵌於 `momentum.py`/`stock_panel.py`（見 docs/00、規劃書 04 A6）
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
#   screen_log.md                    # 由 run_all 自動產生（統計 + 交集）
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

---

# 後續 Milestone（規劃書外擴充）

> M0–M7 為初版骨架。以下為上線後依實戰需求新增的 milestone，仍守「一次一個、做完停下驗收」。

## M-MH：多窗起漲偵測（Multi-Horizon Early-Detection）

> docs/13 CP 值研究 Phase D。與 BWIBBU 官方日 PE milestone **無硬相依**（動 pipeline 不同段），純優先序。

### 動機
2501 國建實證：20 日資金窗會被「舊賣單稀釋」——剛從賣轉買的買盤要 ~2 週才養肥到排得上名（6/5 才 +6444 張），錯過 6/1–6/3 起漲；短窗（外資 3/5d）5/28 就翻正、即時滿格顯示。20d 不是訊號不存在，是**反轉初期被自己的過去壓住、低估約兩週**。目標＝多窗讓偵測早 ~4 日且排序更準，**並存不取代** 20d。

### 總成功標準
- [ ] 拿 W24 既有資料重跑：2501 在 6/5（理想 6/1–6/3）即以「起漲」進埋伏/CP 清單前段；同檔 6/12 被分級「過熱/退潮」（短窗減速＋價量背離＋量縮），**不**標起漲。
- [ ] 校準證明多窗組合在歷史起漲樣本上**領先中位 ≥ 20d-z＋2 日、且 lift 不低於現任 foreign_flow_20d_z（1.69）**；否則不上線（見 Phase 2 中止條件）。
- [ ] 窗集合與所有門檻在 `config/settings.yaml`，零寫死。
- [ ] `make test` 綠；既有 5d/20d 欄與輸出不變（純加法）。

### 跨階段約束
- **並存非取代**：20d-z 全程保留，多窗為新增鏡頭。
- **不寫死**：窗集合、z 門檻、背離/量縮/距低帶全進 settings；連「用哪幾窗」都由 Phase 2 校準決定。
- **外科**：不動 E/F/G 的 YAML 篩選、不動 goodinfo 爬蟲、不加新依賴。

---

### Phase 1：因子層——兩窗一般化成窗集合（純加法）

**目標**：`compute_fund_flows` 從寫死 (短=5,長=20) 改吃窗集合 `[1,3,5,10,20]`；stock_panel 跟進，產各窗自身 z ＋跨窗加速度/背離因子。只增窗、不改計算語意。

**可動檔案範圍**
- `src/tw_screener/analysis/rotation.py`：`compute_fund_flows` 的 `(s, lw)` → `windows: tuple[int,...]`；欄名仍嵌實際天數，舊 `_5d/_20d` 為子集不變。
- `src/tw_screener/analysis/stock_panel.py`：z 與 momentum 對每窗算；新增背離因子（`flow_decel`＝短窗環比減速、`price_flow_div`＝N 日價漲幅 − N 日資金 z 成長）。
- `config/settings.yaml`：`rotation.windows` / `cp_value.windows`（預設 `[1,3,5,10,20]`）。
- `tests/`：斷言新窗欄存在、5d/20d 逐值與改前一致（回歸保護）。

**成功標準**
- [ ] panel 多出 1/3/10d 系列＋背離欄；5d/20d 逐值不變。
- [ ] 改 settings 窗集合，欄名誠實跟著變；`make test` 綠。

**注意**：這階段不動 rule 判讀、不動線上 cp_candidates 輸出。

---

### Phase 2：校準層——讓資料挑窗（★硬閘門，可能中止）

> **結果（2026-06-17）＝❌ 三 label 全未過閘、中止條件成立**：GATE 改判「早偵測力」（使用者拍板）後重跑——減量後短窗 lift 1.62–1.64 ≈ 20d 的 1.67（打平、非贏）、跨窗配對中位領先僅 0–1 日（~50% coin flip）。唯一真發現＝`short_only` ~30%（20d 漏掉、只有短窗抓到的起漲＝額外覆蓋非更早）。裁決與數據詳 [docs/13 Phase D](13-cp-value-research.md)。線上判讀不動；Phase 3 原樣不做。

**目標**：跑 `make cp-value-calib`（`tw-screener cp calibrate`），把多窗因子丟進既有 B2 起漲事件回測，量各窗/組合的 lift、領先中位日數、假動作率，跟現任 `foreign_flow_20d_z` 比。

**可動檔案範圍**
- `src/tw_screener/backtest/rotation_calib.py` / cp calibrate 路徑：掃描候選從單窗擴成窗集合（研究軌，不碰生產）。
- 產出 `research/cp_value/`（本地、不進 git）。

**成功標準（GATE）**
- [ ] 產出多窗 vs 20d 對照表（lift／領先中位／假動作率）。
- [ ] **過閘判定**：存在組合「領先中位 ≥ 20d-z＋2 日 **且** lift ≥ 1.69 **且** 假動作率不顯著惡化」→ 進 Phase 3、勝者寫 settings。
- [ ] **中止條件**：無組合過閘 → 停，把「短窗在台股母體被假動作吃掉領先」結論寫 docs/13，**不改線上判讀**。

**注意**：起漲樣本太少（`min_triggers`）→ 信賴度低，標「資料累積後重校」，不強上。

---

### Phase 3：生產（窄做加值欄；Phase 2 ❌ 後改版・使用者拍板）

> **原設計（趨勢分級取代「距低>15 硬擋」）作廢**：其前提＝短窗有領先力，Phase 2 證實不成立（短窗無系統性領先、減量沒贏 lift）。改採**窄做加值欄**——只把唯一真發現（`short_only` ~30%＝20d 漏掉、短窗抓到）surface 成低信心觀察欄，**不取代距低硬擋、不動候選 gating**。

**已做（2026-06-18）**
1. `cp_candidates.py`：`compute_early_inflow`（短窗 z>門檻 ＋ flow_decel≥0 未減速 ＋ 同 prefix 20d-z<上限＝長窗未追上）／`build_early_inflow_watch`（限庫存∪觀察、排除已是候選者）／`render_early_inflow_section`。
2. `cli.py` cp candidates：算早訊號→`render_cp_candidates_report(early_watch=...)`，md 末段加「短窗早訊號（庫存/觀察・低信心）」區塊。
3. `group_analysis.md.j2` Section 6 加第 5 點：早訊號低信心、**只當「已持有/觀察股資金異動、回頭查一眼」提醒，非新進場理由**。
4. `config/settings.yaml`：`cp_value.early_watch`（enabled/prefixes；z 門檻等沿用 `early_gate`）。

**成功標準**
- [x] `make cp-value-candidates` 跑通：早訊號區塊正確產出（實跑庫存/觀察本週 0 檔＝誠實，市場面 19 檔證邏輯有效）。
- [x] 守人設：低信心標註、非進場訊號、多空並陳、門檻全 settings。
- [x] `make test` 綠（441）、ruff/mypy 零淨增。

**注意**：守人設——早訊號是低信心觀察標註，不下買賣結論、不寫死門檻；校準已證未更早更準，僅補覆蓋。

### 工時估
| Phase | 估時 | 性質 |
|---|---|---|
| 1 因子 | ~2h | code＋回歸測試 |
| 2 校準 | ~1.5h | 回測＋判讀（可能中止）|
| 3 生產分級 | ~2.5h | 條件性，僅過閘才做 |

### 精修輪（2026-06-18・使用者 5 點建議）

承 Phase 3，使用者再提 5 點，對帳後做 1+2+3（量比當預測是反指標、PE 已在三重濾網，皆不重做）：

1. **持有/觀察健檢段**（點 1、4）：group_analysis 加 **Section 7**——叫 Claude 開 `holdings_enriched.csv`／`watchlist_enriched.csv` 逐檔健檢（續抱/收緊/停利/轉弱、接近進場/再等/剔除），**與命中策略同等深度**（命中尤其 E＝均線多頭天生已起漲、進場偏晚）；含「多鏡頭交集優先於單一聯集」（點 4）與「新鮮度過濾」（點 2：裸 5 日漲≈隨機、要搭剛離低+資金+貼低）。
2. **過熱-退潮警示**（點 5、量比正用）：`cp_candidates.py` `compute_overheat_warning`／`build_overheat_watch`／`render_overheat_section`——對稱早訊號，旗標「已漲到 60 日高位＋短窗 flow_decel<0 減速＋（量價背離 price_flow_div>0｜量縮 volume_z<0）」的庫存/觀察股（停利提醒）。**未校準啟發式、明標非賣訊**（使用者拍板先出啟發式）。settings `cp_value.overheat_watch`。
3. **多鏡頭確認**（點 4）：落在 Section 7 prompt 指示（標「幾個鏡頭確認」、交集排前），不另加表欄避免膨脹。

實跑：過熱-退潮本週命中庫存/觀察 5 檔（如 2492 華新科 近 5 日 +33%、距高 0%、減速+背離+量縮）。測試 +5＝446 綠、ruff/mypy 零淨增。詳 docs/13 Phase D 末。

### 退潮警示補校準（2026-06-18・把「先出的啟發式」拿去回測）

承精修輪點 2「未校準先出」，補做對稱 L1 的 **L4 頂部/出貨 label** 校準（`cp calibrate` 新增區塊，研究軌；`stock_calib.detect_top_episodes`／`scan_top_signals`／`render_top_calibration_report`，settings `cp_value.labels.top`＋`top_calib`，掃描沿用 `overheat_watch` 生產門檻＝直接驗生產規則）。**裁決＝維持低信心啟發式、不升級為賣訊**（2237 事件、基率 8.17%）：

- 生產 `★overheat`（高位＋減速＋背離｜量縮）lift **2.15**，**輸給裸『貼高』near_high 3.05、也輸法人賣超×高位 2.39**；背離三因子單獨幾乎無力（div 1.40／decel 1.36／量縮 1.08≈隨機）。
- **對稱 Phase 2 結論：位階在做工、背離因子不是驅動**（起漲端貼低、退潮端貼高皆然）。
- **汙染但書**：絕對下跌含大盤系統性回檔，near_high 高 lift 多半是 beta（高位股大盤回檔時跌最兇）、非個股出貨——故任何變體都不升級為賣訊；要分離需「相對大盤落後」label（選配、未跑）。
- 結論：`overheat_watch` 原樣不動（低信心停利-回查、非賣訊）。校準價值＝證實「不升級」是對的並記錄為何。測試 +5＝451 綠、ruff 全過、mypy 零淨增（既有 11 處與本次無關）。詳 [docs/13 Phase D 末](13-cp-value-research.md)。

---

## M-修法5：pick.md 盲點 5 修法（領頭羊起漲在強勢中被誤殺）

> 源自 [reports/2026-W25/pick_review.md](../reports/2026-W25/pick_review.md) 診斷：W25 半導體硬需求多頭週，系統交出 0 個半導體進場、把記憶體領頭羊整桶 exile。根因＝「只會在弱勢撿便宜、不會在強勢順勢確認」。**只改判讀/呈現層，不動篩選器邏輯、不增資料源。**

### 動機
`過熱`（距季線>40%）硬否決把確認上行週期的領頭羊全打死；四象限只看 20 日累積符號、漏掉近端資金轉向；`土洋對作` 對權值股小量反向誤殺；prompt 把「禁編數字」與「禁結構推理」綁死、連定性週期都不敢講。

### 成功標準
- [x] **修法 1+2**：`過熱` 改脈絡化雙閘＋`強勢領頭` 例外旗標＋買強勢進場階梯（`group_report.py` + `docs/11`）。
- [x] **修法 3**：sector_rotation 加 `flow_turn` 近 5 日資金轉向覆蓋（🔺資金回流／退潮 前哨標）（`rotation_report.py` + 模板 + `docs/11/12`）。
- [x] **修法 4**：`土洋對作` 加相對流通量門檻（弱邊張數 ÷ 20 日均量），解權值股小量反向誤殺（`group_report.py` + settings）。
- [x] **修法 5**：`docs/11` 開「結構/週期」窄門——可定性講週期、仍禁編數字、結論回三鏡頭、空方不少於多方。
- [x] 門檻全進 `config/settings.yaml`（`overheated_ma60_pct` / `strong_leader_yoy_pct` / `cross_trade_lots`），零寫死。
- [x] `make test` 綠（test_group_report +75、test_rotation_report +72）。

### 收官（2026-06-19）
5 修法全數實作，整段收成單一 merge bubble「Merge pick.md 盲點 5 修法 milestone」併入 `main`（原為 4 顆零散 merge，事後重整為 1 顆）。**注意：過去週的 `reports/*/pick.md` 不會回溯反映新旗標/讀法，須 W26+ 重跑才生效。**

---

## M-修法6：個股法人多窗揭露＋趨勢回撤（解 20 日法人標籤幻覺＋下跌反彈誤判健康回踩）

> 承診斷（reports/2026-W25 對帳）：W25 重跑後**仍**把緯創（外資5日 **−57,862**／20日 +43,553）、鴻海標「雙強買·教科書健康回踩」進核心/接近，實為「從高點摔 −8%、外資近端出貨的弱反彈」。根因＝(a) 個股法人欄純 20 日累計、**修法3 flow_turn 只修次產業沒延伸到個股**；(b) 趨勢階段只看 5 日動能（反彈）＋貼 MA20，**分不出健康回踩 vs 下跌反彈**。只改揭露/判讀層，不動篩選器。

### 驗證底氣（事件研究・250 交易日・相對大盤 alpha）
- 外資窗背離（20正5負）vs 一致買：未來 alpha 分不開（中位皆≈−0.2%）→ **只揭露、不設自動 gate**。
- 位階/趨勢：`回踩(MA20下)` alpha 中位最差（−0.31~−0.62%）、`延伸(>15%)` 最好 → **位階有預測力、值得判讀加權**（與 M-MH「位階當主訊號」獨立同證）。
- 故本修法＝「揭露事實＋判讀收嚴」，**不加硬性自動踢除**。

### 6a 法人多窗揭露
- `grouping.py`：institutional 除 20 日和外，加外資近 5/10 日累計（`foreign_net_5d/10d`，窗為模組常數比照 `_MOMENTUM_DAYS`）。
- `group_report.py _build_enriched_rows`：candidates 加 `foreign_net_5d_lots`/`foreign_net_10d_lots`（擺 `foreign_net_lots` 旁）；併入 `_CANONICAL_REUSE_FIELDS`，holdings/watchlist 一致。
- `docs/11`：候選欄說明補多窗；規則「外資 20 日與近 5 日符號背離 → 禁寫『雙強買』，須據實寫『20日累計+X、近5日−Y、投信撐』。揭露事實非自動扣分」。

### 6b 趨勢/回撤揭露＋趨勢階段收嚴
- `grouping.py`：加 `ret_10d`（`compute_n_day_return` n=10＋除息還原，比照 momentum_5d）。
- `group_report.py`：candidates 加 `ret_10d_pct`（擺 `momentum_5d_pct` 旁）。
- `docs/11`：趨勢階段規則——`回踩拉回` 必再分 **健康（ret_10d≥−3% 且外資近 5 日未轉賣）** vs **下跌反彈（ret_10d<−5% 或外資近 5 日大賣）→歸轉弱**，後者不得當健康買點/核心。

### 成功標準
- [ ] candidates_enriched.csv 出現 `foreign_net_5d_lots`/`foreign_net_10d_lots`/`ret_10d_pct` 三欄；20 日欄與既有輸出不變（純加法）。
- [ ] W25 重跑：緯創/鴻海近 5 日外資負值現形、ret_10d≈−8%，可被 docs/11 規則判為下跌反彈；南亞科/華邦電維持多窗一致買＋強勢領頭。
- [ ] holdings/watchlist enriched 同步有三欄（`_build_enriched_rows` 共用）。
- [ ] 窗集合語意（5/10）為模組常數；無硬性新 gate；守人設＝揭露＋判讀收嚴、不下買賣結論。
- [ ] `make test` 綠。

### 收官（2026-06-19）
6a+6b 全實作：`grouping.py` 加 `foreign_net_5d/10d`（外資近端窗）＋`ret_10d`（近10日報酬・除息還原）；`group_report.py` 三 CSV（candidates/holdings/watchlist 共用 `_build_enriched_rows`）加 `foreign_net_5d_lots`/`foreign_net_10d_lots`/`ret_10d_pct`＋併入 `_CANONICAL_REUSE_FIELDS`；`docs/11` 補 6a「20日 vs 近5日背離禁寫雙強買、揭露非扣分」＋6b「回踩拉回分健康(ret_10d≥−3%)/下跌反彈(<−5%或外資近端大賣→轉弱)」規則。**真實快取驗證（跑實際 group_stocks）**：緯創外資近5日 **−57,862**、ret_10d **−8.2%**（下跌反彈現形）；晶豪科三窗一致買、ret_10d −3.1%（健康）。`make test` **457 綠**（+2）、ruff/mypy 零淨增。**注意：W25+ 報告須重跑才反映新欄/讀法。**

---

## M-修法7：進場階梯 × 組合層修法（報告層・規劃書見 docs/14）

> 承外部 Part A 批評（2026-06-21 對帳 reports/2026-W25/pick.md＋docs/11）：進場階梯**最大注 40% 壓在最深的 T2≈MA60、停損價＝T2 進場價、低價股三階假精度、T3 懸空待手動確認**，組合層**只有單檔⅓上限、無因子簇上限**（3 銀行＋建材＋產險全押 PCE 利率事件），核心 vs 機會**籌碼主導標準不一**。全在 `docs/11` prompt（非程式寫死）＋少數計算欄。藍圖＝[docs/14](14-entry-ladder-portfolio-fix.md)；D1–D5 設計決策待使用者拍板。

### 7a 計算欄位先行（code・純加法，比照修法6）
- `momentum.py`：加 `compute_rolling_extrema`（每檔各視窗「最近 N 日收盤」min/max；原始收盤、不除息還原、與 close/MA 絕對價同口徑）。
- `grouping.py`：`_RANGE_WINDOWS=(20,60)` 模組常數；ret_10d 區塊後加 `low_20d`/`high_20d`/`low_60d`/`high_60d`（positional Series 對齊 stock_ids）。
- `group_report.py _build_enriched_rows`：四欄擺 `ma60_price` 旁、併入 `_CANONICAL_REUSE_FIELDS`（candidates/holdings/watchlist 一致）。
- [x] candidates_enriched.csv 出現四欄；既有欄逐值不變（純加法）；holdings/watchlist 同步。
- [x] 單元驗證：極值落在前 5 筆（60 日窗內、20 日窗外）→ low_60d=50/high_60d=200 vs low_20d=100/high_20d=180（窗區隔正確、low_60d≤low_20d≤high_20d≤high_60d）。
- [x] `make test` 綠（**462**，+5）、ruff/mypy 零淨增（餘 2 既有 grouping CJK E501、2 既有 float→int）。
- [ ] W26+ 重跑實測：抽查數檔 low/high 對得起日線（報告不回溯，須重跑才生效）。

### 7b 進場階梯重構（docs/11 prompt・吃 D1/D2/D3＋7a 欄位）｜✅ 完成
- `docs/11` 進場價位/訊號列：① 拉回股配比 **30/40/30 → 50/30/20（前重後輕，D1）**；T3 改用 `low_60d` 欄、不再「待 Goodinfo 確認」；**低價股退化**（MA20≈MA60 價差 <~2% → 兩階＋停損，首批 60%/加碼 40%，D3）；**回檔深度檢核**（「等回 MA20」距現價 >~8% → 改走買強勢階梯，D3）。
- `docs/11` 停損列：**MA60「收盤確認跌破（隔日未收復才出）」、不以盤中觸價為準（D2）**——進場批與停損脫鉤，最大注 T1 落最淺 MA20、不再與停損同價。強勢領頭例外（非 MA60、順勢移動停損）維持。
- `docs/11` candidates 欄說明：補 `low/high_20d/60d`（區間絕對價）可直接用、T3 不 defer。
- 純 docs/11 prompt，無程式變更；驗收剩 **W26+ 重跑**實測（報告不回溯）。

### 7c 組合層因子簇上限（docs/11 prompt・吃 D4）｜✅ 完成
- `docs/11` 任務 2 事件閘門段加「因子簇/族群上限」：**同一因子簇進核心至多 2 檔、合計 ≤ ~50% 已部署資金**，超過明標單點事件風險＋降最弱一檔。主要簇＝利率敏感（銀行+建材營造+產險+壽險），同理套記憶體鏈/AI 伺服器鏈。
- **門檻寫進 prompt 文字、不進 settings.yaml**（此檢核是分析師讀 CSV 次產業/主題欄的人工判斷、無程式取用，放 settings 無人讀；使用者 2026-06-21 認可）。純 docs/11、無程式變更；驗收剩 W26+ 重跑實測。

### 7d 分級一致性（docs/11 prompt・吃 D5）｜✅ 完成
- `docs/11` 核心 high-conviction 定義加「籌碼主導標準一致」：**核心外資主導或投信主導皆可進、須同向非土洋對作；投信主導（投信淨買>外資，如聯詠）必明標「投信主導、外資未領頭」但書並列觀察重點**——解「一邊用外資主導篩銀行、一邊讓投信主導股無但書進核心」標準不一。純 docs/11、無程式變更。

### 收官（2026-06-21）
M-修法7 四子項全完成並 push（分支 `fix/m7-entry-ladder`）：7a 計算欄 low/high_20d/60d（code，commit 3146ffd）＋7b 進場階梯重構（50/30/20 前重後輕·停損收盤確認脫鉤·低價股退化·回檔深度檢核，76b1f3d）＋7c 因子簇上限（利率敏感簇核心≤2檔/合計≤~50%，248ce61）＋7d 籌碼主導標準一致。**規劃書 docs/14、D1–D5 全採推薦版**。`make test` 462 綠（7a +5）、ruff/mypy 零淨增。**注意：W26+ 報告須重跑才反映新階梯/停損/簇上限/分級讀法（報告不回溯）。** Part B 研究三軌（買方主導度單調性／個股×族群交互／payoff·decay）另開規劃書與分支、不重跑已被 M-MH 校準否證的壓縮突破·多窗早訊號。

---

## M-Part B：起漲點研究三軌（研究軌・規劃書見 docs/15）

> 全研究軌、不碰生產（`cp_candidates.py` 不動）。引擎沿用 `backtest/stock_calib.py`＋`analysis/stock_panel.py`。
> 三軌＝T1 買方主導度單調性／T2 個股×族群交互／T3 payoff·decay 四件套；E1–E7 設計決策全採推薦版。

### B-P1 穩健度四件套（T3・code・純加法）｜✅ 完成
- `stock_calib.py` 加 `payoff_decay_table`（觸發日前瞻報酬分布：勝率/賠率/中位/超額 vs 全宇宙同窗）、`holdout_table`（時間 70/30 樣本外、lift 前後段對照）、`liquidity_table`（ADV 成交額硬化、lift 硬化前/後）、`render_robustness_report`，以及 helper `_select_jobs`（沿用主掃描條件子集）/`_forward_returns`/`_scalar`。既有函式簽名零變更。
- `config/settings.yaml` 加 `cp_value.robustness`（anchor_label=ambush・top_k=6・horizons=[5,10,20,40]・holdout_frac=0.7・adv_window=20・adv_min_amount=100 百萬）。
- `cli.py cp calibrate` 末段：錨定 ambush 主掃描合格前 6 名因子，產 `research/cp_value/calibration_*_robustness.md`＋三 CSV。
- 實跑（1375 檔×250 日）洞察：**edge 隨持有期累積**（trust_flow_10d 超額中位 5日 +0.2%→40日 +2.1%、賠率 1.46→1.99）、短窗近打平；**holdout 後段 lift ≥ 前段**（撐住樣本外，tiny-N z>2.0 後段 0 觸發＝誠實標不足）；**流動性硬化後 lift 反升**（1.81→1.93…＝edge 非小量股假象、可交易宇宙成立）。
- 驗收：`make test` 新增 6 測全綠（payoff/decay/holdout/流動性/render/空輸入）、ruff 乾淨、mypy 零淨增（cli.py 11 既有 sector_calibrate 錯不動）。**研究軌、不回溯生產讀法。**
- 註：`tests/screener/goodinfo/test_fetcher.py` 2 個快取新鮮度測試在 main HEAD 即失敗（日期/mtime 敏感、與本milestone無關）。

### B-P2 買方主導度單調性（T1）｜✅ 完成（裁決＝否證，維持 binary 旗標）
- `stock_panel.py` 純加 `dom_{long_window}d`＝(外資+投信長窗淨買)/(|外資|+|投信|)∈[−1,1]（D-E1 拍板「淨買集中度」；分母 0→null；連續化修法4 binary 土洋對作旗標）。既有欄零變更。
- `stock_calib.py` 加 `dom_monotonicity_table`（dom 分 5 分位＝D-E2 拍板，ordinal rank 切以避 ±1/0 重邊；各桶以「桶內全股日當觸發 × 全宇宙基率」算前瞻起漲 lift＋前瞻報酬中位；分全體/貼低/非貼低三層＝控制位階）、`dom_monotonicity_spearman`（dom vs 前瞻報酬 Spearman ρ，z=ρ·√(n−1) 大樣本近似顯著）、`render_dom_monotonicity_report`，helper `_dom_col`/`_spearman`/`_dom_strata`。
- `config/settings.yaml` 加 `cp_value.monotonicity`（n_buckets=5・fwd_window=20・z_sig=1.96；錨定 label 沿用 robustness.anchor_label=ambush・dom 窗＝面板 long_window）。`cli.py cp calibrate` 末段產 `calibration_*_monotonicity.md`＋2 CSV。
- **實跑（1375 檔×250 日）裁決＝否證**：①全體 Spearman ρ **+0.006**（z=2.94「顯著」純為 n=24萬 大樣本假象、效應量≈0；桶 lift 1.26→1.40→1.39→1.17→1.29 **峰在桶2、非單調**）；②控制位階後**貼低層 ρ=−0.013（z=−5.66，顯著為負）**、非貼低 ρ=+0.027——**控制位階即崩**（守 §D「位階在做工」反例）。→ **dom 連續分級無加值，維持修法4 binary 土洋對作旗標、記否證**（不升級分級因子）。
- 驗收：`make test` 新增 6 測全綠（panel dom 值/null＋table 桶遞增/spearman 顯著/空輸入/render）、ruff 乾淨、mypy 零淨增（58→58；既有 sector_calibrate 等錯不動）。**研究軌、不碰生產 `cp_candidates.py`。** goodinfo fetcher 2 測仍為 main HEAD 既有失敗（與本milestone無關）。
- 啟示：土洋「對作 vs 同向」的**程度**對前瞻起漲無系統性單調力——修法4 當初只做 binary 排雷旗標是對的，不需連續化。每季資料累積後可重校。
### B-P3 個股×族群 2×2 交互（T2）｜✅ 完成（裁決＝否證＋反向發現）
- D-E3/D-E4 使用者皆採推薦版（2×2 列聯；G＝面板 `rs_subind_{rs_window}d`）。**D-E4 語意註明**：rs_subind＝個股報酬−次產業籃報酬＝**個股相對其次產業的領先**（去族群 beta），**非族群絕對強度**；故 B-P3 測的是「資金進+貼低(S) × 個股在族群裡領先(G)」的超加性。
- `stock_calib.py` 加 `interaction_2x2_table`（S 高=冠軍 foreign_flow_20d_z>0.5 且貼低、G 高=rs_subind>0；四格各以「格內全股日當觸發×全宇宙基率」算前瞻起漲 lift）、`render_interaction_report`（含加法基準超加性＋S+ 內 G高 vs G低 兩比例 z 檢定＋裁決）、helper `_rs_subind_col`/`_two_prop_z`。settings `cp_value.interaction`；cli cp calibrate 末段產 `calibration_*_interaction.md`＋CSV。
- **實跑（1375 檔×250 日）裁決＝否證＋反向發現**：2×2 lift S+G+ **1.46** / S+G− **2.86** / S−G+ 0.58 / S−G− 2.18 → **S+ 內 G高(個股領先族群) 顯著「降低」起漲命中率（10.1% vs 19.7%，z=−16.89）**、S− 內同向。**否證「強族群裡強個股」交互**；**反向發現：個股『落後』其次產業(G低)才是補漲訊號**（重申 CP 補漲＝買未動/落後的）。→ **個股訊號已自足、不加族群領先確認**（加了反扣分）。註：§2「超加 +0.21」是邊際格高 lift 的算術假象（lift 非線性可加），以 §3 z 方向為準——**實作時修正裁決邏輯，把強顯著的負向 z 正確判為否證、非「未達顯著」**。
- 驗收：`make test` 新增 4 測全綠（table 超加、空輸入/缺欄、render 升級、render 否證反向）、ruff 乾淨、mypy 58→58 零淨增。全套 476 綠。**研究軌、不碰生產 `cp_candidates.py`。** goodinfo fetcher 2 測仍為 main HEAD 既有失敗（無關）。
- **Part B 三軌全收官**：T1 買方主導度單調性（B-P2）否證、T2 個股×族群交互（B-P3）否證＋反向、T3 穩健度四件套（B-P1）為橫切度量＝唯一存活的可複用工具。**結論：起漲端在台股母體，裸位階/資金進+貼低(冠軍 S) 已自足，連續化主導度與族群領先確認皆未加值**（守 docs/15 §6「沒贏與贏同等是有效產出」）。勝出軌＝無；不另開生產實作 milestone。每季資料累積後可重校。

## M-Part C：個股族群內落後度補漲因子（研究軌・規劃書見 docs/16，承 B-P3 反向發現）

> 全研究軌、不碰生產。承 B-P3「個股落後其次產業 rs_subind<0 起漲 lift 高於領先」正式立題：落後度是獨立補漲因子、還是只是位階（貼低）的化身？三層假說 H1 落後度單調／H2 控制位階仍有增量（關鍵閘）／H3 冠軍 S+ 內濾鏡 precision 增量。複用 B-P2 單調機制（`factor_monotonicity_*` 因子參數化）。

### C-P1 落後度單調 × 位階控制（H1+H2）｜✅ 完成（裁決＝進 C-P2）
- D-F1/D-F2/D-F3 使用者皆採推薦版（rs_subind 直接用、5 分位、貼低/非貼低二分）。
- **重構複用（不重造）**：把 B-P2 的 `_dom_strata`/`dom_monotonicity_table`/`dom_monotonicity_spearman` 一般化為 `_factor_strata`/`factor_monotonicity_table`（因子欄參數化、輸出 factor_min/median/max＋hits）/`factor_monotonicity_spearman`（加 `direction` 旗：increasing 或 decreasing），`dom_*` 退為薄包（B-P2 行為/測試零改）。
- **C-P1 專屬**：`render_laggard_monotonicity_report`＋helper `_laggard_lift_significance`；settings 沿用 `cp_value.monotonicity`（n_buckets/fwd_window/z_sig）＋自動偵測 rs_subind 欄；cli cp calibrate 末段產 `calibration_*_laggard.md`＋2 CSV。
- **★實跑暴露量尺陷阱（重要學習）**：先用 B-P2 的「factor vs 前瞻**報酬** Spearman」當裁決 → 全體 ρ=+0.007「否證」；但**桶 lift（起漲機率）強烈單調遞減**（全體 2.74→0.24、貼低 3.64→1.51、非貼低 0.99→0.01）。**根因：落後↑起漲機率，但領先股↑中位報酬，兩者分流**——Spearman 測到「報酬」非「起漲」，誤判否證。**修正＝裁決改用 on-target 的起漲 lift**（最落後桶 vs 最領先桶 hit-rate 兩比例 z），Spearman 退為診斷揭分流。
- **修正後裁決＝進 C-P2**：①全體落後桶起漲 lift 顯著高（z=+54.4）且單調遞減 ✅；②貼低（z=+27.2）/非貼低（z=+23.3）兩層皆然 ✅ → **落後度非僅位階代理、控制位階後仍在＝獨立起漲-機率加分**。**但帶警示**：領先股中位報酬反而高（+2.4% vs 落後桶 +1.5%），故 C-P2 須用 payoff/decay 四件套驗賺賠，不能只看 lift。
- 驗收：`make test` 新增 5 測（factor 一般化 decreasing/direction 旗/缺欄＋render laggard＋_laggard_lift_significance 極端桶；B-P2 dom 薄包測零改）、全套 481 綠、ruff 乾淨、mypy 58→58 零淨增。**研究軌、不碰生產。** goodinfo fetcher 2 測仍 main HEAD 既有失敗（無關）。

### C-P2 冠軍 S+ 內落後濾鏡 precision 增量＋賺賠驗證（H3）｜✅ 完成（裁決＝**升級・首個全勝軌**）
- D-F4 採推薦版（S+全體 vs S+且落後）。`stock_calib.py` 加 `laggard_filter_precision`（冠軍 S+ 觸發切 S+全體/S+且落後/S+且領先，evaluate_triggers 各算 lift＋落後vs領先兩比例 z）、`render_laggard_filter_report`；`payoff_decay_table` 加 `extra_conditions`/`name_suffix`（落後濾鏡疊上冠軍 job 算賺賠）。cli cp calibrate 末段產 `calibration_*_laggard_filter.md`＋CSV。settings 沿用 interaction/robustness（無新設定）。
- **實跑裁決＝升級**：①H3 precision——冠軍 S+ 內 **S+且落後 lift 2.77（19.1% 命中）vs S+且領先 1.70（11.8%），z=+4.54** 顯著（落後濾鏡近乎倍增命中率）；②賺賠（解 C-P1 警示）——**冠軍 S+ 全體中位報酬其實為負**（−0.2→−0.7% / 5-40d），但**+落後中位報酬轉正且隨持有期升**（+0.0→+2.3%、賠率 1.61→2.44、勝率 48→57%）。→ **落後濾鏡 precision↑ 且賺賠不惡化（反改善）→ 升級為冠軍 S+ 進場加分**（S+ 且 rs_subind<0 提高權重/分批）。
- **解 C-P1 警示**：C-P1「領先股報酬高」是**全宇宙**（所有股日）的相對；**條件在冠軍 S+（資金進+貼低）子集內，落後股反而 lift＋報酬雙贏**——C-P1 標警示→C-P2 驗證的設計奏效。
- 驗收：`make test` 新增 3 測（precision 增量/payoff extra_conditions 濾鏡+suffix/render 升級裁決）、全套 484 綠、ruff 乾淨、mypy 58→58 零淨增。雙重濾鏡樣本稀疏，holdout/流動性留待資料累積（冠軍版已 B-P1 驗）。

> **M-Part C 全收官（C-P1+C-P2）**：落後度（rs_subind<0）是**首個全勝研究軌**——在冠軍 CP 補漲訊號（資金進+貼低）上，加「個股落後其次產業」濾鏡顯著提升起漲命中且賺賠改善。**可選後續＝另開生產 milestone**把落後濾鏡接進 `cp_candidates.py`（S+ 且 rs_subind<0 加分/分批），不在 docs/16 研究軌範圍、待使用者拍板。每季資料累積後重校。

---

## M-補窗：分析層法人/族群近端窗補齊（生產・分支 `feat/inst-sector-window-backfill`，merge `e5900a7`，2026-06-24）

> 動機：個股法人近端窗原只算外資（修法6 的盲點殘留），族群輪動只有 5/20 日窗、缺 10 日中段。皆**純揭露補窗、非新增 gate**（比照 [[M-修法6]] 6a「揭露非扣分」原則）。報告不回溯，須 W26+ `make week` 重跑才寫進 reports。

- **① 個股法人近端窗：外資專屬 → 三法人**。`grouping.py` 把 `_FOREIGN_NEAR_WINDOWS` 一般化為 `_INST_NEAR_WINDOWS`＋`_NEAR_SRC_TO_PREFIX`，一次聚合 foreign_net / trust_net / total_net 各 5/10d；`group_report._build_enriched_rows` 三 CSV（candidates/holdings/watchlist）出 `inst_net_5d/10d_lots`＋`trust_net_5d/10d_lots`、併入 `_CANONICAL_REUSE_FIELDS`。
- **② 族群 10 日窗**。根因＝M-MH Phase1 把 `windows` 加進 `compute_fund_flows` 但 **cli `sector_rotation_cmd` 從沒接線**（一直跑預設 5,20）。修法＝cli 接 `settings.rotation.windows`，並把 `[5, 20]`→`[5, 10, 20]`；`sector_rotation.csv` 多出 `{net,foreign,trust}_flow_10d`＋`_z`（z 由 `standardize_signals` 自動產）。
- **③ 前端兩 chart 同步**（hardcoded、非自動浮現）：`SectorHeatmap` COLS 加 3 個 10d_z、`InstFlowBar` PERIODS inst/trust 各加 10 日/5 日。
- **④ webapp schema 文件化**：`EnrichedRow`/`SectorRow` 宣告新欄（資料本由 `extra="allow"` 透傳，宣告只為超集文件化）。docs 11/17 同步。
- 驗收：`make test` **509 綠**（+2）、ruff 11/mypy 58＝baseline 零淨增、frontend tsc+build 過、真資料 smoke test 確認 `*_flow_10d_z` 皆產得出。**已 --no-ff 併 main+push（merge `e5900a7`）。**

---

## M-R-Data1：Goodinfo 韌性（規劃書 02 D1・A/B/C；Part 4 protocol 延後）

> 對應規劃書 [docs/proposals/02-data-resilience-and-expansion.md](proposals/02-data-resilience-and-expansion.md) D1。
> 動機：選股層單點押在 Goodinfo（反爬＋改版風險）。三件事：**健康檢查早停（fail-loud）／單策略失敗不炸整批／離線可重放回歸**。
> Part 4（`ScreenerSource` protocol 抽換接縫）使用者拍板延後＝目前 Goodinfo 唯一實作、單 implementer 的 protocol 屬「為未來抽象」，登記 D6 backlog。

- **Part A 健康檢查 `screen doctor`**：新 `screener/goodinfo/doctor.py`——`diagnose_html` 純函式把一段 HTML 分類成 8 診斷碼（OK／BLOCKED／JS_UNRESOLVED／STRUCTURE_CHANGED／COLUMNS_RENAMED／EMPTY_RESULT／TOO_MANY／NETWORK_ERROR），live（`run_doctor`）打探針 URL 並把封鎖/連線失敗攔下分類不外拋。探針＝`config/doctor_probe.yaml`（純流動性 成交筆數≥50000，恆 >0 且遠低於匿名 300 上限，不放技術 rule 避免回檔誤判改版）。欄位改名靠顯式檢查關鍵中文表頭（parser 改名只會默默回 null、不 raise）。
- **Part B `run_all` 韌性**：單策略 `GoodinfoParseError`/`GoodinfoTooManyResultsError` 降級為「本週未取得」記入 `runner.failures`＋`screen_log.md` 新增段，其餘策略照跑、整批不中斷；**`GoodinfoBlockedError` 保留中斷整批語意**（IP 層封鎖，再打也被擋）。CLI `screen run-all` 末尾列未取得策略。
- **Part C 離線可重放 `screen doctor --replay`**：讀 settings 指定的 committed fixture 跑 `diagnose_html`，驗 parser 沒退化、不打網（CI 友善）。`--save-fixture` 在 live OK 時把探針 HTML 落地供手動刷新黃金樣本（**不在每次抓取自動寫 fixtures**，避免 repo churn／符合 CLAUDE.md 2.6）。
- **接線**：Makefile 新增 `doctor` target；`make week` 在 `screen-all` 前先跑 `doctor`（被擋/改版就早停，不讓 screen-all 白跑）。settings `goodinfo.doctor.{probe_strategy,replay_fixture,save_fixture_path}` 皆可換不寫死。
- **合規**：探針沿用既有 fetcher（3s±1 間隔、24h/交易日快取、concurrency=1、指數退避），doctor 預設不 force、與 screen-all 同快取行為；docs/02 已確認合規規則未變。
- 驗收：`make test` **553 綠**（+23：doctor 17＋runner 韌性 4＋log_writer 2）、ruff 11/mypy 58＝baseline 零淨增；`screen doctor --replay` exit 0、`--replay --fixture blocked.html` exit 1。**注意：W26+ `make week` 重跑才會在 `screen_log.md` 出現未取得段（報告不回溯）。**

---

## M-R-Data3：集保大戶／股權分散（規劃書 02 D3）

> 對應規劃書 [docs/proposals/02-data-resilience-and-expansion.md](proposals/02-data-resilience-and-expansion.md) D3。
> 動機：籌碼面只有三大法人，缺最直接的「籌碼集中」訊號（集保大戶）——個股報告原叫 Claude 去 Goodinfo 手讀千張大戶比。
> 補上 TDCC 每週集保戶股權分散表，把「籌碼集中」從「人工讀」升級為「可篩可排序」。

- **新資料源 `data/tdcc.py`**：TDCC OpenData「集保戶股權分散表」（`getOD.ashx?id=1-5`，免費、無反爬、CSV）。
  `parse_distribution`（純解析；吃 UTF-8 BOM 與固定寬度 stock_id 尾端空白；以位置覆寫中文表頭）→ long df
  （data_date/stock_id/level/holders/shares/pct，保留全 17 級距）。`derive_big_holders` 由**原始股數**精算
  大戶占比（＝該級距股數 ÷ 級距17合計 ×100；比 TDCC 逐級截斷顯示的占比加總更精確、差 ≤~0.03pp）。
  `latest_big_holders_with_wow` 取最新週並與前一週相減算 WoW（僅一週/新股→null）。`TDCCClient`（限速/退避/
  逐週累積 `tdcc_distribution_{YYYYMMDD}.parquet`）＋`create_tdcc_client`。
- **門檻＝兩個都出（使用者拍板）**：`big_holder_pct`＝≥400 張（級距12-15）、`big_holder_1000_pct`＝≥1000 張
  （千張大戶，級距15），各帶 `_wow`。**占比口徑為占集保庫存（≈流通量）非占股本**，報告/docs 據實標明。級距→張數
  與門檻全進 `config/settings.yaml` `tdcc.*`（不寫死）。
- **接進選股**：`group_report._build_enriched_rows` 加 4 欄（`big_holder_map` keyed by stock_id，缺值 null 不補零），
  併入 `_CANONICAL_REUSE_FIELDS` → candidates/holdings/watchlist 三 CSV 一致；cli `analysis group` 純讀 TDCC 快取
  建 map（TDCC 異常回空表→欄退化 null、不擋報告）。
- **接進個股報告**：`data_fetcher._format_big_holder_summary` 進 bundle；j2 prompt 模板＋inline draft 加「集保大戶
  持股比（TDCC）」區塊，並把「打開 Goodinfo 手讀千張大戶比」改為「直接引用上方 TDCC 資料」（董監持股/大股東名單
  TDCC 沒有，仍手讀）。docs/11 候選欄說明補 4 欄＋籌碼讀法（WoW 方向＝吃貨/出貨、週頻有遞延、ETF 看 WoW 非絕對值）。
- **接線**：CLI `data fetch-tdcc`＋Makefile `fetch-tdcc` target；`make week` 在 `fetch-institutional-history` 後
  以 `-$(MAKE) fetch-tdcc` 非阻斷接入（TDCC 異常大戶欄退化 null、不擋主流程）。
- **合規**：TDCC OpenData 為免費公開、無反爬；fetcher 仍加 UA＋限速＋指數退避，逐週快取（同日重跑讀快取）。
  fixture 取自真實檔裁成 5 檔（2330/2317/0050/6182/1216）× 17 級距，離線測試不打網。
- 驗收：`make test` **564 綠**（+11 TDCC：parse/derive/WoW/邊界）、ruff/mypy baseline 零淨增；
  `derive_big_holders` 對 2330 算出 ≥400張 87.85%、≥1000張 85.12%（對得起原始股數）。
  **注意：W26+ `make week` 重跑才會把大戶欄寫進 reports（報告不回溯）；首週 WoW 為 null（需累積第二週才有週變化）。**

---

## M-R-Data4：融資融券 MI_MARGN（規劃書 02 D4）

> 對應規劃書 [docs/proposals/02-data-resilience-and-expansion.md](proposals/02-data-resilience-and-expansion.md) D4。
> 動機：`MI_MARGN` 端點已文件化但 `grep 融資|margin src/` 零使用；CLAUDE.md 人設籌碼段明文要求「融資增減」、目前產不出來。

- **資料層 `data/twse.py`**：`_parse_margin`（OpenAPI `/exchangeReport/MI_MARGN` 回 list[dict]・**單位張不除 1000**・端點不含日期故由 `latest_trading_date()` 錨定，同 `fetch_institutional`）→ date/stock_id/stock_name/margin_balance/margin_chg/short_balance/short_chg/note。`fetch_margin`（逐日累積 `margin_{YYYYMMDD}.parquet`・TTL）。`load_margin_signals`（純讀近 6 日快取・`select_recent_cache_files` 縮檔・回最新日每股餘額/增減＋`margin_chg_5d`＝最新 − 第 6 新；**不足 6 日 → null**，誠實不假裝）。`_safe_int` 把空欄位/「-」視為 0。
- **衍生**：`margin_chg`/`short_chg` 由單日記錄即可算（今日−前日餘額）；`margin_chg_5d` 需 6 日快取累積；`margin_to_vol`（融資餘額÷20日均量・幾日均量）在 group_report 由既有 tot20 算。
- **接線**：`fetch-twse` 末段多抓 MI_MARGN（`make week` 自動含）；`group_report._build_enriched_rows` 經 `margin_map` 加 6 欄（margin_balance_lots/margin_chg_lots/margin_chg_5d_lots/short_balance_lots/short_chg_lots/margin_to_vol）＋併入 `_CANONICAL_REUSE_FIELDS`（三 CSV 一致）；cli `analysis group` 純讀 `load_margin_signals` 建 map。
- **個股報告**：`data_fetcher._format_margin_summary`（餘額/增減/近5日/券資比）進 bundle；builder inline draft＋j2 prompt 加「融資融券（上市 MI_MARGN）」區塊；docs/11 補欄說明＋讀法（融資增減＝散戶槓桿；融資增+價漲＝追價過熱、融資減+價漲＝融資減肥籌碼洗清偏多）。
- **僅上市**：MI_MARGN 為上市；**上櫃融資融券為缺口、登記 D6 backlog**（候選宇宙約半數上櫃股 margin 欄一律 null，誠實非 0）。實測 OpenAPI 為乾淨 per-stock JSON，無 legacy CSV 的「資券當沖/鉅額」特殊列、無需過濾。
- **舊版 margin 快取地雷（實作中發現）**：`data/cache/twse/` 有 **497 個某次廢棄嘗試遺留的 margin 快取**——舊 5 欄 schema（`margin_bal`/`short_bal`）的上市 `margin_*.parquet`（251 個）＋上櫃 `margin_otc_*.parquet`（246 個）。`load_margin_signals` 已硬化：**排除 `margin_otc_` 前綴＋只讀含本版 `_MARGIN_SCHEMA` 欄的檔**（舊 5 欄檔在 select_recent 的 40 日緩衝窗內會被撈到、直接 concat 會 ShapeError）。未刪舊檔（非本次建立）。**2026-07-04 M-Rev2 已按 schema 精準盤點後刪除全部 496 個孤兒**（新版檔 3 個保留、loader 驗證正常）。
- 驗收：`make test` **571 綠**（+7 margin：parse 增減/單位/空輸入/safe_int/chg_5d/不足歷史/無快取/略過舊 schema）、ruff/mypy baseline 零淨增；`_parse_margin` 對 2330 算出 融資 −681 張、融券 −3 張。
  **注意：W26+ `make week` 重跑才把 margin 欄寫進 reports；`margin_chg_5d` 需累積 6 個交易日快取才有值（初期 null）。**

## M-R-Data5：財報細項擴充—體質維度（規劃書 02 D5）

> 對應規劃書 [docs/proposals/02-data-resilience-and-expansion.md](proposals/02-data-resilience-and-expansion.md) D5。
> 動機：基本面只有 營收YoY/毛利率/營益率/單季EPS，缺體質維度（負債比/ROE/純益率）→ D（品質龍頭）/F（價值）的體質判斷踩空。

- **盤點結論（step1，2026-06-27 實測）**：TWSE/TPEX OpenAPI **無現金流量表端點**（全目錄 143 path 無「現金」）、資產負債表為**簡式**（只有資產/負債/權益彙總、**無存貨/應收明細**）→ **營業現金流、存貨/應收週轉率不可得**。使用者拍板「**改抓確定可得者**」（不爬 MOPS、守 API-first），成功標準 #2 由「營業現金流＋負債比」**修正為「負債比＋單季ROE＋稅後純益率」**。
- **資料層 `data/twse.py`**：`_FUNDAMENTALS_SCHEMA` 加 6 欄。`pretax_margin_pct`/`net_margin_pct` 來自**已抓的營益分析**（`t187ap17_L`／`mopsfin_187ap17_O`，0 新端點、全市場含金融業）；`debt_ratio_pct`（負債/資產×100）/`current_ratio`（流動資產/流動負債）/`bvps`（每股參考淨值）/`roe_q_pct`（＝EPS/每股淨值×100，單季、歸屬母公司、**未年化**）來自**新增簡式資產負債表一般業端點**（上市 `/opendata/t187ap07_L_ci`＋上櫃 `mopsfin_t187ap07_O_ci`）。`_parse_quarterly_fundamentals` 擴成 6 端點輸入（bs_listed/bs_otc 預設 None＝向後相容既有 4-arg 呼叫/測試）。`_safe_ratio` 守除零/None。
- **欄名雙保險地雷**：⚠ **上市用「資產總額/負債總額」、上櫃用「資產總計/負債總計」**；**上櫃資產負債表 key 用「年度/季別」（非 Year，與上櫃營益分析/EPS 的 Year 不一致）**；2026-06-27 實測為準。
- **僅一般業 `_ci`**：資產負債表/綜合損益表各分 6 種公司型態端點（`_ci`一般業/`_bd`/`_fh`/`_ins`/`_mim`/`_basi`），D5 只取一般業 → **金融業（金控/銀行/保險/證期）與缺表者負債比/ROE/淨值誠實 null**（金融業負債結構語意本就不同；純益率仍在，來自全市場的營益分析）。
- **接線**：`fetch_quarterly_fundamentals` 多抓 2 端點（`make fetch-twse`/`make week` 自動含、端點失敗回 [] 該批體質欄 null 不擋）；`group_report._build_enriched_rows` 經既有 `fundamentals_map` 加 3 欄（`net_margin_pct`/`debt_ratio_pct`/`roe_q_pct`）＋併入 `_CANONICAL_REUSE_FIELDS`（三 CSV 一致）。
- **個股報告**：`data_fetcher._format_fundamentals_summary`（獲利能力＋財務結構＋誠實標未取得項）進 bundle；builder inline draft＋j2 prompt 加「單季財報體質」區塊、並改基本面段指示（單季值用 OpenAPI、近4季趨勢/本益比河流圖才查 Goodinfo）；docs/02 補端點表＋盤點結論、docs/11 補讀法。
- 驗收：`make test` 綠（+5：parser 衍生/金融業 null/淨值非正 ROE null、formatter empty/有值/null 三路徑）、ruff/mypy 零淨增；2330 算出 負債比 31.50%／流動比 2.49／ROE 9.72%、6182 負債比 35.02%／ROE 1.88%、金融業體質欄 null。
  **注意：W26+ `make fetch-twse`（fundamentals 過 TTL）重跑才把體質欄寫進快取/reports；同季既有 `fundamentals_{Y}Q{q}.parquet` 在 TTL 內會回舊 schema（無新欄），須 TTL 過或刪檔重抓。**

## M-R-Data2：全市場日線歷史密度—冷啟動（規劃書 02 D2）

> 對應規劃書 [docs/proposals/02-data-resilience-and-expansion.md](proposals/02-data-resilience-and-expansion.md) D2（§2.2 冷啟動歷史密度）。
> 動機：`STOCK_DAY_ALL`/`otc_daily_all` 的 date 參數被無視、**只能往未來累積、過去補不回**；rotation z 需 ~60+ 日、calibration 需 ~250 日 → 新環境前幾個月歷史窗偏短、訊號統計意義薄弱。

- **回補指令（核心交付）**：新增 `data backfill-universe-history`（CLI＋Makefile `backfill-universe-history`）。對 **concepts.yaml 全部次產業成員（上市＋上櫃）** 逐檔走既有 `fetch_stock_history`（自動分派 TWSE `STOCK_DAY`／TPEX `tradingStock`，限速 1 秒/請求，過去月份永久快取＝天然可中斷續跑）。**依次產業成員數由多到少排序、跨次產業去重**（成員多的先補、密度優先見效）。為 `backfill-otc-history` 的**超集**（後者只 ∩ 上櫃）→ 兩者並存、舊指令未退役。全量首跑 ~1500 檔×13 月、估 8-12 小時、建議掛背景。
- **報表誠實密度註記**：新 `report/density.py`（純函式 `data_density_note(actual_days)`＋常數 `Z_MIN_DAYS=60`/`CALIB_TARGET_DAYS=250`），依實際交易日數分三段信心（≥250 充足／≥60 中等／<60 統計意義有限）。接進 **rotation 報表頭**（`render_rotation_report` 加 `density_note` 參數＝`market["date"].n_unique()`）與 **group/cp 報表頭**（`render_group_report` 加 `density_note`＝候選股 `price_history["date"].n_unique()`）；空字串則整行不渲染（向後相容）。
- **文件校正**：README §12 cron 段改定位「法人可不靠它（可回補）、**全市場日線密度建議常駐**」＋補「兩種補法：常駐 cron 或一次性 `backfill-universe-history`」；`scripts/fetch_cron.sh` 頭註補「全市場日線不可回補＝建議常駐的第一理由」；docs/02 端點表＋累積表標明 `STOCK_DAY_ALL` 不可回補、單檔 `STOCK_DAY` 可回補（backfill 用）。
- **未做（誠實）**：原方案列的 README §2.2「正式化每日抓取＝改建議常駐」採「保留 cron 為法人選配、但對日線密度明標建議常駐」的折衷（不全面反轉 57ab1f7 對法人的正確降級）；rotation/cp 密度註記為「揭露」非「擋流程」。
- 驗收：`uv run tw-screener data backfill-universe-history --help` 註冊正常；`make test` 581 綠（+8：density 三段信心＋邊界、rotation 報表頭密度有/無兩路徑）、ruff/mypy 零淨增（mypy 58＝D5 基線）。
  **注意：W26+ 跑 `make rotation`/`make group` 才把密度註記寫進 reports；歷史密度本身需跑一次 `make backfill-universe-history` 或讓 cron 累積數月後才補足。**

## M-R-Val1：個股策略回測閉環（規劃書 03 V1）

> 對應規劃書 [docs/proposals/03-quant-validation-loop.md](proposals/03-quant-validation-loop.md) V1（審查 §4#1，整份審查 ROI 最高）。
> 動機：核心主張「D/E/F/G 選股策略本身有效」從未被回測——`backtest/strategies.py` 三函式原為 `NotImplementedError`、`make backtest-strategies` 直接 exit 1。**取代 M5 預留的「印未實作提示並 exit 1」占位**（M5 §驗收該項已被本里程碑落實）。

- **三函式落實（核心交付）**：[backtest/strategies.py](../src/tw_screener/backtest/strategies.py)
  - `load_historical_screens`：掃 `reports/<week_tag>/screen_result_*.csv` 合併長表（week_tag/screened_at/stock_id/name/close/change_pct/strategy_id；stock_id 保留前導碼）。
  - `compute_forward_returns`：entry＝入選日**次一交易日收盤**（嚴格晚於 `screened_at`，防前視）、exit＝entry 起 `hold_weeks×5` 交易日；**三類邊界明確**——未到期（大盤日曆不足 → 整批排除不污染）、下市/停牌（個股序列早停 → exit null 非 0）、除權息（ex_date∈(entry,exit] 現金股利加回）；併同期等權全市場指數算超額。
  - `strategy_summary`：按 (strategy_id, hold_weeks) 算 勝率/平均/中位/最差單檔(max_drawdown)/樣本數/平均超額/勝過大盤率，下市另計 n_delisted 不入勝率。
  - `render_backtest_report`：產 markdown，樣本<門檻標 ⚠️＋「僅供方向性」。
- **CLI／設定／Makefile**：新 `backtest` sub-app＋`tw-screener backtest strategies`（`--hold-weeks`/`--out-dir`）；`settings.backtest.strategies`（hold_weeks/history_days/trading_days_per_week/clip/min_sample_warn 全參數化）；`Makefile backtest-strategies` 移除 exit 1、改真的跑（輸出 `research/strategy_backtest/`＝gitignore 本地研究產物）。
- **未做（誠實）**：V2 大盤 regime 閘門、V3 組合層風控（規劃書 03 後續，未開始）；max_drawdown 定義為「最差單檔報酬」非組合權益曲線回撤（組合層屬 V3）；樣本仍薄（W21–W26＝6 週，2/4 週窗才有樣本、8/12 週窗全未到期被排除），結論定位方向性、隨週數變厚每季重算。
- 驗收：`make backtest-strategies` 產各策略×持有窗 勝率/報酬/回撤/超額表（不再 exit 1）；`make test` 594 綠（+13：load/forward(基本·未到期·下市·除息·超額)/summary(統計·下市·空)/render(小樣本旗標·空)）；新檔 ruff 淨、mypy 58＝D2 基線零淨增。
  **注意：W27+ 隨 `reports/` 週數增厚重跑才會填出 8/12 週窗樣本與更紮實的統計。**

## M-R-Val2：大盤 regime 總控閘門（規劃書 03 V2）

> 對應規劃書 [docs/proposals/03-quant-validation-loop.md](proposals/03-quant-validation-loop.md) V2（審查 §4#2，缺市場層剎車）。
> 動機：所有訊號都在個股/族群層，缺市場層的多空/位階剎車——空頭或高位期照推 breakout 危險。補一個市場層姿態訊號，**定位＝輔助姿態揭露，不硬性 gate 掉訊號（守 CLAUDE.md Part 3「由人決策」）**。

- **regime 計算層（核心交付）**：新 [analysis/regime.py](../src/tw_screener/analysis/regime.py)（純函式、IO 由 cli 載入）
  - `compute_trend_score`：等權全市場指數 vs MA20/60/120 多空排列（[指數,MA20,MA60,MA120] 相鄰「前>後」各 ±1 取均值；多頭排列 +1、空頭 −1）。資料不足最長 MA → None。
  - `compute_breadth_score`：個股站上自身 MA60 比例（截面位階廣度）＋ 等權指數在自身 120 日高低區間位階，各映射 [-1,1] 取均值；有效報價檔數 < `min_priced` → None。
  - `compute_flow_score`：全市場三大法人近 5/20 日 `total_net` **日均**淨流（股）÷ `saturate_shares` 夾 ±1，多窗取均值。無法人 → None。
  - `compute_regime`：三分項各正規化、按 `weights` 對「可得分項」正規化加權合成連續分數 ∈ [-1,1]，門檻切 進攻/中性/防禦；全缺 → 「資料不足」。`describe_regime` 產報表/CLI 共用顯示 dict（一行摘要＋姿態建議）。
  - 共用 helper：等權指數邏輯抽成 `rotation.compute_market_index`（regime 與 backtest 共用大盤基準；V1 backtest 私有 `_market_index` 維持不動＝外科手術，接受小重複）。
- **CLI／設定／報表介接**：新 `market` sub-app＋`tw-screener market regime`（印 regime＋趨勢/廣度/資金分項依據）；`settings.regime`（history_days/clip/trend.ma_windows/breadth/flow/weights/thresholds 全參數化）；`analysis group`（group_analysis.md 大盤姿態段）與 `sector rotation`（表頭 regime 行）報表頭顯示姿態、防禦期提示「降低總曝險」。
- **明文修正（對規劃書）**：規劃書 §V2 廣度寫「上漲家數比、距低位階」——單日上漲家數比噪音大，改用截面位階廣度（站上 MA60 比例）＋指數自身位階，語意一致且穩健；flow 單位＝**股**（T86 `total_net` 為股數非張），`saturate_shares` 為正規化常數（非門檻、隨資料校準）。
- **未做（誠實）**：regime 本身的回測（V1 完成後可順帶驗「防禦期是否真少賠」，屬後續）；V3 組合層風控（未開始）。**報表畫面需 W27+ 重跑才顯示 regime 段**（程式碼已落地即生效）。
- 驗收：`uv run tw-screener market regime` 印當前 regime＋分項；`make test` 綠（+9：bull→進攻/bear→防禦/空輸入→資料不足/趨勢排列/資金正負+飽和/廣度 gate 重正規化/短歷史僅資金/門檻三branch/describe 形狀）；新檔 ruff 淨、mypy 零淨增。

## M-R-Val3：組合層風控（規劃書 03 V3）

> 對應規劃書 [docs/proposals/03-quant-validation-loop.md](proposals/03-quant-validation-loop.md) V3（審查 §4#6，缺組合層風控）。
> 動機：把 [docs/14](14-entry-ladder-portfolio-fix.md) D4「因子簇上限」目前**只在 prompt 層的人工檢核**（[docs/11:202](11-propicks-analysis.md#L202)）落成**可計算模組**——揭露 picks/holdings 五檔其實押同一題材/事件的隱性集中。**定位＝風險揭露，非硬約束（守 CLAUDE.md Part 3「由人決策」）。**

- **portfolio 計算層（核心交付）**：新 [analysis/portfolio.py](../src/tw_screener/analysis/portfolio.py)（純函式、IO 由 cli 載入）
  - `compute_label_concentration`：次產業/主題標籤逐標籤統計持有檔數／佔比（**多標籤 aware**，industry＋theme 以「、」拆，一檔可計入多標籤），達 `min_count` 或 `min_share` → flagged。
  - `compute_correlation_clusters`：近 `window` 日**日報酬**兩兩 Pearson 相關，|ρ| ≥ `threshold` → union-find 連通成簇（size ≥ 2）；重疊有效交易日 < `min_overlap` 的對跳過、無價格史的檔標進 notes。日報酬夾 ±漲跌停防未還原毒化。
  - `compute_factor_cluster_exposure`：預定義因子簇（settings，如「利率敏感＝銀行＋建材營造＋產險＋壽險」）標籤任一命中即歸屬，命中檔數 > `max_count` 或佔比 > `max_share` → flagged。
  - `compute_portfolio_check` 合成三段＋`describe_portfolio_check` 產報表/CLI 共用顯示 dict（摘要行＋三段＋警示計數）。
- **CLI／設定／報表介接**：新 `portfolio` sub-app＋`tw-screener portfolio check [--week] [--include-candidates]`（預設讀 holdings_enriched.csv＝**持股為主**，可選併入候選；印標籤集中度/相關簇/因子簇）；`settings.portfolio`（history_days/corr.{window,min_overlap,threshold,clip}/label_concentration/factor_clusters 全參數化、簇定義不寫死）；`analysis group`（group_analysis.md「組合體檢」段）顯示持股**因子簇超限＋集中標籤**。
- **設計取捨（誠實）**：
  - holdings_enriched **無部位大小欄** → 所有「合計%」皆為**等權檔數佔比近似**，CLI/報告皆明標。
  - **報告段只揭露標籤集中度＋因子簇曝險**（價格無關、render 期即可得）；**報酬相關簇需全市場日線、留給 `portfolio check` CLI**——因 group 流程的 `price_history` 只含候選股、非全持股，render 期算相關不可靠（不半套上報告）。
  - 相關隨市況變、非穩定 → 守規劃書風險段，定位風險揭露非硬 gate。
- **未做（誠實）**：dashboard 持股頁顯集中度（使用者拍板本輪只做 CLI＋報告段，frontend 留下一輪）；regime/組合風控的回測驗證（屬 V1 衍生後續）。**group_analysis.md 組合體檢段需 W27+ 重跑才顯示**（程式碼已落地即生效）。
- 驗收：`uv run tw-screener portfolio check` 印當前持股集中度/相關簇/因子簇；`make test` 綠（+13：標籤拆解/集中度 count·share 兩路徑/多標籤/因子簇超限·零命中/相關簇分群·低重疊跳過·單檔 noop/合成 orchestration·空持股·無價格/describe 形狀）；新檔 ruff 淨、mypy 零淨增。

---

## M-R-Refactor-A6：文件漂移修正（規劃書 04 A6）

> 對應規劃書 [docs/proposals/04-architecture-refactor-and-slimming.md](proposals/04-architecture-refactor-and-slimming.md) A6（審查 §1#8/§5#7 文件漂移）。
> 規劃書 04（架構重構）建議順序 A6→A1→A2→A3→A4→A5→A7，A6 零風險先做。**純文件、不動程式碼。**

- **盤點結論**：A6 列的三處漂移，其中 **README §12／docs/02 §2.2 的 cron／歷史密度描述早在 D2 已校正**（`STOCK_DAY_ALL`／`otc_daily_all` 只能往未來累積·不可回補；法人改 TPEX `3itrade_hedge` 可逐日回補故 cron 非必要），本輪確認為正確版、不動。
- **真正殘留＝docs/00 的幽靈目錄**：[docs/00-architecture.md](00-architecture.md) 仍宣稱不存在的 `analysis/indicators/`（macd/kd/rsi 一檔一指標）與「Rust + PyO3 預埋」計畫。實際指標以 **Polars 向量化內嵌**在 `analysis/momentum.py`（N 日報酬／rolling 高低／除息加回／RS／族群動能）與 `analysis/stock_panel.py`（均線距離 MA20/60/240／價格位階／rolling z-score），且從無 MACD/KD/RSI 實作。
  - 模組表幽靈列改為實際的 `momentum.py`／`stock_panel.py`。
  - 「Rust 預埋位置」整段改寫為「**原計畫從未實作、已放棄**」，指向實際內嵌實作，註明現階段不預埋抽象層。
- **docs/08 歷史註記**：M4「可動檔案範圍」仍列 `indicators/`，加一行歷史註記（不改寫歷史、只說明該目錄最終未建立）——超出 A6 明列三檔範圍一行，因 A6 成功標準是「`grep -r indicators docs/ README.md` 無誤導描述」，docs/08 那行會被掃到。
- 驗收：`grep -rn indicators docs/ README.md` 剩餘三筆全非誤導（規劃書本身描述問題／docs/00 明標「已放棄」／docs/08 明標「歷史紀錄·未建立」）；純文件變更、`make test/lint/typecheck` 不受影響（測試護欄留給 A1–A7 動程式碼的 milestone）。

---

## M-F1：pick 閉環＋反事實追蹤（規劃書 05 F1・PO1–PO4）

> 對應規劃書 [docs/proposals/05-efficacy-overhaul.md](proposals/05-efficacy-overhaul.md) F1（2026-07-02 實證重寫第一批；裁決點 #1–#5 已於 2026-07-02 全數拍板）。
> 動機：pick 層從無裁判——每週核心對不對、被旗標剔除的錯不錯，全靠人腦印象。§1.2 實算 W22–W25 核心 α≈−2.1pp／勝率 40%，且產物斷供（W26）與命名漂移（W24 picks.md）讓閉環前置條件是壞的。本 milestone 建底帳＋裁判。

- **PO1 持久化**：新 [report/pick_store.py](../src/tw_screener/report/pick_store.py)——`reports/<week>/picks.csv`（week/data_date/stock_id/name/layer(core|opportunity|pool)/sub_industry/entry_zone/stop/ext_ma60_pct/thesis_tag）＋ **excluded.csv**（reason/detail＝被旗標剔除底帳）；upsert 冪等、schema 驗證、`weeks_without_picks` 斷供偵測。CLI `picks record`（data_date 自動取 screen_result screened_at、name/ext_ma60 自動讀 candidates_enriched，皆可覆寫）。**回填 W21–W27 共 95 picks＋83 excluded**（自各週 pick.md 人工萃取；W22 資料基準 05-29 依報告表頭覆寫；W26 缺週如實標＝不造檔、報告明列斷供）。
- **PO2 命中率×α**：新 [backtest/picks_outcome.py](../src/tw_screener/backtest/picks_outcome.py)＋runner——①**到期快照**（entry＝資料日次一交易日收盤防前視、exit＝`--exit-date`（預設快取最新日）、除息加回、路徑最深回撤；基準＝同窗快取宇宙等權**中位**（§1.2 口徑）＋所屬次產業籃中位（concepts.yaml 成員、<3 檔標 null）雙超額）；②**固定持有窗**整套複用 V1 `compute_forward_returns`/`strategy_summary`（layer 當 strategy_id）。
- **PO3 翻轉解剖**：`picks outcome --diff`——相鄰紀錄週 layer 降級（core→opportunity→pool→除名）＋降級當週 enriched 可見訊號（距月/季線、外資近5/20日、投信近5日、flags）。
- **PO4 反事實**：excluded.csv 算同樣前進報酬，按 reason 分桶＝**旗標偽陰性帳**。首跑（至 7/1）：**土洋對作 n=15 平均 α +2.5pp、67% 跑贏大盤（最大遺珠台新新光金 +20.2%）；投信主導＝W21 國巨 +65.8%**——「旗標流放真領頭」首次有數字；反面：高PE（−9.6pp α）、強漲法人賣（−5.1pp）、族群逆風確實擋掉虧損＝旗標非全壞，F2/F3 校準有據。
- **附帶修復（根因與 stock_calib 零收盤防護 9c768b4 同類）**：V1 `strategies.py` 等權指數被快取 close=0 髒資料（6/26 起權證/ETF 類 30 筆）毒化——0/0=NaN 使 cum_prod 之後全 NaN、W24 持有窗超額全 nan；`_market_index`／px 各加 `close > 0` 濾網＋回歸測試。
- **設定／Makefile**：`settings.backtest.picks`（history_days/hold_weeks/trading_days_per_week/clip/min_sample_warn/min_subind_members/output_dir）；`make pick-outcome` → `research/pick_outcome/outcome_<date>.md`＋picks/excluded returns CSV＋layer_diff CSV。
- **§1.2 重現驗收**：`picks outcome --exit-date 2026-07-01` 逐週勝率 1/5、2/5、2/5、3/5（合計 8/20＝40%）與規劃書全同；W24 −1.43%／W25 +0.09% 平均精確吻合；純價逐檔（創見 −29.93／華碩 −21.51／研華 −2.71／緯創 −8.33／國碩 +11.98／聯詠 +13.74）與 §1.3 全同；W22/W23 週平均差異＝**除息加回**（工具做了規劃書 §1.1 自承缺的還原：慧洋 −9.04→−4.59% 正是規劃書預告值）＋基準宇宙（全市場快取 vs 手算 176 檔追蹤宇宙）。
- **未做（誠實）**：excluded 回填只收「旗標型」剔除（過熱/土洋對作/投信主導/強漲法人賣/法人倒貨/近端倒貨/低流動/高PE/量能未確認/爆量/族群逆風＋個案營收衰退），「轉弱/跌破均線」等趨勢型拒絕不入帳（PO4 的問題是旗標、不是趨勢判斷）；樣本 6 週屬方向性、每季重算；F2 位階門檻校準／F3 旗標改規則皆待樣本變厚後由本閉環裁決。
- 驗收：`make pick-outcome` 產分層命中率×α（vs 大盤＋vs 族群）＋偽陰性報表；`make test` 綠（+18：store 6＋outcome 11＋零收盤回歸 1）；lint/typecheck 新檔淨、零淨增。

---

## M-F4：一頁決策卡＋產物完整性檢查（規劃書 05 F4）

> 對應規劃書 [docs/proposals/05-efficacy-overhaul.md](proposals/05-efficacy-overhaul.md) F4（第一批、與 F1 平行；裁決點 #4 決策卡 ≤60 行已於 2026-07-02 拍板）。
> 動機：§1.6 決策密度過低——pick.md 底稿擺最前、200 行讀不完；且 make week 容錯步驟（rotation/cp-value-candidates）失敗無聲，cp_candidates.md 曾 W25–W27 連三週斷供無人知、W26 整週 pick 斷供也是事後才發現。

- **決策卡框架（docs/11 交付結構重寫）**：pick.md 分兩層——首屏**一頁決策卡 ≤60 行**（①姿態一行 ②持股動作表（0-A 蒸餾）③核心每檔 ≤5 行（次產業/趨勢階段/進場階梯/停損/風險）④機會表一行式（瑕疵明說）⑤本週三風險）→ **附錄 A–F**（補充池／訊號交集／市場節奏／觀察名單觸發表／watchlist 逐檔／族群深度解讀底稿）。**不刪資訊、只分層**；行數預算與「超過 60 行往附錄搬、不壓縮成不可讀」自查規則入框架。**W28+ 起生效（報告不回溯）**。
- **產物完整性檢查（承舊 09 RQ3）**：新 [report/artifact_check.py](../src/tw_screener/report/artifact_check.py)——比對 `settings.report.artifact_check` 應產出清單，**machine**（4 篩選 CSV＋enriched＋group_analysis＋sector_rotation md/csv＋theme_strength＋cp_candidates.md）只查最新週＝步驟無聲失敗偵測；**analyst**（pick.md/picks.csv）往週缺＝斷供 WARNING（W26 型）、最新週缺＝僅提醒（剛篩完本來就沒有）；excluded.csv 不查（當週可能真無旗標剔除）。CLI `report check`／`make week-check`，掛 `make week` 尾段；**只 WARNING 不擋流程（exit 0）**。首跑即抓到真洞：W27 缺 cp_candidates.md、W21/W24 缺 pick 底帳。
- **附帶**：docs/11 流程範例 `picks.md` → `pick.md` 命名漂移修正（W24 漂移根源、F1 斷供偵測認 pick.md）＋補「定稿後 picks record 落底帳」步驟；`pick_store._week_dirs` 升格公開 `week_dirs`（檢查器共用週次目錄判準）。
- **未做（誠實）**：本 milestone 是「框架＋檢查器」——現存 W27 pick.md 不回頭重排（報告不回溯，W28 起新框架產出）；本機 reports/ 的 F1 底帳缺口（W21/W24 無 picks.csv、全部週次無 excluded.csv、現存 picks.csv 為舊 schema）屬 F1 資料重建，另案處理，檢查器已如實點名。
- 驗收：`make week-check` 對真實 reports/ 印出上述 WARNING；`make test` 綠（+7 artifact_check）；lint/typecheck 零淨增。

---

## M-F2：核心層位階紀律（規劃書 05 F2）

> 對應規劃書 [docs/proposals/05-efficacy-overhaul.md](proposals/05-efficacy-overhaul.md) F2（第二批；裁決點 #1 收回買強勢例外・#2 位階門檻 **+15%（嚴）** 已分別於 2026-07-02／07-03 拍板並回寫規劃書 §5）。
> 動機：§1.3 主虧損剖面——入選時距季線 >15% 的 8 檔核心平均 −9.17% vs ≤15% 的 12 檔 +1.19%，且 W27 核心 3/4 重演同剖面；研究層（CP 貼低 lift 1.6–1.8、C-P2 落後濾鏡）早已反覆證明低位階進場贏，pick 層卻反其道而行。

- **硬擋板**：[pick_store.core_extension_violation](../src/tw_screener/report/pick_store.py)（純函式）＋ `picks record` 落帳前查核——**core 且距季線乖離 > `settings.picks.core_ext_ma60_max_pct`（15.0 試行）→ 拒收（exit 1）**，訊息導向「改列 opportunity／等 F3 趨勢領頭板／等回踩」。乖離未知（enriched 無此檔）→ 警告「位階無法查核」後如實記錄、不硬擋；opportunity/pool 不受限（延伸股降層仍可入帳）。
- **收回修法 5 例外（裁決 #1）**：docs/11——`強勢領頭` 由「可納核心/機會」改為「**可納機會/趨勢領頭板，不得入核心**」；核心精選段加位階紀律硬規則（核心保留給起漲/健康拉回/CP 貼低＋族群內落後）；買強勢階梯標「僅適用機會/領頭板、核心層禁用」。
- **歷史重跑驗收（成功標準①②）**：測試以 §1.3 與 W27 實際乖離重放——創見 +24.8／華碩 +26.3／W27 華航 +23.7・長榮航 +23.5・矽創 +27.5 **全數被 +15% 擋下**；贏家層彰銀 +8.6／遠東銀 +4.4 通過；實機對真實 W27 記矽創入 core 被硬擋（exit 1、底帳未動）。
- **未做（誠實）**：F3 趨勢領頭板未建——F2 生效期間延伸強勢股先列機會/觀察（docs/11 已明標）；成功標準③「首季核心（新規則）α ≥ 舊規則模擬 α」屬 F1 閉環季度對照，樣本變厚後跑；門檻 +15% 標「試行」，每季由 make pick-outcome 校準。**既有底帳不回溯改層**（W22–W27 帳保持當時決策原貌，供新舊規則對照）。
- 驗收：`picks record --week 2026-W27 --stock 8016 --layer core` 被硬擋；`make test` 綠（+4 gate 測試）；lint/typecheck 零淨增。

---

## M-F3：趨勢辨識改造—價格趨勢優先＋趨勢領頭板（規劃書 05 F3）

> 對應規劃書 [docs/proposals/05-efficacy-overhaul.md](proposals/05-efficacy-overhaul.md) F3（第二批，接續 M-F2）。
> 動機：§1.5——20 日流量排序天然落後（航空/金融價格已走、流量榜 W27 才浮出）；真領頭被旗標流放（國巨 +44% 兩度剔除）後「找不到趨勢標的」。

- **價格趨勢分數（主排序鍵）**：新 [rotation.compute_trend_scores](../src/tw_screener/analysis/rotation.py)——籃子等權指數 vs 自身月/季線（0.40）＋成員站上自身季線比例（0.35）＋領頭股 RS 跨次產業百分位（0.25），權重進 `settings.rotation.trend_score`；`rank_by` 由 `net_flow_20d` 改 `trend_score`，**20 日流量降為確認欄**。在既有 rank_flows/build_rotation_table 上加欄重排、非重寫；rank 鍵缺欄誠實退回流量鍵。
- **趨勢領頭板**：新 `compute_trend_leaders`——全市場 RS（20 日報酬 − 全市場中位）前 15＋次產業＋距季線＋旗標；**過熱/土洋對作只標註不剔除**（口徑同 propicks_flags），近 20 日均成交額 <1 億不入板（妖股）；產 sector_rotation.md §1.5＋`trend_leaders.csv`（入 week-check 應產清單），風險預算明文（部位減半、移動停損非 MA60）＝ **F2 被擋延伸股的合法出口**。
- **雙鏡頭仲裁明文化**：docs/11 固定三種矛盾讀法（雷達強×趨勢低＝窄／流量強×價低＝下一棒觀察非進場／趨勢高×流量負＝經領頭板參與）＋小族群榜外→看領頭板。
- **六月回放驗收（成功標準①）**：**證券 06-05 趨勢 #5 vs 流量 #28、06-12 #3 vs #35**＝價格證據比流量榜早數週浮出金融✓；銀行 06-19 趨勢 #5（其流量 06-12 已 #1＝資金先進型，雙鏡頭互補）；**國巨 W23–W27 全程入板（#1–#5・過熱;土洋對作標註）、台新新光金 W24/W26 入板**✓。**航空未達（如實）**：次產業僅 3 檔 < min_members=5 永不入排名、個股 6 月中 RS 前 15 被記憶體/被動 +50~100% 佔滿——屬排名門檻/板深結構限制，非分數失靈；要浮出需降 min_members 或加深板（動排名口徑，留使用者裁決）。
- **W27 實跑**：銀行以價格證據坐 #1（雙線上・100% 站季線）；被動元件/PCB 價格強但流量負→趨勢分抬前＋出貨警訊象限與確認欄互制（風險不失控）。**主鍵切換首週 ΔRank 與舊流量榜相比、僅供方向參考（W28 起恢復同鍵可比）**。
- 驗收：`make rotation` 產新版 sector_rotation.md（§1 價格趨勢主鍵＋§1.5 領頭板）＋trend_leaders.csv；`make test` 綠（+4 rotation 測試）；lint/typecheck 零淨增；回放腳本結果如上（scratchpad、一次性）。

---

## M-F5：揭露欄位包（規劃書 05 F5・沿舊 06 NF1＋07 TR1；收官）

> 對應規劃書 [docs/proposals/05-efficacy-overhaul.md](proposals/05-efficacy-overhaul.md) F5（收尾批）。裁決 #3：舊 08 主動/被動 proxy 確認丟棄不做。
> 動機：近端籌碼拆窗與「健康回踩 vs 下跌第一天」全靠分析師人工逐檔比對，遲早漏（台新新光金差點漏）；§1.4 實證近端佔比**單獨無判別力**（聯詠近端在賣仍 +13.7%）——固化成揭露欄、明確不當排序主判。

- **NF1 近端籌碼欄**：[grouping.near_flow_state](../src/tw_screener/analysis/grouping.py)＋`classify_risk_kind` 純函式——`flow_state`（轉賣/熄火/加速/平穩＋主體，外資投信各評、警示邊優先）、`near_share_5d_pct`（近5佔20累計%）、`risk_kind`（**價格已跌＞籌碼熄火＞價格延伸**，延伸門檻對齊 F2 +15%）；`inst_missing`／無大額買超邊 → null 如實。門檻進 `settings.near_flow`。
- **TR1 軌跡欄**：新 [analysis/trajectory.py](../src/tw_screener/analysis/trajectory.py)——`down_days_streak`／`pullback_vol_ratio`（回踩 5 日均量/前 20 日均量）／`above_ma20_days`（站上/跌破月線連續天數）／`pullback_quality`（**止穩＝縮量守月線；破線＝連跌≥3 或放量≥1.2 且破月線；其餘觀察**；無量資料不臆造止穩）。歷史 <25 日全 null（上櫃缺口如實）。門檻進 `settings.trajectory`。餵 F2「健康拉回」判定與 F1-PO3 翻轉解剖。
- **接線**：group_stocks 加 `trajectory_cfg` join 軌跡欄；`_build_enriched_rows` 統一產 7 欄→candidates／holdings／watchlist 三份 enriched 同欄（watchlist 管線無軌跡來源＝null）；runner 量窗自動放大到 26 日（量比 tail 不受影響）。**附帶修復**：price_history 舊 schema volume 整欄 null 時 coalesce volume_history 補洞（原設計只查欄存在、量比恆 null）。
- **三案例驗收（舊 06 沿用）**：台新新光金（外資 20日 +162,168、近5 +2,720＝1.7%）→ **熄火(外資)**；華航（近5佔比 54%、距季 +23.7%）→ **加速＋價格延伸**；國泰金（5日 −7.03%、破月線）→ **價格已跌**——測試＋W27 實跑皆過（華航實跑 53.6% 加速/延伸；國建 轉賣(外資)→籌碼熄火 與 pick.md 人工判讀一致）。W27 實跑分佈：flow_state 63/132 有值、止穩 23/觀察 87/破線 22。
- **docs/11**：F5 揭露欄讀法＋**鐵律明寫「近端佔比單獨無判別力（2026-07-02 實證）——不得當排序主判或硬 gate」**；健康回踩判定改讀 `pullback_quality`。
- **未做（誠實）**：各欄是否有 edge 交 F1 分桶回測（樣本變厚後跑，屆時才有資格談升主判）；watchlist/holdings 管線的軌跡欄（需該管線也載歷史，另案）；舊 08 被動 proxy 確認棄案。
- 驗收：`make group` 產出含 7 揭露欄的三份 enriched；`make test` 綠（+9 trajectory/near_flow）；lint/typecheck 零淨增。**規劃書 05（F1–F5）全數收官。**
