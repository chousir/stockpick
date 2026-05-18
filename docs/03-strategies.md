# 03 — 三組策略定義

## 設計哲學

**經典三角**：三組策略**不重疊、互補**，分別覆蓋技術 / 基本面 / 品質三個維度：

| | 策略 A | 策略 B | 策略 C |
|---|---|---|---|
| **代號** | a_breakout | b_growth_institutional | c_quality_value |
| **中文名** | 動能突破 | 成長主力 | 品質價值 |
| **核心邏輯** | 技術面動能 | 基本面 + 籌碼 | 高 ROE + 配息 + 估值 |
| **核心條件** | 週 MACD + 均線多頭 + 流動性 | 營收 YoY + 連續季淨利成長 + 外資連買 | ROE ≥ 15% + 連續配息 + 殖利率 |
| **進場時機** | 已啟動，追強勢 | 已啟動，跟法人 | 不擇時，分批 |
| **持有時間** | 1-4 週 | 1-3 個月 | 6 個月以上 |
| **市場適用** | 多頭 | 多頭 / 盤整 | 任何市場（防守） |
| **角色** | 攻擊 | 主力 | 防守 / 壓艙石 |
| **預期篩出數** | 20-40 檔 | 10-30 檔 | 30-60 檔 |

**三角互補的邏輯**：
- A 抓**短線爆發力**（量價、技術面強勢），勝率不確定但賠率高
- B 抓**中線基本面 + 籌碼**（成長、法人），同時要求 EPS 與營收成長雙引擎避免單月營收灌水
- C 抓**長線品質**（ROE、估值、配息），用「品質 + 不貴」過濾，提供組合穩定度

**三組可能的重疊**：
- A ∩ B = 強勢成長股（動能 + 基本面雙確認，最強候選）
- B ∩ C = 高品質成長股（成長股估值不過高 → 可持有更久）
- A ∩ C 較罕見（高 ROE 大型權值股不常有技術突破）

> **2026-W21 起策略大改版**：
> - A 改名「動能突破」，提高流動性門檻
> - B 改名「成長主力」，加 EPS YoY 條件，拿掉投信連買（避免雙連買過嚴）
> - C 由「低基期成長」改為「品質價值」（ROE + 連續配息 + PB）
> 舊版檔保留於 `config/strategies/archive/c_low_base_growth.yaml.bak`、`c_dividend_steady.yaml.bak`。

> **不在策略範圍內的**：
> - **主題追蹤 / 概念股**（如低軌衛星、AI、5G）：A/B/C 是「通用條件篩」，
>   不挑特定產業概念。專屬概念股篩選需另設計，且 Goodinfo「概念股」分類粒度
>   粗，建議搭配新聞 / 法人報告人工判讀。

> **規則格式說明**（2026-05-16 驗證）：
> Goodinfo FL_RULE 格式為 `category||value@@display1@@display2`。
> FL_ITEM 為 Goodinfo 可篩選的數值欄位名稱（167 個選項，已從 JS 確認）。
> 格式錯誤會導致特定條件完全無效但不報錯。

---

## 策略 A：動能突破（a_breakout）

`config/strategies/a_breakout.yaml`：

```yaml
id: a_breakout
name: "動能突破"
description: "週MACD翻多 + 5/10/20 均線多頭排列且走揚 + 流動性過濾的攻擊型短線股"
holding_period: "1-4 weeks"
market: "上市/上櫃"

filters:
  - item: "成交筆數"
    min: 15000

rules:
  - "MACD||週MACD ↗–還原權值@@週MACD走勢@@還原權值–MACD ↗"
  - "均線位置||5日/10日/20日線多頭排列且走揚@@均價線多頭排列且走揚@@5日/10日/20日"
```

**取捨**：
- 流動性門檻 15000 筆（原 10000）：過濾冷門股，避免進場後流動性風險
- 還原權值版 MACD：避免除息造成的假跌訊號
- 不加「KD 黃金交叉」：避免錯過剛轉強、KD 還沒交叉的票
- 不加「外資買超」：技術面策略不混入籌碼，留給 B
- 「創 60 日新高」Goodinfo FL_RULE 無對應項目；改在 `group_analysis.md` 用 5 日累計動能 + 族群排名補強

---

## 策略 B：成長主力（growth_institutional）

`config/strategies/b_growth_institutional.yaml`：

```yaml
id: b_growth_institutional
name: "成長主力"
description: "營收成長 + 連續季淨利增加 + 外資布局中的主力中線股"
holding_period: "1-3 months"
market: "上市/上櫃"

filters:
  - item: "累計月營收年增減率(%)"
    min: 15
  - item: "連續增加季數–單季稅後淨利"   # en dash U+2013
    min: 2

rules:
  - "法人買賣||外資連買 – 日@@外資連續買超@@外資連續買超 – 日"
```

**取捨**：
- 月累計營收 YoY ≥ 15%：營收動能
- **連續增加季數–單季稅後淨利 ≥ 2**：實質獲利連增（FL_ITEM 名稱從 Goodinfo URL
  實測校正，使用 en dash `–` U+2013 非 ASCII hyphen）。避免單純營收灌水但無利潤的票。
- **拿掉「投信連買」**：舊版「外資 + 投信雙連買」每週撈 < 10 檔太嚴，
  改用「淨利連增」補強質量；外資連買單一條件即可（投信通常跟外資）
- FL_RULE 的「連買 – 日」是連續買超（日計），沒有指定最少天數，Goodinfo 預設顯示任何連買天數

---

## 策略 C：品質價值（quality_value）

`config/strategies/c_quality_value.yaml`：

```yaml
id: c_quality_value
name: "品質價值"
description: "高 ROE + 長年連續配息 + 殖利率合理的長線品質股（防守 / 壓艙石）"
holding_period: "6+ months"
market: "上市/上櫃"

filters:
  - item: "近四季–ROE(%)–本季度"   # ⚠️ EN DASH「–」(U+2013) 非 ASCII hyphen
    min: 15
  - item: "連續配發現金股利次數"
    min: 8
  - item: "成交價現金殖利率 (%)"
    min: 3

rules: []
```

**取捨**：
- 近 4 季 ROE ≥ 15%：高品質公司（巴菲特標準的低標）。能在多種市場條件下持續產生回報。
  FL_ITEM 名稱使用 EN DASH（U+2013），ASCII hyphen 不會匹配
- 連續配發現金股利 ≥ 8 年：跨完整景氣循環，過濾掉一次性配息與假成長股
- 成交價現金殖利率 ≥ 3%：**取代 PB 的價值屬性**，確保不在估值高點
  （Goodinfo 沒有「股價淨值比」filter，只能用殖利率反推「不貴」訊號）
- 不設技術 rule：品質股不擇時，何時進場都能慢慢累積部位
- 預期撈出 30–80 檔（不像舊版「ROE + 配息 5 年」單條件撈 300+ 灌爆篩選上限）

> 設計原則：A/B/C CSV 一律是「純 Goodinfo 結果快照」。策略可以換來換去，
> 但 CSV schema 保持單純（純 Goodinfo 12 欄），避免特定策略綁住整體流程。

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
