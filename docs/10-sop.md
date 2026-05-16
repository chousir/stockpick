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
make week
```

等同於：

```bash
make fetch-twse     # 抓 TWSE 全市場日線、法人、月營收
make screen-all     # 跑三組策略，產 screen_result_*.csv
make group          # 族群分析，產 group_analysis.md
```

**產出**：

```
reports/YYYY-Www/
  ├─ screen_result_a_breakout.csv
  ├─ screen_result_b_growth_institutional.csv
  ├─ screen_result_c_dividend_steady.csv
  └─ group_analysis.md         ← Step 2 看這個
```

### Step 2：讀 group_analysis.md 挑股

`group_analysis.md` 結構：

- **第 1-2 節**：入選統計、族群強度排名
- **第 3 節**：各族群領頭羊
- **第 5 節**：**推薦個股深度分析優先順序（前 10 檔）** ← 重點看這
- **第 6 節**：給 Claude 的族群深度分析請求（前 4 大領漲族群）

從第 5 節挑 5-10 檔有興趣的，記下股號（如 `2330`、`3008`、`6147`...）。

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

也可以一次跑前 5 檔：

```bash
make report-batch
```

會自動讀 `group_analysis.md` 第 5 節，產出 5 份 draft。

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

`make report-batch` 一次產 5 份 draft 後，建議的工作流：

1. 5 個 Claude 對話視窗各開一檔
2. 平行貼上 + 等 Claude 回覆
3. 一份份貼回對應檔案

或更慢但更安全：一檔一檔做完，避免搞混。

---

## 第 4 節 — 完成後

報告寫完後，依個人習慣決定要不要：

- 把追蹤的股加進 `watchlist/active.md`
- `git add reports/ watchlist/ && git commit -m "Weekly analysis YYYY-Www"`
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
