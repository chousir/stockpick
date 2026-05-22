# 00 — 系統架構

## 資料流總覽

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1：資料層                                              │
│  ─────────────                                                │
│  Goodinfo 自訂篩選 ──┐                                        │
│  TWSE OpenAPI ───────┤                                        │
│  TWSE Legacy（STOCK_DAY、T86）─┤→ Fetcher → Parser → Polars   │
│  ISIN 頁面（OTC 產業）─────────┘                              │
│                                       │                       │
│                                       ▼                       │
│                          data/cache/*.parquet                 │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 2：選股層                                              │
│  ─────────────                                                │
│  config/strategies/*.yaml ──→ ScreenerRunner ──→ 每組一份 CSV │
│                                                               │
│  輸出：reports/YYYY-Www/screen_result_{strategy_id}.csv       │
│        GROUP=defg → d/e/f/g 四份（主流程）                    │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 3：族群分析層                                          │
│  ──────────────                                               │
│  選股結果 + TWSE/TPEX 產業對照表 ──→ GroupAnalyzer            │
│                                       │                       │
│                                       ▼                       │
│           reports/YYYY-Www/group_analysis.md                  │
│           （含族群強度排序、領頭羊候選）                       │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 4：個股深度報告                                        │
│  ──────────────                                              │
│  make report STOCK_ID=2330 → data_fetcher → builder           │
│    ├─ 有 ANTHROPIC_API_KEY：呼叫 Claude API 產完整分析        │
│    └─ 無 API key：產資料草稿，手動貼到 Claude 對話補寫        │
│                                                               │
│  輸出：reports/YYYY-Www/stocks/{股號}_{簡稱}.md               │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 5：累積知識庫（Git）                                   │
│  ─────────────                                                │
│  - 每週 reports/ 都 commit                                    │
│  - watchlist/active.md, waiting.md, closed.md                 │
│  - 三個月後跑 `make backtest-strategies` 看勝率               │
└──────────────────────────────────────────────────────────────┘
```

## 模組職責

| 模組 | 職責 | 依賴 |
|---|---|---|
| `src/tw_screener/data/twse.py` | 證交所 OpenAPI + Legacy + ISIN 抓價量、法人、月營收、產業分類 | httpx |
| `src/tw_screener/screener/goodinfo/` | Goodinfo 爬蟲三件套（URL/Fetch/Parse） | httpx, beautifulsoup4, lxml |
| `src/tw_screener/screener/runner.py` | 讀 YAML、跑策略、輸出 CSV | polars |
| `src/tw_screener/analysis/grouping.py` | 按官方產業分組計分 | polars |
| `src/tw_screener/analysis/leader.py` | 相對強度、領頭羊判斷 | polars |
| `src/tw_screener/analysis/indicators/` | 技術指標（MACD/KD/RSI 等），預埋 Rust 替換空間 | polars |
| `src/tw_screener/report/group_report.py` | 族群分析 Markdown 渲染 | jinja2 |
| `src/tw_screener/report/data_fetcher.py` | 個股報告資料打包（OHLCV + 營收 + 法人 + 族群資訊） | polars |
| `src/tw_screener/report/builder.py` | 個股報告 builder（API 模式 / 草稿模式） | anthropic, jinja2 |
| `src/tw_screener/backtest/` | 策略勝率回測（骨架，2026-08 後實作） | polars |
| `src/tw_screener/cli.py` | CLI 入口（Typer） | typer |

## 為什麼這樣分層

1. **資料層獨立**：之後想換 FinMind、TEJ、券商 API，只改 Layer 1，不影響選股邏輯。
2. **策略外抽 YAML**：你想加新策略不用寫 Python，改 YAML。
3. **族群分析在選股之後**：因為「族群強度」要看當週入選分布，不是事前定義。
4. **個股報告獨立**：產報告是 Claude Code 互動式做的，不是 batch job，不能跟前面 pipeline 綁死。

## Rust 預埋位置（Phase 2 才導入）

`src/tw_screener/analysis/indicators/` 目錄下，每個指標一個檔：
- `macd.py` — 純 Python 實作（MVP）
- `kd.py`
- `rsi.py`

每個檔 export 一個 pure function：
```python
def calculate(df: pl.DataFrame, **params) -> pl.DataFrame: ...
```

Phase 2 用 Rust + PyO3 重寫時，**換實作不換介面**，呼叫端不動。

## 非目標（明確不做）

- ❌ 自動下單、券商 API 整合
- ❌ 即時行情、Tick 級資料
- ❌ 美股、加密貨幣、期貨
- ❌ Web Dashboard、行動 App（Phase 1）
- ❌ 多人協作、用戶系統
