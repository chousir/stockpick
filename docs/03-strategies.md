# 03 — 策略定義

> ⚠️ **2026-08-28 更新（docs/31 §20.6）**：舊 Goodinfo 四式 **D/E/G 已軟退場**——
> D（近四季 ROE＋連續配息 8 年）、E/G（連續季數-單季稅後淨利）結構性無官方 API 歷史
> 查詢、永遠無法本地重建。`make week` 預設流程改跑 `screen-f-local`（F 官方 API 等價
> 定義本地算）＋ `screen-redesign-local`（本地新設計式 F2'/G1/G2/G4/G5/L6，docs/31
> §4/§7.2，`source=local_unvalidated`）。**Part B 以下的 D/E/G 定義只留作歷史參照**
> （`config/strategies/{d,e,g}_*.yaml` 未刪，手動 `make screen-all GROUP=defg` /
> `make screen STRATEGY=xxx` 不變）；G2 是 D 的正式本地接班（§20.1）。現行本地式的
> 完整清單見 README「策略體系」與 docs/11 策略代號框。
>
> 以下 Part A 的「經典三角」A/B/C 為**更早的實驗、已退役**（規劃書 04 A4）：YAML 檔已移至
> `config/strategies/archive/`，`GROUP=abc` **不再可跑**（會明確報退役）。本段僅留作
> 歷史紀錄。

## Part A：經典三角（A/B/C・已退役）

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
description: "頂尖 ROE + 長年連續配息 + 高殖利率的長線品質股（防守 / 壓艙石）"
holding_period: "6+ months"
market: "上市/上櫃"

filters:
  - item: "近四季–ROE(%)–本季度"   # ⚠️ EN DASH「–」(U+2013) 非 ASCII hyphen
    min: 20
  - item: "連續配發現金股利次數"
    min: 10
  - item: "成交價現金殖利率 (%)"
    min: 4

rules: []
```

**取捨**（全面提高版本）：
- 近 4 季 ROE ≥ 20%：頂尖品質（巴菲特買進級別的高標，台股上市公司中前 15% 才達標）。
  FL_ITEM 名稱使用 EN DASH（U+2013），ASCII hyphen 不會匹配
- 連續配發現金股利 ≥ 10 年：跨完整景氣循環 + 金融海嘯 / 疫情 / 升息等壓力測試
- 成交價現金殖利率 ≥ 4%：**取代 PB 的價值屬性**，確保不在估值高點
  （Goodinfo 沒有「股價淨值比」filter，只能用殖利率反推「不貴」訊號）
- 不設技術 rule：品質股不擇時，何時進場都能慢慢累積部位
- 預期撈出 15–40 檔（鎖定「頂尖品質職業個股」）

> 三條件全面提高的設計目的：避免「ROE 15% + 配息 8 + 殖利率 3%」這種「合格但
> 不出色」的票灌進來。本策略只挑「真正稀有的好公司」。如果撈出 < 10 檔（罕見），
> 適度降低殖利率到 3.5% 即可。

> 設計原則：A/B/C CSV 一律是「純 Goodinfo 結果快照」。策略可以換來換去，
> 但 CSV schema 保持單純（純 Goodinfo 12 欄），避免特定策略綁住整體流程。

---

# Part B：ProPicks 復刻組（D/E/F/G）— 歷史定義（D/E/G 已軟退場）

> ⚠️ **2026-08-28 起（docs/31 §20.6）**：`make week GROUP=defg` 預設流程**不再跑 Goodinfo
> D/E/G**（結構性無法本地重建），改跑 `screen-f-local`＋`screen-redesign-local`。以下 D/E/G
> 定義只留作歷史參照＋手動路徑（`make screen-all GROUP=defg` / `make screen STRATEGY=xxx`
> 未動）。F 改走本地等價定義。現行本地式清單見 README「策略體系」／docs/11 策略代號框。

D/E/F 是 ProPicks 復刻組，**每組混合多個 ProPicks 因子**（財務/成長/估值/動能），
目標是逼近 Investing.com ProPicks AI 在台股的選股風格；**G（成長拉回）是 E 的逆勢
孿生**，補抓「長多短空、回踩季線」的優質成長股（見下方策略 G）。

透過 `make week GROUP=defg`（現行唯一主流程）執行；abc/def 已退役（見下方「GROUP 機制」）。

| | 策略 D | 策略 E | 策略 F |
|---|---|---|---|
| **代號** | d_quality_leader | e_growth_momentum | f_value_rebound |
| **中文名** | 品質龍頭 | 成長動能 | 價值反彈 |
| **對標 ProPicks** | TWCH15（台灣晶片冠軍） | Tech Titans | Top Value Stocks |
| **核心邏輯** | 市值 + ROE + 配息 + 淨利連增 | 市值 + 強成長 YoY + 均線動能 | 市值 + 低 PER + 殖利率 + YoY≥10 |
| **持有時間** | 6 個月以上 | 1–3 個月 | 3–6 個月 |
| **預期篩出數** | 15-40 檔 | 10-30 檔 | 20-50 檔 |

**三組共用「市值 ≥ 100 億」**：D/E/F 風格的識別記號，鎖定中大型權值，剔除小型雜訊
（A/B/C 是全市場視角）。

## 策略 D：品質龍頭（quality_leader）

`config/strategies/d_quality_leader.yaml`：

```yaml
filters:
  - item: "市值 (億元)"
    min: 100
  - item: "近四季–ROE(%)–本季度"   # ⚠ EN DASH U+2013
    min: 15
  - item: "連續配發現金股利次數"
    min: 8
  - item: "連續增加季數–單季稅後淨利"  # ⚠ EN DASH U+2013
    min: 2

rules: []
```

**取捨**：
- ROE ≥ 15%（C 是 ≥ 20%，D 故意放寬以差異化）
- ProPicks TWCH15 的 ROIC + 低 D/E + 穩定 FCF 在 Goodinfo 找不到對應 filter，
  用「連續配息 8 年 + 連 2 季淨利」做現金流質量的間接證明
- 不放技術 rule：品質股不擇時

## 策略 E：成長動能（growth_momentum）

`config/strategies/e_growth_momentum.yaml`：

```yaml
filters:
  - item: "市值 (億元)"
    min: 100
  - item: "累計月營收年增減率(%)"
    min: 20
  - item: "連續增加季數–單季稅後淨利"  # ⚠ EN DASH U+2013
    min: 2

rules:
  - "均線位置||5日/10日/20日線多頭排列且走揚@@均價線多頭排列且走揚@@5日/10日/20日"
```

**取捨**：
- 月營收 YoY ≥ 20%（高於 B 的 15%）：E 抓「強成長」、B 抓「中成長 + 外資」
- 動能訊號用「均線多頭排列」（A 已驗證）；刻意不用 MACD 或外資連買，避免變 A/B 的子集

## 策略 F：價值反彈（value_rebound）

`config/strategies/f_value_rebound.yaml`：

```yaml
filters:
  - item: "市值 (億元)"
    min: 100
  - item: "本益比 (PER)"
    max: 15
  - item: "成交價現金殖利率 (%)"
    min: 3
  - item: "累計月營收年增減率(%)"
    min: 10

rules: []
```

**取捨**：
- 「累計月營收 YoY ≥ 10」是**價值陷阱過濾器**：低估的股要有實質成長（早期版本 ≥ 0 太寬鬆）
- 殖利率 ≥ 3%（C 是 ≥ 4%）：寬鬆版價值；本組與 C 不會同時跑（GROUP 互斥）
- 不放技術 rule

## 策略 G：成長拉回（growth_pullback）

`config/strategies/g_growth_pullback.yaml`：

```yaml
filters:
  - item: "市值 (億元)"
    min: 100
  - item: "累計月營收年增減率(%)"
    min: 20
  - item: "連續增加季數–單季稅後淨利"  # ⚠ EN DASH U+2013
    min: 2

rules: []   # 刻意不放均線 rule；拉回 timing 在分析層做
```

**為何存在（E 的逆勢孿生）**：A/E 都寫死「均線多頭排列」rule，AI 龍頭一拉回、
均線排列就斷 → E 不再命中，而它同時也過不了 F（本益比貴）、D（沒配息 8 年），
**整條 pipeline 隱形**。G 用與 E 相同的基本面、但**故意拿掉均線 rule**，把「拉回中的
優質成長股」收進候選宇宙。E 順勢（突破/續強，多頭盤強）、G 逆勢低接（回踩季線，
整理盤強），兩者近乎互斥、共同覆蓋各種市況。

**拉回 setup（在分析層 `grouping.py` 用已快取的收盤價 / 量比計算，零新增資料來源）**：
- **季線上揚**：MA60 今天 > 約 10 交易日前（趨勢未壞）
- **乖離帶**：股價距季線 −5% ~ +10%（回到季線附近，非延伸、非崩跌）
- **量縮**：量比（今日量 / 20 日均量）≤ 1.0（健康拉回在量縮上發生）

三者皆硬門檻、寫在 `settings.yaml` 的 `g_pullback` 可調。Goodinfo 端只出「成長宇宙」，
有效命中（`in_g`）= 基本面命中 ∧ 三條件全中。

**取捨**：
- 乖離/量縮不靠 Goodinfo FL_RULE（其支援度不明），改用本地 MA60/量比，完全可控
- 量比基準用 20 日均量（與報表「量比」欄一致），非 5 日
- 與 E 的火花：**E∩G**＝主升段回踩、雙確認最強；**G 單獨**＝被拉回的成長領先股，
  「跌後可能再起漲」的核心候選；搭配法人欄判讀——**G+量縮+法人續買＝健康、G+法人倒貨＝陷阱**

## D/E/F 內部互補

| | 獨有條件 | ProPicks 維度 |
|---|---|---|
| D | ROE + 配息 | 財務面 |
| E | 均線 rule + YoY ≥ 20 | 成長面 + 市場面 |
| F | PE max + YoY ≥ 0 | 估值面 |

**預期交集**：
- D ∩ E（品質+成長）：稀有但最有意義，0–5 檔
- D ∩ F（高 ROE + 低 PE）：巴菲特核心持股，5–15 檔
- E ∩ F（戴維斯雙擊候選）：0–5 檔
- D ∩ E ∩ F：理論接近 0

## D/E/F 欄位校正記錄

新增 D/E/F 時手動到 Goodinfo UI 校正過的欄位（**正式名稱已固定**）：

| 欄位 | 原推測 | Goodinfo 真實名稱 |
|---|---|---|
| 市值 | `市值(億)` | `市值 (億元)`（半形空格 + 億元） |
| 本益比 | `本益比 (PER)` | `本益比 (PER)`（推測即正確） |

校正流程（新增策略時都該跑一遍）：跑 `--dry-run` → 把 URL 貼到瀏覽器 → 開 Goodinfo
自訂篩選頁 → 看 filter 是否真有被勾起（**錯誤名稱會被 Goodinfo 靜默忽略**）→ 不對
就找正確的 dropdown 字串改 YAML → 重跑 `--dry-run`。

---

## GROUP 機制

`make week` 強制要求 `GROUP`，無預設值。**現行主流程是 `GROUP=defg`**。

```bash
make week GROUP=defg       # ★ 現行唯一主流程（GROUP=defg 為必填 guard；2026-08-28 起實際跑 screen-f-local＋screen-redesign-local，不跑 Goodinfo D/E/G）
make week GROUP=abc        # ❌ 已退役（規劃書 04 A4）：明確報退役、不跑
make week GROUP=def        # ❌ 已退役：併入 defg
make week                  # ❌ 報錯：請指定 GROUP=defg
make weekend GROUP=defg    # 含 commit/push
make screen-all GROUP=defg # 只跑 screen-all 部分
make screen STRATEGY=d_quality_leader  # 單策略仍可（不需 GROUP）
```

**實作**：
- `config/strategies/` 平鋪 yaml，不用子目錄；退役策略移至 `config/strategies/archive/`
- `tw-screener screen run-all --group defg`：runner 依 `strategy.id` 首字母過濾
  （d/e/f/g 屬 defg；abc/def 已退役，給定即明確報退役）
- 互斥語意：reports/Www/ 只會有 defg（d/e/f/g）的 CSV

---

## 策略執行

```bash
# 跑單一策略（不需 GROUP）
make screen STRATEGY=d_quality_leader

# 跑指定組（GROUP 必填）
make screen-all GROUP=defg   # 現行唯一主流程（abc/def 已退役）

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
| `d_*` | `D` | ProPicks 品質龍頭 |
| `e_*` | `E` | ProPicks 成長動能 |
| `f_*` | `F` | ProPicks 價值反彈 |

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

不含觀察判斷段——觀察由互動式 session 或個股報告（`make report STOCK_ID=`）後 Claude 補。
