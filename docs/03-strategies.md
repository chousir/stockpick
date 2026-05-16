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

## 策略 C：穩健存股（dividend_steady）

`config/strategies/c_dividend_steady.yaml`：

```yaml
id: c_dividend_steady
name: "穩健存股"
description: "連續配息 8 年以上且現金股利持續增加或持平的長期持有候選"
holding_period: "6+ months"
market: "上市/上櫃"

filters:
  - item: "連續配發現金股利次數"
    min: 8

rules:
  - "股利政策||連續配發現金股利@@連續配發股利次數@@連續配發現金股利"
  - "股利政策||現金股利連續增加或持平@@股利連續增減或持平@@現金股利連續增加或持平"

display_sheet: "交易狀況"
display_period: "日"

post_filter_sort:
  - field: "漲跌幅"
    order: "desc"
  - field: "成交張數"
    order: "desc"
```

**取捨**：
- 連續配息 8 次（≈8 年）：跨越完整景氣循環（通常 7-10 年），排除一次性配息。
- 「現金股利連續增加或持平」：排除股利逐年遞減（財務惡化訊號）。
- 不設殖利率下限：殖利率高低需對比當時利率環境，由人判斷；避免程式鎖死邏輯。
- display_sheet 統一用「交易狀況」：篩選結果 CSV 欄位一致，個股詳情由報告階段另行抓取。

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

## 新增策略的流程

1. 在 `config/strategies/` 加 YAML
2. 確認 FL_ITEM 名稱在 Goodinfo 167 個選項中（見 JS 驗證）
3. 確認 FL_RULE 格式為 `category||value@@display1@@display2`（從 StockList_ProtoAsync.js 提取）
4. 在 `tests/fixtures/goodinfo/` 加一份該策略的真實 HTML
5. 跑 `make test`
6. 跑 `make screen STRATEGY=<new_id>` 確認可運作
7. 在本檔加上策略卡片

**新增策略不需要寫 Python**——這是 YAML-driven 設計的目的。

## 策略迭代記錄

每週執行後，由 Claude Code 在 `reports/YYYY-Www/screen_log.md` 寫一段：
- 三組各篩出幾檔
- 重疊標的（A∩B, B∩C, A∩C, A∩B∩C）
- 異常觀察（例如全市場大跌時 A 篩出 0 檔，這是正常的）
