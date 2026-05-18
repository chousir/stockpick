# 每週選股 SOP — 以 2026-05-17（W20）為例

---

## Step 1 — 環境初始化
> 首次 clone 後做一次，之後不需重跑。

```bash
make sync && make init
```

| 動作 | 說明 |
|---|---|
| `uv sync` | 安裝 / 更新 Python 依賴 |
| `make init` | 建立必要資料夾 |

**產出：**

```
data/cache/      ← 快取目錄
data/raw/        ← 原始暫存
reports/         ← 每週報告
logs/            ← 執行 log
watchlist/       ← 自選股清單
```

---

## Step 2 — 每週主流程

```bash
make week GROUP=abc   # 跑 A/B/C 經典三角
# 或
make week GROUP=def   # 跑 D/E/F ProPicks 復刻組
```

`make week` 是三個子步驟的串聯：

```
fetch-twse  →  screen-all (GROUP)  →  fetch-candidates-history  →  group
```

**GROUP 必填**：`make week` 不帶 GROUP 會報錯。兩組互斥，每週只能跑一組。

---

### 2a. `fetch-twse` — 抓今日全市場資料

| 資料 | 來源 | 快取位置 |
|---|---|---|
| 全市場日線（1800+ 檔，今日 OHLCV） | TWSE OpenAPI | `data/cache/twse/daily_20260517.parquet` |
| 三大法人買賣超（今日） | TWSE Legacy T86 | `data/cache/twse/institutional_20260517.parquet` |
| 月營收（最新一期） | TWSE OpenAPI | `data/cache/twse/revenue_*.parquet` |
| 上市產業分類 | TWSE OpenAPI | `data/cache/twse/industry_*.parquet` |
| 上櫃產業分類 | ISIN 頁面 | `data/cache/twse/otc_industry_*.parquet` |

---

### 2b. `screen-all` — 三組策略篩選

從 Goodinfo 自訂條件篩選，每組策略對應一份 CSV。**依 GROUP 決定跑哪 3 組**。

`GROUP=abc`（經典三角，不重疊、各覆蓋單維度）：

| 策略 | 條件概念 | 產出檔案 |
|---|---|---|
| **A 動能突破** | MACD 翻多 + 均線多頭 + 流動性 | `screen_result_a_breakout.csv` |
| **B 成長主力** | 營收 YoY + 連續淨利 + 外資連買 | `screen_result_b_growth_institutional.csv` |
| **C 品質價值** | ROE + 連續配息 + 殖利率 | `screen_result_c_quality_value.csv` |

`GROUP=def`（ProPicks 復刻組，每組混合多個 ProPicks 因子）：

| 策略 | 條件概念 | 產出檔案 |
|---|---|---|
| **D 品質龍頭** | 市值 + ROE + 配息 + 淨利連增（TWCH15 風） | `screen_result_d_quality_leader.csv` |
| **E 成長動能** | 市值 + YoY 20% + 均線多頭（Tech Titans 風） | `screen_result_e_growth_momentum.csv` |
| **F 價值反彈** | 市值 + 低 PER + 殖利率 + 反陷阱（Top Value 風） | `screen_result_f_value_rebound.csv` |

每個 CSV 約 5–30 檔個股，欄位包含：股號、股名、策略分數、關鍵指標。

**產出路徑：** `reports/2026-W20/`

---

### 2c. `group` — 族群分析

```
三個 CSV  →  對照產業分類  →  計算族群強度分數  →  找領頭羊  →  group_analysis.md
```

**產出：** `reports/2026-W20/group_analysis.md`

報告結構：

1. 本週入選總覽（各策略筆數、三策略交集）
2. 族群強度排序（哪幾個產業本週最強）
3. 各族群領頭羊候選
4. 推薦深度分析優先順序（前 5–10 檔）
5. 觀察段（`<!-- TODO: Claude 補寫 -->`，等你手動補）

---

## Step 3 — 人工判斷

```bash
cat reports/2026-W20/group_analysis.md
```

讀第 4 節「推薦深度分析優先順序」，挑出 5–10 檔感興趣的股號。

> 這步沒有程式產出，是人腦決策。

---

## Step 4 — 個股資料打包與報告產出

```bash
# 單檔
make report STOCK_ID=2330

# 或批次跑推薦清單前 5 檔
make report-batch
```

執行流程：

1. 自動補抓該檔 **3 個月歷史 OHLCV**（首次約 5–10 秒）
2. 打包資料：近 60 天 K 線、近 20 日法人買賣超、近 12 月營收、族群資訊
3. 依 API key 狀態走不同路（見 Step 5）

**產出：** `reports/2026-W20/stocks/2330_台積電.md`

---

## Step 5 — 完成分析（兩種模式）

| | 有 `ANTHROPIC_API_KEY` | 無 API Key |
|---|---|---|
| **Step 4 行為** | 自動呼叫 Claude API | 產出資料草稿（數字齊全，分析空白） |
| **你需要做的** | 直接讀報告 | 把整份 `.md` 貼到 Claude 網頁對話 |
| **參考文件** | — | `docs/10-sop.md`（含範本 prompt） |
| **耗時** | 5–10 秒 / 檔 | 約 2–3 分鐘 / 檔（手動） |

---

## 今日時間線（0517 週日早上）

```
08:00  make week GROUP=abc   → 約 3–8 分鐘（含 Goodinfo rate limit sleep）
                              # 想跑 ProPicks 風：make week GROUP=def
08:10  讀 group_analysis.md，挑 5 檔
08:15  make report-batch     → 跑前 5 檔，每檔 5–15 秒
08:20  讀 5 份個股報告，決定本週 watchlist
08:30  make weekend GROUP=abc → commit + push 結果到 git（GROUP 必填）
```

今天是 W20 最後一天。**下週六重複 Step 2–5。**
