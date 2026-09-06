# 05 — 族群分析與領頭羊判斷

## 為什麼這層存在

台股最重要的市場特性：**強族群帶動明顯**。
- 一檔孤鳥強勢上漲，事後常被驗證為假突破。
- 一個族群同步走強，個股勝率大幅提升。
- 族群內最早動、量最大的那檔，常是領頭羊；領頭羊轉弱往往是族群結束的先行訊號。

所以選股結果出來後**先做族群分析、再決定個股深度報告的優先順序**。

## 模組輸入

- 各策略的 CSV：`reports/YYYY-Www/screen_result_*.csv`（主流程 GROUP=defg → d/e/f/g）
- 候選股價量歷史（13 個月）：`data/cache/twse/stock_day_*.parquet` + `daily_*.parquet`
- 近 20 日三大法人（含上市 T86 + 上櫃 TPEX）：`data/cache/twse/institutional_*.parquet`

## 模組輸出

`reports/YYYY-Www/group_analysis.md`：

```markdown
# 2026-W21 族群分析報告

## 0. 策略代號說明

> ⚠️ **2026-08-28 起（docs/31 §20.6）**：舊 Goodinfo D/E/G 已軟退場、`make week` 不再產出其
> `screen_result`。現行本地篩選：**F**（價值反彈・官方 API 等價定義本地算，`source=local`）＋
> **F2'/G1/G2/G4/G5/L6**（本地新設計式，`source=local_unvalidated`，docs/31 §4/§7.2）。
> G2 是 D 的正式本地接班（§20.1）。以下 D/E/G 定義只留作歷史參照。

- F 價值反彈：市值≥100 億 + PER≤15 + 殖利率≥3 + 累計月營收 YoY≥10（3–6 月，現行）
- F2' 本地-成長優質股：PE 15–30 ∧ 毛利優於同儕 ∧ Δ營益率≥0 ∧ 市值≥300億（§20.10，現行）
- G1/G2/G4/G5/L6：本地未驗證式，代號與定義見 README「策略體系」／docs/11 策略代號框（現行）
- ~~D 品質龍頭：市值≥100 億 + ROE≥15 + 配息 8 年 + 連 2 季淨利~~（軟退場，G2 接班）
- ~~E 成長動能：市值≥100 億 + 營收 YoY≥20 + 連 2 季淨利 + 均線多頭~~（軟退場）
- ~~G 成長拉回：同 E 基本面 + 季線上揚回踩 + 量縮~~（軟退場）

## 1. 入選分布總覽
- 各式命中檔數（動態、隨門檻變）＋交集，見 `group_analysis.md` Section 1；本地未驗證式門檻鬆、聯集常達數百檔
- 總聯集：僅含有效命中股（去重）

## 2. 族群強度排名（前 10）

| Rank | 族群 | D | E | F | G | 族群入選率 | 5 日中位 | 上漲家數 | 法人買超 | 強度分數 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 通信網路業 | 1 | 2 | 1 | 1 | 30% | +2.0% | 4/7 | 6/7 | 58.1 |
| ... |

## 3. 各族群本週表現
（每族群列出「前 3 名（依 leader_score）」+ 命中策略標籤，不再用「領頭羊」字眼）

### 1. 通信網路業（共 7 檔入選・5 日中位 +2.0%・上漲 4/7）
本週族群表現前 3 名（欄含量比、距月線、距季線、法人/外資/投信）：

| # | 股號 | 名稱 | 5 日漲幅 | 當日 | 量比 | 距月線 | 距季線 | 成交金額 | 法人(張) | 外資(張) | 投信(張) | 命中策略 | Goodinfo |
| 1 | 2455 | 全新 | +7.1% | +4.6% | 3.1x | +12.6% | +40.9% | 10967 | +13,131 | +7,900 | +6,744 | D+E | 連結 |
| 2 | ... |

其他入選股：8069 元太、3596 智易...

## 5. Claude 次產業深度分析請求（雷達驅動）
（由 Section 2.8 輪動雷達挑 top-N 領先次產業、逐塊列成員候選股，請 Claude 檢查籌碼/趨勢/訂單外溢）

## 6. Claude CP 補漲候選分析請求（個股層・讀同夾 cp_candidates.md）
（「法人資金已進、股價尚未反映」的補漲觀察清單：埋伏/追突破/反轉三型態＋C2 三重濾網，請 Claude 分析）

## 7. Claude 持有/觀察清單健檢請求（你的部位・與命中策略同等深度）
（讀同夾 holdings_enriched.csv / watchlist_enriched.csv，逐檔判續抱/收緊/停利、接近進場/再等/剔除；
含多鏡頭交集優先、新鮮度過濾，對照 cp_candidates.md 末段過熱-退潮警示。M-MH 精修輪新增）
```

## 演算法

### 5.1 族群分組

來源：TWSE 官方產業類別（28 類上市）+ TPEX ISIN 頁面（上櫃，`isin.twse.com.tw/isin/C_public.jsp?strMode=4`）。

每檔股票屬於**唯一一個**官方產業類別（單標籤）。ETF（股號開頭 `00`）和權證（非數字開頭）在族群分析前過濾掉。

分類粒度：TWSE 官方 28 類（半導體業、電腦及周邊設備業、通信網路業…）為主。
更細的次產業（記憶體/記憶體模組/IC設計/封測/晶圓代工、金融證券/壽險/銀行、航運貨櫃/散裝/航空…）**已由 `config/concepts.yaml` 手標、在 Section 2.6/2.8 自動分群排名**（補大分類太粗）；Claude 在 Section 6 再就成員股做籌碼/趨勢深判。

> **設計考量（已決策）**：Goodinfo 自編的「概念股」tag 粒度更細，且一檔可屬多個概念（multi-label）。未實作原因：需逐檔爬 Goodinfo（2000 檔 × 3 秒 ≈ 1.5 小時），且 tag 為 Goodinfo 自編文字、維護成本高。現行做法：粗分類自動化，細分類交由 Claude 判斷。

只顯示族群入選股 ≥ 2 檔的族群（單一個股不算族群）。

### 5.2 族群強度分數（2026-W21 起改為動能主導）

```
強度分數 = w_mom  * 100 * sigmoid(momentum_5d / 5)            # 動能（sigmoid 校準）
         + w_er   * 100 * entry_rate                          # 入選率
         + w_inst * 100 * inst_score                          # 法人買超家數比（已啟用）
         + w_sz   * 100 * (log1p(members) / log1p(max))       # 規模

其中：
  momentum_5d   = 族群入選股的 5 日累計報酬「中位數」（median，抗單檔小型股灌水；
                  取自 stock_day_*.parquet 或 daily_*.parquet；資料不足時 fallback
                  到 change_pct，並在族群層級記錄 momentum_5d_days_used）
  entry_rate    = 族群入選股數 / 族群在 industry_df 中的總檔數
  inst_score    = 族群內近 20 日三大法人淨買超 (inst_net>0) 的家數 / 成員數（落在 [0,1]）
  members       = 族群入選股數
  另記 up_count（5 日漲幅 > 0 家數，供報告算「上漲家數」廣度）、
       inst_buy_count（法人買超家數），個股層另帶量比 / 距月線 / 距季線。

預設權重（config/settings.yaml 可調）：
  w_mom  = 0.50  # 動能：5 日累計漲幅（族群實際走勢）
  w_er   = 0.25  # 入選率：條件命中度
  w_inst = 0.15  # 法人偏好
  w_sz   = 0.10  # 對數規模：避免小族群因高入選率過度佔先

sigmoid 而非 clip：避免 5 日大漲 20% 仍只拿到 clip(10) 的天花板，
讓動能強族群在分數上有區分度（5 日漲 5% → 0.5；漲 10% → 0.73；漲 20% → 0.88）。
```

**動能 / 均線資料來源（X+Y 混合策略）**：
- **X**：`make week` 流程中跑 `make fetch-candidates-history`，對本週入選股聯集去重
  個股批次補抓 STOCK_DAY **13 個月**歷史（MA60 斜率需 ≥70 日）。過去月份永久快取，
  首次 ~30–40 分鐘，之後每週只抓當月
- **Y**：`data/cache/twse/daily_*.parquet` 每週累積一筆
- 兩者由 `TWSEClient.load_candidate_history()` 合併，算 5 日動能、MA20/60＋斜率；
  量比由 `load_volume_history()` 算（今日量 / 近 20 日均量）

**完整 `make week GROUP=defg` 流程**（節錄；完整 14 步見 README／Makefile week target）：
```
1. fetch-twse              → daily / T86 / 上櫃 TPEX 法人 / revenue / industry
2. screen-f-local          → F 官方 API 等價定義本地算（source=local）
2b. screen-redesign-local  → F2'/G1/G2/G4/G5/L6 本地新設計式（source=local_unvalidated）
3. fetch-candidates-history → 對聯集個股抓 stock_day（純加工資料層）
4. group                   → 算 5 日中位 / 量比 / MA60 / 法人 → group_analysis.md
```
（舊 `screen-all`＝跑 Goodinfo D/E/F/G，2026-08-28 起不再被 week 自動呼叫，手動仍可跑）

**策略 CSV 一律是純 Goodinfo 結果快照**，不被任何後處理覆寫。
（G 的 CSV 是基本面成長宇宙；拉回過濾只在 group 步驟標記於 group_analysis.md，不改 CSV。）
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
  + 0.10 * strategy_count_rank_norm  # 入選策略數排名（D∩E∩F > D∩E > 單一；G 為有效命中後計）
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
    institutional: pl.DataFrame | None = None,  # 近 20 日三大法人 → inst_net / inst_score
    volume_history: pl.DataFrame | None = None, # → 量比 vol_ratio
    g_pullback: dict[str, float] | None = None, # G 拉回 setup 門檻（季線/乖離帶/量縮）
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """回傳 (groups_df, members_df)。members 含 momentum_5d / inst_net / 外資 / 投信 /
    vol_ratio / ma20_dist_pct / ma60_dist_pct / ma60_slope_pct / in_{sid}（G 已收斂）。
    G 收斂後丟掉 strategy_count==0 的宇宙雜訊，避免污染族群統計。"""

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

## 觀察段（Section 4）已移除

早期設計留「## 4. 觀察」自由文段給 Claude Code 事後補寫，但實務上解讀全走
docs/11 的 pick.md 流程、7 週從未補寫過——2026-07 起模板移除此段（死佔位）。
Section 5-7 編號保留不變（docs/11 prompt 以編號交叉引用）。

## 已知限制

| 項目 | 狀態 | 影響 |
|---|---|---|
| 族群分類粒度 | TWSE/TPEX 官方 28 類（粗，單標籤） | 無法區分 IC 設計 vs 封測；由 Claude Section 6 分析補充 |
| earliest_breakout_rank | 不含此因子 | 族群內排名用 momentum / 成交值 / 法人 / 策略數四因子；突破時序未納入 |
| 5 日動能資料 | 首次 `make week` 前 5 日漲幅依快取深度有 `*` 標註 | 第 1 週可能只有 1–2 日資料；跑過 `fetch-candidates-history` 後第 2 週起穩定 |
| 上櫃股 OHLCV 歷史 | 上櫃走 TPEX `tradingStock`、上市走 `STOCK_DAY`（同 schema） | 與上市無差別；ETF/權證會被 `is_etf_or_warrant` 過濾 |
| 法人資料 | 上市 T86 + 上櫃 TPEX；非交易日為空 | `make fetch-twse` 需在交易日執行；上櫃 TPEX 僅最新日、逐次累積 |
| inst_score | 已啟用＝法人買超家數比 | 法人權重 15% 計入強度；無法人快取時退回 0 |
| MA60 斜率 / G 拉回 | 需 ≥70 交易日歷史 | 新上市未滿者該欄 null，G 不標（誠實，非錯誤） |
