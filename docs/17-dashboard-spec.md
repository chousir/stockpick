# 17 — 投資戰情室 Dashboard 規劃書（War-Room HUD．讀 reports/ 的本機自用網頁）

> 把每週跑出來的 `reports/` 結構化資料（candidates / holdings / watchlist / sector_rotation /
> theme_strength / screen_result_*）＋手寫敘事（pick.md / group_analysis.md）做成一個**本機自用、
> cyberpunk「交易戰情室」風格**的儀表板。**只讀不寫**：dashboard 不改任何分析結果，純粹呈現。
> 規格由 2026-06-22 與使用者三輪討論定案，**決策見 §1**。
> 2026-06-22 依實地核對 `reports/` 資料修訂：週次實為 W21/W22/W24/W25（**無 W23**）、
> enriched schema 跨週演進、多數結構化檔僅最新週齊備——詳見 §2.4 與各節「資料事實」標註。
> **2026-06-23 更新**：自 **W26** 起主流程已每週輸出完整 schema（含 `sector_rotation.csv`／
> `theme_strength.csv`／`low/high_20d/60d`／`val_pctile`／`dividend_yield_pct` 等），故下文 §2.4
> 與各節「僅 W25」之「資料事實」屬 **W26 前歷史快照**；W26+ 各週應齊備、相關圖表與區間條皆可繪。

---

## 0. 一句話目標

讓我每週一打開 `localhost`，就能在一個 War-Room HUD 介面裡，把當週的候選股、族群資金輪動、個股鑽取、
庫存損益**用可排序篩選的表格＋衍生圖表**看完，並能切回任一已產出週次（目前 W21/W22/W24/W25）的快照；下單決策仍由人做，
dashboard 不下結論、不報目標價（守 [CLAUDE.md](../CLAUDE.md) Part 3）。

---

## 1. 決策定案表（三輪討論結果）

| 項目 | 定案 | 備註 |
|---|---|---|
| **技術架構** | FastAPI（讀 CSV → JSON API）＋ 前端 SPA | 後端沿用現有 Python/Polars |
| **前端選型** | React + Vite + ECharts | TanStack Table 做資料表、react-markdown 渲染敘事 |
| **執行/部署** | 本機自用 `localhost`（uvicorn 綁 `127.0.0.1`），**無驗證** | 含個人持股損益無妨，**永不進 git、不對外** |
| **專案結構** | Mono-repo | 後端 `src/tw_screener/webapp/`、前端 repo 根 `frontend/` |
| **服務模式** | dev 前後端各跑；prod 由 FastAPI 服務 Vite build 後靜態檔 | 啟動只跑一個指令 |
| **視覺調性** | **War-Room HUD（交易戰情室）** | teal `#18e0c8` ＋ 預警橙/紅，冷色高密度，見 §6 |
| **漲跌色慣例** | **台股慣例：紅漲綠跌** | 漲 `#ff4d5e`、跌 `#2bd576`，全表格與圖表一致 |
| **頁面範圍** | 四頁全做：候選股 / 族群輪動 / 個股 detail / 持股損益 | MVP 邊界見 §9 |
| **週次/歷史** | **週次切換器**（動態週次清單，目前 W21/W22/W24/W25、**無 W23**，預設最新週） | 跨週比較列為 stretch（資料已含 `rank_delta`） |
| **敘事報告** | **兩者並陳** | 結構化表/圖為主，pick.md / group_analysis.md 收側欄或分頁可展開 |
| **視覺化深度** | 表格＋多圖表儀表板 | 優先 4 圖：族群資金熱力圖 / 動能×估值散佈 / 法人買賣超 bar / 估值殖利分布 |

---

## 2. 資料盤點（dashboard 的輸入）

來源固定為 `reports/<週次>/`，週次資料夾**動態偵測**（不寫死週數）。實地核對：目前為
**W21 / W22 / W24 / W25（無 W23），且各週檔案嚴重不齊**——完整缺檔矩陣與 schema 演進見 §2.4。
API 一律容忍缺檔（缺檔回 404、缺欄回 null，**絕不丟 500**）。

### 2.1 結構化 CSV

| 檔名 | 行數量級 | 用途 | 關鍵欄位 |
|---|---|---|---|
| `candidates_enriched.csv` | ~140 | 候選股主表 | 見 §2.3 共用 schema |
| `holdings_enriched.csv` | 庫存數 | 持股損益（**個人敏感**） | 共用 schema ＋ `buy_price, return_pct, market_value_k` |
| `watchlist_enriched.csv` | ~40 | 觀察清單 | 共用 schema |
| `sector_rotation.csv` | ~35 | 族群資金雷達 | `radar_rank, sub_industry, net_flow_5d/10d/20d, foreign/trust_flow_5d/10d/20d, *_z（含 10d）, quadrant, cp_score, freshness, basket_ret_5d_pct, entry/confirm_triggered`（10d 為分析層補窗的中端鏡頭，W26+ 起齊備）|
| `theme_strength.csv` | ~35 | 主題/次產業強度 | `theme, kind, radar_rank, lead_score, score, momentum_5d, members_count, foreign_score, vol_surge_score, rank_delta` |
| `screen_result_d/e/f/g_*.csv` | 各 30–120 | 四策略原始篩選命中 | `stock_id, name, market, close, change_pct, volume_lots, amount_million, pe_ratio, pb_ratio, strategy_id, screened_at, goodinfo_url` |

### 2.2 敘事 Markdown

| 檔名 | 內容 | 呈現位置 |
|---|---|---|
| `pick.md` | 本週進場清單＋執行摘要＋多空論點 | 候選股頁側欄/分頁 |
| `group_analysis.md` | 族群分析（含 §0.5 本週除權息表） | 族群頁側欄/分頁；除權息表餵持股頁提醒 |
| `sector_rotation.md` | 族群輪動敘事 | 族群頁分頁 |
| `screen_log.md` | 篩選紀錄 | 次要，候選股頁可展開 |

### 2.3 enriched 共用 schema（以最新週 W25 為超集）

⚠️ **跨週不一致**：下列為 **W25 完整欄位（candidates 40 欄）**；W22 / W24 僅 25 欄，缺
`ret_10d_pct, ex_div_cash, div_addback_pct, low_20d, high_20d, low_60d, high_60d,
dividend_yield_pct, val_metric, val_pctile, cheap_flag, gross_margin_pct, eps_q,
foreign_net_5d_lots, foreign_net_10d_lots`；W21 無 enriched 檔。後端 Pydantic 模型以
**W25 為超集、所有欄位 `Optional`**，舊週缺欄回 `null`、前端顯示 `—`。分組對應報告框架
（[docs/06](06-report-spec.md)、CLAUDE.md Part 3.2）：

- **識別**：`stock_id, name, industry, theme, strategy, rank_in_group, flags, goodinfo_url`
- **動能/報酬**：`momentum_5d_pct, ret_10d_pct, change_pct`
- **技術**：`close, vol_ratio, ma20_dist_pct, ma60_dist_pct, ma20_price, ma60_price, low_20d, high_20d, low_60d, high_60d`
- **估值**：`pe_ratio, pb_ratio, dividend_yield_pct, val_metric, val_pctile, cheap_flag`
- **基本面**：`rev_yoy_pct, gross_margin_pct, eps_q`
- **籌碼**：`volume_lots_today, amount_million, inst_net_lots, inst_net_5d_lots, inst_net_10d_lots, inst_pct20d, foreign_net_lots, foreign_net_5d_lots, foreign_net_10d_lots, trust_net_lots, trust_net_5d_lots, trust_net_10d_lots`（三大法人/外資/投信皆有 5/10d 近端窗，揭露 20 日累計蓋住的近端轉向；W26+ 起齊備）
- **除權息**：`ex_div_cash, div_addback_pct`
- **holdings 專屬**：`buy_price, return_pct, market_value_k`

### 2.4 跨週缺檔矩陣（2026-06-22 實地核對，✓＝存在）

| 檔案 | W21 | W22 | W24 | W25 |
|---|:--:|:--:|:--:|:--:|
| `candidates_enriched.csv` | ✗ | ✓(25欄) | ✓(25欄) | ✓(40欄) |
| `holdings_enriched.csv` | ✗ | ✓ | ✓ | ✓ |
| `watchlist_enriched.csv` | ✗ | ✓ | ✓ | ✓ |
| `sector_rotation.csv` | ✗ | ✗ | ✗ | **✓（僅此週）** |
| `theme_strength.csv` | ✗ | ✗ | ✓ | ✓ |
| `pick.md` | ✗ | ✓ | ✗ | ✓ |
| `sector_rotation.md` | ✗ | ✗ | ✗ | ✓ |
| `group_analysis.md` | ✓ | ✓ | ✓ | ✓ |
| `screen_result_d/e/f/g_*.csv` | ✓ | ✓ | ✓ | ✓ |

**呈現原則（定案）**：切到缺資料的週次時，**顯示空狀態「本週無此資料」並保留該週於切換器**
（不隱藏、不自動回退）。實務後果須在規格內承認：
- **族群輪動頁**（依 `sector_rotation.csv`）目前**僅 W25 有完整資料**，其餘週為空狀態。
- **§7-2 動能×估值散佈、§7-4 估值/殖利分布**依賴 `val_pctile / dividend_yield_pct`，**僅 W25 可繪**。
- **W21** 無 candidates，候選股頁與個股鑽取在該週為空狀態。
- **pick.md** 缺於 W21/W24，敘事側欄在該週顯「本週無進場敘事」。

---

## 3. 系統架構

### 3.1 Mono-repo 佈局（最終樣貌，由 milestone 漸進建立）

```
stockpick/
├── src/tw_screener/webapp/        # ← 新增：FastAPI 後端
│   ├── __init__.py
│   ├── app.py                     # FastAPI app、CORS(dev)、prod 靜態掛載
│   ├── server.py                  # uvicorn 進入點（CLI: tw-screener serve）
│   ├── data_access.py             # Polars 讀 reports/，mtime 快取
│   ├── schemas.py                 # Pydantic 回應模型
│   └── routers/
│       ├── weeks.py               # 週次清單、meta
│       ├── tables.py              # candidates/holdings/watchlist/sectors/themes/screens
│       ├── stock.py               # 個股彙整（跨表 join）
│       └── narrative.py           # 原始 markdown
├── frontend/                      # ← 新增：Vite + React + TS
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts             # dev proxy /api → :8000
│   ├── src/
│   │   ├── main.tsx
│   │   ├── theme/                 # War-Room HUD design tokens（§6）
│   │   ├── lib/api.ts             # fetch wrapper、型別
│   │   ├── components/            # DataTable, HudCard, NumberCell, WeekSwitcher, ...
│   │   ├── charts/                # SectorHeatmap, MomentumValueScatter, InstFlowBar, ValuationDist
│   │   └── pages/                 # Candidates, Sectors, StockDetail, Holdings
│   └── dist/                      # build 產物（gitignore）
├── pyproject.toml                 # ＋ fastapi, uvicorn[standard]
└── Makefile                       # ＋ dash-* 指令
```

### 3.2 資料流

```
reports/<週次>/*.csv ──(Polars 讀，mtime 快取)── data_access.py
        │                                              │
   *.md ┘                                              ▼
                                          FastAPI routers → JSON
                                                       │
                              dev:  Vite dev :5173 ──proxy──┐
                                                            ▼
                                          React + ECharts (HUD UI)
                              prod: FastAPI :8000 服務 frontend/dist + /api
```

- **讀取策略**：read-on-request；以 `(週次, 檔名, mtime)` 為 key 做記憶體快取，檔案變動才重讀。
  **`/api/weeks` 的目錄掃描不快取（或極短 TTL）**，新跑的一週資料夾重整即見，**不需重啟**。
- **Polars 為主**：CSV→`pl.read_csv`→`to_dicts()`。**數值欄**的空字串/`-`/`NaN` 統一轉 `null`
  （字串欄如 `flags / val_metric / quadrant` 不套此清洗、保留原值），前端負責顯示 `—`。
- **缺檔容錯**：缺欄一律補 `null`（Pydantic Optional）；整檔不存在則該端點回 404、頁面顯空狀態。

### 3.3 不引入的東西

不加 pandas、不加資料庫、不加狀態管理大庫（先用 React Query 或單純 fetch）。守 CLAUDE.md
Simplicity First。

---

## 4. 後端 API 設計

| Method | Path | 回應 | 說明 |
|---|---|---|---|
| GET | `/api/weeks` | `{weeks: [...], latest: "2026-W25"}` | 動態掃 `reports/` 目錄 |
| GET | `/api/weeks/{week}/meta` | 各檔是否存在＋資料基準日 | 基準日來源優先序：`pick.md` 標頭「資料基準」→ `group_analysis.md`「生成時間」→ 檔案 mtime（md 解析為 best-effort，失敗即退 mtime） |
| GET | `/api/weeks/{week}/candidates` | `Row[]`（§2.3 schema） | 主候選股 |
| GET | `/api/weeks/{week}/holdings` | `Row[]`＋持股欄 | API 回明文；**遮罩在前端**（§5.1）|
| GET | `/api/weeks/{week}/watchlist` | `Row[]` | 觀察清單 |
| GET | `/api/weeks/{week}/sectors` | `SectorRow[]` | sector_rotation |
| GET | `/api/weeks/{week}/themes` | `ThemeRow[]` | theme_strength |
| GET | `/api/weeks/{week}/screens/{id}` | `ScreenRow[]` | `id ∈ d/e/f/g`，後端映射檔名（`d`→`screen_result_d_quality_leader.csv` …）|
| GET | `/api/weeks/{week}/stock/{stock_id}` | 彙整單檔（見下） | 跨表 join |
| GET | `/api/weeks/{week}/narrative/{name}` | `{markdown: "..."}` | name ∈ pick/group_analysis/sector_rotation/screen_log |

**`/stock/{stock_id}` 彙整**：合併該股在 candidates/holdings/watchlist 的資料（**同欄衝突時優先序
holdings > candidates > watchlist**）、命中哪些 screen（掃 d/e/f/g 四檔、**去重**後的命中集合）、
所屬族群在 sector_rotation 的位置與 quadrant（**該週無 sector_rotation 時此段回 null**）、Goodinfo
連結。三表皆查無此股回 404。

回應一律 Pydantic 驗證（**欄位皆 Optional**）；缺檔回 `404`＋明確 message（前端顯示「本週無此資料」），
缺欄回 `null`，**不丟 500**。

---

## 5. 前端頁面與元件規格

### 5.1 共用外殼（App Shell）

- 頂部：站名（戰情室）＋ **週次切換器**（動態週次清單，目前 W21/W22/W24/W25，預設 latest，切換全頁同步）＋ 分頁導覽。
- 左/頂導覽：候選股 ｜ 族群輪動 ｜ 個股 ｜ 持股。**切到該頁無資料的週次時顯示空狀態「本週無此資料」**（§2.4）。
- 右上：**Privacy 遮罩 toggle**（截圖友善、**純前端打碼**：把 holdings 的買價/市值/損益以 ● 遮蔽；API 仍回明文）。
- 全站數字採台股慣例紅漲綠跌、等寬字（§6）。

### 5.2 候選股頁（Candidates）— **MVP 首頁**

- **主元件：可排序/篩選資料表**（TanStack Table）
  - 預設欄（W25 完整）：`stock_id, name, industry, theme, strategy, momentum_5d_pct, ret_10d_pct,
    change_pct, close, vol_ratio, ma20_dist_pct, ma60_dist_pct, pe_ratio, dividend_yield_pct,
    val_pctile, rev_yoy_pct, inst_net_lots, foreign_net_lots, trust_net_lots, flags`
    —— **舊週缺的欄位（如 W22/W24 無 `ret_10d_pct/dividend_yield_pct/val_pctile`）整欄顯 `—`**。
  - 欄位可顯隱、排序；篩選：**策略(D/E/F/G，採「成員包含」比對：`D+E+F` 同時命中 D/E/F)**、
    產業/主題、`cheap_flag`、`flags` 標籤、數值區間。
  - `change_pct/momentum` 等漲跌欄套紅漲綠跌；`flags`（過熱/高PE/強勢領頭…）做彩色 chip。
  - 點 row → 個股 detail 頁。
- **衍生圖（§7）**：動能 × 估值散佈（M-Dash 1）＋ 法人買賣超 bar。
- **敘事側欄**：`pick.md` 渲染（執行摘要、可動作清單、風險）。

### 5.3 族群輪動頁（Sectors）

> **資料事實**：`sector_rotation.csv` 目前**僅 W25 有**，其餘週此頁主視覺顯空狀態（§2.4）；
> `theme_strength.csv` 有 W24/W25。

- **族群資金熱力圖**（§7-1）為主視覺。
- **主題/次產業強度排行**：theme_strength 表（lead_score 排序，`rank_delta` 上升紅/下降綠箭頭）。
- 每族群可展開成員股（連回候選股/個股頁）。
- 敘事分頁：`sector_rotation.md`、`group_analysis.md`。

### 5.4 個股 detail 頁（Stock Drill-down）

按 CLAUDE.md Part 3.2 報告框架排版（HUD 卡片化）：

1. 頭部卡：股號/名稱/產業/主題、close、change（紅綠）、Goodinfo 連結。
2. 基本面卡：rev_yoy / gross_margin / eps_q / pe / pb / 殖利率 / val_pctile / cheap_flag。
3. 籌碼卡：近 20 日三大法人、外資 5/10/20 日、投信、`inst_pct20d`、量比。→ 法人 bar。
4. 技術卡：close 相對 MA20/MA60、20/60 日高低**區間條**（非 K 線，見 §8）。
   **`low/high_20d/60d` 僅 W25 有**，舊週此卡降級為僅顯均線距離。
5. 族群相對位置：所屬族群 radar_rank / quadrant、本檔 `rank_in_group`。
6. 命中策略徽章（D/E/F/G）＋ `flags`。
> **不做**多空結論、目標價（守 Part 3.5）。

### 5.5 持股損益頁（Holdings）

- 損益表：`buy_price, close, return_pct, market_value_k`（紅綠）。
- **停損距離**：close vs `ma60_price`（CLAUDE.md「停損為絕對 MA60 價」）→ 距離%與燈號。
- **除權息提醒**：**以 holdings 自身 `ex_div_cash` ＞ 0 為主**（group_analysis §0.5 表只列候選股、
  未必涵蓋持股，僅用來補除權息日期）；標未來 ~2 週除權息。
- 受前端 Privacy toggle 控制；此頁資料**永不進 git**（`reports/*` 已在 .gitignore）。

---

## 6. 視覺設計系統（War-Room HUD）

### 6.1 Design tokens

```
背景  bg        #0b0f17     面板  panel    #111826     格線/邊框 #1c2738
主色  accent    #18e0c8 (teal)            預警  warn   #ffae00 (amber)
文字  text      #9fb3c8     次文字 muted   #5d6b7e      高亮文字  #e6f0f7
─ 漲跌（台股慣例）─
漲    up        #ff4d5e (紅)   跌  down   #2bd576 (綠)   平  flat  #9fb3c8
```

- 字體：數字/代號用等寬（JetBrains Mono / IBM Plex Mono）；標籤用無襯線。
- 風格：細線、無重圓角、高資訊密度、冷色基調，**預警色（橙/紅）只當重點提示**（觸發進場、過熱、停損逼近）。
- 微互動：hover 細微 teal 發光描邊、掃描線/格線背景；克制，不喧賓奪主（這是分析工具不是遊戲）。

### 6.2 顏色語意規則

- 漲跌、報酬、法人買超（紅）/賣超（綠）一律台股慣例。
- z-score / 熱力圖：用 teal→透明→amber 雙向色階表「資金流入/流出」，**與漲跌紅綠分離**避免混淆。
- `flags` chip：過熱=amber、高PE=amber、強勢領頭=teal、停損逼近=alert 紅。

---

## 7. 優先圖表規格（ECharts，暗色主題）

1. **族群資金熱力圖**：列＝次產業（依 `radar_rank`），欄＝`net_flow_5d/20d_z, foreign_*_z,
   trust_*_z, flow_breadth_*`，色＝z-score（teal↔amber 雙向）。配 `quadrant`（主升續勢/加速…）
   標記與 `entry/confirm_triggered` 圖示。
2. **動能 × 估值散佈**：x＝`val_pctile`，y＝`momentum_5d_pct`，點色＝strategy 或族群，點大小＝
   `amount_million`，四象限參考線（便宜×強勢 / 貴×強勢…）。hover 顯個股、點擊進 detail。
3. **法人買賣超 bar**：分群/雙向長條，`foreign_net_lots / trust_net_lots / inst_net_lots`，可切
   5/10/20 日；買超紅、賣超綠。個股頁與族群頁共用。
4. **估值/殖利分布**：`pe_ratio / pb_ratio` 的 `val_pctile` 分布 ＋ `dividend_yield_pct` 直方圖，
   標 `cheap_flag` 群落。

---

## 8. 資料限制與「不做」清單（先講清楚，免得期待落空）

- **沒有逐日 K 線**：reports 是每週快照，只有 MA20/MA60、20/60 日高低、法人張數。技術面用
  **區間條＋均線距離**呈現，**不畫 candlestick**。要 K 線＝另開「擴充逐日資料源」的研究軌（本規劃書不含）。
- **跨週比較**先只用 CSV 既有的 `rank_delta`（族群/主題排名變化）；完整跨週 join 趨勢列為 stretch。
- **不下單一結論、不報目標價、不省略風險**（守 CLAUDE.md Part 3）。dashboard 只呈現事實與既有敘事。
- **不寫回 reports**、不改分析程式、不引入 DB / pandas / 重前端狀態庫。
- **持股資料只在本機**，可一鍵**前端遮罩**（防截圖；API 仍回明文，非機密級防護），永不進 git
  （`reports/*` 已在 .gitignore；`frontend/dist`、`node_modules` 需加入 .gitignore）。
- **服務只綁 `127.0.0.1`**（不綁 `0.0.0.0`），避免 WSL/區網意外暴露無驗證的持股損益。
- **缺檔/缺欄/缺週**統一空狀態、不丟 500（規則見 §2.4）。

---

## 9. Milestone 拆解（前綴 M-Dash，依序做、做完停下驗收）

> 遵循 [docs/08](08-milestones.md) 規範：一次一個、列完成清單、等使用者說「下一個」。

### M-Dash 0：骨架 ＋ 候選股表（端到端）← **先做這個**
- **目標**：打通 reports→FastAPI→React 的最小垂直切片。
- **範圍**：`pyproject.toml`(＋fastapi,uvicorn)、`Makefile`(dash-*)、`src/tw_screener/webapp/`
  (app/server/data_access/schemas/routers: weeks+tables 的 candidates)、`frontend/` 初始化
  (Vite+React+TS、HUD tokens、App Shell、WeekSwitcher、DataTable、Candidates 頁)、`.gitignore`。
- **成功標準**
  - [ ] `make dash-install` 安裝前後端依賴成功
  - [ ] `make dash-dev` 同時起 FastAPI(:8000)＋Vite(:5173)，瀏覽器開得到
  - [ ] `GET /api/weeks` 回**實際週次清單（W21/W22/W24/W25，無 W23）**、`latest=2026-W25`；
        `/api/weeks/2026-W25/candidates` 回 ~140 列
  - [ ] 候選股頁顯示可排序/篩選表、週次切換可用、紅漲綠跌正確；**切到 W21（無 candidates）顯空狀態不報錯**
  - [ ] `make dash-build && make dash` 單一 FastAPI 進程服務 build 後前端＋API
- **驗收**：`make dash-dev` 後人工開頁確認；後端 `pytest` 有 weeks/candidates happy-path test。

### M-Dash 1：候選股圖表 ＋ pick.md 敘事
- 動能×估值散佈、法人買賣超 bar、估值/殖利分布；`pick.md` 側欄渲染（react-markdown）。
- 成功標準：三圖渲染正確、點散佈點可跳個股頁、敘事側欄可開合。

### M-Dash 2：族群輪動頁
- 族群資金熱力圖、主題強度排行（rank_delta 箭頭）、成員展開、`sector_rotation.md`/`group_analysis.md` 分頁。
- **資料事實**：熱力圖/輪動敘事**僅 W25 有資料**，其餘週驗收以「空狀態正確」為準。
- 成功標準：熱力圖色階與 quadrant 標記正確、排行可排序、敘事可讀。

### M-Dash 3：個股 detail 鑽取
- `/api/.../stock/{id}` 彙整端點；detail 頁五卡＋策略徽章＋Goodinfo 連結＋區間條技術視覺。
- 成功標準：任一候選股/族群成員可鑽取，缺資料顯「未取得」不報錯。

### M-Dash 4：持股損益頁 ＋ Privacy 遮罩
- holdings 損益表、停損距離(MA60)燈號、除權息提醒、Privacy toggle（全站持股打碼）。
- 成功標準：損益正確、遮罩開關有效、此頁不影響 git 狀態。

### M-Dash 5（stretch）：週對週比較 ＋ 收尾
- 跨週 join 趨勢、meta 頁、效能與 RWD 收尾。視需要再決定是否做。

---

## 10. 開發/驗收指令（Makefile 草案）

```bash
make dash-install   # uv sync ＋ (cd frontend && npm install)
make dash-dev       # 併發起 FastAPI(:8000) ＋ Vite(:5173, proxy /api)
make dash-build     # cd frontend && npm run build → frontend/dist
make dash           # uv run tw-screener serve（FastAPI 服務 dist ＋ /api，:8000）
make dash-test      # 後端 pytest（webapp 路由 happy path）
```

---

## 11. 開放問題 / 待你確認

**2026-06-22 已定案（本次修訂併入規格）**：
- **缺資料週/頁**：顯示空狀態「本週無此資料」並保留該週於切換器（不隱藏、不自動回退）。見 §2.4。
- **策略代號**：API 用短代號 `d/e/f/g`、後端映射檔名；候選股策略篩選採「成員包含」比對。見 §4/§5.2。
- **Privacy 遮罩**：純前端打碼（API 回明文）。見 §5.1/§8。
- **除權息來源**：以 holdings 自身 `ex_div_cash` 為主、`group_analysis §0.5` 補日期。見 §5.5。
- **字體**：JetBrains Mono / IBM Plex Mono（OFL 開源可商用），本地打包不走 CDN。

**2026-06-22 補充定案**：
- **watchlist**：dashboard **只用 `reports/<週>/watchlist_enriched.csv`**，不呈現原始 `watchlist/watchlist.csv`。
- **舊週缺欄不回補**：W22/W24 缺的 15 欄維持「缺欄顯 `—`」，**不動分析程式回補舊週**。

---

> 本規劃書依 2026-06-22 三輪規格討論定案，並於同日依實地核對 `reports/` 資料修訂（週次校正、
> schema 演進、缺檔矩陣，及缺資料呈現/策略代號/遮罩深度三項定案）。實作仍 milestone-driven，先做
> M-Dash 0，完成後停下驗收。修改範圍/新增依賴前先與使用者確認（守 CLAUDE.md Part 4）。
