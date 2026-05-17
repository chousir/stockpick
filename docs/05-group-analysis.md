# 05 — 族群分析與領頭羊判斷

## 為什麼這層存在

台股最重要的市場特性：**強族群帶動明顯**。
- 一檔孤鳥強勢上漲，事後常被驗證為假突破。
- 一個族群同步走強，個股勝率大幅提升。
- 族群內最早動、量最大的那檔，常是領頭羊；領頭羊轉弱往往是族群結束的先行訊號。

所以選股結果出來後**先做族群分析、再決定個股深度報告的優先順序**。

## 模組輸入

- 三組策略的 CSV：`reports/YYYY-Www/screen_result_{a,b,c}.csv`
- 全市場價量歷史（60 天）：`data/cache/twse/daily_*.parquet`
- 大盤指數（加權、櫃買）：同上

## 模組輸出

`reports/YYYY-Www/group_analysis.md`：

```markdown
# 2026 W21 族群分析報告

## 1. 入選分布總覽
- 策略 A（波段啟動）共 18 檔
- 策略 B（法人成長）共 12 檔
- 策略 C（穩健存股）共 24 檔
- 三組總聯集：48 檔

## 2. 族群強度排名（前 5）

| Rank | 族群 | A 入選 | B 入選 | C 入選 | 族群入選率 | 7日平均漲幅 | 強度分數 |
|---|---|---|---|---|---|---|---|
| 1 | 半導體業 | 6 | 3 | 2 | 28% | +4.2% | 9.1 |
| 2 | 電腦及周邊設備業 | 4 | 2 | 0 | 22% | +5.8% | 8.7 |
| 3 | ... |

## 3. 各族群領頭羊候選

### 半導體業
- **領頭羊候選：3034 聯詠**
  - RS：1.18（強於大盤）
  - 在族群中：成交值排名 1/6、漲幅排名 2/6、外資買超排名 1/6
  - 入選策略：A + B（雙料入選）
  - Goodinfo: https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID=3034
- 跟漲候選：8069 元太、3596 智易...

## 4. 觀察
（<!-- TODO: Claude 補寫 -->）

## 5. 推薦個股深度分析優先順序（前 10）
1. 3034 聯詠（族群 #1 + A∩B）
2. ...

## 6. 族群深度分析請求（給 Claude）
（前 4 大非未分類族群的個股表格，請 Claude 細分實際次族群並分析輪動訊號）
```

## 演算法

### 5.1 族群分組

來源：TWSE 官方產業類別（28 類上市）+ TPEX ISIN 頁面（上櫃，`isin.twse.com.tw/isin/C_public.jsp?strMode=4`）。

每檔股票屬於**唯一一個**官方產業類別（單標籤）。ETF（股號開頭 `00`）和權證（非數字開頭）在族群分析前過濾掉。

分類粒度：TWSE 官方 28 類（半導體業、電腦及周邊設備業、通信網路業…）為主。
更細的次族群（AI 伺服器、IC 設計、封測…）由 Claude 在 Section 6 的分析中人工識別，不在自動計算範圍內。

> **設計考量（已決策）**：Goodinfo 自編的「概念股」tag 粒度更細，且一檔可屬多個概念（multi-label）。未實作原因：需逐檔爬 Goodinfo（2000 檔 × 3 秒 ≈ 1.5 小時），且 tag 為 Goodinfo 自編文字、維護成本高。現行做法：粗分類自動化，細分類交由 Claude 判斷。

只顯示族群入選股 ≥ 2 檔的族群（單一個股不算族群）。

### 5.2 族群強度分數

```
強度分數 = w_er * (entry_rate × 10)
         + w_sz * (log1p(members_count) / log1p(max_members_count) × 10)
         + w_rs * clip(rs_avg, 0, 10)
         + w_inst * (inst_score × 10)

其中：
  entry_rate    = 族群入選股數 / 族群在 industry_df 中的總檔數
  members_count = 族群入選股數
  rs_avg        = 族群入選股的 change_pct 中位數（無歷史時）
                  或 (最新收盤 - N日前收盤) / N日前收盤 × 100（有歷史時）
  inst_score    = 族群入選股外資+投信近 5 日累計買超的標準化分數（0-1）

預設權重（config/settings.yaml 可調）：
  w_er   = 0.50  # 入選率：族群整體動能
  w_sz   = 0.15  # 對數規模：避免小族群因高入選率過度佔先
  w_rs   = 0.20  # 絕對 RS：剪裁至 0-10，避免離群值
  w_inst = 0.15  # 法人偏好
```

### 5.3 領頭羊判斷

「領頭羊」綜合以下四個面向排名，每個面向在族群內從 0-1 正規化：

```python
領頭羊分數 = (
    0.35 * rs_rank              # 在族群中相對強度排名
  + 0.30 * absolute_amount_rank # 絕對成交值排名（避免冷門股當領頭）
  + 0.20 * institutional_rank   # 外資+投信買超排名
  + 0.15 * strategy_count_rank  # 入選策略數排名（A∩B∩C > A∩B > A）
)
```

每個族群取分數最高者為「領頭羊候選」。報告中用「候選」字樣，附出各指標的族群排名理由。

> **設計考量（已決策）**：原規格含「突破日早晚（earliest_breakout_rank）」因子（0.10 權重），需逐日掃描每檔 OHLCV 並定義突破條件。未實作原因：上櫃股 OHLCV 歷史有限，breakout 定義需與策略 A 邏輯對齊，實作複雜度高而邊際效益有限。現行 4 因子已能區分主要領頭羊。

### 5.4 推薦深度分析優先順序

```
優先順序分數 =
    族群強度分數 × 0.6
  + (個股是否為領頭羊 ? 1.0 : 0.5) × 0.3
  + (個股入選策略數 / 最大策略數) × 0.1
```

排序後取前 10 檔（排除「未分類」族群），建議用 `make report STOCK_ID=XXXX` 產深度報告。

## 模組主要 API

```python
# src/tw_screener/analysis/grouping.py

def group_stocks(
    screener_results: dict[str, pl.DataFrame],  # 三組策略結果
    price_history: pl.DataFrame,                # 60 天 OHLCV
    benchmark: pl.DataFrame,                    # 大盤指數
    industry_df: pl.DataFrame | None = None,    # TWSE+TPEX 產業對照表
    min_group_size: int = 2,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """回傳 (groups_df, members_df)。"""

# src/tw_screener/analysis/leader.py

def find_leaders(
    group_members: pl.DataFrame,
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
) -> pl.DataFrame:
    """每個 group 回傳領頭羊候選 + 跟漲者。"""

# src/tw_screener/report/group_report.py

def render_group_report(
    groups: pl.DataFrame,
    leaders: pl.DataFrame,
    week_tag: str,
    output_path: Path,
) -> None:
    """用 jinja2 模板產出 group_analysis.md。"""
```

## 觀察段由誰寫

「## 4. 觀察」這段是**自由文**，留 `<!-- TODO: Claude 補寫 -->` 標記：

```
你執行: make group
程式: 產出 group_analysis.md（Section 1-3、5-6 都填好）
程式: Section 4 留空，附上「待 Claude 補寫」標記
你開 Claude Code: 「補寫 reports/2026-W21/group_analysis.md 的觀察段」
Claude: 讀資料、補寫
```

## 已知限制

| 項目 | 狀態 | 影響 |
|---|---|---|
| 族群分類粒度 | TWSE/TPEX 官方 28 類（粗，單標籤） | 無法區分 IC 設計 vs 封測；由 Claude Section 6 分析補充 |
| earliest_breakout_rank | 不含此因子 | 領頭羊判斷用 RS / 成交值 / 法人 / 策略數四因子；突破時序未納入 |
| 上櫃股 OHLCV 歷史 | 只有累積 daily cache | RS 計算可能使用 change_pct 代替（無歷史時 fallback） |
| 法人資料 T86 | 非交易日為空 | `make fetch-twse` 需在交易日執行 |
