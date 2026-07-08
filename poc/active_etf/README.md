# PoC：主動式 ETF 每日持股 — 資料源可行性勘查

> **隔離沙盒。** 與 `src/tw_screener` 主流程完全脫鉤，未接 CLI / Makefile /
> `config/` / `data/`。確認可行後才談怎麼接進籌碼面/雷達。

日期：2026-06-07。樣本：群益 00992A（主動群益科技創新）。

## 結論一句話

資料**公開、每日揭露、欄位正是我們要的**；但**沒有穩定 CSV、也沒有中央 API**，
每家投信是各自的 SPA + 各自的後端 JSON API，且**後端多半 geo-fence 到台灣**
——本勘查環境在美國，連不到後端（HTTP 000），只能拿到公開頁的「前十大」。

## 標的池

約 18 檔台股主動式 ETF（總 30 檔扣掉海外股 00983A/00988A/00989A/00990A/00997A
與所有 `D` 系列債券）。涵蓋約 11 家投信。

## 欄位（實測 群益 00992A，已驗證可解析）

`stock_id, name, weight_pct, shares` + 資料日期。範例（2026/06/05，前十大）：

| stock_id | name | weight_pct | shares |
|---|---|---|---|
| 2330 | 台積電 | 8.61 | 1,706,000 |
| 2383 | 台光電 | 5.87 | 563,000 |
| 2345 | 智邦 | 5.19 | 977,000 |

→ 足以算 **ΔWeight / 新進剔除**（要追權重%，不追股數，避免被申贖規模污染）。

## 各投信取得難度（實測）

| 投信 | 公開頁 | 完整持股來源 | 從本沙盒(US) |
|---|---|---|---|
| 群益 capitalfund | Angular SSR，**只內嵌前十大** | 後端 `http://125.227.3.107/CapitalFundAPI/api/etf/...`（從 main.js 反解出） | ❌ 後端 geo-fence，connect timeout |
| 統一 ezmoney | — | — | ❌ 純抓會 302 自我跳轉（需 session/JS） |
| 野村 nomurafunds | — | — | ⚠️ 回 1.7KB JS 殼，需 API/headless |

**意涵**：
1. 完整名單**必須打後端 API**（公開頁只給 top10）。
2. 後端 API 真實存在且回 JSON（群益 `/api/etf/*`），所以**程式化抓是可行的**——
   前提是**在台灣可達的網路**跑。
3. 每家投信 API 不同 → 每家一支 parser/adapter（異質成本）。

## 合規

公開、法規強制全透明揭露，非破解。但：瀏覽器 UA、單線、限速、退避（同
CLAUDE.md 鐵律 1 Goodinfo 紀律）；後端為**未公開文件的內部 endpoint，隨時可能變**，
production 要有「公開頁 top10」當降級備援 + 改版告警。

## 怎麼跑

```bash
python3 poc/active_etf/fetch_capital.py          # 公開頁 top10（US 可跑，已驗證）
python3 poc/active_etf/fetch_capital.py --full   # 後端完整持股（需在台灣本機/TW proxy）
```

snapshot 輸出在 `poc/active_etf/snapshots/`。

## 第1步驗證後（2026-06-07 更新）：沒有乾淨統一的程式化來源

`--full` 在**使用者台灣本機也 timeout** → `125.227.3.107` 不是 geo-fence，是 main.js
裡的**內網/dev 位址**（旁邊就有 `localhost:44394`），對外根本不通；www 同源
`/CapitalFundAPI/...` 回 404。逐一驗證的死路：

| 來源 | 結果 |
|---|---|
| 群益後端 IP | 內網位址，US 與 TW 本機都 timeout；www 同源 404 |
| 國泰 cathaysite | 403（擋 bot） |
| 統一 ezmoney | 302 自我跳轉（需 session/JS） |
| 野村 nomurafunds | 1.7KB JS 殼 |
| 富邦 fsit `Pcf.aspx?stkId=&ddate=` | ASP.NET 可達、帶參數，但成分非靜態 HTML；且富邦主動 ETF 是債券 |
| TWSE OpenAPI / e添富 / TDCC 開放資料 / FinMind | **皆無 ETF 持股/成分 資料集** |

**結論**：完整持股程式化取得 = 只剩兩條重路 —— (1) **headless browser(Playwright) 逐家 render**
（CLAUDE.md 鐵律 4 需先問才可加依賴），或 (2) **付費資料商**（TEJ/CMoney）。
公開頁 SSR 只有 top10、且非每家都給。⇒ 此功能 friction 高，建議降級待決，
或確認願上 Playwright 再做。
