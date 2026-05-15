# 02 — 資料來源規範

## 主要資料源

| 來源 | 用途 | 取得方式 | 合規狀態 |
|---|---|---|---|
| Goodinfo!台灣股市資訊網 | 自訂篩選、產業/概念分類 | 爬蟲（合規限速） | 灰色，需自律 |
| 證交所 OpenAPI (openapi.twse.com.tw) | 日 K 線、法人買賣、月營收 | REST API | 完全合法 |
| 櫃買中心 (otc.org.tw) | 上櫃股票資料 | REST API | 完全合法 |

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
    def parse_stock_list(html: str) -> pl.DataFrame
    def parse_stock_detail(html: str) -> dict
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

## 證交所 OpenAPI

文件：https://openapi.twse.com.tw/

**不需要 token、不需要註冊、限速寬鬆**（但仍建議 1 req/sec 內）。

### 常用 endpoints

| 用途 | URL |
|---|---|
| 上市公司基本資料 | `/v1/opendata/t187ap03_L` |
| 每日收盤行情（全市場） | `/v1/exchangeReport/STOCK_DAY_ALL` |
| 三大法人買賣超 | `/v1/fund/T86` |
| 融資融券餘額 | `/v1/exchangeReport/MI_MARGN` |
| 個股月營收 | `/v1/opendata/t187ap05_L` |

櫃買中心 OpenAPI: https://www.tpex.org.tw/openapi/

### 用途定位

證交所 API 補充 Goodinfo 不便取得的「歷史長序列」資料：
- 計算族群相對強度需要 60-120 天 OHLCV → 證交所
- 個股深度報告需要近 12 個月營收 → 證交所
- 全市場掃描的快取基底 → 證交所

Goodinfo 主要用在「條件組合篩選」這個它真正強的地方。

## 資料更新頻率

| 資料 | 更新頻率 | 處理方式 |
|---|---|---|
| Goodinfo 篩選結果 | 每週一次（週末手動 `make screen`） | 24h 內讀 cache |
| TWSE 日線 | 收盤後抓增量 | 累積 parquet |
| 月營收 | 每月 10 號前 | cron 或手動 |
| 三大法人 | 每日收盤後 1 小時 | 累積 parquet |

## 合法性聲明

- Goodinfo 沒有公開 API、沒有明確禁止爬蟲的服務條款，但有反爬機制。
- 本專案的合規策略：**像一個認真使用者用程式輔助瀏覽，而非攻擊者**。
- 每週請求量級：3 個策略 × 1 個篩選結果頁 + ~30 檔個股詳情頁 ≈ 35 個請求 / 週。
  在 3 秒間隔下總耗時 < 3 分鐘，遠低於人工瀏覽量。
- 不用於商業用途、不對外提供服務、不分享抓取結果。
