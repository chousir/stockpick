---
description: 外部總經風險掃描（docs/26 B案）：美股情緒/籌碼位階廣度掃描，人工觸發、走網路搜尋、不進 pipeline、不影響燈號與選股
argument-hint: "[週次，如 2026-W32；省略＝用最新一週]"
allowed-tools: WebSearch, WebFetch, Read, Write, Bash, Glob, Grep
---

你要執行一次**外部總經風險掃描**，產出一份獨立報告。

先讀 `docs/26-macro-scan-integration.md` §0、§6.2、§8——那裡寫了這份掃描的定位、五項強制改造與
風險對策。本檔是執行手冊，docs/26 是為什麼這樣做。衝突時以 docs/26 為準。

## 這份掃描補的是什麼盲點（別搞錯目標）

現有 `make macro` 總經燈號盯的是**信用/利率/波動**（FRED，主訊號 BAA10Y）。它看不到
**「信用還平靜、但情緒與籌碼已經極端」**這一族——2021 式的融資餘額高峰＋內部人賣超＋IPO 洪峰
＋恐懼貪婪極度貪婪。這份掃描就是去補那一族，**不是把燈號已有的東西重抓一遍**。

## 硬規則（違反就是這份掃描失去意義）

1. **不重抓本專案已有的序列**。VIX／BAA10Y／DGS20／WTI 油價／STLFSI4／DGS10／USD-TWD／USD-JPY
   一律從 `reports/<週次>/macro_regime.csv` 讀（欄位：role, series_id, transform, as_of,
   raw_value, score_pct, stale, source）。`$ARGUMENTS` 給了週次就用它，沒給就用 `reports/` 下
   最新一週。讀不到就在報告裡寫「未取得（`make macro` 尚未跑過本週）」，**不要改用網路數字替代**
   ——那會讓同一個指標在兩份文件裡出現兩個不同的值。
   **同理，FRED 上有的序列一律走 FRED API 不走網搜**（key 在 `.env` 的 `fred_api`，端點
   `https://api.stlouisfed.org/fred/series/observations?series_id=<ID>&api_key=<KEY>&file_type=json&sort_order=desc&limit=3`）
   ——本清單裡的 `BAMLH0A0HYM2`（高收益債利差）與 `T10Y2Y`（殖利率曲線）屬此類，
   媒體轉述的數字會跟本 repo 自己算的漂移。
2. **不編數字**。找不到就寫「數據滯後至 <具體日期>」或「未找到」。絕不用訓練資料或推測值填充。
   最容易抓不到的是 BofA Bull & Bear（只在美銀週報，靠媒體轉載）與 Insider Buy/Sell
   （GuruFocus 常擋爬蟲）——**抓不到就老實說滯後幾週，不接受一個「看起來很合理」的數字**。
3. **門檻是歷史經驗值，不准用當前市場情緒重新解釋**。例如 VIX < 13 永遠是「自滿」，
   不可以寫「現在結構性偏低所以不算自滿」。門檻要改是另一件事（要有實測依據），不是掃描時順手改。
4. **觸發狀態判定不得加軟化語**（「但是」「不過」「雖然」）——觸發就是觸發，沒觸發就是沒觸發。
   但**逐條解讀段與綜合結論段必須多空並陳，空方論點不得少於多方**（本 repo 報告紅線，
   見 playbook/60）。這兩件事不衝突：判定要硬，解讀要平衡。
5. **不下買賣結論、不給倉位數字、不給目標價**（鐵律 2）。
   來源 prompt 原本要求「該說『減倉 15%』就說『減倉 15%』」——**這條已刪除**，因為它直接違反
   本 repo 的報告紅線。改成：**描述風險水位與位階，把「要不要調整、調多少」留給人決定**。
   禁用詞：強烈建議／絕對／保證／飆股。
6. **每項標資料日期**，報告末尾集中列出所有滯後或來源衝突的項目。
7. **不合成**。不得產生任何加總分數、加權指數或綜合評分；不得建議調整本專案的燈色、排序、
   剔除或任何 pick 決策。這份報告是**並列的第二意見**，不是覆蓋層。
8. **不動 pipeline 產物**。不寫 `reports/`、不改 `pick.md`／`picks.csv`／`excluded.csv`、
   不改 `data/`。唯一輸出是 `research/macro_scan/<YYYY-MM-DD>.md`。

## 掃描清單（17 項，分三個時間尺度）

先確認今天日期（`date +%F`），確保時效判斷正確。

**已有（讀 macro_regime.csv，不搜網）**：VIX、Baa 利差 BAA10Y、DGS20、WTI、STLFSI4、DGS10、
USD/TWD、USD/JPY。

**要搜網的（本專案沒有）**：

🟢 短期（天－週，過熱回調）
1. CNN Fear & Greed Index — cnn.com/markets/fear-and-greed
2. AAII Investor Sentiment Survey（Bullish %／Bearish %／多空差）— aaii.com/sentimentsurvey
3. CBOE Equity Put/Call Ratio — cboe.com/us/options/market_statistics
4. NAAIM Exposure Index — naaim.org（**注意：2026-08-01 起訂閱制，免費資料落後約 3 個月**；
   抓不到就標滯後，或找 Investors Intelligence／SentimenTrader 的替代讀數並註明來源不同）

🟡 中期（週－月，趨勢轉折）
5. FINRA Margin Debt（最新月值＋年增率＋月增方向）— finra.org margin statistics
6. Margin Debt / GDP 比率
7. Renaissance IPO 發行量（當季件數＋募資總額）— renaissancecapital.com/IPO-Center/Stats
8. Insider Buy/Sell Ratio（GuruFocus USA Overall Market）
9. BofA Bull & Bear Indicator（搜「BofA Bull Bear Indicator <當前月份>」，多搜幾次交叉驗證）
10. ICE BofA US High Yield OAS —— **走 FRED API 抓 `BAMLH0A0HYM2`，不要網搜**；
    **只取當前絕對值**：FRED 對此序列只公開滾動 3 年歷史，任何百分位/歷史位階說法都不成立
    （docs/26 §1.2）
11. NYSE Advance/Decline Line 是否與 S&P 500 頂背離

🔴 長期（月－年，結構性週期頂）
12. Buffett 指標（總市值/GDP）— currentmarketvaluation.com
    （**註明分母 GDP 為季頻且落後約一季**，週對週不會變）
13. Shiller CAPE / PE10 — multpl.com/shiller-pe
14. 10Y-2Y 殖利率曲線 —— **走 FRED API 抓 `T10Y2Y`，不要網搜**；
    **本專案已實測此指標對本地目標無預測力（docs/26 §1.2），只列讀數當脈絡，不可寫成訊號**
15. Conference Board US LEI（最新月值＋6 個月變化率）—
    **FRED 的免費替代 `USSLIND` 已於 2020-02 停更**，本體付費；抓不到就標未取得
16. AAII 家庭股票配置比（% allocation to stocks）— aaii.com/assetallocationsurvey

## 輸出格式

寫入 `research/macro_scan/<YYYY-MM-DD>.md`（目錄不存在就建；`research/` 已 gitignored）。

開頭固定放這個標頭（一字不改）：

```
> **定位**：外部第二意見。未經本專案任何實測驗證、與 `make macro` 總經燈號**不合成**、
> **不影響**燈色/排序/剔除/pick。所有門檻皆為外部來源的歷史經驗值，未在本專案校準。
> 本檔為研究軌產出（`research/`，不進 git），最終決策由人做。
```

然後六段：

1. **儀表盤表格** — 全部指標一表，按短期→中期→長期分組小標題。欄位：
   `指標 | 當前值 | 資料日期 | 來源 | 警戒閾值 | 信號`。本專案已有的項目在來源欄註明
   `macro_regime.csv`，其餘註明實際 URL/媒體。
2. **求值覆蓋率速覽** — 三個尺度各報「已求值 N 項／共 M 項」，並**明列哪幾項未能求值及原因**。
   不要把未求值的項目當成「未警報」。
3. **逐條解讀** — 每項 2-3 句：當前讀數與歷史位置／與上次掃描的變化方向／對決策的含義。
   多空並陳。
4. **短中長期綜合結論** — 三段（短期 1-3 月／中期 3-12 月／長期 1-3 年+）。
   講風險水位與位階變化，**不講加減倉幅度**（規則 5）。
5. **賣出觸發狀態追蹤** — 表格逐條列 7 個硬閾值：VIX > 25、Margin Debt 連 3 月減、
   HY Spread > 4.5%、Fear & Greed 從 >75 跌回 <50、A/D 頂背離、BofA B&B > 8.0、
   Insider Buy/Sell < 0.17。每條標 ✅觸發／❌未觸發／**⬜無法求值**（缺資料，第三種狀態必須存在）。
   表格下方寫：**「已求值 N 項中觸發 M 項；另有 K 項無法求值」**——
   **不得**把結論寫成「M/7」，那會把拿不到的資料默默算成沒事（docs/26 §2）。
6. **今日最需關注的一條** — 從所有已求值項目裡挑變化最劇烈或最接近觸發的一條，一段話說明為什麼。

末尾固定兩段：
- **滯後與衝突清單**：所有滯後 >1 週或多來源數字不一致的項目，逐條列出日期與差異。
- **與本專案燈號的並列對照**：一句話說明「本掃描的姿態」vs「`make macro` 燈色」是同向還是背離；
  背離時**只描述背離事實與可能原因，不裁決誰對**（docs/25 §0 不合成鐵則）。

## 節奏建議

與週報同節奏（每週一次）即可，急變時人工加跑。**不設自動排程、不掛 `make week`**
（需要網路搜尋，make 跑不了；且掃描結果未經驗證，不該進主流程）。
第一次跑沒有歷史基準，變化箭頭只能用各指標自己的近期序列推——跑滿幾輪後對比才有意義，
所以每次都要落檔（同一天重跑就覆蓋同一個檔名，不要產生多份）。
