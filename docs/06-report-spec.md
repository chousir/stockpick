# 06 — 個股深度報告規格

## 定位

這份報告是 **Claude Code 互動式產出** 的，不是 batch job。
工作流：
1. 你看完 `group_analysis.md`，挑出 5-10 檔要深入
2. 開 `claude` session
3. 說「分析本週 2330、3034、2454」
4. Claude Code 自動抓資料、套框架、產報告
5. 每檔一份 Markdown，存 `reports/YYYY-Www/stocks/`

## 為什麼互動式而非批次

- 寫深度報告需要 Claude 的判斷力（多空權重、論點取捨）
- 不同時期的市場氣氛不同，prompt 需要微調
- 批次跑容易產出「八股式」報告，互動允許追問與修正

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
> 族群：半導體-IC 設計  
> 族群排名：1/5（領頭羊候選）  
> Goodinfo: https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID={stock_id}

## 基本資訊
產業 | 市值 | 近一年股價區間

## 基本面
近 4 季營收 YoY、毛利率趨勢、EPS 趨勢、本益比相對歷史區間。

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

## Prompt 模板

`src/tw_screener/report/prompts/stock_report.md`：

```markdown
你正在為 {stock_id} {name_zh} 產出個股分析報告。

## 已抓取的資料（直接引用，不要從記憶估算）

### Goodinfo 個股頁摘要
{goodinfo_summary}

### 近 12 月營收
{monthly_revenue}

### 近 4 季財報
{quarterly_financials}

### 近 20 日三大法人
{institutional}

### 近 60 日 OHLCV
{price_history}

### 所屬族群分析結果
- 族群：{group_name}
- 族群排名：{group_rank}
- 是否為領頭羊候選：{is_leader_candidate}

## 要求

請依照 CLAUDE.md Part 3 的個股報告框架產出 Markdown，存到：
reports/{week_tag}/stocks/{stock_id}_{name_zh}.md

特別注意：
1. 所有數字必須來自上面的資料區塊，不可從記憶估算
2. 多方論點 3-5 點，空方論點 3-5 點，空方不可少於多方
3. 進場條件給具體價位
4. 結尾要有「資料來源」段，列出各資料的取得時間
```

## 資料抓取（給 Claude Code 用）

Claude Code 不直接打網，透過你寫好的 helper：

```python
# src/tw_screener/report/data_fetcher.py

def fetch_stock_bundle(stock_id: str) -> dict:
    """
    一次取得個股報告所需的所有資料。
    
    回傳：
    {
        "goodinfo_summary": str,        # 從 Goodinfo 個股頁解析的摘要
        "monthly_revenue": pl.DataFrame, # 近 12 月
        "quarterly_financials": pl.DataFrame,
        "institutional": pl.DataFrame,
        "price_history": pl.DataFrame,
        "group_info": dict,
    }
    """
```

Claude Code 互動時：

```
你：分析 2330
Claude Code: 
  - 呼叫 fetch_stock_bundle("2330")
  - 讀 prompts/stock_report.md
  - 填入資料
  - 產出 Markdown
  - 寫到正確路徑
  - 給你檔案連結
```

## CLI 觸發（半自動）

`make report STOCK_ID=2330` 也可以單檔觸發，不一定要走 Claude Code 互動。

```bash
# 單檔
make report STOCK_ID=2330

# 多檔（讀本週 group_analysis.md 的推薦清單）
make report-batch

# 自訂清單
make report-list STOCKS="2330,3034,2454"
```

但**互動式產出（透過 Claude Code）的品質會比較好**，因為 Claude 可以根據前一份報告的結構與內容調整下一份。

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

每份報告寫完後，會被加入：
- `watchlist/active.md`（若你決定追蹤）
- 三個月後 `make backtest-strategies` 會回看：當週入選的這些股，後續表現如何
- 可以用來檢視「Claude 報告的多空判斷準度」
