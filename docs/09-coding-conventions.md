# 09 — 程式碼風格與慣例

## 命名

- 模組 / 檔名：`snake_case.py`
- 類別：`PascalCase`
- 函數 / 變數：`snake_case`
- 常數：`UPPER_SNAKE_CASE`
- 私有：`_leading_underscore`
- 股票相關 ID：`stock_id`（不用 `code`, `symbol`, `ticker`，因為台股慣例叫「股號」）

## 型別

```python
# 一律給 type hints
def fetch_stock(stock_id: str, period_days: int = 60) -> pl.DataFrame:
    ...

# pydantic models 用於 schema、JSON parsing
class StockInfo(BaseModel):
    stock_id: str
    name: str
    industry: str | None = None
```

## 錯誤處理

```python
# 自訂例外，分層次
class TWScreenerError(Exception): pass
class GoodinfoError(TWScreenerError): pass
class GoodinfoBlockedError(GoodinfoError): pass
class GoodinfoParseError(GoodinfoError): pass
class TWSEError(TWScreenerError): pass

# 不要 bare except
try:
    ...
except (httpx.HTTPError, GoodinfoBlockedError) as e:
    logger.error(f"抓取失敗: {e}")
    raise
```

## Logging

```python
from loguru import logger

logger.info("跑策略 {}", strategy.id)        # 不用 f-string，留給 loguru 處理
logger.warning("篩出 {} 檔，可能太寬鬆", n)
logger.error("Goodinfo 被擋: {}", url)
```

絕對不用 `print()` 在正式 code 裡（測試與 CLI 例外）。

## 設定

```python
# 絕不寫死
INTERVAL = 3.0  # ❌

# 從 settings 讀
from tw_screener.config import settings
interval = settings.goodinfo.request_interval_seconds  # ✅
```

## DataFrame

```python
# 用 Polars，不用 pandas
import polars as pl

# 欄位名用底線命名
df = df.rename({"成交筆數": "trade_count", "收盤價": "close"})

# 中文欄名只在 IO 邊界（檔案/HTML 解析）出現，內部處理一律英文
```

## 函數設計

```python
# pure function 優先（同樣輸入 → 同樣輸出，無副作用）
def calculate_macd(df: pl.DataFrame, fast: int = 12, slow: int = 26) -> pl.DataFrame:
    """計算 MACD，回傳含 macd, signal, hist 三欄的 DataFrame。"""
    ...

# 有副作用的（讀檔、寫檔、打網）放 service / runner 層
class ScreenerRunner:
    def run_strategy(self, ...) -> pl.DataFrame:  # 有 I/O，OK
        ...
```

## 測試

```python
# 一個檔對應一個測試檔
# src/tw_screener/screener/goodinfo/parser.py
# → tests/screener/goodinfo/test_parser.py

# 解析測試用 fixture，不打網
def test_parse_screener_result_a_breakout():
    html = (FIXTURES / "screener_a_breakout_20260515.html").read_text()
    df = parse_screener_result(html)
    assert df.shape[0] > 0
    assert "stock_id" in df.columns
    assert df["stock_id"].dtype == pl.Utf8
```

## Docstring

```python
def find_leaders(group_members: pl.DataFrame, price_history: pl.DataFrame) -> pl.DataFrame:
    """
    從族群成員中找出領頭羊候選。
    
    Args:
        group_members: 族群成員，需含 stock_id, group_id 欄位
        price_history: 60 天 OHLCV，需含 stock_id, date, close, volume
    
    Returns:
        DataFrame: 含 stock_id, group_id, leader_score, is_top_leader
    
    Notes:
        領頭羊判定公式見 docs/05-group-analysis.md 5.3 節
    """
```

## Import 順序

```python
# 1. stdlib
import json
from pathlib import Path

# 2. 第三方
import httpx
import polars as pl
from loguru import logger

# 3. 本地
from tw_screener.config import settings
from tw_screener.screener.goodinfo import GoodinfoFetcher
```

`ruff` 會自動排序。

## Git

- branch 命名：`milestone/M{n}-{short-name}`（例：`milestone/M2-goodinfo`）
- commit message：`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` 開頭
- 一個 milestone 一個 PR（即使只有自己），方便回看

## 不要做的事

- ❌ 不要用 `pandas`（除非有第三方庫卡死，要先問）
- ❌ 不要用 `requests`（用 `httpx`，async-ready）
- ❌ 不要用 `selenium`、`playwright`、`requests-html`（爬蟲一律 httpx + bs4，被擋就被擋）
- ❌ 不要寫 abstract base class 除非真的有 ≥ 2 個 concrete subclass
- ❌ 不要建 plugin system / hook framework（YAGNI）
- ❌ 不要在 production code 裡 `print()`
- ❌ 不要 commit `.env`、`data/`、`reports/` 下含個人持股的內容
