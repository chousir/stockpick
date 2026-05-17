# 06 — 個股深度報告規格

## 定位

這份報告有兩種產出模式：
1. **API 模式**：`make report STOCK_ID=2330`，自動呼叫 Claude API 產出完整報告
2. **手動模式**：`make report STOCK_ID=2330`（無 API key）→ 產出資料草稿 → 手動貼給 Claude 補寫分析

詳細手動模式 SOP 見 `docs/10-sop.md`。

## 報告檔名規範

```
reports/YYYY-Www/stocks/{stock_id}_{name_zh}.md
```

範例：`reports/2026-W21/stocks/2330_台積電.md`

## 報告結構

完整框架見 `CLAUDE.md` Part 3.2，這裡是檔案版的模板：

```markdown
# {stock_id} {name_zh} 個股分析報告

> 分析日期：YYYY-MM-DD  
> 入選策略：A（波段啟動）/ B（法人成長）  
> 族群：半導體業  
> 族群排名：1/5（領頭羊候選）  
> Goodinfo: https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID={stock_id}

## 基本資訊
產業 | 市值 | 近一年股價區間

## 基本面
近 4 季營收 YoY、毛利率趨勢、EPS 趨勢、本益比相對歷史區間。
（數字請打開 Goodinfo 連結查看）

## 籌碼面
近 20 日三大法人買賣超、融資增減、董監持股變化、大戶持股比。

## 技術面
當前位於月線/季線/年線位置、近期關鍵價位（壓力、支撐）。

## 多方論點
- 點 1
- 點 2
- 點 3

## 空方論點 / 風險
- 風險 1
- 風險 2
- 風險 3

## 進場條件
- 觸發訊號：...
- 建議價位：...
- 停損點：...

## 不適合進場的情境
- 情境 1
- 情境 2

## 族群與相對位置
本檔在所屬族群中的角色、與龍頭股的比較。

## 資料來源
- Goodinfo 個股頁（YYYY-MM-DD 取）
- 證交所月營收（YYYY-MM-DD 取）
- 三大法人 T86（YYYY-MM-DD 取）
```

## 資料抓取

```python
# src/tw_screener/report/data_fetcher.py

def fetch_stock_bundle(stock_id: str, settings_path: Path) -> dict:
    """
    一次取得個股報告所需的所有資料。

    回傳：
    {
        "stock_id": str,
        "name": str,
        "industry_name": str,
        "goodinfo_url": str,          # https://goodinfo.tw/tw/...
        "price_summary": str,         # 近 60 日 OHLCV 摘要文字（含 MA20、MA60）
        "revenue_summary": str,       # 近 12 月營收與 YoY 文字
        "institutional_summary": str, # 近 20 日三大法人摘要文字
        "group_info": dict,           # 族群名、排名、是否領頭羊候選
        "fetched_at": str,            # ISO 8601 timestamp
    }
    """
```

> **設計決策（已決策）**：`goodinfo_summary` 和 `quarterly_financials`（EPS、毛利率、本益比）**不在 bundle 中**。
> 這類資料由 Claude 自行打開 `goodinfo_url` 查看，省去爬 Goodinfo 的複雜度與速率限制。
> prompt 中明確指示 Claude 查看 Goodinfo 的哪些頁面。

流程：
1. `fetch_stock_history(stock_id, months=3)` — 自動回補 3 個月 OHLCV（第一次跑約 5-10 秒）
2. `fetch_stock_ohlcv(stock_id, n_days=60)` — 讀快取，優先讀 `stock_day_*.parquet`
3. `fetch_stock_revenue(stock_id, n_months=12)` — 讀快取
4. `fetch_stock_institutional(stock_id, n_days=20)` — 讀快取
5. 合併上市 + 上櫃 industry_df，查出產業名稱
6. 讀本週 `group_analysis.md` 取得族群排名與是否領頭羊

## Prompt 模板

`src/tw_screener/report/prompts/stock_report.md.j2`（Jinja2）：

```markdown
你正在為 {{ stock_id }} {{ name }} 產出個股分析報告。

## 已抓取的資料（直接引用，不要從記憶估算）

### 近 {{ price_days }} 個交易日 OHLCV 摘要
{{ price_summary }}

### 近 12 月營收
{{ revenue_summary }}

### 近 20 日三大法人
{{ institutional_summary }}

### 所屬族群
- 族群：{{ group_name }}
- 族群強度排名：{{ group_rank }}
- 是否為領頭羊候選：{{ is_leader }}

## 補充資料指示

上方資料未含 EPS、毛利率、本益比、股利歷史、股本、董監持股。
請打開 Goodinfo 連結（{{ goodinfo_url }}）查看以下頁面：
- 經營績效（EPS、毛利率、營業利益率、ROE 趨勢）
- 本益比河流圖（當前 PE 相對歷史區間位置）
- 股利政策（連續配息年數、現金股利歷史）
- 籌碼/董監（董監持股比例、大股東名單）

## 輸出要求

請依照 CLAUDE.md Part 3 的個股報告框架產出完整 Markdown。

特別注意：
1. 所有數字必須來自上面的資料區塊或 Goodinfo，不可從記憶估算
2. 多方論點 3-5 點，空方論點 3-5 點，空方不可少於多方
3. 進場條件給具體價位（從 MA20、MA60、區間高低點推算）
4. 結尾要有「資料來源」段，列出各資料的取得時間
5. 禁用「目標價」「強烈建議」「飆股」「保證」「絕對」字眼
```

## CLI 觸發

```bash
# 單檔
make report STOCK_ID=2330

# 批次（讀本週 group_analysis.md 推薦前 N 檔）
make report-batch
```

> `report-list STOCKS="2330,3034,2454"`（自訂清單批次）**未實作**，如需要自行多次執行 `make report STOCK_ID=XXXX`。

產出路徑：`reports/YYYY-Www/stocks/{stock_id}_{name}.md`

## 兩種模式

| 模式 | 觸發條件 | 產出 |
|---|---|---|
| API 模式 | 環境變數 `ANTHROPIC_API_KEY` 存在 | 完整分析報告（Claude 直接產出） |
| 草稿模式 | 無 API key | 資料草稿 + 「給 Claude 的指示」段 + `<!-- TODO: Claude 補寫 -->` 標記 |

草稿模式手動流程見 `docs/10-sop.md`。

## 三大禁區

報告**絕對不能**出現：
1. 「目標價 XXX 元」、「會漲到 XXX」這種價格預測
2. 「強烈建議買進」、「飆股」、「絕對不會錯」這類字眼
3. 「我推薦…」、「我認為應該買」這種第一人稱推薦

報告**必須**：
1. 多空並陳，空方不可少於多方
2. 每個數字附時間與來源
3. 明確列出「不適合進場的情境」

## 後續累積

每份報告寫完後，可加入：
- `watchlist/active.md`（若決定追蹤）
- `watchlist/waiting.md`（條件未達，等訊號）
- 三個月後 `make backtest-strategies` 回看當週入選股的後續表現（M6 骨架，尚未實作回測邏輯）
