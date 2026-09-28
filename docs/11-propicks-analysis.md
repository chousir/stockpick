# 11 — 週選股：shortlist 機器排序 ＋ Opus 一頁決策卡（pick.md／pick_detail.md）

> 本檔是「跑完 `make week GROUP=defg` 後，怎麼產出本週 `pick.md`（一頁決策卡＋機器排序 Top 5）與
> `pick_detail.md`（明細）」的標準流程，**也是兩種跑法（網頁手動貼／`/daily-picks`）共用的唯一規格來源**。
>
> **排序由程式依已驗證訊號完成**（`reports/<週>/shortlist.csv`）；Opus 只負責依序說明、查證、最多否決
> `max_vetoes` 檔、整理持股動作與風險——**不再自由選股**。

---

## 變更紀錄

- **2026-09-28 M-Pick2（只改狀態字樣，排序規則不變）**：族群內挑檔研究結案（docs/32）——四個預註冊個股因子
  （個股 RS 6-1 月動能、52 週高點接近度、EPS 加速、月營收 YoY 加速）在 shortlist 可入選池內 r+20 族群內 IC
  皆未過關（點估計 −0.013～+0.024、CI 全跨 0），首選也不勝現行決勝規則（偏好帶→成交額）。本檔「M-Pick2 研究中」
  字樣改為結果；`bear_hints` 固定首項「族群層訊號、個股層未驗證」照舊（仍屬實）。
- **2026-09-27 M-Pick1**：`pick.md` 改為「一頁決策卡（≤50 行）＋機器排序 Top 5＋picks YAML」，其餘內容搬到新檔
  `pick_detail.md`（附錄 A–H＋資料品質披露）。Top 5 順序＝`shortlist.csv` 的 `rank`、不得重排；Opus 最多否決
  `picks.shortlist.max_vetoes`（現行 2）檔。
  - **退役**：四路匯流候選來源（任務 1 深度解讀／全宇宙掃描／CP 補漲／觀察清單升格）、兩階段挑股（排雷＋精選）、
    clean 池排序階梯、轉折早段 quota、核心／機會／乾淨補充池三層選股、機會層 M-BR1 左側小注子表（改到
    `pick_detail.md` 附錄 H 揭露、標 ⚠️未驗證）、決策卡的估值兩欄（估值缺口%／綜合估值區間，後者只留在附錄 G）。
  - **理由**：分層無鑑別力（M-Pick1 開工盤點，4 週窗：核心 −0.4%／機會 −0.9%／補充池 +0.2%）＋個股層法人流已否證
    （docs/19、docs/20、docs/22 §4）而舊 prompt 的入選理由大量倚賴它＋使用者要一頁 Top 5。
  - **回退**：`config/settings.yaml` 設 `picks.shortlist.enabled: false`，並把本檔還原為 M-Pick1 之前的版本
    （`git log -- docs/11-propicks-analysis.md` 可查）。

---

## 為何這樣做（M-Pick1）

| 證據 | 結論 | 在本流程的角色 |
|---|---|---|
| 族群趨勢分 `trend_score`（sector_rotation，F3 價格趨勢分） | **唯一穩健族群訊號**：r+20 IC +0.11，進攻／中性／防禦三個 regime CI 皆 >0（全量重跑，docs/23 §2.1；首輪 +0.18，docs/22 §0／§2） | shortlist 主排序鍵（`trend_bucket`） |
| 候選宇宙內距季線 `ma60_dist_pct` | 越近越好（r+20 IC −0.217，5–10% 桶最好）——**弱證據、單 regime**（docs/19 §3、docs/22 §1.3、docs/23） | 同桶內次序鍵（偏好帶 5–10%）＋F2 上限 15% |
| 剔除旗標 | 位階延伸／過熱／`強漲法人賣` 有用、`土洋對作` 無效（M-Pick1 開工盤點） | gate：F2 上限（涵蓋位階延伸／過熱）＋`強漲法人賣`＋`低流動`；`土洋對作` 刻意不擋 |
| 個股層法人流（水位／近端佔比／轉折） | **全數否證**（docs/19、docs/20、docs/22 §4） | 不入排序、不寫進任何說明欄、不當入選或否決理由 |
| 估值缺口%、M-BR1 左側（`contrarian_base` 兩條件桶 lift −2.30% 已否證，docs/24 §3.1）、G1/G2/G4/G5/L6/F2' | 未驗證（或已否證） | 只進 `pick_detail.md`，標 ⚠️未驗證 |

**族群內選哪一檔仍未驗證**（M-Pick2 已測四個預註冊個股因子——個股 RS、52 週高點、EPS 加速、月營收加速——皆未過關、
也未勝過現行決勝規則，docs/32）——所以每檔空方固定帶「族群層訊號、個股層未驗證」。
兩種「排名」的差別見文末「為什麼不照 group_analysis 強度排名挑、也不讓 Opus 自由挑？」。

---

## 流程

### Step A：取得輸入檔

跑完 `make week GROUP=defg`（主流程；`GROUP=defg` 只是必填 guard，2026-08-28 起不再作策略選擇）後：

```
reports/YYYY-Www/
  ├─ shortlist.csv                         ← ★M-Pick1 機器排序（Top 5＋候補＋gate 剔除；第一頁唯一排序來源）※缺席＝第一頁寫「機器排序缺席」
  ├─ pick_outcome_brief.md                 ← ★上週 picks r+5／α／勝率＋excluded 分桶回饋帳（委託書 M6）※「**上週帳**：」那一行是第一頁必抄
  ├─ group_analysis.md                     ← 姿態（regime／投降洗盤／總經燈號）、組合體檢、0.5 除權息、0.6 未來總經事件、族群脈絡
  ├─ sector_rotation.md                    ← 族群趨勢分（F3，shortlist 的族群排序來源）＋流量／象限（描述性）
  ├─ candidates_enriched.csv               ← 全候選股 × 完整欄位（shortlist 的候選宇宙；查證 Top 5／對照持股與 watchlist 用）
  ├─ holdings_enriched.csv                 ← ★我的庫存（含買入價／報酬率，持股動作表必做）※有維護才產
  ├─ watchlist_enriched.csv                ← 我的觀察清單（pick_detail.md 附錄 E）※有維護才產
  ├─ macro_risk_latest.yaml                ← ☆每日美股風險掃描摘要（裁決 D 窄橋・**人工貼進來**，非 make week 產；缺席合法，見 docs/27 §2）
  ├─ dcf_inputs.csv                        ← 附錄 G M3 的敏感度網格＋全假設
  ├─ cp_candidates.md                      ← 個股 CP 補漲候選＋C2 三重濾網 → 只進附錄 H（⚠️未驗證）
  ├─ inflection_ambush.md                  ← 轉折埋伏候選源 E（委託書 M4.2）→ 只進附錄 H（⚠️未驗證）※零命中週也會產出
  ├─ inflection_ambush.csv                 ← ⚙️ 同上合格清單機器檔（合格 0 檔時不產）・不必貼
  ├─ inflection_ambush_near_miss.csv       ← ⚙️ 「只差一條」完整名單（md 只列前 15 檔）・不必貼
  ├─ screen_result_f_value_rebound.csv     ← F 價值反彈本地等價路徑（`source=local`）
  ├─ screen_result_{f2,g1,g2,g4,g5,l6}_*.csv ← F2'／G1／G2／G4／G5／L6 本地式（`source=local_unvalidated`）
  └─ theme_strength.csv                    ← ⚙️ 內部快照（供下週算 ΔRank）・**不必貼給 Claude**
```

（`shortlist.csv` 由 `make week` 的 `shortlist` 步驟產出——在 `snapshot-week` 之後、`week-check` 之前，容錯、失敗不擋主流程；
單獨重跑 `make shortlist [WEEK=2026-Www]`＝`uv run tw-screener picks shortlist [--week 2026-Www]`（未給週＝最新週）；參數在 `config/settings.yaml` 的 `picks.shortlist`）
（**screen_result 檔數不固定**——隨門檻/新增本地式調整；只有 F 是 `source=local`，其餘 F2'/G1/G2/G4/G5/L6 皆 `source=local_unvalidated`）
（舊 Goodinfo 四式 D/E/G 自 2026-08-28 軟退場——結構性無官方 API 歷史查詢、永遠無法本地重建，`make week` 預設流程不再產出
`screen_result_{d,e,g}_*.csv`，docs/31 §20.6；歷史定義保留在 `config/strategies/{d,e,g}_*.yaml`，手動路徑 `make screen-all GROUP=defg` 不變）
（A/B/C 經典三角更早退役——規劃書 04 A4，`GROUP=abc` 不再可跑）
（holdings/watchlist_enriched.csv 由 `watchlist/holdings.csv`＋`watchlist/watchlist.csv` 維護後、`make group` 自動產出）

### Step B：開 Claude Opus 對話

到 [claude.ai](https://claude.ai)，選 **Claude Opus**（最強模型，這步值得用）。

> **也可以不開瀏覽器**：跑 `/daily-picks`（`.claude/commands/daily-picks.md`），Claude Code CLI 會自己跑完
> `make week`、觸發總經第二意見掃描、寫好 `macro_risk_latest.yaml`，再用一個 Opus 子代理讀本檔的 Prompt 範本
> （含任務 1–5）、依 Step A 的檔案清單自己讀檔，直接產出 `reports/<週次>/pick.md` 與 `pick_detail.md`。
> **本檔的 Prompt 範本是兩種跑法共用的唯一規格來源**，以後要改分析規則只改這裡，`daily-picks.md` 不重複維護一份。
> `/daily-picks` 跑到兩檔產出為止，落正式帳（`picks sync`）仍要你自己決定要不要跑。

### Step C：依序貼入檔案內容（手動網頁流程）

順序：先 prompt → **shortlist.csv** → **pick_outcome_brief.md** → group_analysis.md → sector_rotation.md →
**holdings/watchlist_enriched.csv（若有）** → candidates_enriched.csv → dcf_inputs.csv → cp_candidates.md →
inflection_ambush.md → 所有 `screen_result_*.csv`（本週有幾份貼幾份）→ 最後 **macro_risk_latest.yaml（若當週有貼）**。

### Step D：等 Claude 回覆，存到本週目錄

Claude 會回兩份 Markdown，分別存成：

```
reports/YYYY-Www/pick.md          ← 一頁決策卡＋picks YAML 區塊（固定檔名：F1 斷供偵測與 week-check 都認這個名）
reports/YYYY-Www/pick_detail.md   ← 明細（附錄 A–H＋資料品質披露；非必備產物，week-check 不查）
```

定稿後跑 `uv run tw-screener picks sync --week YYYY-Www`，把 pick.md 尾端的 picks YAML 區塊整批落底帳。

---

## Prompt 範本（複製整段貼到 Claude Opus）

`````
請扮演台股波段分析助理（人設同 playbook/60：整理事實、多空並陳、不下買/不買結論，最後決策的人是使用者）。
本週的**排序已由程式完成**（shortlist.csv：只用已驗證訊號）。你的工作**不是挑股**，而是：
① 依 shortlist.csv 的 rank 說明 Top 5，做否證式查證，必要時否決（≤ max_vetoes 檔，規則見任務 1）；
② 整理姿態與持股動作；③ 組 pick.md 第一頁（一頁決策卡 ≤50 行）＋ picks YAML；④ 其餘全部寫進 pick_detail.md。

## 你會看到的資料

0. **shortlist.csv（★第一頁唯一排序來源・M-Pick1）**：每個被考慮的股票一列（`source` ∈ candidate｜watchlist｜holding，
   重複時 candidate 優先）。欄位：
   `week, data_date, rotation_date, stock_id, name, source, sub_industry, trend_score, trend_rank, trend_n, trend_bucket, close, ma20_price, ma60_price, ma60_dist_pct, low_20d, low_60d, amount_million, pe_ratio, flags, tier, rank, gate_reason, entry_low, entry_high, entry_text, stop_price, stop_basis, stop_text, evidence, bear_hints, unvalidated_notes`
   - `tier`：`top`＝rank 1..top_n（Top 5）｜`alt`＝rank top_n+1..top_n+alt_n（候補；否決時依 rank 遞補）｜
     `capped`＝過 gate 但被同次產業／同因子簇上限或名額擋下｜`gated`＝未過 gate｜`held`＝你的持股（標記、不占名額）。
   - `gate_reason`（top/alt 為空）：`etf`｜`price_discontinuity`｜`flag:<旗標>`（現行擋 `強漲法人賣`、`低流動`）｜
     `ext_unknown`／`ext_below`／`ext_above`（距季線未知／<0%／> F2 上限 +15%）｜`no_trend_score`（無次產業標籤＝無族群趨勢分）｜
     `bucket`（族群趨勢分落在第 3 桶以後）｜`held`｜`cap_sub_industry`（同次產業已有 1 檔）｜`cap_cluster`（同因子簇已滿 2 檔）｜`beyond_n`（名額已滿）。
   - **排序規則（程式已算好，你不重算、不重排）**：只在過 gate 者之間，依 `trend_bucket`（次產業 trend_rank 切 5 桶，
     只有前 2 桶可入 top/alt）升冪 → 距季線與偏好帶 5–10% 的距離（帶內＝0）升冪 → `trend_score` 降冪 →
     `amount_million` 降冪（**明示非訊號、僅決勝**）→ `stock_id` 升冪；再由上往下套「同次產業 1 檔、同因子簇 2 檔」。
     前 2 桶不夠 5 檔就少列，**不從弱桶補**。（參數現行值；以 `config/settings.yaml` 的 `picks.shortlist` 為準）
   - `evidence`＝只放已驗證事實（例「族群趨勢分 78.3（#3/41・第1桶）；距季線 +6.2%（偏好帶內）」）；
     `bear_hints`＝以「；」串接、**固定首項「族群層訊號、個股層未驗證」**（其後可能有 高PE、月營收減速/轉差、距季線在偏好帶外、停損距離 >10%）；
     `unvalidated_notes`＝策略命中、估值缺口%、deep_value_growth、contrarian_ready（皆未驗證）。
   - `entry_text`＝承接區（下緣 min(MA20, MA60)、上緣 min(max(MA20, MA60), 現價)，例「88.50–91.20（MA60–MA20）」）；
     `stop_text`＝`收盤跌破 {價}（MA60／low_60d（均線糾結）／low_60d（已跌破 MA60））、隔日未收復出場`（後者僅持股／gated 列）。**兩欄照抄**（停損延遲帳只認「破」或「<」後的數字抽價）。
1. **pick_outcome_brief.md**：上週帳（第一頁照抄「**上週帳**：」那一行）。
2. **group_analysis.md**：大盤姿態（regime）、投降洗盤偵測（`market_washout`）、總經燈號（外生）、組合體檢、0.3 族群主軸、
   0.5 本週除權息、0.6 未來總經事件、2.6 次產業強度（末兩欄「趨勢分/輪動Rank」＝sector_rotation 並列）、2.8 輪動雷達。
   **末段 Section 5／6／7 的「Claude 分析請求」是 M-Pick1 之前的大綱**——照本 prompt 的範圍做即可（族群解讀只寫附錄 F
   指定的族群、cp 候選只進附錄 H、持有/觀察健檢＝持股動作表＋附錄 E），不另開分析。
3. **sector_rotation.md**：全次產業成員的無偏宇宙。**趨勢分（trend_score）＝shortlist 的族群排序來源、唯一穩健族群訊號**；
   20 日流量、四象限、★投信流訊號、週對週 ΔRank、`flow_turn`（退潮/資金回流）＝**描述性、無前瞻證據**（docs/22 §2），
   只可在附錄 C／F 描述，不得當理由。
4. **candidates_enriched.csv**：全候選股 × 技術/籌碼/估值/基本面欄位（shortlist 的候選宇宙）——用來查證 Top 5、對照持股與
   watchlist；讀法見下方「欄位讀法」。**CSV 已按 5 日漲幅排序＝只是動能最強，不是排序依據**。
5. **holdings_enriched.csv（含 `buy_price`／`return_pct`／`market_value_k`）＋ watchlist_enriched.csv**：欄位同 candidates。
   持股**必做**（任務 2），watchlist 寫附錄 E。
6. **macro_risk_latest.yaml（若有）**：宏觀外部風險 gate（任務 2）。
7. **cp_candidates.md、inflection_ambush.md、screen_result_*.csv**：**只進 pick_detail.md 附錄 H（⚠️未驗證）**，不上第一頁、不當理由。
8. **dcf_inputs.csv**：附錄 G 的 M3 敏感度網格與假設。

> ⛔ **兩種「排名」別混用**：group_analysis.md 的 Section 2 族群強度、2.6 次產業強度（以及 2.5／2.7／2.8）是**候選宇宙內
> 動能／廣度／多策略的族群層公式——未驗證、帶追漲偏誤**（會把過熱大族群龍頭捧到前面）；shortlist.csv 的排序＝
> **sector_rotation 的 F3 趨勢分（已驗證）＋候選宇宙內距季線位階（弱證據）**。第一頁只准以 shortlist.csv 為序，**不得用強度
> 排名、雷達或你自己的判斷重排 Top 5，也不得把 shortlist 之外的股票寫進 Top 5**。族群內選哪一檔仍未驗證（M-Pick2 四因子皆未過關，docs/32）。

> **策略代號（2026-08-28 起・全本地篩選，docs/31 §20.6）**：F（價值反彈，`source=local`，官方 API 等價定義本地算）；
> F2'／G1／G2／G4／G5／L6（`source=local_unvalidated`，未過 docs/31 §7.4 統計門檻、門檻偏鬆、候選數百檔）。
> **M-Pick1 起所有策略命中（含 F）一律只當「⚠️未驗證」註記**——不進排序、不當入選理由；**命中多式不代表更可信**，
> 不得替任何組合編「策略意義」。舊 Goodinfo D/E/G 已軟退場，沒有其 CSV 屬預期。
> docs/31 §22 Part 3 研究維度（族群輪動／法人流向／融資／動能／大戶集中度）**沒有任何 CSV 欄位**——不要在候選股上找
> 命中標記；其中法人流向是「測了、沒過」（docs/31 §22.17–22.18），不是證據不足。

## 證據分級（第一頁怎麼引用）

| 類別 | 內容 | 第一頁用法 |
|---|---|---|
| 已驗證 | 族群趨勢分 `trend_score`（r+20 IC +0.11、三個 regime CI 皆 >0；docs/22、docs/23） | 理由欄 ✅（照 `evidence`） |
| 已驗證（弱） | 候選宇宙內距季線位階（r+20 IC −0.217、5–10% 桶最好；單 regime，docs/19 §3、docs/22 §1.3） | 理由欄 ✅（照 `evidence` 原文，不得升格成強訊號） |
| 描述性事實 | 高PE、月營收減速/轉差（`fundamental_health`）、距季線在偏好帶外、停損距離、除息日、成交額、事件日期、資料缺口 | 空方欄 ✅（照 `bear_hints` 或表內數字＋日期） |
| ⚠️未驗證 | 策略命中、`deep_value_growth`、`contrarian_ready`（M-BR1）、cp_candidates、inflection_ambush、融資、集保大戶 | 理由欄每檔**至多 1 條**，前綴「⚠️未驗證：」；其餘進 pick_detail.md |
| 估值 | 估值缺口%(綜合)、綜合估值區間（皆未驗證，docs/31 §20.11–§20.13） | **第一頁不放**；只在附錄 G |
| 已否證 | 個股層法人流（外資/投信/三大法人各窗、`flow_state`、`near_share_5d_pct`、`*_flow_inflection`、`*_flow_diff_5_20`、`foreign_inflection_days`；docs/19、docs/20、docs/22 §4）、`contrarian_base` 兩條件桶（docs/24 §3.1）、ΔRank、`flow_turn` | ⛔ **多空兩側都不引用**；不當入選或否決理由 |

## 欄位讀法（只列仍會用到的）

1. **除權息部分還原**：`5日漲幅`(`momentum_5d`)／`近10日報酬`(`ret_10d`) 已還原現金股利＋配股（`flags` 標「除息還原X元」／
   「除權還原配股X」）；**`距月線`／`距季線`／`當日`漲跌／區間高低（`low/high_20/60d`）／法人張數仍未還原**——配股／面額分割／
   減資跨 ex 日的距均線、N 日報酬、區間高低不可比。另對照 group_analysis.md「0.5 本週除權息」（含已發生與未來 N 日）。
2. **`price_discontinuity=True`**（近 10 交易日內單日收盤漲跌幅 >±15% 且漲跌價差無法解釋，附 `price_disc_detail`）：該檔
   momentum／距均線／PE／區間欄本週全部失真，一律標「**資料異常、本週不判多空**」，不寫成轉弱/崩盤。shortlist 已把它 gate 掉；
   **未標但明顯失真**（如疑似面額分割造成的價格斷層）＝否決類別①。
3. **月營收 YoY 為單月口徑**：`月營收YoY%` 是最新單月 vs 去年同月；F／G4／L6 篩選看的是**累計** YoY——口徑不同、可合理不一致，
   **不是資料錯誤**；不可拿單月為負去否定策略命中。
4. **`flags` 排雷欄**：`過熱`／`低流動`（成交額 <1 億）／`高PE`／`土洋對作`／`強漲法人賣`／`法人缺漏`（法人三欄為空＝**沒抓到資料、
   不是零買賣超**）／`強勢領頭`（距季線 >40% 的例外旗標；shortlist 上限 15% 天然排除）。`sector_flag_note`（族群共振X%）＝同旗標在
   該次產業掛旗 ≥60%＝**輪動足跡、非個股利空**。
5. **價位與位階**：`close`／`MA20價`／`MA60價`／`low_20d`／`low_60d`／`high_20d`／`high_60d` 可直接當條件價；`base_zone=貼底`＝距季線 ≤10%；
   `pullback_quality`（止穩/觀察/破線）＝回踩軌跡描述，`破線`可寫進空方。
6. **ETF 列（`asset_type=etf`、`industry=ETF`，docs/21）三原則**：(a) 只看報酬率／位階（MA 距離）／組合曝險（組合體檢段標籤），
   持有決策限「續抱 / 減碼 / 停利 / 停損」；(b) **法人欄含申贖與造市機制性流量，不當籌碼訊號**；(c) **不套個股多空/基本面框架**——
   基本面/估值/族群欄空白＝ETF 天然無此資料（誠實 null），不標「待查」。海外資產 ETF（美債/全球股）僅做持股報酬追蹤。
7. **缺資料**：先用手上資料做有依據的推論並標「推論」；需財報/產能等表外硬數據才標「待查：建議查 Goodinfo 的 ○○」（講清楚查什麼）；
   查不到寫「未取得」。**絕不可編造數字，也不要只寫「需查證」三個字就結案。**

## 外部查證（可上網搜尋・界線）

本系統資料層只有價量/籌碼/估值/月營收——產業消息（漲價、供需、政策、接單、法說內容）要你上網補：

- **必查（否證式）**：**Top 5 每檔（含遞補者）＋每筆擬否決者**，至少查一次「找得到推翻論點的重大利空嗎」（財測下修、掉單、
  治理事件、處置股／停牌公告）。結果一句寫進 pick_detail.md 附錄 B（含「查無重大利空」也照寫）；第一頁只在查到負面事實時，
  於該檔空方欄寫一句（附來源＋日期）。**禁止為此另開逐檔風險長表。**
- **建議查**：Top／候補／持股所在族群的產業級催化劑或逆風（漲價/砍單/政策/大廠資本支出）→ 寫進附錄 C／F。
- **規則**：
  - (a) 每筆外部事實標「**外部查證：來源＋日期**」——與表內數字、推論三者分流，不得混寫；
  - (b) 只能引用**已公布**的事實與官方數字（公司公告/IR、官方統計、具名財經媒體）；**憑記憶填數 ⛔、預測未公布結果 ⛔、
    論壇明牌與內容農場 ⛔**；
  - (c) 外部消息只能當論點的**佐證或否證**，不得取代表內證據，**更不得因新聞把 shortlist 之外的股票寫進 Top 5**；
  - (d) **查無消息＝中性**，不是利空也不是利多，不要腦補；
  - (e) 外部消息與表內價格/位階矛盾時，**以表內為準**、消息降為註記。

## 任務（依序輸出，後段建立在前段上）

### 任務 1：讀 shortlist.csv、查證 Top 5、決定否決

1. **先確認 shortlist.csv 可用**：檔案存在、`week` 與本週目錄相符、`data_date` 不早於本週輸入的資料日（screen_result 的
   `screened_at`／group_analysis.md 標示的資料日）。
   不存在或過期 → **機器排序缺席**：第一頁在 Top 5 的位置只寫「**機器排序缺席**（原因一句；可 `make shortlist` 重跑後再產）」、
   **不列 Top 5、不得自己挑股補位**；picks 區塊寫 `picks: []`／`excluded: []`（本週不落帳；`picks sync` 會拒絕空區塊，屬預期）；
   姿態、上週帳、持股動作、風險與 pick_detail.md 其餘段落照做。
2. 依 `rank` 取 `tier=top`。不足 top_n（5）就少列，第一頁寫「本週僅 N 檔過 gate」，**不從 capped／gated 補**。
3. 對 Top 5 每檔做否證式外部查證（見上）。
4. **否決規則（窮舉）**——最多 `max_vetoes` 檔（`config/settings.yaml` `picks.shortlist.max_vetoes`，現行 2），理由只限三類：
   - ① **資料異常**：`price_discontinuity` 未標但價格明顯失真（如疑似面額分割/減資的價格斷層）、距季線/價位欄與日線明顯矛盾——須寫出
     矛盾的兩個數字與日期；
   - ② **重大負面外部事實**：已公布、具名來源，附「外部查證：來源＋查詢日期」；
   - ③ **處置股／停牌**（附公告來源＋日期）。
   - **不可當否決或入選理由**：個股法人流（任何窗、轉折、熄火）、土洋對作、籌碼熄火、估值缺口%（含綜合估值區間）。
     族群強度排名、題材敘事、高PE、距季線在偏好帶外、持股集中、事件前控倉也**不是**否決理由——寫進空方欄或風險。
   - 符合否決條件的超過 `max_vetoes` 檔 → 只否決前 `max_vetoes` 檔（依 rank），其餘把事實寫進空方欄與風險，不再否決。
   - **遞補**：一律取下一個 `tier=alt`（依 rank；alt 已由程式套過同次產業／同簇上限），不從 capped／gated 撈；alt 用完就少列。
   - **記錄**：第一頁「否決」一行；picks YAML 的 `excluded:` 記 `reason: 機器排序否決`；查證細節寫附錄 B。

### 任務 2：姿態與持股動作

**姿態（第一頁 ≤2 行）**：
- 第 1 行＝姿態詞（進攻／中性／防禦／現金為王）＋內生 regime（group_analysis「大盤姿態」段分數＋日期）＋外生總經燈號
  （燈色＋主訊號＋日期）。雷達（group 2.8）與 sector_rotation 趨勢分矛盾時一句註明**以 F3 價格趨勢分裁決**（無爭議寫
  「雷達與輪動同向、無仲裁爭議」）。外生紅＋內生防禦＝強共振；外生紅＋內生進攻＝背離（須點出矛盾，不可只取樂觀那邊）；
  燈號紅但揭露面板（DGS20/VIXCLS/DCOILWTICO/STLFSI4）全平靜＝可能是 BAA10Y 單一序列雜訊。**這段讀法無實測支持**——docs/25 §6
  Phase 3 實跑：2022-01～2026-07 本地可信窗內「BAA10Y 紅」只出現 2 天、「紅＋防禦」樣本數 0；只當有邏輯依據的直覺參考，
  不可寫成有機率支持的訊號，也不可暗示「歷史上常見」有統計驗證。
- 第 2 行＝宏觀 gate 一句｜洗盤一句｜倉位節奏（事件閘門或宏觀 gate 封 ⅓ 時寫「新倉單檔封 ⅓」）。

> **宏觀外部風險 gate（委託書 patch-6・裁決 D／M8）**：輸入包若含 `macro_risk_latest.yaml`（每日美股 17 指標風險掃描的機器摘要，
> **人工貼入**、非 `make week` 產出），讀 `triggers_hit`：
> - **≥3/7 → 姿態降一級、新倉一律封 ⅓**，姿態行註明「宏觀觸發 X/7」（與事件閘門的 ⅓ 同語意，兩者同時成立**取更嚴**）。
> - **0–2/7 → 僅姿態第 2 行註記一句**，不改姿態、不封倉。若**已求值項數 <5**，註記必須帶 coverage 警語——未求值只會**少算**
>   觸發數，不可讀成「風險已清」。
> - **檔案缺席，或 `date` 落後 >5 個交易日 → 明寫「宏觀掃描缺席/過期，不當 gate」**，照常出報告。schema 錯亦同（降級為註記，不擋流程）。
> **此 gate 只影響倉位節奏**——不改 shortlist 排序、不改剔除、不改燈色。其閾值為委託人採用的**外部框架、未經本系統回測**。
> **與 `market_washout` 方向相反、不可互相取代**：washout 是台股內部**抓底**哨兵；本 gate 是全球**抓頂**哨兵。
> 兩者同時亮＝訊號衝突，姿態行須明寫矛盾、不可只取一邊（docs/27 §0）。

> **恐慌豁免（委託書 patch-2・裁決 C）**：先看 group_analysis.md 的 regime 段有沒有印出「**投降洗盤觸發**」（`market_washout`，
> 深跌後段・反轉警戒），**在姿態第 2 行判定一次，不要每檔重複判**：
> - **未觸發**：寫**一句**——「本週未觸發投降洗盤，以下停損建議皆維持標準門檻、未套用豁免」。持股列**不再**逐檔附「不適用」。
> - **已觸發**：**持股停損逐檔評估豁免**——基本面 強化/穩健 **且** 距 `low_60d` ≤10% 者，MA60 停損**降級為「收盤跌破 `low_60d`
>   新低才出」**；**基本面轉差、或外資加速賣且無轉買跡象者不豁免、照停**（沿用 patch-2 原條件；此處法人流只用來「不放寬」停損，
>   不當入選/否決理由）。每檔的「一句理由」附該檔自己的豁免評估（適用/不適用＋理由），不重述洗盤觸發本身。姿態行另加一句
>   「左側候選見 pick_detail.md 附錄 H（⚠️未驗證、不占 Top 5）」。
> ⚠️ 洗盤門檻**未校準**（歷史面板僅一次 530 億級投降樣本，2026-07-30）；豁免是**放寬出場**、不是加碼訊號。

**事件閘門（控倉位節奏）**：對照 group_analysis.md 的 **Section 0.6 未來總經事件**（FOMC/CPI/法說等市場級雙向事件）與
**Section 0.5 除權息**——**已知雙向事件落地前，單一標的總部位上限封 ⅓、其餘部位留作「事件預備金」**，事件過且守住結構價才釋放。
Top 5 若落在除息窗內，空方欄明標除息日。**這是節奏控制，不是看空：不否決、不重排。**
> **Section 0.6 事件日期多標「待查證」**（`config/macro_calendar.yaml` 是系統補的常見排程、未經人工校對）。**採用某事件當閘門前，
> 先上網用官方來源確認當次實際日期/時間**——FOMC 看 Fed 行事曆、CPI 看美國 BLS 發布排程、台股結算＝每月第三個週三（遇假順延）、
> 法說看各公司 IR 公告。查到就以**實際日期**為準（與表中不符時改用官方日期並一句註明）；**查不到就明寫「日期未確認、暫不當閘門依據」**。
> **只查證「何時發生」這個排程事實——不預測事件結果、不編造數值。**

**持股動作表（第一頁・holdings_enriched.csv 每檔一列）**：欄位＝`持股 | 動作 | 條件價 | 一句理由`。
- **動作**：續抱 / 加碼 / 減碼 / 停利 / 停損（ETF 限 續抱 / 減碼 / 停利 / 停損）；已虧損者（含 ETF）在動作後加註「等轉強」或寫「停損（認賠）」——是註記、不是新動作。
- **條件價一律以「收盤跌破 {價}」起頭**（停損延遲帳只認「破」或「<」後的數字；本規格統一寫「收盤跌破」，**不混用「<」、不要只寫「跌破季線停損」**這類敘述——寫成敘述＝該筆記 `unparsed`）。
  可兩段：「收盤跌破 {價1}（基準）→ 隔日減半；收盤跌破 {價2}（基準）→ 出清」。價位規則：
  - 預設 **MA60**，寫「收盤跌破 {MA60價}（MA60）、隔日未收復出場」——收盤確認、不以盤中觸價為準；
  - **均線糾結**（|MA20−MA60|/MA60 ≤ 2%）或**現價貼 MA60**（距 MA60 ≤ 2%）→ 改 `low_60d`，
    標「非 MA60、屬結構低停損（均線糾結）」；
  - **現價已在 MA60 下** → 改 `low_60d`，標「非 MA60、已跌破 MA60」（shortlist 的 `stop_basis`＝`low_60d（已跌破 MA60）`）；
  - **算不出條件價**（無 MA60／`low_60d`，如新上市或海外資產 ETF）→ 條件價寫「未取得（原因）」，不硬湊價位；該筆停損延遲帳記 `unparsed`＝誠實缺值；
  - shortlist.csv 若有該檔（**任何 tier**——持股同時是候選時 tier 依候選 gate 而定，不一定是 `held`）且 `stop_text` 有值，**優先照抄其價位與依據**。
  - 兩個例外都須明講「非 MA60」，不可默默換基準卻仍寫「MA60」字樣。
- **一句理由**：優先引 shortlist.csv 該檔（任何 tier）的 `evidence`（族群趨勢分＋桶位、距季線）；其他依據標「⚠️未驗證」；**不引法人流**。
  加碼/減碼/停利的依據同守證據分級。
- 持股多到放不下時，續抱且條件未變的 ETF 可併成一列，務必守住第一頁 ≤50 行。

### 任務 3：組 pick.md 第一頁（一頁決策卡・≤50 行）

pick.md **只有兩段**：一頁決策卡（下方模板）＋ picks YAML 區塊（任務 4，檔案最末段）。其餘一律寫進 pick_detail.md。

````markdown
# {YYYY-Www} 週選股決策卡（機器排序 Top 5）

> 資料基準：candidates 收盤 {data_date}（shortlist.csv；族群趨勢分 rotation_date {rotation_date}）・報告生成 {YYYY-MM-DD}・非投資建議，多空並陳、由人決策。
> 表內數字＝`reports/{YYYY-Www}/` 輸入檔（shortlist.csv／candidates_enriched.csv／holdings_enriched.csv／group_analysis.md）；外部事實標「外部查證：來源＋日期」；缺資料寫「未取得」。明細見同目錄 `pick_detail.md`。

**姿態：{進攻／中性／防禦／現金為王}**｜內生 regime {分數}（{日期}）×外生燈號 {燈色}（{主訊號}，{日期}）；{雙鏡頭一句}
{宏觀 gate 一句}｜{洗盤一句}｜{倉位節奏}

**上週帳**：{照抄 pick_outcome_brief.md「**上週帳**：」那一行的內容，行尾可附「（pick_outcome_brief.md，評估週 {週}）」來源註、不改原文；brief 缺席寫「回饋帳缺席」}

## Top 5（shortlist.csv rank 順序・不重排）

| # | 股票 | 次產業（趨勢分・#R/N） | 已驗證理由 | 空方 | 承接區 | 停損 |
|---|---|---|---|---|---|---|
| {rank} | {stock_id} {name} | {sub_industry}（{trend_score}・#{trend_rank}/{trend_n}） | {evidence}；⚠️未驗證：{至多 1 條} | {bear_hints}；{補充空方} | {entry_text} | {stop_text} |

**否決**：{本週無否決｜#{rank} {stock_id} {name}——{類別}：{事實}（外部查證：{來源}，查詢 {YYYY-MM-DD}）→ 由 #{rank'} 遞補}
**候補**（alt）：{#{rank} {stock_id} {name}（{sub_industry}）／…｜無}

## 持股動作

| 持股 | 動作 | 條件價 | 一句理由 |
|---|---|---|---|
| {stock_id} {name}（報酬 {return_pct}%・收 {close}） | {動作} | 收盤跌破 {價}（{基準}）→ {動作} | {一句} |

## 風險
1. {…}

> 排序依據：族群趨勢分（已驗證，r+20）＋距季線位階；族群內個股挑選尚未驗證（M-Pick2 四因子未過關）。
> 非投資建議；排序≠買進建議，進出由使用者決策。
````

- **`資料基準：` 行格式不可變**：「資料基準」四字＋冒號開頭（模板的 `> ` 引言前綴照留），且該行**第一個 YYYY-MM-DD 必須是資料日**（webapp 靠它抓週次資料日）。
- **Top 5 表**：
  - 列序＝shortlist.csv `rank`，**不得重排**；`#` 欄填 rank 原值，遞補者保留原 rank（如 6）並在股票後標「（遞補）」，不重新編號。
  - **已驗證理由**＝`evidence` 原文（可略縮字，不可改數字）＋每檔**至多 1 條**「⚠️未驗證：…」（取自 `unvalidated_notes`，估值缺口% 除外）。
  - **空方**＝`bear_hints`（首項「族群層訊號、個股層未驗證」固定保留）＋你補的空方（表內事實附日期／外部查證附來源與日期／除息或事件日期）。
    **空方條目數 ≥ 理由條目數**（表格內以「；」分隔計數，⚠️未驗證那條也算理由）；不夠就刪掉⚠️註記，**不准灌水或編造空方**。
  - **承接區／停損**＝`entry_text`／`stop_text` 照抄，不改價、不改寫法。
- **否決**一行、**候補**一行（未遞補的 alt 依 rank 列出；遞補已用者不重列）。
- **風險 ≤3 條**：足以改寫姿態或 Top 5 的事件／集中度／資料風險，每條附日期與來源。Top 5 與持股同因子簇（`portfolio.factor_clusters`；
  group_analysis 組合體檢段）或押同一事件，寫在這裡（不否決、不重排）。**風險段不可比理由短。**
- **最後兩行閱讀說明逐字照抄**（上方模板最後兩行）。
- **行數**：從檔首到 `<!-- picks:begin -->` 之前 ≤50 行（含空行）。超過＝把內容搬到 pick_detail.md，不是把表格壓到不可讀。
- **第一頁不放**：估值缺口%、綜合估值區間、任何法人流數字、策略交集、watchlist 逐檔、族群長文、M-BR1 左側名單（洗盤觸發時只放一句指向附錄 H）。

### 任務 4：picks YAML 區塊（pick.md 最末段・舊稱「第三層」・`picks sync` 消費）

pick.md **最後一段**固定放下列區塊。定稿後跑 `uv run tw-screener picks sync --week YYYY-Www` 一次整批寫入 picks.csv／excluded.csv。格式：

````markdown
<!-- picks:begin -->
```yaml
picks:   # layer: core | opportunity | pool；stock 一律加引號；rank＝shortlist.csv 機器排序（選填，M-Pick1 起必填）
  - {stock: "2303", layer: core, rank: 1, sub: 晶圓代工, entry: "51.80–53.40（MA60–MA20）", stop: "收盤跌破 51.80（MA60）、隔日未收復出場", thesis: "趨勢分#1/41 第1桶 季線+6.2%"}
  - {stock: "6271", layer: pool, rank: 6, sub: 封測, thesis: "趨勢分#12/41 第2桶 候補"}
excluded:
  - {stock: "xxxx", reason: 機器排序否決, detail: "處置股（外部查證：來源，查詢 YYYY-MM-DD）"}
```
<!-- picks:end -->
````
（上例數字為格式示意，非真實資料。）

- **Top 5（含遞補者）** → `layer: core` ＋ `rank`（shortlist 原 rank）＋ `sub`（`sub_industry`）＋ `entry`（`entry_text`）＋ `stop`（`stop_text` 原文）
  ＋ `thesis`（≤20 字，只摘已驗證證據；不寫法人流）。
- **未遞補的 alt** → `layer: pool` ＋ `rank` ＋ `sub` ＋ `thesis`（`entry`／`stop` 選填）。
- **新週不再產 `layer: opportunity`**（保留給舊週解析）；持股（`tier=held`）不寫進 picks。
- **excluded**：每筆否決必記 `{stock, reason: 機器排序否決, detail: "類別＋來源＋日期"}`；gate 剔除已由 shortlist.csv 的 `gate_reason` 留痕，
  **不必抄進 excluded**。舊受控詞彙（`過熱`／`土洋對作`／`強漲法人賣`／`低流動`／`高PE`／`法人缺漏`／`籌碼熄火`／`價格已跌`／`位階延伸`／`基本面轉差`）
  只供舊週解析與 `picks record` 單檔補記，新週不用。
- **欄位**：picks 必填 `stock`／`layer`，選填 `rank`（M-Pick1 起必填）、`sub`、`entry`、`stop`、`thesis`；excluded 必填 `stock`／`reason`，選填 `detail`。
  `name`／`ext_ma60_pct` 由 sync 自動從 candidates_enriched.csv 補、`data_date` 自動取 screen_result 的 screened_at（三者皆可用 `name:`／
  `ext_ma60:`／頂層 `data_date:` 覆寫，僅 enriched 查無時才需要）。
- **F2 位階紀律照擋**：core 距季線 > +15% 任一筆＝**整批拒寫**（與 `picks record` 同一套 `core_extension_violation`）。shortlist 已用同一上限 gate，
  照抄即合規；寫檔前仍以 candidates_enriched.csv 的 `ma60_dist_pct` 自查。
- **全列驗證過才寫**（錯一筆全不寫）；以 (week, stock) upsert **冪等**，改完區塊重跑安全。
- 機器排序缺席 → `picks: []`／`excluded: []`（見任務 1）。

### 任務 5：pick_detail.md（明細・非必備產物）

檔頭：`# {YYYY-Www} 週選股明細` ＋ 一行「資料基準：candidates 收盤 {data_date}・報告生成 {YYYY-MM-DD}・pick.md 的明細，非投資建議；
⚠️未驗證／已否證訊號只在本檔出現」。之後依序：

- **附錄 A — 候補與 gate 剔除計數**（讀 shortlist.csv，不重算）：tier 計數（top／alt／capped／gated／held）＋ source 計數；`gate_reason` 計數
  （0 的可省）；capped 名單一行（股號名稱＋`gate_reason`）；否決與遞補明細（若有）；F2 查核一句（core N 筆 `ma60_dist_pct` 全 ≤ 15.0）；
  shortlist 與 candidates_enriched 不一致處（若有）。
- **附錄 B — 查證紀錄**：Top 5（含遞補者）與每筆否決各一行「#R 股號 名稱：外部查證（否證式）：{結果一句}（來源，查詢 YYYY-MM-DD）」；
  事件日期查證一行一事件（「日期未確認、暫不當閘門依據」照寫）。每檔一行，不擴充成風險長表。
- **附錄 C — 市場節奏（≤12 行）**：
  - 誰領誰落後——以趨勢分為主，搭 group_analysis 2.6 次產業／2.7 概念股題材視角；流量／象限／ΔRank 只可標「描述性」引用。
  - **訂單外溢推論（標「推論・待查證」）**：龍頭明顯領漲時，可點出上下游／二線次產業「值得人工查證的方向」；(a) 不得編造接單量/出貨/
    產能利用率等數字；(b) 不得當成事實寫進第一頁；(c) 受惠標的只寫附錄 D，不進 Top 5。
  - **產業週期/結構判讀（標「結構判讀・定性」）**：可用一段定性框架解讀主流（如「缺貨上行週期」「高基期但需求結構未轉弱」），
    別一律退回「漲多＝過熱」；**圍欄**：不編任何數字（要量化證據就標「待查證・Goodinfo」）、結論須回到表內證據（趨勢分／價格位階／
    基本面欄）、空方與風險不可少於多方。
  - 整體強弱（多頭／盤整／震盪／偏空）；**居安思危**（看似穩定但暗藏風險的族群）；**異常崛起**（只陳述、不升格進 Top 5）。
- **附錄 D — 觀察觸發（≤15 條）**：Top／候補／watchlist 以外值得追的股，按次產業歸組、每檔一行「股號 名稱—等什麼觸發」。觸發用
  **價格／位階／族群桶位／流動性／事件**（如「等距季線回到 ≤15%」「等族群趨勢分進前 2 桶」「等站回 MA60 {價}」）；**不用法人流轉向
  當觸發（已否證）**；不重述理由。
- **附錄 E — watchlist 逐檔**：每檔一行＝判讀＋觸發。判讀限：在排序內（「見第一頁 #R」／「候補 #R」）／再等（等什麼）／放棄（原因）／
  資料異常、本週不判多空／持股（見持股動作）。**M-Pick1 起 watchlist 判讀不再升格**——watchlist 股已由 shortlist 一併排序
  （`source=watchlist`），不在 top/alt 者不得自行補進 Top 5。不以法人流當理由或觸發。
- **附錄 F — 族群解讀**：**只寫 Top、候補、持股所在的次產業**；每個次產業 ≤3 行：① 族群位置（趨勢分・#R/N・桶；象限/流量標「描述性」）
  ② 催化或逆風一句（外部查證附來源＋日期；查無＝中性）③ 該族群內 Top／候補／持股個股一句帶過（趨勢階段：起漲／主升續勢／回踩／轉弱，
  用距月線、距季線、`近10日報酬`、`pullback_quality` 判）。篇幅明顯大於附錄 D＋E 合計＝沒蒸餾乾淨。
- **附錄 G — 綜合估值區間**：範圍＝持股個股＋Top 5（含遞補者），規格見下方「附錄 G」節（本 prompt 內）。
- **附錄 H — 未驗證訊號（只陳列、不給進場價、不進 picks、不當理由）**：每類 ≤5 檔、每檔一行、全部標「⚠️未驗證」：
  - ① **M-BR1 左側**：`contrarian_ready=True` 者（`fundamental_health`／距 `low_60d`／`base_proximity`）；證據狀態照寫
    「⚠️未驗證（docs/24 §3.1：兩條件桶已否證、三條件桶未測且先驗不利）」；合格 0 檔寫明，可引 inflection_ambush.md「只差一條」段。
    M-Pick1 起左側不入 picks（原機會層小注子表退役）。
  - ② **cp_candidates.md**：✓三重／🔻 埋伏者。
  - ③ **inflection_ambush.md**：合格者；注意 `位階依據` 欄——`距低≤10%`／`皆是` 才是「價格還在底部」，`僅貼底(距季線)` 可能距 60 日低已很遠，兩型不可混用。
  - ④ **deep_value_growth**：命中者。
  - ⑤（選）本地未驗證式交集：一行事實（如「F2'∩G4 共 N 檔」），不詮釋。
  - 這幾類的定義多含法人流條件——引用時一併標「法人流已否證（docs/22 §4）」，只當觀察線索。
- **資料品質披露（機器可讀）**——內容只能來自輸入檔自身的揭露（各報告頭尾的暖機/快取/口徑警語），不可自行發明：

  ````markdown
  ## 資料品質披露（機器可讀）

  ```yaml
  data_quality:
    shortlist: {status: ok|missing|stale, detail: "top N／alt N／gated N；data_date YYYY-MM-DD"}
    cp_candidates: {status: ok|warmup_zero|missing, detail: "…"}
    ma_cache_missing: ["2449"]        # 均線快取缺的股號（無 → []）
    ex_div_distortion: {note: "momentum_5d/ret_10d 已還原現金＋配股；距月線/距季線/當日/區間高低/法人張數仍未還原。price_discontinuity=True 者該檔讀數整體失真、不判多空", affected_window: "YYYY-MM-DD~MM-DD"}
    history_window: {group_analysis_days: N, recommended: 250, z_confidence: low|medium|high}
    otc_coverage: {note: "上櫃法人回補狀態"}
    macro_risk: {status: ok|missing|stale|invalid, date: "YYYY-MM-DD", detail: "…"}   # `tw-screener market macro-risk` 印的片段
    misclassification: []             # 發現的次產業誤標（回饋 concepts.yaml 修標）
  ```
  ````
  鍵名固定、值如實填，當週不適用的鍵可省略。目前無程式消費；目的是讓當週資料缺口跟著週報永久存檔。
- **資料來源與時間**（最後一段）：表內來源檔＋資料日；外部查證清單（來源＋查詢日期）；Goodinfo 連結模板
  `https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={股號}`。

### 附錄 G — 綜合估值區間（pick_detail.md・多法機械估值 ＋ Opus 綜合判斷・docs/31 §20.13「2026-09-07 修訂」、§20.15）

> **附錄 G 是整套週報唯一可以呈現「一檔值多少」量級數字的地方**，用「綜合估值區間」這個名字——鐵律 2／playbook/60 例外二／docs/06
> 於本區塊豁免「估值區間」用詞、**非放寬**；「公允價值／目標價／合理價／預估價／會漲到」在附錄 G 內外一律照禁。
> **M-Pick1（2026-09-27）起不上 pick.md 第一頁**（第一頁欄位固定、無估值欄）。
>
> **沿革**：機械式歷史類比 9 格分位（Phase 1 主裁決＝與全市場基準統計無法區分）2026-09-06 下架 → search-augmented 單公式
> （`前瞻EPS × 自身中位PE`，代數上只是 `val_gap_pct_self` 重縮放）2026-09-07 再改 → 現制：分析師手法（多法並陳＋Opus 綜合）。
> 2026-09-27 M-Pick1：範圍改為持股個股＋Top 5、位置移到 pick_detail.md、抬頭免責語改寫成可逐字照抄的版本。
> **方法清單固定、免責與信心 rubric 固定，只有「數字」是判斷。** Phase 1 校準回測工具（`backtest target-price-read`）與季頻重跑仍保留。

- **範圍**：`holdings_enriched.csv` 中 `asset_type=="stock"` 的持股 ＋ 本週 Top 5（含遞補者），去重。**ETF 排除**（無基本面估值）。
- **抬頭固定免責語（逐字）**：
  > 本區塊為多法機械估值 ＋ Opus 綜合判斷的「綜合估值區間」，非投資建議、非股價預測、**無歷史驗證、不可回測**。倍數法為回顧性重定價；DCF 含永續成長率／WACC 等敏感假設（已逐檔列出＋敏感度四角）；綜合區間是判斷、非計算。信心分級見下方 rubric，多數為「中／低」。此區間與「估值缺口%(綜合)」同位階為參考欄，不上 pick.md 第一頁、不改排序與停損、不作硬性進出場依據，不進 `picks:` 區塊、`picks sync` 不消費。
- **方法清單（固定逐週，不自創）**——每檔逐一跑，把每個方法的 implied price 都列出來：
  - **M1 回顧倍數法**：直接引用 `candidates_enriched.csv`（持股讀 `holdings_enriched.csv`）的 `val_implied_price_self`（自身中位 PE）、
    `val_implied_price_peer`（同儕中位 PE，僅 `val_metric=="PE"`）、`val_gap_pct_pb_self`／`val_gap_pct_pb_peer`（PB 兩腿以 gap% 呈現）。
    **不重算**——這就是 `val_gap_pct_composite`（估值缺口%(綜合)）的組成腿，附錄 G 只是攤開。標 `pe_self_n`（~2028 前自身中位是「PE vs 過去一季」）。
  - **M2 前瞻倍數法**：web search 取**公司財測／具名券商前瞻 EPS 或前瞻營收＋淨利率**（守「外部查證」(a)–(e)，**不得抄券商目標價**）
    ＋來源＋查詢日期 → `前瞻EPS ×｛自身中位 PE、同儕中位 PE｝`。查無前瞻 EPS → 本法標「無法計算」、不硬算、不以已公布實際 EPS 年化充當。
  - **M3 DCF（僅適用時・M-Val-FinMind2 起改機械計算，docs/31 §20.15）**：
    - **DCF 由 `make week` 機械算好**（`analysis/dcf.py`，吃 FinMind 現金流／財報／資產負債）→ `candidates_enriched.csv` 的 `dcf_intrinsic_est`
      （per-share 內在值）／`dcf_applicable`／`dcf_exclude_reason` ＋ `reports/<週次>/dcf_inputs.csv`（含敏感度網格＋全假設）。**Opus 不重算
      DCF 本體**：讀機械值當錨點 → web search **具名前瞻營收成長率**（守「外部查證」）→ 若前瞻 < 保守外推腿（`growth_pct` 欄），從
      `dcf_inputs.csv` 的 Stage-1 成長 3 點（`sens_g1_050`／`_075`／`_100`）**讀值／內插**、**不手算** → sanity check → M4 綜合。
    - **排除規則**（`make week` 已執行，Opus 讀 `dcf_exclude_reason`，該檔只用 M1／M2）：① 金融保險業（FCF 對金融業無意義，看 PB／股利）；
      ② 近 4 季有單季虧損；③ 近 3 年營收年增率波動 >±40 個百分點；④ FCF／成長／股數 不足、或近年 FCF 為負。清單／門檻在
      `config/settings.yaml` `cp_value.valuation.dcf`（8 個護欄 key **凍結不得改**，docs/31 §20.13）。
    - **讀法警語（逐字，附在附錄 G 表下）**：`dcf_intrinsic_est` 折現率一律 = 8.0%（地板恆綁定，TW 無風險利率 ~1.6% 過低）＝**單一風險參數模型**，
      跨股差異全來自 FCF／成長／淨負債／股數，不含個股風險區分。永續成長率固定 2%。淨現金因 `CashAndCashEquivalents` 不含短投／流動金融資產而
      **系統性低估**（保守偏差）。低信心腿，務必參照 `dcf_inputs.csv` 敏感度網格。
    - **輸出**：引用 `dcf_intrinsic_est` ＋ **敏感度四角**（`sens_wacc_*`：WACC ±1% × 永續成長 ±1%）＋ Stage-1 成長 3 點 ＋ 假設全列
      （`fcf_base`／`growth_pct`／`net_debt`／`shares`／`dcf_discount_rate`／`dcf_rate_binding`，皆在 `dcf_inputs.csv`）。
  - **M4 Opus 綜合判斷**：看 M1–M3 產出的 4–7 個 implied price 分布 → 給**一個區間（低端～高端，非單點）** ＋ 一句「哪個方法在這檔最可信、
    為什麼」（週期股信 PB、穩定成長股信 DCF、轉機股信前瞻倍數…）＋**信心分級**：
    - **高**：≥3 個方法落在彼此 ±15% 內 ∧ 前瞻 EPS 來自 FactSet／多分析師 ∧ **DCF 可算且與倍數法同向（±15%）**
    - **中**：方法落在 ±30% 內，或前瞻 EPS 單一來源，或 DCF 不適用但倍數法一致
    - **低**：方法發散 >±30%，或前瞻 EPS 弱／缺，或僅剩自身倍數一條線索
- **輸出格式**：每檔一段——現價｜M1 各值｜M2 各值（或「無法計算」）｜M3 `dcf_intrinsic_est` ＋敏感度四角＋Stage-1 成長 3 點＋假設全列
  （或 `dcf_exclude_reason`）｜M4 綜合區間（附「低端%~高端%」相對現價）＋依據＋信心。
- **讀法提醒（逐字附在附錄 G 表下）**：M1 是回顧性重定價、跟「估值缺口%(綜合)」同源（不是獨立佐證）；M2 唯一多出來的資訊是前瞻 EPS 相對當期的修正幅度；M3 `dcf_intrinsic_est` 折現率恆 = 8.0%（單一風險參數模型，跨股差異只來自 FCF／成長／淨負債／股數），對永續成長率／成長假設高度敏感（見敏感度網格），淨現金系統性低估。綜合區間是**判斷**、無回測，可信度以信心分級為準。**不作硬性進出場依據，與估值缺口% 同位階。**

## 規則

- **不下單一結論，多空並陳**；每檔空方條目數 ≥ 理由條目數；**風險段不可比理由短**。
- **禁用詞**（全清單見 playbook/60 禁止事項）：不給目標價／公允價／合理價，不寫「強烈建議」「絕對」「保證」「飆股」「預估價」「會漲到」——
  **唯一例外：附錄 G「綜合估值區間」**（docs/31 §20.13；得用「綜合估值區間」字眼，仍禁「公允價值／目標價／合理價」；M-Pick1 起不上第一頁）。
- **數字必須來自附帶資料或已查證的外部來源**，每個數字附日期與來源；資料沒有就寫「未取得」，不要自己編。
- **宏觀數字不得加工**：附檔以外的市場級數字——指數點位、台股/台指期夜盤漲跌點數、SOX/費半、美股、非農、CPI 數值——**只能引用使用者在 prompt
  中明確提供的、或你上網查證到的官方已公布數值（標「外部查證：來源＋日期」）**；不得憑記憶補上未查證的精確統計、不得把使用者給的「大跌」加工成
  具體點數或創造「史上最大」這類描述。**個股當日強弱一律以附檔 `change_pct` 為準**，不得宣稱與該欄矛盾的「撐盤/抗跌/相對強」。
  - **界線**：此條約束的是事件「結果數值」——未公布的一律不得預測或編造；已公布的可上網查證引用（附來源＋日期）。Section 0.6 事件的「排程日期/
    時間」是公開事實，可、也應該上網向官方來源查證。**查證後引用 ✅；憑記憶或推測填數值 ⛔。**
- **推論與事實分流**：產業延伸推論（訂單外溢、輪動下一棒、產業週期/結構判讀）一律標「推論・待查證／結構判讀・定性」，不得偽裝成既成事實或資料數字。
- **不得用當週敘事改寫既定門檻**（playbook/60）：shortlist 的 gate／上限只判過或不過，不寫「這檔雖超標但本週結構特殊所以算」。
- 繁體中文，台股術語。

開始分析。
`````

---

## 流程套用範例

```bash
# 1. 跑完週流程（含 shortlist 步驟：產 reports/<週>/shortlist.csv；容錯、失敗不擋主流程）
make week GROUP=defg
# （shortlist.csv 缺或參數改了想重排：單獨重跑）
make shortlist                                   # ＝ uv run tw-screener picks shortlist；指定週：make shortlist WEEK=2026-Www

# 2. 開 Claude Opus（網頁），貼 prompt（上方那段），再依 Step C 順序貼檔
cat reports/$(date +%Y-W%V)/shortlist.csv            # 第一頁唯一排序來源
cat reports/$(date +%Y-W%V)/pick_outcome_brief.md    # 上週帳
cat reports/$(date +%Y-W%V)/group_analysis.md        # 姿態／事件／族群脈絡
cat reports/$(date +%Y-W%V)/candidates_enriched.csv  # 查證與持股對照

# 3. Claude 回覆兩份 → pick.md（一頁決策卡＋picks 區塊；固定檔名，F1 斷供偵測與 week-check 都認這個名，
#    別再寫成 picks.md——W24 曾因此漂移、閉環底帳對不上）＋ pick_detail.md（明細，非必備）

# 4. 定稿後一次落底帳 picks.csv/excluded.csv（F1-PO1；week-check 會提醒缺帳）：
#    單檔事後補記才用 picks record --stock XXXX --layer core ...
uv run tw-screener picks sync --week $(date +%Y-W%V)
```

跑完手上就有一頁決策卡（Top 5＋持股動作＋風險）與一份明細，可進一步：

- 對 Top 5 每檔跑 `make report STOCK_ID=XXXX` 產深度報告
- 把追蹤的股加進 `watchlist/active.md`

---

## 為什麼不照 group_analysis 強度排名挑、也不讓 Opus 自由挑？

週報裡有兩種「排名」，M-Pick1 起只有一種能決定第一頁的順序：

| | group_analysis.md Section 2 族群強度／2.6 次產業強度 | shortlist.csv 排序（M-Pick1） |
|---|---|---|
| 宇宙 | 本週篩中的候選股（有入選偏誤） | 族群分數取自 sector_rotation 全次產業成員（無偏）；個股取自候選股＋watchlist |
| 公式 | 形如 `0.6×族群強度 ＋ 0.3×族群內排名加分 ＋ 0.1×多策略命中`（已停用的舊 Section 5「族群輪動機械基準」同源）——動能／廣度／多策略 | `trend_bucket` → 距季線偏好帶 → `trend_score` → 成交額（僅決勝）→ 股號；同次產業 1 檔、同因子簇 2 檔 |
| 證據 | 未驗證；偏向「強族群＋族群內前段班＋多策略命中」，會把過熱、籌碼差的大族群龍頭捧前 | F3 趨勢分已驗證（r+20 IC +0.11、三個 regime CI 皆 >0，docs/22、docs/23）；距季線位階＝弱證據 |
| 用途 | 輪動脈絡，只在附錄 C／F 描述 | 第一頁 Top 5 的唯一順序 |

**為什麼不再讓 Opus 自由挑**：舊流程（四路匯流 → 兩階段 → 核心／機會／補充池）的分層在 4 週窗沒有鑑別力（M-Pick1 開工盤點：
核心 −0.4%／機會 −0.9%／補充池 +0.2%），而入選理由大量倚賴已否證的個股層法人流。誠實帳：docs/31 §0 ② 曾記錄「Opus 精選層是全流程唯一有
鑑別力的一段」（贏家進 picks 7.2% vs 輸家 1.0%）——那是「贏家有沒有被選進 picks」的召回差，與「層與層之間報酬差」是不同統計物件，
兩者不矛盾。M-Pick1 的取捨是：**排序交給已驗證訊號、Opus 保留查證與有限否決權**；族群內個股挑選要由規則還是由 Opus 做得更好，
交給 M-Pick2 研究——2026-09-28 結案（docs/32）：四個預註冊個股因子皆未過關、首選也不勝現行規則，本檔排序規則不變。

**仍然適用的「居安思危」**：強度排名後段、族群弱但個股強的逆勢票，在 M-Pick1 下不會因 Opus 判斷而進 Top 5——它們只會出現在附錄 D／H
（觀察、未驗證），等研究或族群趨勢分本身轉強再說。寧可少做，不可含糊做。
