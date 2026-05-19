# 02 — 資料來源規範

## 主要資料源

| 來源 | 用途 | 取得方式 | 合規狀態 |
|---|---|---|---|
| Goodinfo!台灣股市資訊網 | 自訂條件篩選 | 爬蟲（合規限速） | 灰色，需自律 |
| TWSE OpenAPI (`openapi.twse.com.tw/v1`) | 全市場當日日 K、月營收、上市公司產業 | REST API | 完全合法 |
| TWSE Legacy (`www.twse.com.tw`) | 歷史 OHLCV (`STOCK_DAY`)、三大法人 (`T86`) | REST API（response=json） | 完全合法 |
| TWSE ISIN (`isin.twse.com.tw/isin/C_public.jsp?strMode=4`) | 上櫃公司產業分類 | HTML（MS950 編碼） | 完全合法 |

## Goodinfo 爬蟲規範（重要，違反會被擋）

### 反爬機制
Goodinfo 會偵測：
1. 短時間高頻請求
2. 沒帶 User-Agent 的請求
3. 沒帶 Referer 的請求
4. 同 IP 大量不同頁面遍歷

被擋時錯誤訊息：「您的瀏覽量異常已影響網站速度」，IP 通常會被擋 1-24 小時。

### 強制規則（**寫死在 fetcher 中，不可繞過**）

```python
# config/settings.yaml
goodinfo:
  base_url: "https://goodinfo.tw"
  request_interval_seconds: 3.0       # 最小間隔
  jitter_seconds: 1.0                  # ±1 秒隨機抖動
  cache_ttl_hours: 24                  # 同 URL 24 小時內讀快取
  max_retries: 3                       # 失敗最多重試 3 次
  backoff_base: 5                      # 指數退避，5s, 25s, 125s
  concurrency: 1                       # 嚴格序列
  user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
  referer: "https://goodinfo.tw/tw/index.asp"
```

### URL 結構（自訂篩選）

```
https://goodinfo.tw/tw/StockList.asp
  ?MARKET_CAT=自訂篩選
  &INDUSTRY_CAT=我的條件
  &FL_ITEM0=<條件1名稱>          ← URL-encoded 中文
  &FL_VAL_S0=<起始值>
  &FL_VAL_E0=<結束值>
  &FL_ITEM1=...
  &FL_RULE0=<規則1>              ← 如「週MACD ↗ 還原權值」
  &FL_RULE1=...
  &FL_SHEET=交易狀況               ← 顯示欄位類別
  &FL_SHEET2=日                    ← 時間粒度
  &FL_MARKET=上市/上櫃
  &FL_QRY=查 詢                    ← 觸發查詢
```

**關鍵注意事項**：
- 條件項目名稱、規則名稱**會更新**（歷史上 `FILTER_ITEM` → `FL_ITEM`）
- 所以爬蟲不要寫死，**從 YAML 讀條件**、由 `url_builder` 組裝
- HTML 結構小幅變動時，parser 用「中文欄名」定位欄位，不用 CSS index

### 三層模組設計

```
src/tw_screener/screener/goodinfo/
├── url_builder.py
│   def build_screener_url(strategy: StrategyConfig) -> str
│
├── fetcher.py
│   class GoodinfoFetcher:
│       def get(url) -> str       # 含 rate limit + cache + retry
│
└── parser.py
    def parse_screener_result(html: str) -> pl.DataFrame
```

### 快取設計

```
data/cache/goodinfo/
├── screener/
│   └── <md5(url)>.html.gz           # 含時間戳，超過 24h 失效
└── detail/
    └── <stock_id>_<yyyymmdd>.html.gz
```

讀取時：先檢查快取存在 + 未過期，再決定打網。

### 測試規範
- 解析器測試**禁止打網**，用 `tests/fixtures/goodinfo/*.html` 離線檔。
- 加新策略時，跑一次抓真實 HTML 存進 fixtures，作為回歸測試基準。

## 證交所 OpenAPI + Legacy

文件：https://openapi.twse.com.tw/

**不需要 token、不需要註冊、限速寬鬆**（但仍建議 1 req/sec 內）。

### 常用 endpoints

| 用途 | 端點 | 備註 |
|---|---|---|
| 上市公司產業 | OpenAPI `/v1/opendata/t187ap03_L` | 只含上市股 |
| 上櫃公司產業 | ISIN `isin.twse.com.tw/isin/C_public.jsp?strMode=4` | MS950 編碼，需 HTML 解析 |
| 全市場當日 OHLCV | OpenAPI `/v1/exchangeReport/STOCK_DAY_ALL` | **`date` 參數被無視，永遠回今天** |
| 單檔月份 OHLCV | Legacy `www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMM01&stockNo=XXXX` | 支援歷史，按需回補 3 個月 |
| 三大法人買賣超 | Legacy `www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&selectType=ALLBUT0999` | OpenAPI 版已失效（回 HTML）|
| 個股月營收 | OpenAPI `/v1/opendata/t187ap05_L` | |
| 融資融券餘額 | OpenAPI `/v1/exchangeReport/MI_MARGN` | |

> 已知陷阱見 `docs/99-troubleshooting.md` #1（T86 endpoint 變化）與 #2（STOCK_DAY_ALL 不支援歷史日期）。

櫃買中心 OpenAPI: https://www.tpex.org.tw/openapi/

### 用途定位

證交所 API 補充 Goodinfo 不便取得的「歷史長序列」資料：
- 計算族群相對強度需要 60-120 天 OHLCV → 證交所
- 個股深度報告需要近 12 個月營收 → 證交所
- 全市場掃描的快取基底 → 證交所

Goodinfo 主要用在「條件組合篩選」這個它真正強的地方。

## 資料更新頻率與最早可用時點

| 資料 | 最早可用時點 | 更新頻率 | 處理方式 |
|---|---|---|---|
| Goodinfo 篩選結果 | 收盤後 30–60 分鐘 | 每週一次或更頻繁 | 24h 內讀 cache |
| TWSE 日線（STOCK_DAY_ALL）| 收盤後 ~30 分鐘 | 每交易日 | 累積 parquet |
| TWSE T86 三大法人 | 收盤後約 90 分鐘（**15:00 起穩定**）| 每交易日 | 累積 parquet |
| 月營收（t187ap05_L） | 每月 10 號前 | 每月 | cron 或手動 |
| 上市產業分類（t187ap03_L）| 月內穩定 | 每月 | 月更新 |
| 上櫃產業分類（ISIN）| 月內穩定 | 每月 | 月更新 |

**建議跑 `make week` 的時段**：交易日 **15:00 起**。在此之前 T86 可能還沒發布，
Goodinfo 漲跌幅資料也可能不完整。

## TPEX 上櫃個股歷史（2026-W21 新增）

上市股的 OHLCV 歷史走 TWSE `STOCK_DAY`，上櫃股原本沒有對應來源 →
W20 觀察到 OTC 股的 5 日動能 fallback 到當日 change_pct（誤差）。
2026-W21 起新增 TPEX 來源。

### 端點
`https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={sid}&date=YYYY/MM/01`

回 JSON，結構：
```json
{
  "stat": "ok",
  "code": "3293",
  "tables": [{
    "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤", "漲跌", "筆數"],
    "data": [["115/05/01", "1,234", "987,654", ...], ...]
  }]
}
```

### 與 TWSE STOCK_DAY 差異
| 項目 | TWSE | TPEX |
|---|---|---|
| 日期格式（請求）| `YYYYMM01`（如 `20260501`）| `YYYY/MM/01`（如 `2026/05/01`）|
| 日期格式（回應）| 民國年 `115/05/01` | 民國年 `115/05/01` |
| 量單位 | 股 | 仟股（parser 自動 ×1000）|
| 金額單位 | 元 | 仟元（parser 自動 ×1000）|
| fields 位置 | 頂層 | `tables[0]` 內 |
| 成交筆數欄名 | `成交筆數` | `筆數` |
| stat 值 | `OK` | `ok` |

### 自動分派
`TWSEClient.fetch_stock_history(stock_id)`：
1. lazy load `otc_industry_*.parquet`，取 OTC stock_id 集合
2. stock_id 在集合內 → 走 TPEX
3. 否則 → 走 TWSE
Cache 命名共用 `stock_day_{stock_id}_{YYYYMM}.parquet`，下游 `load_candidate_history()`、
`fetch_stock_ohlcv()`、`momentum.compute_n_day_return()` 一律無感。

`fetch_stock_history_tpex()` 也對外公開，可單獨呼叫（測試或除錯用）。

---

## trading_date 錨點

為了支援「任意時段跑」（週末、週一早上、收盤前），所有交易日相關的檔名與週標籤
**不再使用執行當下的 `date.today()`**，而是統一以 `TWSEClient.latest_trading_date()`
回傳的「最近一個交易日」為錨點。

| 觸發點 | 錨點來源 |
|---|---|
| `daily_{YYYYMMDD}.parquet` 檔名 | 解析後 `df["date"].max()` |
| `institutional_{YYYYMMDD}.parquet` 檔名 + T86 query date | `latest_trading_date()` |
| `screen_result_*.csv` 的 `screened_at` 欄 | `latest_trading_date()` |
| `reports/YYYY-Www/` 週目錄名 | `latest_trading_date().strftime("%Y-W%V")` |

範例：
- 2026-05-17（週日）下午跑 → TWSE 回 5/15 → 全部檔名 / 週標籤對齊 `2026-05-15` / `W20`
- 2026-05-18（週一）09:00 跑 → 同上（5/18 T86 還沒發） → 仍對齊 W20
- 2026-05-18（週一）15:30 跑 → TWSE 回 5/18 → 對齊 `2026-05-18` / `W21`，新建 reports/2026-W21/

## 合法性聲明

- Goodinfo 沒有公開 API、沒有明確禁止爬蟲的服務條款，但有反爬機制。
- 本專案的合規策略：**像一個認真使用者用程式輔助瀏覽，而非攻擊者**。
- 每週請求量級：3 個策略 × 1 個篩選結果頁 + ~30 檔個股詳情頁 ≈ 35 個請求 / 週。
  在 3 秒間隔下總耗時 < 3 分鐘，遠低於人工瀏覽量。
- 不用於商業用途、不對外提供服務、不分享抓取結果。
