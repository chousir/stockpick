# 03 — 三組策略定義

## 設計哲學

三組策略**不重疊、互補**，分別代表三種不同的市場觀點：

| | 策略 A | 策略 B | 策略 C |
|---|---|---|---|
| **代號** | breakout | growth_institutional | low_base_growth |
| **中文名** | 波段啟動 | 法人成長 | 低基期成長 |
| **核心邏輯** | 技術面強勢 | 基本面 + 籌碼 | 成長 + 回檔 + 法人布局 |
| **進場時機** | 已啟動，追強勢 | 已啟動，跟法人 | 跌深反彈，分批 |
| **持有時間** | 1-4 週 | 1-3 個月 | 1-3 個月 |
| **市場適用** | 多頭 | 多頭 / 盤整 | 盤整 / 修正 |
| **角色** | 攻擊 | 主力 | 反彈 / 逆向 |
| **預期篩出數** | 10-30 檔 | 10-20 檔 | 10-40 檔 |

**三組可能的重疊**：A∩B = 強勢成長股（最強候選）、B∩C = 法人布局中的成長股。

> **2026-W21 起策略 C 由「穩健存股」改為「低基期成長」**。原 C 條件（連續配息 8 年 + 殖利率 4%）
> 每週撈出 200+ 檔，灌票族群分析；舊版檔保留於 `config/strategies/archive/c_dividend_steady.yaml.bak`。

> **規則格式說明**（2026-05-16 驗證）：
> Goodinfo FL_RULE 格式為 `category||value@@display1@@display2`。
> FL_ITEM 為 Goodinfo 可篩選的數值欄位名稱（167 個選項，已從 JS 確認）。
> 格式錯誤會導致特定條件完全無效但不報錯。

---

## 策略 A：波段啟動（breakout）

`config/strategies/a_breakout.yaml`：

```yaml
id: a_breakout
name: "波段啟動"
description: "週MACD翻多 + 短中期均線多頭排列且走揚的起漲股候選"
holding_period: "1-4 weeks"
market: "上市/上櫃"

filters:
  - item: "成交筆數"
    min: 10000

rules:
  - "MACD||週MACD ↗–還原權值@@週MACD走勢@@還原權值–MACD ↗"
  - "均線位置||5日/10日/20日線多頭排列且走揚@@均價線多頭排列且走揚@@5日/10日/20日"

display_sheet: "交易狀況"
display_period: "日"

post_filter_sort:
  - field: "漲跌幅"
    order: "desc"
  - field: "成交張數"
    order: "desc"
```

**取捨**：
- 不加「股價區間」濾網，避免濾掉大漲後股價偏高但仍有空間的票。
- 使用還原權值版 MACD，避免除息造成的假跌。
- 不加「KD 黃金交叉」，避免錯過剛轉強、KD 還沒交叉的票。
- 不加「外資買超」，這留給策略 B。

---

## 策略 B：法人成長（growth_institutional）

`config/strategies/b_growth_institutional.yaml`：

```yaml
id: b_growth_institutional
name: "法人成長"
description: "月累計營收年增 15% 以上 + 外資與投信同步連續買超的成長股"
holding_period: "1-3 months"
market: "上市/上櫃"

filters:
  - item: "累計月營收年增減率(%)"
    min: 15

rules:
  - "法人買賣||外資連買 – 日@@外資連續買超@@外資連續買超 – 日"
  - "法人買賣||投信連買 – 日@@投信連續買超@@投信連續買超 – 日"

display_sheet: "交易狀況"
display_period: "日"

post_filter_sort:
  - field: "漲跌幅"
    order: "desc"
  - field: "成交張數"
    order: "desc"
```

**取捨**：
- 月累計營收年增 15% 過濾微弱成長，聚焦真正有動能的公司。
- 外資 + 投信雙確認：雙法人同步連買，籌碼穩定度高於單一法人。
- FL_RULE 的「連買 – 日」是連續買超（日計），沒有指定最少天數，Goodinfo 預設顯示任何連買天數。

---

## 策略 C：低基期成長（low_base_growth）

`config/strategies/c_low_base_growth.yaml`：

```yaml
id: c_low_base_growth
name: "低基期成長"
description: "月營收成長 + 股價自高檔回落但法人布局中的中線標的"
holding_period: "1-3 months"
market: "上市/上櫃"

filters:
  - item: "累計月營收年增減率(%)"
    min: 10

rules:
  - "法人買賣||外資連買 – 日@@外資連續買超@@外資連續買超 – 日"

display_sheet: "交易狀況"
display_period: "日"

post_filter_sort:
  - field: "漲跌幅"
    order: "desc"
  - field: "成交張數"
    order: "desc"

# 本地過濾：距 6 個月內最高價 -20% 以下
post_filter:
  - field: "pct_from_52w_high"
    months: 6
    max: -20.0
```

**取捨**：
- 月累計營收年增 ≥ 10%：比 B 策略寬鬆（B 是 15%），讓回檔成長股有機會入選。
- 外資連買：基本面 + 籌碼雙確認；不加投信，因低基期股投信常還沒進場。
- 距 6 個月高 -20% 以下：用本地 `stock_day_*.parquet` 算「目前股價 / 過去 6 個月最高 - 1」，
  保留跌幅夠深的成長股，排除追高股。**Goodinfo FL_ITEM 暫無等效項目**，所以走 post_filter。

**post_filter 機制**：
- 在 `runner.py` 解析 Goodinfo CSV 後跑，欄位 `pct_from_52w_high` 用 TWSE `STOCK_DAY` 6 個月歷史計算
- 首次跑 C3：30–50 檔候選 × 6 個月 × 1.5s ≈ 5–8 分鐘
- 之後過去月份永久快取，每週只多抓當月（10–20 秒）
- 資料不足的股票會**保留**並記 warning，不強制 filter（避免漏掉新上市股）

---

## 策略執行

```bash
# 跑單一策略
make screen STRATEGY=a_breakout

# 跑全部三組
make screen-all

# 看本週結果
ls reports/$(date +%Y-W%V)/
```

輸出格式：`reports/YYYY-Www/screen_result_{id}.csv`

CSV 欄位（所有策略一致）：
```
stock_id,name,market,close,change_pct,volume_lots,amount_million,pe_ratio,pb_ratio,strategy_id,screened_at,goodinfo_url
```

## 策略 ID 標籤命名規範

`group_analysis.md` Section 0「策略代號說明」會根據 `strategy.id` 的開頭字母給標籤：

| `strategy.id` 開頭 | 標籤 | 顯示意義 |
|---|---|---|
| `a_*` | `A` | 攻擊型短線 |
| `b_*` | `B` | 法人 / 基本面中線 |
| `c_*` | `C` | 反彈 / 逆向中線 |

替換同代號策略（如本次 `c_dividend_steady` → `c_low_base_growth`）時，**`group_report.py` 的
`_STRATEGY_LABEL` / `_STRATEGY_NAME` / `_STRATEGY_DESCRIPTION` 三個 dict 需同步加新項**，否則
Section 0 描述會缺漏（標籤本身會 fallback 到首字母大寫）。

## 新增策略的流程

1. 在 `config/strategies/` 加 YAML
2. 確認 FL_ITEM 名稱在 Goodinfo 167 個選項中（見 JS 驗證）
3. 確認 FL_RULE 格式為 `category||value@@display1@@display2`（從 StockList_ProtoAsync.js 提取）
4. 若需要本地過濾條件，加 `post_filter` 段（見策略 C3 範例）
5. 在 `tests/fixtures/goodinfo/` 加一份該策略的真實 HTML
6. 在 `group_report.py` 補上 `_STRATEGY_LABEL` / `_NAME` / `_DESCRIPTION` 三個 dict 的條目
7. 跑 `make test`
8. 跑 `make screen STRATEGY=<new_id>` 確認可運作
9. 在本檔加上策略卡片

**新增策略不需要寫 Python（除了 group_report.py 的描述 dict）**——這是 YAML-driven 設計的目的。

## 策略迭代記錄

`screen run-all` / `make screen-all` 跑完後，`ScreenerRunner.run_all` 會自動產出
`reports/YYYY-Www/screen_log.md`，純機械統計：
- 各策略篩出檔數表
- 兩兩交集（A∩B、A∩C、B∩C）
- 三方交集（A∩B∩C）

不含觀察判斷段——觀察由互動式 session 或 `make report-batch` 後 Claude 補。
