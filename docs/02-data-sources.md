# 02 — 資料來源規範

## 主要資料源

| 來源 | 用途 | 取得方式 | 合規狀態 |
|---|---|---|---|
| Goodinfo!台灣股市資訊網 | 自訂條件篩選 | 爬蟲（合規限速） | 灰色，需自律 |
| TWSE OpenAPI (`openapi.twse.com.tw/v1`) | 全市場當日日 K、月營收、上市公司產業 | REST API | 完全合法 |
| TWSE OpenAPI 除權息預告 (`exchangeReport/TWT48U_ALL`) | 事件層 Tier 1：候選股未來除權息行事曆 | REST API（免 key） | 完全合法 |
| TWSE Legacy (`www.twse.com.tw`) | 歷史 OHLCV (`STOCK_DAY`)、三大法人 (`T86`) | REST API（response=json） | 完全合法 |
| TWSE ISIN (`isin.twse.com.tw/isin/C_public.jsp?strMode=4`) | 上櫃公司產業分類 | HTML（MS950 編碼） | 完全合法 |
| Yahoo 股市 (`tw.stock.yahoo.com`) | 概念股/趨勢主題成分（多標籤主題） | 爬蟲（合規限速；**只爬概念股**） | 灰色，需自律 |
| FRED (`api.stlouisfed.org/fred/series/observations`) | 總經指標（BAA10Y 主訊號＋揭露面板，docs/25 M-Macro1） | 官方 REST API（免費 key） | 完全合法 |

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
│   └── <md5(url)>.html.gz           # 對齊交易日：盤後界線(預設 15:00)之前抓的視為過期
└── detail/
    └── <stock_id>_<yyyymmdd>.html.gz
```

讀取時：先檢查快取存在 + 未過期，再決定打網。

篩選快取的「未過期」對齊交易日，而非滾動 24h：以 `cache_refresh_hour`（盤後資料穩定時點，預設 15:00）為界，快取需在「最近一個交易日的界線之後」抓取才算新鮮。同一交易日盤後重複跑讀快取（不重複打 Goodinfo）；跨交易日則重抓。週末沿用前一交易日界線、不誤判失效；國定假日未建表，連假最多每日多抓一次（量極小）。

### 測試規範
- 解析器測試**禁止打網**，用 `tests/fixtures/goodinfo/*.html` 離線檔。
- 加新策略時，跑一次抓真實 HTML 存進 fixtures，作為回歸測試基準。

### 健康檢查 `screen doctor`（規劃書 02 D1，2026-06-27 新增）
- **合規確認**：doctor 探針沿用同一個 `GoodinfoFetcher`，上述「強制規則」（3s±1 間隔、交易日/24h 快取、concurrency=1、指數退避、可換 UA）**全部不變**。
- doctor 預設**不 force**、與 `screen-all` 共用快取行為：同一交易日只有第一次打網、之後讀快取，**不額外增加 Goodinfo 請求量**。
- 探針＝`config/doctor_probe.yaml`（純流動性 成交筆數≥50000，恆 >0 且遠低於匿名 300 上限），用來探「正常／被擋／改版／欄位改名」。
- `--replay` 離線解析既有 committed fixture 驗 parser 沒退化（不打網）；`--save-fixture` 才手動刷新黃金樣本，**不在每次抓取自動寫 fixtures**。

## 證交所 OpenAPI + Legacy

文件：https://openapi.twse.com.tw/

**不需要 token、不需要註冊、限速寬鬆**（但仍建議 1 req/sec 內）。

### 常用 endpoints

| 用途 | 端點 | 備註 |
|---|---|---|
| 上市公司產業 | OpenAPI `/v1/opendata/t187ap03_L` | 只含上市股 |
| 上櫃公司產業 | ISIN `isin.twse.com.tw/isin/C_public.jsp?strMode=4` | MS950 編碼，需 HTML 解析 |
| 全市場當日 OHLCV | OpenAPI `/v1/exchangeReport/STOCK_DAY_ALL` | **`date` 參數被無視，永遠回今天 → 只能往未來累積、過去補不回**（同 `otc_daily_all`）|
| 單檔月份 OHLCV | Legacy `www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMM01&stockNo=XXXX` | 支援歷史，**可回補**；冷啟動歷史密度靠它（`backfill-universe-history` 對次產業全成員逐檔補，D2）|
| 三大法人買賣超 | Legacy `www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&selectType=ALLBUT0999` | OpenAPI 版已失效（回 HTML）|
| 個股月營收 | OpenAPI `/v1/opendata/t187ap05_L` | |
| 融資融券餘額 | OpenAPI `/v1/exchangeReport/MI_MARGN` | |
| 上市官方日估值比 | OpenAPI `/v1/exchangeReport/BWIBBU_d` | 官方 trailing 本益比/殖利率/股價淨值比；只回最新交易日（逐日累積）|
| 上櫃官方日估值比 | TPEX OpenAPI `/openapi/v1/tpex_mainboard_peratio_analysis` | 同上（上櫃）；只回最新交易日 |
| 單季營益分析（上市） | OpenAPI `/v1/opendata/t187ap17_L` | 營收/毛利率/營益率/**稅前純益率/稅後純益率**；全市場、最新一季 |
| 單季營益分析（上櫃） | TPEX `/openapi/v1/mopsfin_187ap17_O` | 同上；欄名無括號（`稅前純益率`/`稅後純益率`）|
| 單季 EPS（上市/上櫃） | OpenAPI `/v1/opendata/t187ap14_L`／TPEX `mopsfin_t187ap14_O` | 基本每股盈餘；ROE 由 EPS/每股淨值算 |
| 單季簡式資產負債表（上市，一般業） | OpenAPI `/v1/opendata/t187ap07_L_ci` | 資產**總額**/負債**總額**/流動資產/流動負債/每股參考淨值 → 負債比/流動比/淨值（D5）|
| 單季簡式資產負債表（上櫃，一般業） | TPEX `/openapi/v1/mopsfin_t187ap07_O_ci` | 同上但欄名用「總**計**」、key 用「年度/季別」（非 Year）（D5）|

> 已知陷阱見 `docs/99-troubleshooting.md` #1（T86 endpoint 變化）與 #2（STOCK_DAY_ALL 不支援歷史日期）。

> **D5 財報體質盤點結論（2026-06-27）**：TWSE/TPEX OpenAPI **無現金流量表端點**（全目錄 143 path
> 無「現金」），且資產負債表為**簡式**（只有資產/負債/權益彙總，**無存貨、應收明細**）→
> **營業現金流、存貨/應收週轉率不可得**，個股報告誠實標「未取得」、不硬湊。資產負債表/綜合損益表
> 各依公司型態分 6 端點（`_ci`一般業/`_bd`/`_fh`/`_ins`/`_mim`/`_basi`），D5 只取 `_ci`（一般業）：
> 金融業負債結構語意本就不同，**金融業負債比/ROE 留 null**。

櫃買中心 OpenAPI: https://www.tpex.org.tw/openapi/

### 用途定位

證交所 API 補充 Goodinfo 不便取得的「歷史長序列」資料：
- 計算族群相對強度需要 60-120 天 OHLCV → 證交所
- 個股深度報告需要近 12 個月營收 → 證交所
- 全市場掃描的快取基底 → 證交所

Goodinfo 主要用在「條件組合篩選」這個它真正強的地方。

## Yahoo 股市 概念股主題（多標籤主題）

只爬「概念股」主題成分（次產業〔電子＋金融/航運〕沿用手標，完整不截斷）。資料在 SSR `root.App.main`、純 HTTP
取得，每筆帶乾淨 `systexId` 股號＋`symbolName`，**免名稱→股號比對**。`make build-themes` 把概念股
**merge 進 `config/concepts.yaml`**（單一檔）：以檔內 `concept_themes` 清單分辨自動概念股，重跑時
清舊換新、**手動次產業（電子＋金融/航運）原封不動**；`load_themes` 據 `concept_themes` 判每個標籤的 kind。

### 限制（已知、誠實揭露）
- 每個概念股主題 SSR **只內嵌前約 30 檔**（領頭觀察、非全量）；「載入更多」是 runtime 組出、
  帶 crumb 的前端 XHR，逆向脆弱且屬 ToS 灰區，**依爬蟲自律不爬**。
- 故 Yahoo 不抓次產業（電子約半數 >30 檔會被截斷，且來源分類太粗、無記憶體/封測/晶圓代工桶）；次產業（電子＋金融/航運）維持手標、完整。

### 只抓重要題材（白名單）
`config/settings.yaml` 的 `themes_build.concept_whitelist` 列出要抓的題材名（留空＝全部 101 個）。
有白名單時**只抓那幾頁**（又快又乾淨）。名稱須與 Yahoo 完全一致。目前 Yahoo 全部 101 個概念股題材：

> 蘋果200大供應商、MOSFET、AI理財機器人、Sharp、Google Pixel、米其林摘星、夏日飲料、無人商店、跨國連鎖餐飲、AirPods、VR虛擬實境、汽車電子、台幣升值、中國台商汽車組件、智慧音箱、Apple watch、AI人工智慧、空污、Apple Pay、紡織機能運動、再生循環、銀髮商機、Tesla、雙十一、3D感測、日圓貶值、互聯網+、三星、第三方支付、iPhone、一帶一路、車聯網、3D列印、補教/民辦教育、新掛牌、Apple iTV、小米、Nike、雲伺服器、Amazon Go應用技術、iPad、電競產業、空污防治、行動支付、台資在中國高獲利、折疊手機、人民幣升值、歐元貶值、台積電、無人機、聯發科、WiFi 6、Mini LED、FinTech、網紅經濟、水資源、日圓升值、矽智財(IP)、OPPO、都更、ADAS、國防自主、Intel、眼球商機、風力發電/離岸風電、醫療呼吸器、宅經濟、功率半導體、大數據2.0、HomePod、無線充電、任天堂Switch、智慧家庭、防疫、博弈觀光、5G、Toyota、穿戴裝置、丹麥沃旭、華為、半導體設備、衛星/低軌衛星、體育/運動產業、智慧汽車、電子商務、海外掛牌、嬰幼兒相關、比特幣挖礦、機器人/智慧機械、工業4.0、電動車/油電車、通膨、航空/航太、環保綠能材料、智慧城市、植物工廠、雲端產業、PlayStation 5、物聯網、智慧醫療、人臉辨識

### 強制規則（同 Goodinfo 等級自律，寫在 `config/settings.yaml` `yahoo:`）
- 請求間隔 ≥3 秒 + 抖動、真實瀏覽器 UA（可換）、24h 快取、concurrency=1、連續失敗指數退避。
- 全量約 101 個概念股頁 × 3 秒 ≈ 5–7 分；久久跑一次（`make build-themes`，`DRY=1` 只產 candidate）。
- 解析離線測試用 `tests/fixtures/yahoo/`，不每次打網。

## 資料更新頻率與最早可用時點

| 資料 | 最早可用時點 | 更新頻率 | 處理方式 |
|---|---|---|---|
| Goodinfo 篩選結果 | 收盤後 30–60 分鐘 | 每週一次或更頻繁 | 對齊交易日：同日讀 cache、跨交易日重抓 |
| Yahoo 概念股主題成分 | 即時 | 鮮少變（季度級） | 24h 快取；手動 `make build-themes` 更新 |
| TWSE 日線（STOCK_DAY_ALL）| 盤後（**OpenAPI 版實測常延到隔日凌晨/早上才更新**；2026-07-20 週一 20:45 實查仍只回上週五 7/17）| 每交易日 | 累積 parquet（**不可回補**：每日累積或一次性 `backfill-universe-history` 補單檔歷史）|
| TWSE T86 三大法人 | 收盤後約 90 分鐘（**15:00 起穩定**）| 每交易日 | 累積 parquet |
| 官方日估值比（BWIBBU_d / peratio）| 收盤後 ~30 分鐘 | 每交易日 | 累積 parquet（自身歷史百分位用）|
| 月營收（t187ap05_L） | 每月 10 號前 | 每月 | cron 或手動 |
| 上市產業分類（t187ap03_L）| 月內穩定 | 每月 | 月更新 |
| 除權息預告（TWT48U_ALL）| 隨時（前瞻 ~2 個月）| 每日小幅變動 | 事件層：只取候選股、未來約 2 週窗 |
| 上櫃產業分類（ISIN）| 月內穩定 | 每月 | 月更新 |

**建議跑 `make week` 的時段**：交易日 **15:00 起**。在此之前 T86 可能還沒發布，
Goodinfo 漲跌幅資料也可能不完整。

**週次錨點與「上櫃法人領先」的不對稱**：週次標籤來自 `latest_trading_date()`
＝TWSE 日線（STOCK_DAY_ALL）的 max(date)。由於該端點常延到隔日才更新，週一盤後跑
仍會落在上週五 → 報告標為上週（正確：代表剛收盤的交易週）。但 **TPEX 上櫃法人
更新較快**，快取可能已含「比日線更新一天」的上櫃法人（如週一那筆），若直接餵進
族群/輪動/CP 的法人多窗，會讓上櫃股領先價格一天、且與上市股不對稱。**處理**：所有
即時週報路徑呼叫 `load_institutional_history(as_of=latest_trading_date())` 把法人右界
對齊價格錨點（回測/歷史序列用 `as_of=None` 不封頂）。

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

## 官方日估值比 BWIBBU（2026-06-14 新增）

估值層（CP 值研究 C1）原本用「單季 EPS×4 年化」代理 PE，非真 trailing。改用官方日資料：

### 端點
| 市場 | 端點 | 日期格式 | PE 欄 | PB 欄 | 殖利率欄 | 缺值 |
|---|---|---|---|---|---|---|
| 上市 | TWSE OpenAPI `/v1/exchangeReport/BWIBBU_d` | `Date` 西元緊湊 `20260612` | `PEratio`（空字串=虧損/無正盈餘）| `PBratio`（幾乎全有）| `DividendYield` | 空字串 |
| 上櫃 | TPEX OpenAPI `tpex_mainboard_peratio_analysis` | `Date` 民國緊湊 `1150612` | `PriceEarningRatio`（`N/A`=無）| `PriceBookRatio` | `YieldRatio` | `N/A` |

兩者都**只回最新一交易日、不可回補** → `fetch_valuation_ratios()` 逐日累積成
`valuation_ratios_{YYYYMMDD}.parquet`（同 daily_all/institutional 模式）。`_clean_float`
已把空字串與 `N/A` 一律轉 null。`_parse_valuation_ratios()` 合併兩市成統一 schema
（stock_id/date/market/pe/pbr/dividend_yield）。

### 用途與設計
- **PE 主、PB 補虧損股**：有正 trailing PE 用 PE 算次產業相對位階；虧損/無正盈餘者無 PE，
  改用官方 PBR（`val_metric=PB`）——虧損股不再估值缺。詳見 `analysis/valuation.py`。
- **自身歷史百分位**：逐日累積數月後可算（本期僅當日橫斷面，明標未取得）。
- 進 `make fetch-twse`（緊接 fundamentals 後一步）；`cp candidates`/`cp valuation` 讀
  `load_latest_valuation_ratios()` 最新一份做橫斷面。
- **全市場中位數累積（docs/25 §2.4 M-Macro2b，2026-08 新增）**：`fetch-twse` 同時呼叫
  `data/valuation_history.py` 把當日全市場 PE/PBR/殖利率中位數 append 進
  `data/macro_regime/tw_valuation_history.parquet`——**只累積、不驗證、不計分**。刻意不放
  `data/cache/` 底下：本節開頭已提到 `BWIBBU_d`／`peratio_analysis` 兩端點只回最新一交易日、
  不可回補，`data/cache/twse/valuation_ratios_*.parquet` 若被 `cache.retention.valuation_days`
  （現 400 天）的 `prune-cache` 清理會永久遺失；獨立存放確保養 3 年（跟 BAA10Y 同規格驗證所需
  的 756 個交易日）的過程不會被中途清一次快取就腰斬。快取本身從 2026-06-12 才開始有資料。

---

## FRED 官方 API（總經指標，2026-08 新增，docs/25 M-Macro1）

`analysis/macro_regime.py` 的資料源，非個股資料，與上方 Goodinfo/TWSE 資料流平行、互不依賴。

### 端點與 key
`https://api.stlouisfed.org/fred/series/observations`（JSON，`series_id`+`api_key`+`file_type=json`）。
key 免費註冊（`https://fred.stlouisfed.org/docs/api/api_key.html`），存 `.env` 的 `fred_api=<key>`
（不進 git，不印出／不 log；讀取見 `_load_fred_api_key`，同時容許 `FRED_API_KEY` 環境變數）。

### 抓取合規（鐵律 1 精神外推；比照 Goodinfo 但門檻更鬆，官方開放資料）
```yaml
# config/settings.yaml → macro_regime.fetch
request_interval_sec: 1     # 序列間隔 ≥1 秒
cache_ttl_hours: 24         # 同序列 24h 快取（per-series parquet，data/cache/fred/）
max_retries: 2              # 連錯 3 次（含首次）即停（fetch_all 逐序列累計，見 FREDClient.fetch_all）
concurrency: 1              # 嚴格序列（fetch_all 依序抓，非並發）
user_agent: "tw-stock-screener/0.1"  # 可設定
timeout_sec: 30
```
每次請求回傳序列**全歷史**（非增量），故快取粒度是 per-series 整檔覆蓋，不是逐日累積。

### 已知風險
FRED 序列的公開歷史範圍可能被**追溯限縮**（非端點失效）——`BAMLH0A0HYM2` 於 2026/4 起被限縮到僅
約 3 年歷史，直接導致原設計的 3 年滾動窗無法計分，已改用 `BAA10Y` 取代。抓取失敗或歷史長度不足
須明確報錯／降級「資料不足」，不可靜默假裝有值（docs/25 §5 風險 10）。

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
