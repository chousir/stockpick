# 03 — 三組策略定義

## 設計哲學

三組策略**不重疊、互補**，分別代表三種不同的市場觀點：

| | 策略 A | 策略 B | 策略 C |
|---|---|---|---|
| **代號** | breakout | growth_institutional | dividend_steady |
| **中文名** | 波段啟動 | 法人成長 | 穩健存股 |
| **核心邏輯** | 技術面強勢 | 基本面 + 籌碼 | 穩定配息 + 合理估值 |
| **進場時機** | 已啟動，追強勢 | 已啟動，跟法人 | 不擇時，分批 |
| **持有時間** | 1-4 週 | 1-3 個月 | 6 個月以上 |
| **市場適用** | 多頭 | 多頭 / 盤整 | 任何市場 |
| **角色** | 攻擊 | 主力 | 防守 / 壓艙石 |
| **預期篩出數** | 10-30 檔 | 10-20 檔 | 15-30 檔 |

**三組可能的重疊**：A∩B = 強勢成長股（最強候選）、B∩C = 穩定成長配息股（核心持股）。

---

## 策略 A：波段啟動（breakout）

`config/strategies/a_breakout.yaml`：

```yaml
id: a_breakout
name: "波段啟動"
description: "技術面剛轉強、量價配合的起漲股候選"
holding_period: "1-4 weeks"
market: "上市/上櫃"

filters:
  - item: "成交筆數"
    period: "日"
    min: 5000
  - item: "月成交均量"
    min: 1000      # 張
  - item: "股價"
    min: 15
    max: 300

rules:
  - "週MACD ↗–還原權值"          # 週 MACD 翻多動能初現
  - "均線多頭排列–日"             # 5/10/20/60 日線多頭排列
  - "股價站上月線"
  - "量增價漲（突破日）"

display_sheet: "交易狀況"
display_period: "日"

# 用於後續排序與分析
post_filter_sort:
  - field: "成交筆數"
    order: "desc"
  - field: "近5日漲幅"
    order: "desc"
```

**取捨**：
- 不加「KD」濾網，避免錯過剛轉強、KD 還沒交叉的票。
- 不加「外資買超」濾網，這留給策略 B。

---

## 策略 B：法人成長（growth_institutional）

`config/strategies/b_growth_institutional.yaml`：

```yaml
id: b_growth_institutional
name: "法人成長"
description: "營收動能 + 法人連續買超的成長股"
holding_period: "1-3 months"
market: "上市/上櫃"

filters:
  - item: "近 1 月營收年增率 (%)"
    min: 15
  - item: "最新季 EPS (元)"
    min: 0.01
  - item: "毛利率（近4季）年增 (%)"
    min: 0

rules:
  - "月累計營收連年增加"                     # ≥ 4 季
  - "外資連續買超 5 日以上"
  - "投信連續買超 3 日以上"
  - "融資 5 日減少"                           # 散戶賣 + 法人買 = 鎖籌

display_sheet: "法人買賣"
display_period: "日"

post_filter_sort:
  - field: "外資近5日買超張數"
    order: "desc"
  - field: "月營收年增率"
    order: "desc"
```

**取捨**：
- EPS 門檻 0.01 而非 0：剔除微小虧損但避免完全過濾掉轉盈個股。
- 「外資 5 日 + 投信 3 日」雙條件，門檻嚴格，篩出量會少但品質高。
- 加「融資減少」是台股獨特訊號：散戶賣、法人買，籌碼結構轉佳。

---

## 策略 C：穩健存股（dividend_steady）

`config/strategies/c_dividend_steady.yaml`：

```yaml
id: c_dividend_steady
name: "穩健存股"
description: "穩定配息 + 合理估值 + 財務體質佳的長期持有候選"
holding_period: "6+ months"
market: "上市/上櫃"

filters:
  - item: "連續配發現金股利次數"
    min: 8                       # 連續配息 8 年以上
  - item: "近 5 年平均現金殖利率 (%)"
    min: 4
  - item: "近 5 年平均盈餘配發率 (%)"
    min: 40
    max: 80
  - item: "ROE (近4季)"
    min: 10
  - item: "負債比 (%)"
    max: 60
  - item: "日均成交筆數"
    min: 1000

rules:
  - "EPS 連續 8 季為正"
  - "股價淨值比 (PB) 介於歷史 30~70 百分位"
  - "近 1 月股價距年線 -10% ~ +15%"

display_sheet: "獲利狀況"
display_period: "年"

post_filter_sort:
  - field: "近5年平均殖利率"
    order: "desc"
  - field: "ROE近4季"
    order: "desc"
```

**取捨**：
- 殖利率不設無上限，避免抓到「殖利率高是因為股價暴跌」的陷阱。
- 配發率 40-80%：不是把賺的全發掉（沒成長），也不是吝嗇（不發給股東）。
- 加流動性下限 1000 筆／日：存股也要能進出。

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

CSV 欄位：
```
stock_id,name,industry,close,volume,strategy_id,screened_at,goodinfo_url
```

## 策略迭代記錄

每週執行後，由 Claude Code 在 `reports/YYYY-Www/screen_log.md` 寫一段：
- 三組各篩出幾檔
- 重疊標的（A∩B, B∩C, A∩C, A∩B∩C）
- 異常觀察（例如全市場大跌時 A 篩出 0 檔，這是正常的）

三個月後跑 `make backtest-strategies`，產出：
- 每組策略在過去篩出的標的，後 1/4/12 週的平均報酬
- 勝率
- 建議調整方向

## 新增策略的流程

1. 在 `config/strategies/` 加 YAML
2. 在 `tests/fixtures/goodinfo/` 加一份該策略的真實 HTML（防 HTML 結構變化）
3. 跑 `make test`
4. 跑 `make screen STRATEGY=<new_id>` 確認可運作
5. 在本檔加上策略卡片

**新增策略不需要寫 Python**——這是 YAML-driven 設計的目的。
