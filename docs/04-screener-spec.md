# 04 — 選股模組規格

## 模組職責

讀 `config/strategies/*.yaml` → 透過 Goodinfo 取得篩選結果 → 解析成 Polars DF → 輸出 CSV 到 `reports/`。

## 主要 class / function

```python
# src/tw_screener/screener/runner.py

from pathlib import Path
import polars as pl
from .goodinfo.url_builder import build_screener_url
from .goodinfo.fetcher import GoodinfoBlockedError, create_fetcher
from .goodinfo.parser import parse_screener_result, GoodinfoTooManyResultsError

class ScreenerRunner:
    def __init__(self, settings_path: Path = Path("config/settings.yaml")):
        ...

    def run_strategy(self, strategy_path: Path) -> pl.DataFrame:
        """跑單一策略，回傳純 Goodinfo 結果 DataFrame。
        - 0 筆結果為正常（市場大跌時可能無符合條件標的）
        - > 100 筆：logger.warning（條件太寬鬆）
        - > 300 筆：raise GoodinfoTooManyResultsError（Goodinfo 匿名上限）
        - 設計原則：CSV 一律是 Goodinfo 結果快照，不做本地後處理
        """

    def run_all(self, week_tag: str | None = None) -> dict[str, pl.DataFrame]:
        """跑所有 config/strategies/ 下的 YAML，輸出到 reports/YYYY-Www/。
        遇到 GoodinfoBlockedError 時：呼叫 write_blocked_log() 後 re-raise，停止執行。
        跑完後（results 非空時）自動呼叫 log_writer.write_screen_log()
        產出 screen_log.md（純機械統計，含交集）。
        """

    def export_csv(self, df: pl.DataFrame, strategy_id: str, week_tag: str) -> Path:
        """寫入 reports/YYYY-Www/screen_result_{id}.csv。"""

    def write_blocked_log(self, strategy_id: str, week_tag: str) -> Path:
        """被 Goodinfo 封鎖時，附加一行到 reports/YYYY-Www/blocked.log，回傳路徑。
        格式：{date} strategy={strategy_id} Goodinfo access blocked
        由 run_all() 和 CLI screen_run 呼叫。
        """
```

## 輸出 CSV 規格

> **注意**：欄位依據 Goodinfo 交易狀況篩選結果實際回傳欄位更新（2026-05-16 驗證）。
> Goodinfo 篩選結果不提供 industry/concept，那些在個股詳情頁，由使用者自行判斷。

```
stock_id,name,market,close,change_pct,volume_lots,amount_million,pe_ratio,pb_ratio,strategy_id,screened_at,goodinfo_url
2330,台積電,市,2265.0,-0.22,18560,42.5,,1.2,a_breakout,2026-05-16,https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID=2330
...
```

欄位定義：

| 欄位 | 型別 | 來源 | 說明 |
|---|---|---|---|
| `stock_id` | str | Goodinfo | 股號（必填） |
| `name` | str | Goodinfo | 股票簡稱 |
| `market` | str | Goodinfo | 市場別（市=上市, 櫃=上櫃, 興=興櫃） |
| `close` | float | Goodinfo | 篩選當日收盤價 |
| `change_pct` | float | Goodinfo | 漲跌幅（%） |
| `volume_lots` | int | Goodinfo | 成交張數 |
| `amount_million` | float | Goodinfo | 成交額（百萬元） |
| `pe_ratio` | float | Goodinfo | 本益比（可空，ETF 無此值） |
| `pb_ratio` | float | Goodinfo | 股價淨值比（可空） |
| `strategy_id` | str | Runner | 策略識別碼（a_breakout 等） |
| `screened_at` | date | Runner | 執行日期 |
| `goodinfo_url` | str | Runner | 個股詳情頁連結（從 `base_url` 衍生拼接，非硬碼） |

## URL Builder

```python
# src/tw_screener/screener/goodinfo/url_builder.py

from pydantic import BaseModel

class FilterCondition(BaseModel):
    item: str            # e.g. "成交筆數"
    period: str | None = None  # 說明用途（不加進 URL），e.g. "日", "月"
    min: float | None = None
    max: float | None = None

class StrategyConfig(BaseModel):
    id: str
    name: str
    description: str
    market: str
    filters: list[FilterCondition]
    rules: list[str]
    display_sheet: str
    display_period: str
    holding_period: str | None = None        # 選填，參考用
    post_filter_sort: list[...] | None = None  # 選填，後處理排序

def build_screener_url(strategy: StrategyConfig, base_url: str) -> str:
    """
    將 strategy 轉成 Goodinfo 自訂篩選 URL（供瀏覽器手動驗證用）。

    URL params:
      MARKET_CAT=自訂篩選
      INDUSTRY_CAT=我的條件
      FL_ITEM{i}=<item name>
      FL_VAL_S{i}=<min>
      FL_VAL_E{i}=<max>
      FL_RULE{i}=<rule>
      FL_SHEET=<display_sheet>
      FL_SHEET2=<display_period>
      FL_MARKET=<market>
      FL_QRY=查 詢
    """

def build_data_url(strategy: StrategyConfig, base_url: str) -> str:
    """
    建立 Goodinfo AJAX 資料端點 URL（STEP=DATA）。

    此 URL 回傳 HTML fragment 含股票結果表，傳給 parser.parse_screener_result()。
    與 build_screener_url() 的差異：加了 STEP=DATA、SHEET、RPT_TIME、RANK_RANGE。
    Runner 實際打網用此 URL；build_screener_url 僅供人工驗證。
    驗證日期：2026-05-16
    """
```

## Fetcher

```python
# src/tw_screener/screener/goodinfo/fetcher.py

class GoodinfoFetcher:
    def __init__(
        self,
        cache_dir: Path,
        interval_sec: float,
        jitter_sec: float,
        ttl_hours: float,
        max_retries: int,
        backoff_base: float,   # 指數退避底數（seconds）
        user_agent: str,
        base_url: str,
        referer: str,
    ):
        ...

    def get(self, url: str, *, force: bool = False) -> str:
        """
        取得 HTML。

        流程：
        1. 計算 cache key = md5(url)
        2. 若 cache 存在且未過期且 !force → 讀 cache
        3. 否則：sleep(interval ± jitter) → httpx.get → 寫 cache
        4. 失敗：tenacity 指數退避，multiplier=backoff_base, min=backoff_base, max=backoff_base³
        5. 連續 max_retries 次失敗 reraise
        """
```

**錯誤類型**：
- `GoodinfoBlockedError`：被擋（HTTP 403 或回傳的 HTML 包含「您的瀏覽量異常」）
- `GoodinfoParseError`：HTML 結構解析失敗（網站改版）
- `GoodinfoTooManyResultsError`：篩選結果超過 300 筆匿名上限（`count` 屬性帶實際筆數）

被擋時的處理：
- `GoodinfoFetcher.get()` log error + raise `GoodinfoBlockedError`
- `ScreenerRunner.run_all()` catch → 呼叫 `write_blocked_log(strategy_id, week_tag)` → re-raise
- CLI `screen run` 也 catch → 呼叫 `write_blocked_log()` → 印紅色提示 → `Exit(1)`
- blocked.log 路徑：`reports/YYYY-Www/blocked.log`，每次封鎖 append 一行

## Parser

```python
# src/tw_screener/screener/goodinfo/parser.py

def parse_screener_result(html: str) -> pl.DataFrame:
    """
    解析 Goodinfo STEP=DATA AJAX 回傳的 HTML fragment。

    定位方式：
      1. 找到結果 table（id="tblStockList"）
      2. 從表頭中文字定位欄位 index，不用固定 index
      3. 表頭可能因 display_sheet 不同而變化
      4. Goodinfo 每 ~20 行重複表頭，自動跳過

    錯誤處理：
      - 篩選結果 > 300 筆 → raise GoodinfoTooManyResultsError(count)
      - 找不到 tblStockList → raise GoodinfoParseError("找不到結果表，可能網站改版")
      - 表存在但 0 row → 回傳空 DataFrame（正常情況）
    """
```

## 設定檔範例

`config/settings.yaml`：

```yaml
goodinfo:
  base_url: "https://goodinfo.tw/tw"
  request_interval_sec: 3.0        # 注意：key 名稱為 _sec，非 _seconds
  request_interval_jitter_sec: 1.0 # 注意：key 名稱為此格式
  cache_ttl_hours: 24
  max_retries: 3
  backoff_base: 5                  # 指數退避底數（s）：5→25→125
  referer: "https://goodinfo.tw/tw/index.asp"
  concurrency: 1
  user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."

paths:
  cache_dir: "data/cache"
  reports_dir: "reports"
  strategies_dir: "config/strategies"

logging:
  level: "INFO"
  file: "logs/tw_screener.log"
```

## 測試要點

1. **離線測試**（必須通過）：用 `tests/fixtures/goodinfo/screener_*.html` 測 parser。
2. **整合測試**（手動跑）：`make screen STRATEGY=a_breakout`，檢查 CSV 產出。
3. **被擋偵測測試**：假造「您的瀏覽量異常」HTML，確認 raise `GoodinfoBlockedError`。
4. **快取測試**：同 URL 連兩次呼叫，第二次必須讀 cache（觀察沒新增網路請求 log）。
5. **blocked.log 測試**：`run_all()` 的 fetcher 丟出 `GoodinfoBlockedError` 時，確認 blocked.log 被建立且 error re-propagate。

## 限制

- 一次只跑一個策略的篩選，不平行。
- 不抓「跨頁」結果（若篩選結果 > 300 筆，raise `GoodinfoTooManyResultsError`；> 100 筆印警告）。
- 不抓個股詳情頁（那由 report builder 在 Claude Code 互動式抓）。
