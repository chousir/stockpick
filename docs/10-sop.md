# 10 — 每週使用 SOP（手動 Claude 對話模式）

> 本檔說明**沒有設定 ANTHROPIC_API_KEY** 的情況下，怎麼把 `tw-screener` 產出的資料草稿
> 拿到 Claude 對話視窗（網頁版或桌面版）手動完成個股分析。
>
> 如果已設定 API key，`make report STOCK_ID=XXXX` 會自動呼叫 Claude API 產出完整報告，
> 不需走這個流程。

---

## 第 1 節 — 每週流程總覽

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  Step 1                Step 2              Step 3               │
│  抓資料 + 選股   ──►   看族群分析   ──►   選 5-10 檔重點         │
│  (make week)           (group_analysis.md)                      │
│                                                                 │
│  Step 4                              Step 5                     │
│  逐檔產資料草稿                 貼到 Claude 對話 + 範本 prompt   │
│  (make report STOCK_ID=...)     → Claude 看 Goodinfo 補分析     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

預估每週時間：抓資料 5 分鐘 + 挑股 10 分鐘 + 每檔 Claude 對話 5 分鐘 ≈ **1 小時完成 5-10 檔**。

---

## 第 2 節 — 詳細步驟

### Step 1：抓資料 + 跑篩選 + 族群分析

```bash
make week GROUP=defg   # 主流程：D/E/F/G（ProPicks 復刻組＋成長拉回）
# （def=D/E/F 不含 G；abc=A/B/C 經典三角 legacy）
```

等同於：

```bash
make fetch-twse                # 抓 TWSE 全市場日線、法人、月營收
make screen-all GROUP=defg     # 跑指定組策略，產 screen_result_*.csv
make fetch-candidates-history  # 補抓候選股 13 個月歷史 OHLCV（MA60 斜率用）
make rotation                  # 次產業資金輪動，產 sector_rotation.md/csv
make cp-value-candidates       # 個股 CP 補漲候選，產 cp_candidates.md
make group                     # 族群分析，產 group_analysis.md＋candidates_enriched.csv
```

**產出**：

```
reports/YYYY-Www/
  ├─ screen_result_*.csv              ← 4 個 CSV（GROUP=defg → d/e/f/g；legacy abc → a/b/c）
  ├─ sector_rotation.md               ← ★ 全市場資金輪動地圖（四象限/★訊號/ΔRank）貼給 Claude
  ├─ cp_candidates.md                 ← ★ 個股 CP 補漲候選＋三重濾網（group Section 6 要讀）貼給 Claude
  ├─ group_analysis.md                ← Step 2 看這個（族群脈絡）貼給 Claude
  ├─ candidates_enriched.csv          ← 全候選股 × 完整技術/籌碼/估值/flags（主要挑股宇宙）貼給 Claude
  └─ holdings/watchlist_enriched.csv  ← 有維護 watchlist/ 才產（庫存/觀察清單・必分析）貼給 Claude
```

### Step 2：產出進場清單（兩種模式擇一）

**模式 2a（推薦）：ProPicks 風格全清單分析**

把 `group_analysis.md` + `sector_rotation.md` + `candidates_enriched.csv` + `cp_candidates.md`
+（若有）`holdings/watchlist_enriched.csv` + 4 個 `screen_result_*.csv` 貼到 Claude Opus 網頁對話，
配合範本 prompt 讓 AI 在「完整候選宇宙」中挑：

- **任務 0（必做）**：庫存決策（續抱/加碼/減碼/停利/停損）＋觀察清單進場時機
- 精選進場清單（寧缺勿濫）+ 為何入選 + 進場思路 + 主要風險
- 訊號交集（D∩E、E∩F、D∩E∩F）
- 本週市場節奏 + 居安思危訊號 + 異常崛起個股
- 觀察名單（追蹤但不進場）

完整 prompt + 流程 → [`docs/11-propicks-analysis.md`](./11-propicks-analysis.md)

Claude 回覆存到 `reports/YYYY-Www/picks.md`。

**模式 2b：純人工**

讀 `group_analysis.md`：

- **第 0.5-0.6 節**：候選股除權息、未來總經事件（FOMC/CPI/結算/法說…，事件曝險用）
- **第 1-2 節**：入選統計、族群強度排名
- **第 2.6-2.8 節**：次產業強度（半導體拆記憶體/封測…、含金融/航運）、概念股題材、**輪動雷達**（領先鏡頭 lead_score＋ΔRank）
- **第 3 節**：各族群前 3 名
- **第 4 節**：觀察
- **第 5 節**：Claude 次產業深度分析請求（2.8 雷達挑 top-N 領先次產業、逐塊列成員股）
- **第 6 節**：Claude CP 補漲候選分析請求（個股層，讀同夾 cp_candidates.md）

挑股以 picks.md 精選清單或 candidates_enriched.csv 為準（族群/次產業強度排名是機械公式、僅輪動參考，勿直接照挑），記下股號（如 `2330`、`3008`、`6147`...）。

> 模式 2b 簡單快，但只看機械強度排名（5 日漲幅 + 族群強度），會漏
> 「逆勢佈局」「低基期反轉」這類 setup。建議用 2a。

### Step 3：逐檔產資料草稿

對每一檔執行：

```bash
make report STOCK_ID=2330
```

第一次跑某檔會花約 5-10 秒（補該檔 3 個月 STOCK_DAY 歷史 OHLCV），之後吃快取。

**產出**：

```
reports/YYYY-Www/stocks/2330_台積電.md
```

裡面包含：

- 近 60 日 OHLCV（含真實 MA20、MA60）
- 近 12 月營收與 YoY
- 近 20 日三大法人買賣超
- **「給 Claude 的指示」段**（範本 prompt 已內建）
- 多個 `<!-- TODO: Claude 補寫 -->` 待填段落

依本週 `picks.md`（Claude 精選清單）逐檔跑 `make report STOCK_ID=XXXX`，即產出多份 draft。

### Step 4：把 draft 貼到 Claude 對話

1. 開 Claude（[claude.ai](https://claude.ai) 或 macOS/Windows 桌面 app）
2. **每檔開新對話**（避免上一檔的內容污染下一檔）
3. 開 `reports/YYYY-Www/stocks/2330_台積電.md` 把**整份檔案內容**複製
4. 貼進 Claude 對話框
5. 在貼上的內容**下方**加上 Step 5 的範本 prompt

### Step 5：給 Claude 的範本 prompt

複製下面整段，每檔貼一次：

```
以上是個股資料草稿。請依照檔案內「給 Claude 的指示」段的要求，補完所有
<!-- TODO --> 段落，產出完整的個股分析報告。

**特別注意**：

1. 請實際打開檔案內的 Goodinfo 連結，查看以下頁面再寫基本面與籌碼面：
   - 經營績效（EPS、毛利率、營業利益率、ROE 近 4 季）
   - 本益比河流圖（當前 PE 相對歷史位置）
   - 股利政策（連續配息年數、現金股利歷史）
   - 籌碼/董監持股
2. 多方論點 3-5 點，空方論點 3-5 點，空方不可少於多方
3. 進場條件給具體價位（從技術面 MA20、MA60、區間高低點推算）
4. 禁用「目標價」「強烈建議」「飆股」「保證」「絕對」字眼
5. 「資料來源」段補上 Goodinfo 個股頁（YYYY-MM-DD 取）

請直接輸出完整的 Markdown，我會複製貼回 reports/YYYY-Www/stocks/ 取代草稿。
```

Claude 回覆完整 Markdown 後：

1. 複製 Claude 的回覆
2. 貼回 `reports/YYYY-Www/stocks/2330_台積電.md`（覆蓋原本的 draft）
3. 重複下一檔

---

## 第 3 節 — 批次處理建議

依本週 `picks.md` 逐檔跑 `make report STOCK_ID=XXXX` 產出多份 draft 後，建議的工作流：

1. 多個 Claude 對話視窗各開一檔
2. 平行貼上 + 等 Claude 回覆
3. 一份份貼回對應檔案

或更慢但更安全：一檔一檔做完，避免搞混。

---

## 第 4 節 — 完成後

報告寫完後，依個人習慣決定要不要：

- **維護庫存/觀察清單**（下次 `make group` 會自動 enrich＋強制分析，見任務 0）：
  - 新進場 → 加進 `watchlist/holdings.csv`（`股號,買入價,股數,備註`；含成本、已 gitignore 不外流）
  - 想追蹤 → 加進 `watchlist/watchlist.csv`（`股號,備註`）
  - `watchlist/active.md` 仍可當自由筆記（不進分析流程）
- `git add reports/ watchlist/ && git commit -m "Weekly analysis YYYY-Www"`（holdings.csv 已 gitignore，不會被加入）
- 用 `make weekend` 也可以（會自動 commit + push）

---

## 第 5 節 — 已知限制

| 項目 | 狀態 | 影響 |
|---|---|---|
| 上櫃股票（5xxx、6xxx、8xxx）歷史 OHLCV | 不支援（TPEX API 尚未接） | 上櫃股票仍只有累積中的 daily 快取，MA20 可能不準 |
| 季報 EPS / 毛利率 | 走 Goodinfo（Claude 看） | 數字依賴 Goodinfo 即時可用 |
| 法人 T86 | 已修（legacy URL） | 上市股可用；上櫃股不在 T86 範圍 |
| Claude API 自動產報告 | 需 ANTHROPIC_API_KEY | 無 key 走本 SOP 手動模式 |

---

## 第 6 節 — 加快流程：設定 API key（可選）

如果想跳過手動 Claude 對話，到 [console.anthropic.com](https://console.anthropic.com) 申請 API key：

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # 加到 ~/.zshrc 或 ~/.bashrc 永久生效
make report STOCK_ID=2330
```

`make report` 會直接呼叫 Claude API（claude-sonnet-4-6），產出完整報告，
不需要本 SOP 的 Step 4-5。
