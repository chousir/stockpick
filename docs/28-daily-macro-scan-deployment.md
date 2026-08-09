# 28. 每日宏觀掃描部署（委託書 M9・裁決 D）

> 對應委託書 `propicks_完整規劃委託書_FINAL_20260808.md` M9。**文件交付，不寫進本 repo 程式**。
> 上游規格：docs/27 §2（M8 宏觀窄橋）、docs/26（外部掃描不進 pipeline 的理由）。

## 0. 為什麼要「雙軌」而不是把 17 項指標接進 pipeline

裁決 D 把每日美股風險掃描切成**獨立 project**，週報只經 `macro_risk_latest.yaml`
這座**窄橋**消費其結論。三個理由：

1. **資料性質不同**。17 項指標多數靠網路搜尋、來源分散、公布頻率從日到季不等
   （FINRA margin debt 是月頻、Buffett indicator 是季頻）。接進 `make week` 等於
   把週報的產出時間綁在 15–20 次網路搜尋的成敗上——docs/26 §2 已裁定不合成。
2. **歷史連貫性需要對話延續**。掃描報告的 ↑↓→ 箭頭來自「與上次掃描比」，那是
   project 內對話歷史提供的，pipeline 重跑無此上下文。
3. **語言紀律相反**。掃描 prompt 明文要求「該說『減倉 15%』就說『減倉 15%』」；
   週報紅線是**多空並陳、不下買賣結論**（CLAUDE.md 鐵律 2）。兩套語言必須隔離，
   見下方 §4。

窄橋的另一面是**方向也相反**：`market_washout`（M2）是台股內部**抓底**哨兵，
本掃描是全球**抓頂**哨兵。兩者同時亮＝訊號衝突，決策卡須明寫矛盾（docs/27 §0）。

---

## 1. 部署（一次性）

| 項目 | 設定 | 理由 |
|---|---|---|
| 執行位置 | 一個獨立的 Claude **project**，命名如「每日風險掃描」 | 每天新開對話直接調用，project 內保有歷史 → ↑↓→ 箭頭才有意義 |
| 模型 | **Sonnet 4.6** | 重複性檢索工作（15–20 次搜尋），快且省；判斷含量低，不需 Opus |
| Prompt | repo 根目錄 `宏觀風險掃描提示詞.docx`（V2.0）＋ §2 的附加段 | 原 prompt 不改，輸出義務用附加段疊上去 |
| 頻率 | 工作日每早 8:00（美股盤前）；週末用週五收盤數據 | 掃描 prompt 使用建議 1 |
| 產物歸檔 | 每天把報告存檔（project 內即可） | 第一次跑沒有歷史基準，跑滿一週後對比才有意義 |

**指標構成**：短期 5 項（VIX／CNN Fear & Greed／AAII／Put-Call／NAAIM）、中期 7 項
（FINRA Margin Debt／Margin Debt-GDP／IPO 發行量／Insider B-S／BofA Bull & Bear／
HY OAS／NYSE A-D Line）、長期 5 項（Buffett Indicator／Shiller CAPE／10Y-2Y／
Conference Board LEI／AAII 家庭股票配置）。

**7 項硬閾值**（掃描報告第五部分；窄橋的 `triggers_hit` 從這裡數，
但**分母是 `of`＝實際判定得出的項數、不是 7**，理由見 §2 規則 2）：

| # | 觸發條件 | 閾值 |
|---|---|---|
| 1 | VIX 突破並站穩 | > 25 |
| 2 | Margin Debt 月減 | 連續 3 個月 |
| 3 | HY Spread 擴張 | > 4.5% |
| 4 | Fear & Greed 從高位回落 | >75 跌回 <50 |
| 5 | A/D Line 頂背離 | S&P 新高但 A/D 不創新高 |
| 6 | BofA Bull & Bear | > 8.0 |
| 7 | Insider Buy/Sell | < 0.17 |

⚠️ **這 7 個閾值是委託人採用的外部框架，未經本系統回測**。窄橋只讓它影響**倉位
節奏**（姿態降級／新倉封 ⅓）、不進選股邏輯——這個限制本身就是圍欄（委託書誠實帳）。

---

## 2. 掃描 prompt 的附加段（**照抄貼到 V2.0 prompt 最末**）

> ---
>
> ## 【第七部分：機器摘要（供台股週報窄橋消費）】
>
> 報告最後**固定輸出**下列 YAML 區塊，用 ```yaml 程式碼框包起來，方便我整段複製：
>
> ```yaml
> macro_risk:
>   date: YYYY-MM-DD        # 本次掃描的**資料基準日**（不是你回答的日期）
>   triggers_hit: 2         # 第五部分 7 項硬閾值中，**判定為已觸發**的項數
>   of: 5                   # 7 項中**實際判定得出**的項數（抓不到的不算進來）
>   hits: [HY_spread, margin_debt_mom]   # 已觸發者，用下列固定代號：
>                           # VIX / margin_debt_mom / HY_spread / fng_rollover
>                           # / ad_divergence / bofa_bull_bear / insider_ratio
>   vix: 18.3               # VIX 當前值；未取得寫 null
>   fng: 41                 # CNN Fear & Greed 當前值；未取得寫 null
> ```
>
> **規則**：
> 1. `triggers_hit` 必須與第五部分表格的計數一致，不得另算。
> 2. ⚠️ **`of` 是「已求值項數」，不是常數 7**。7 項裡通常有 1–2 項抓不到
>    （BofA B&B、Insider ratio），那些**不計入 `triggers_hit`，也不計入 `of`**。
>    下游用 `of` 當分母判斷「分母夠不夠厚」——寫死 7 會讓它以為全部都查過了，
>    把「0–2 觸發」誤讀成「風險已清」。`of` 誠實寫小，警語才會正確地出現。
> 3. `hits` 只用上面列的固定代號，**不要自創**（下游按代號讀，自由發揮＝對不上）。
> 4. 找不到就寫 `null`，**絕不用訓練資料或猜測值填充**（同本 prompt 紀律要求第 1 條）。
> 5. 只輸出這 6 個鍵。**不要加 `conclusion`／`action`／建議句**——窄橋刻意只搬數字，
>    理由見台股端 docs/28 §4。
>
> ---

## 3. 每週怎麼用（人工動作，約 30 秒）

1. 跑完當日掃描後，複製報告末尾的 `macro_risk:` YAML 區塊。
2. 存成 `reports/<本週週次>/macro_risk_latest.yaml`（檔名可在
   `config/settings.yaml` 的 `macro_risk.filename` 改）。
3. 跑 `make week GROUP=defg`。`week-check` 會印出該檔的三態判定；也可單獨跑
   `uv run tw-screener market macro-risk` 先看一眼。
4. 貼週報輸入包時把該檔一起貼（docs/11 Step C 貼檔順序的最後一項）。

**缺席完全合法**。沒貼＝週報照出，只是決策卡會寫「宏觀掃描缺席，不當 gate」。

### 三態容錯（實作在 `analysis/macro_risk.py`，規格見 docs/27 §2）

| 狀態 | 條件 | 週報行為 |
|---|---|---|
| `ok` | 檔案存在、schema 正確、`date` 落後 ≤5 個交易日 | 依 patch-6 消費：`triggers_hit` ≥3 → 姿態降一級＋新倉封 ⅓；0–2 → 僅註記一行 |
| `stale` | 同上但 `date` 落後 >5 個交易日 | 降級為註記，**不當 gate**，明寫「過期」 |
| `missing` | 檔案不存在 | 明寫「宏觀掃描缺席，不當 gate」 |
| `invalid` | schema 錯／必要欄缺 | 同 `missing` 處理，另印錯在哪 |

`of < 5` 時（`macro_risk.min_coverage`），即使 `triggers_hit` 是 0–2 也必須
帶 coverage 警語——**未求值只會少算，不可讀成「風險已清」**（docs/26 §2 分母圍欄）。

---

## 4. 語言隔離（紀律，不是建議）

掃描 project 的「減倉 15%」式硬話術**只存在於該 project**，**不得滲入週報**：

- 週報維持**多空並陳、不下買賣結論、不給目標價**（CLAUDE.md 鐵律 2）。
- 窄橋只搬**三個數字**（`date`／`triggers_hit`／`of`）與一個代號清單，
  **不搬掃描報告的結論句**。yaml schema 刻意沒有 `conclusion` 或 `action` 欄，
  就是為了讓這條紀律在資料結構層面成立、而不是靠人自律。
- 反過來也一樣：週報的「不下結論」不必套進掃描 project——那邊是給你一個人看的
  倉位節奏工具，硬話術正是它的用途。

## 5. 已知抓不到率高的項目（守誠實 null）

- **BofA Bull & Bear**：只在美銀每週 Flow Show 報告公布，靠媒體轉載，常抓不到。
- **Insider Buy/Sell**：GuruFocus 頁面常擋爬蟲。
- **NAAIM**：2026-08-01 起改訂閱制，免費資料滯後約 3 個月；可考慮換
  Investors Intelligence 或 SentimenTrader（**換了要同步改本檔與附加段的代號**）。

抓不到就讓它老實說滯後幾週，**絕不接受「看起來很合理」的數字**——與週報同一套紀律。
這也是 `of` 要誠實寫小的原因：抓不到要能被下游算進分母警語，否則等於白誠實。
