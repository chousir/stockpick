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
# 2026-W21 族群分析報告

## 0. 策略代號說明
- A 波段啟動：週 MACD + 5/10/20 均線多頭排列（短線 1–4 週）
- B 法人成長：月營收 YoY ≥ 15% + 外資投信同步連買（中線 1–3 月）
- C 低基期成長：月營收 YoY ≥ 10% + 距 6 月高 -20% 以下 + 外資連買（中線）

## 1. 入選分布總覽
- 策略 A 共 18 檔；B 共 12 檔；C 共 24 檔
- 三組總聯集：48 檔

## 2. 族群強度排名（前 5）

| Rank | 族群 | A 入選 | B 入選 | C 入選 | 族群入選率 | 5 日均漲 | 強度分數 |
|---|---|---|---|---|---|---|---|
| 1 | 半導體業 | 6 | 3 | 2 | 28% | +4.2% | 52.1 |
| 2 | 電腦及周邊設備業 | 4 | 2 | 0 | 22% | +5.8% | 51.7 |
| ... |

## 3. 各族群本週表現
（每族群列出「前 3 名（依 5 日漲幅）」+ 命中策略標籤，不再用「領頭羊」字眼）

### 1. 半導體業（共 11 檔入選・5 日均漲 +4.2%）
本週族群表現前 3 名：

| # | 股號 | 名稱 | 5 日漲幅 | 當日 | 成交金額 | 命中策略 | Goodinfo |
| 1 | 3034 | 聯詠 | +8.5% | +2.1% | 1500 | A+B | 連結 |
| 2 | ... |

其他入選股：8069 元太、3596 智易...

## 4. 觀察
（<!-- TODO: Claude 補寫 -->）

## 5. 推薦個股深度分析優先順序（前 10）
1. 3034 聯詠（族群 #1 半導體業・族群內 #1・A+B）
...

## 6. 族群深度分析請求（給 Claude）
（前 4 大族群的完整個股表格，請 Claude 細分實際次族群、檢查漲幅領先個股的合理性）
```

## 演算法

### 5.1 族群分組

來源：TWSE 官方產業類別（28 類上市）+ TPEX ISIN 頁面（上櫃，`isin.twse.com.tw/isin/C_public.jsp?strMode=4`）。

每檔股票屬於**唯一一個**官方產業類別（單標籤）。ETF（股號開頭 `00`）和權證（非數字開頭）在族群分析前過濾掉。

分類粒度：TWSE 官方 28 類（半導體業、電腦及周邊設備業、通信網路業…）為主。
更細的次族群（AI 伺服器、IC 設計、封測…）由 Claude 在 Section 6 的分析中人工識別，不在自動計算範圍內。

> **設計考量（已決策）**：Goodinfo 自編的「概念股」tag 粒度更細，且一檔可屬多個概念（multi-label）。未實作原因：需逐檔爬 Goodinfo（2000 檔 × 3 秒 ≈ 1.5 小時），且 tag 為 Goodinfo 自編文字、維護成本高。現行做法：粗分類自動化，細分類交由 Claude 判斷。

只顯示族群入選股 ≥ 2 檔的族群（單一個股不算族群）。

### 5.2 族群強度分數（2026-W21 起改為動能主導）

```
強度分數 = w_mom  * 100 * sigmoid(momentum_5d / 5)            # 動能（sigmoid 校準）
         + w_er   * 100 * entry_rate                          # 入選率
         + w_inst * 100 * inst_score                          # 法人佔位
         + w_sz   * 100 * (log1p(members) / log1p(max))       # 規模

其中：
  momentum_5d   = 族群入選股的 5 日累計報酬 mean（取自 stock_day_*.parquet
                  或 daily_*.parquet；資料不足時 fallback 到 change_pct，
                  並在族群層級記錄 momentum_5d_days_used）
  entry_rate    = 族群入選股數 / 族群在 industry_df 中的總檔數
  inst_score    = 族群入選股外資+投信累計買超的標準化分數（暫為 0）
  members       = 族群入選股數

預設權重（config/settings.yaml 可調）：
  w_mom  = 0.50  # 動能：5 日累計漲幅（族群實際走勢）
  w_er   = 0.25  # 入選率：條件命中度
  w_inst = 0.15  # 法人偏好
  w_sz   = 0.10  # 對數規模：避免小族群因高入選率過度佔先

sigmoid 而非 clip：避免 5 日大漲 20% 仍只拿到 clip(10) 的天花板，
讓動能強族群在分數上有區分度（5 日漲 5% → 0.5；漲 10% → 0.73；漲 20% → 0.88）。
```

**動能資料來源（X+Y 混合策略）**：
- **X**：`make week` 流程中跑 `make fetch-candidates-history`，對本週入選股
  聯集去重個股批次補抓 STOCK_DAY 2 個月歷史。過去月份永久快取，首次 ~5–10 分鐘
- **Y**：`data/cache/twse/daily_*.parquet` 每週累積一筆，第 5 週起累積成完整 5 日窗
- 兩者由 `TWSEClient.load_candidate_history()` 合併，按 (stock_id, date) 去重後算 5 日 gap

**完整 `make week` 流程**：
```
1. fetch-twse              → daily / T86 / revenue / industry
2. screen-all              → A/B/C 純跑 Goodinfo，三份 CSV 為純結果快照
3. fetch-candidates-history → 對聯集個股抓 stock_day（純加工資料層）
4. group                   → 讀 stock_day 算 5 日動能 → group_analysis.md
```

**A/B/C CSV 一律是純 Goodinfo 結果快照**，不被任何後處理覆寫。
5 日動能值**只在 group_analysis.md 顯示**，CSV 維持 Goodinfo 原 schema（含當日 change_pct）。

設計原則：策略可換來換去，但 CSV schema 與後處理機制保持單純，避免特定策略綁住整體流程。

### 5.3 族群內排名（取代「領頭羊」概念）

每檔股票在所屬族群內計算 leader_score，並依分數高低排出 `rank_in_group`（1, 2, 3, …）。
模板顯示前 3 名 + 其他入選股清單，**不再單獨標出「領頭羊」**。

```python
leader_score = (
    0.50 * momentum_rank_norm     # 在族群中 5 日漲幅排名
  + 0.25 * amount_rank_norm       # 絕對成交值排名（避免冷門股）
  + 0.15 * inst_rank_norm         # 外資+投信買超排名
  + 0.10 * strategy_count_rank_norm  # 入選策略數排名（A∩B∩C > A∩B > A）
)
```

**為什麼改名又改公式**：
- 半導體常識上就是台積電帶頭，硬標「領頭羊：6147 頎邦（當日 +9.92%）」反而誤導
- 排名基準從「相對強度」（含 vs 大盤）改為「5 日累計漲幅」，配合 5.2 動能主導的方向
- 模板呈現為「本週族群表現前 3 名」表格，讓使用者自己判斷誰是真正帶頭的

### 5.4 推薦深度分析優先順序

```
優先順序分數 =
    族群強度分數 × 0.6
  + rank_bonus × 0.3      # rank_in_group=1 → 1.0；2 → 0.7；3 → 0.5；其餘 → 0.3
  + (個股入選策略數 ≥ 2 ? 1.0 : 0.0) × 0.1
```

排序後取前 10 檔（排除「未分類」族群），建議用 `make report STOCK_ID=XXXX` 產深度報告。

## 模組主要 API

```python
# src/tw_screener/analysis/momentum.py（2026-W21 新增）

def compute_n_day_return(
    stock_ids: list[str],
    price_history: pl.DataFrame,
    n: int = 5,
) -> dict[str, tuple[float, int]]:
    """{stock_id: (cumulative_return_pct, actual_days_used)}。"""

# src/tw_screener/analysis/grouping.py

def group_stocks(
    screener_results: dict[str, pl.DataFrame],
    price_history: pl.DataFrame,
    benchmark: pl.DataFrame,                    # 預留，目前未使用
    industry_df: pl.DataFrame | None = None,
    weights: dict[str, float] | None = None,
    min_group_size: int = 2,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """回傳 (groups_df, members_df)。groups_df 含 momentum_5d / momentum_5d_days_used。"""

# src/tw_screener/analysis/leader.py（find_leaders 為 rank_within_groups 的 alias）

def rank_within_groups(
    group_members: pl.DataFrame,
    price_history: pl.DataFrame,
    institutional: pl.DataFrame,
) -> pl.DataFrame:
    """每個 group 回傳含 rank_in_group / leader_score 的 DataFrame。"""

# src/tw_screener/report/group_report.py

def render_group_report(
    groups: pl.DataFrame,
    members: pl.DataFrame,    # 含 rank_in_group
    screener_results: dict[str, pl.DataFrame],
    week_tag: str,
    output_path: Path,
    top_groups: int = 10,
    top_stocks: int = 10,
) -> None:
    """用 jinja2 模板產出 group_analysis.md（含 Section 0 策略代號說明）。"""
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
| earliest_breakout_rank | 不含此因子 | 族群內排名用 momentum / 成交值 / 法人 / 策略數四因子；突破時序未納入 |
| 5 日動能資料 | 首次 `make week` 前 5 日漲幅依快取深度有 `*` 標註 | 第 1 週可能只有 1–2 日資料；跑過 `fetch-candidates-history` 後第 2 週起穩定 |
| 上櫃股 OHLCV 歷史 | `STOCK_DAY` 同樣支援上櫃 | 與上市無差別；ETF/權證會被 `is_etf_or_warrant` 過濾 |
| 法人資料 T86 | 非交易日為空 | `make fetch-twse` 需在交易日執行 |
| inst_score | 永遠是 0（佔位） | 法人權重 15% 目前不影響強度，待 T86 累積後啟用 |
