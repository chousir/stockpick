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
- 策略 A 共 18 檔
- 策略 B 共 12 檔
- 策略 C 共 24 檔
- 三組總聯集：48 檔
- A∩B：3 檔（最強候選，列於下方）

## 2. 族群強度排名（前 5）

| Rank | 族群 | A 入選 | B 入選 | C 入選 | 族群入選率 | 7日平均漲幅 | 強度分數 |
|---|---|---|---|---|---|---|---|
| 1 | 半導體-IC設計 | 6 | 3 | 2 | 28% | +4.2% | 9.1 |
| 2 | AI 伺服器 | 4 | 2 | 0 | 22% | +5.8% | 8.7 |
| 3 | ... |

## 3. 各族群領頭羊候選

### 半導體-IC設計
- **領頭羊候選：3034 聯詠**
  - 7日 RS: 1.18（強於大盤）
  - 量價：近 5 日成交值放大 65%
  - 入選策略：A + B（A∩B 雙料）
  - 在族群中：成交值排名 1/6、漲幅排名 2/6、外資買超排名 1/6
  - Goodinfo: https://goodinfo.tw/tw/StockInfo/StockDetail.asp?STOCK_ID=3034
- 跟漲候選：8069 元太、3596 智易...

### AI 伺服器
- ...

## 4. 觀察
（Claude 寫一段對族群輪動、市場氣氛的觀察，2-3 段。）

## 5. 推薦個股深度分析優先順序
1. 3034 聯詠（族群 #1 + A∩B）
2. ...
（建議產 5-10 份深度報告）
```

## 演算法

### 5.1 族群分組

來源：Goodinfo 在每檔股票的「產業類別」+ 「概念股」欄位。

但這兩個欄位粒度有差：
- **產業**：證交所的官方 28 類，較粗（電子業、半導體業…）
- **概念股**：Goodinfo 自編，較細（AI/HPC、低軌衛星、矽光子…）

實作策略：
1. 主分類用 Goodinfo 的「細產業」（例如「半導體-IC 設計」、「面板」）
2. 同時保留「概念股」欄位，用於 cross-cut（一檔可能屬於多個概念）
3. 一檔股可以在多個族群裡計次（但避免重複加權）

### 5.2 族群強度分數

```
強度分數 = w1 * 入選率 + w2 * 平均 RS + w3 * 法人偏好

其中：
  入選率 = 該族群被三組策略選到的檔數 / 該族群總檔數
  平均 RS = 該族群入選股 7 日相對大盤強度的中位數
            RS_7d = (個股 7 日漲幅) - (大盤 7 日漲幅)
  法人偏好 = 該族群外資+投信近 5 日累計買超 / 族群總成交值
  
  初始權重: w1=0.4, w2=0.4, w3=0.2
  可在 config/settings.yaml 調整
```

只顯示族群入選股 ≥ 2 檔的族群（單一個股不算族群）。

### 5.3 領頭羊判斷

「領頭羊」不是單一指標能決定，是多面向綜合：

```python
領頭羊分數 = (
    0.30 * RS_rank_in_group       # 在族群中相對強度排名
  + 0.25 * volume_growth_rank     # 量能放大排名
  + 0.20 * absolute_amount_rank   # 絕對成交值排名（避免冷門股當領頭）
  + 0.15 * institutional_rank     # 法人買超排名
  + 0.10 * earliest_breakout_rank # 突破日早晚（越早越領頭）
)
```

每個族群取分數最高者為「領頭羊候選」，但 Claude 寫報告時不能寫死「就是這檔」，要說「候選」，並附判斷理由。

### 5.4 推薦深度分析優先順序

```
優先順序分數 = 
    族群強度分數 * 0.6
  + (個股是否為領頭羊 ? 1.0 : 0.5) * 0.3
  + (個股是否在策略交集 A∩B 等) * 0.1
```

排序後取前 5-10 檔，建議使用者用 Claude Code 產這幾檔的深度報告。

## 模組主要 API

```python
# src/tw_screener/analysis/grouping.py

def group_stocks(
    screener_results: dict[str, pl.DataFrame],  # 三組策略結果
    price_history: pl.DataFrame,                # 60 天 OHLCV
    benchmark: pl.DataFrame,                    # 大盤指數
) -> pl.DataFrame:
    """回傳 group_id, members, score, rs_avg, ..."""

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

「## 4. 觀察」這段是**自由文**，由 Claude Code 在執行 `make group` 後互動式產出（或由 `claude` 開 session 後手動觸發）：

```
你執行: make group
程式: 產出 group_analysis.md（前 3 段、第 5 段都填好）
程式: 第 4 段留空，附上「待 Claude 補寫」標記
你開 Claude Code: 「補寫 reports/2026-W21/group_analysis.md 的觀察段」
Claude: 讀資料、補寫
```

這個分工的理由：自動化框架資料、人工 + LLM 補洞察，**洞察是 LLM 真正的價值**。

## 限制與已知問題

- 一檔股可能屬於多個概念股，目前簡化為「取主要概念」，未來可改 multi-label。
- 「先漲性」目前用突破日早晚近似，並不完美。
- 族群歸類依賴 Goodinfo 的標籤，標籤錯了結果就錯（極少數情況）。
