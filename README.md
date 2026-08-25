# 台股波段選股與分析系統 (TW Stock Screener)

> 半自動化的台股每週流程：選股 → 次產業資金輪動 → 族群分析 → AI 挑股 → 個股深度報告
> 個人工具，非投資建議。

## 一句話說明

每週把 1800 檔台股 → 篩成 ~150 檔候選 → 疊上「全市場資金往哪流」的輪動地圖 → AI 蒸餾出 5-10 檔
＋進場價/停損 → **你下決策**。

---

## 系統怎麼運作（全貌）

```
                    ┌─────────────── 資料層（全部本地 parquet 快取）───────────────┐
  TWSE/TPEX OpenAPI │ 日線 daily_*  法人 institutional_*(上市+上櫃)  月營收  產業別  官方估值比 valuation_ratios_* │
                    │ 融資融券 margin_*(上市)  財報體質 fundamentals_*(負債比/ROE/純益率)   │
  TDCC 集保         │ 大戶持股比 tdcc_*（≥400 張/≥1000 張＋WoW，規劃書 02 D3）        │
  Goodinfo（限速爬蟲）│ 策略篩選結果（YAML 條件 → URL → HTML → CSV）                │
  Yahoo 概念股       │ config/concepts.yaml 主題標籤（手標次產業＋自動爬概念股）      │
                    └──────────────────────────┬───────────────────────────────┘
                                               ▼
 make week GROUP=defg ＝ 一條指令串起以下十四步：
 ① fetch-twse                日線/法人/月營收/產業別/官方估值比(PE/PB/殖利率) 增量入快取
 ② fetch-institutional-history 回補近 20 日上市＋上櫃法人（隔幾天沒跑也自動補齊）
 ③ fetch-tdcc                集保大戶持股比（容錯：TDCC 異常不擋，大戶欄退化 null）
 ④ doctor                    Goodinfo 健康檢查（只診斷不擋，2026-08-23 拍板，docs/31 §19.3；⑤仍會嘗試執行）
 ⑤ screen-all GROUP=defg     Goodinfo 跑 D/E/F/G 四策略 → screen_result_*.csv（純快照；被擋時逐策略印「本週未取得」，不中止流程）
 ⑥ screen-redesign-local     docs/31 §4 全新本地filter(G1/G2/G4/G5/L6)，不打Goodinfo→screen_result_*.csv（2026-08-24拍板直接掛進主流程，**實驗性質未過統計驗證**，容錯：失敗不擋）
 ⑦ fetch-candidates-history  對命中股聯集（含⑤⑥）補抓 13 個月個股日線（MA60/量比/動能用）
 ⑧ rotation                  ★ 次產業輪動（全市場宇宙・價格趨勢分數主鍵＋趨勢領頭板）→ sector_rotation.md/csv
 ⑨ macro                     docs/25 v2 總經燈號（BAA10Y 主訊號＋揭露面板，容錯：FRED 掛了不擋主流程）
 ⑩ cp-value-candidates       個股 CP 補漲候選＋C2 三重濾網 → cp_candidates.md（group Section 6 要讀）
 ⑪ group                     族群分析（候選股宇宙）→ group_analysis.md ＋ candidates_enriched.csv（含揭露欄；docs/31 §13 官方族群前5＋§4/§9/§11 G1/G2/G4/G5/L6 新設計候選觀察欄與前瞻累積軌皆已內含）
 ⑫ snapshot-week             point-in-time 週快照：凍結 concepts/宇宙/持股 → data/snapshots/（容錯：失敗不擋）
 ⑬ week-check                產物完整性檢查：本週機器產物＋歷週 pick 底帳，缺者 WARNING（不擋流程）
 ⑭ pick-outcome-brief        上週 picks r+5/α/勝率＋偽陰性一頁 → 本週輸入包（容錯：失敗不擋）
                                               ▼
 手動：把報告貼給 Claude（docs/11 prompt）→ pick.md（首屏 ≤60 行一頁決策卡；核心層距季線 >+15% 硬擋）
 手動：tw-screener picks sync 解析 pick.md 尾端區塊、整批寫底帳 → 每季 make pick-outcome 算命中率×α（pick 閉環）
 手動：make report STOCK_ID=XXXX → 個股深度報告
```

> **候選宇宙現含兩種來源，規模差很大**：⑤ Goodinfo D/E/F/G（~87 檔）＋⑥ docs/31
> 本地 G1/G2/G4/G5/L6（2026-08-24 拍板：不用全部跟隨 Goodinfo，用現有本地資料
> 做 filter，直接掛進主流程，接受未驗證/結果可能不理想——實測單週命中數
> g1=107／g2=44／g4=351／g5=21／l6=281 檔，門檻目前很鬆）。兩者一起流進
> `candidates_enriched.csv`，`strategy` 欄用 G1/G2/G4/G5/L6 標籤跟 Goodinfo 的
> D/E/F/G 明確區分（見 `screen_result_*.csv` 的 `source` 欄：Goodinfo 來源無此欄、
> F 本地替代路徑是 `local`、G1/G2/G4/G5/L6 是 `local_unvalidated`）。**候選數因此
> 會比純 Goodinfo 時代明顯變大**，屬預期行為。
>
> `candidates_enriched.csv` 的 `redesign_watch` 欄（docs/31 §4/§9/§11，2026-08-24
> 新增）＝G1/G2/G4/G5/L6 命中旗標（逗號分隔，未命中留白）——純觀察揭露，不影響
> 排序/篩選/pick.md 核心層，跟⑥的候選生成是兩條獨立機制（前者是揭露欄，標註任何
> 候選股是否也命中這五式；後者是這五式自己產生候選股）。**全部未經統計驗證**
> （G3 已驗證未過關，不在此欄）。`research/g1_g2_g5_watch/`／
> `research/l6_g4_watch/` 底帳（gitignored）跟著 `group`（步驟⑪）自動累積，
> `make l6-g4-watch`／`make g1-g2-g5-watch` 只是可單獨重跑的手動工具。

兩個分析宇宙刻意不同、互相校驗：

|      | ⑪ 族群分析（group_analysis.md）             | ⑧ 資金輪動（sector_rotation.md）          |
| ---- | -------------------------------------------- | ------------------------------------------ |
| 宇宙 | **本週篩中的候選股**（精、有選擇偏誤） | **全次產業成員**（無偏、含未入選股） |
| 鏡頭 | 漲幅/breadth/法人（候選股之間比強弱）        | 20 日法人資金流時間序列＋位階象限          |
| 回答 | 「篩中的股裡，哪群在跑、誰帶頭」             | 「全市場資金往哪流、下一棒可能是誰」       |

兩邊在 `group_analysis.md` **Section 2.8 並列對照**（雷達 lead_score × 輪動 Rank/象限）：
**同強＝最強確認；雷達強輪動弱＝只有篩中股在動（防單檔灌水）；輪動強雷達弱＝資金已進但
候選未跟上（更早期訊號）**。

---

## 首次設定（一次性）

```bash
make sync && make init          # 裝依賴（uv）、建目錄/.env
```

接著兩個**個人化設定**——不設也能跑 `make week`，但設了 AI 分析會更貼你的實際部位：

**① 我的庫存／觀察清單**（放 `watchlist/`，已 gitignore、不外流）
建這兩個 CSV，`make week` 會自動 enrich（算報酬率/MA60 停損價）、標進輪動「我的參與度」，
貼給 Claude 時走 prompt 任務 0（庫存給續抱/加碼/減碼/停利/停損、觀察給進場時機）：

```
watchlist/holdings.csv     stock_id,buy_price,shares,note    # 例：2330,1050,2,核心持股
watchlist/watchlist.csv    stock_id,note                     # 例：3035,等回測季線
```

> 只有 `stock_id` 必填，其餘可空；沒建這兩檔 → 跳過庫存/觀察 enrich，主流程照常跑。

**② 次產業標籤 `config/concepts.yaml`**（已預先標好、開箱即用）
每檔股票的「次產業＋概念股」多標籤（並存於 TWSE 官方 28 類之上）。**已內建**電子細分
（記憶體/記憶體模組/IC設計/封測/晶圓代工…）＋金融＋航運＋15 個概念股主題，**第一次就能跑、
不必先填**。次產業是**輪動引擎的分群依據**——想拆更細或修正分類時，**手動編 `concepts:` 段**
（`股號: 標籤` 或 `股號: [標籤, ...]`，一檔可多標籤）；`make group` 末尾會列出「電子股未標
次產業」提醒你增量補。概念股標籤則由 `make build-themes` 自動爬 Yahoo 維護。詳見 [§7](#7-主題模型維護configconceptsyaml)。

## 何時跑 `make week`

- **推薦時段**：交易日收盤後 **15:00 起**（TWSE T86 法人 ~15:00 穩定，Goodinfo 也同步更新完成）
- **週六/日 / 週一早上跑也 OK**：自動對齊「最近一個交易日」，報告落在正確的 `reports/YYYY-Www/`
- **跨日重複跑**：第二次走 cache，不會重抓
- 詳見 [docs/02-data-sources.md](./docs/02-data-sources.md) 的「最早可用時點」表

## 主流程：一條指令 `make week GROUP=defg`

平時**只需要這一條**（整合最完整的主流程，含資料抓取＋法人回補＋集保大戶＋Goodinfo 健檢＋篩選＋資金輪動＋個股 CP 補漲＋族群分析＋週快照＋產物檢查）：

```bash
make week GROUP=defg          # defg 為現行唯一主流程；abc/def 已退役（規劃書 04 A4）
```

跑完產出全在 `reports/YYYY-Www/`，分兩類：

**📋 要貼給 Claude 分析的（配合 [docs/11](./docs/11-propicks-analysis.md) 範本 prompt）**

| 檔案                                                   | 是什麼                                                                                                                                                                           |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `group_analysis.md`                                  | 族群脈絡＋強度排名＋次產業/概念股/輪動雷達＋給 Claude 的分析請求（Section 5 次產業深度、6 CP 補漲、7 持有/觀察健檢）                                                             |
| `sector_rotation.md`                                 | 全市場輪動地圖（**價格趨勢分數主鍵排序**＋流量確認欄/四象限/★訊號/週對週 ΔRank/**趨勢領頭板**，**無入選偏誤**，對照 group Section 2.8）                      |
| `candidates_enriched.csv`                            | **主要挑股宇宙**：全候選股 × 技術/籌碼/估值（官方 PE/PB/殖利率＋次產業相對便宜位階）/月營收/flags 排雷欄/揭露欄（flow_state・risk_kind・pullback_quality，純揭露非 gate） |
| `cp_candidates.md`                                   | 個股 CP 補漲候選＋三重濾網（錢進＋沒漲＋相對便宜；埋伏/追突破/反轉三型態）＋末段短窗早訊號／過熱-退潮警示（限庫存/觀察・低信心）                                                 |
| `holdings_enriched.csv` / `watchlist_enriched.csv` | 我的庫存/觀察清單（有維護才產，**無論如何都要分析**）                                                                                                                      |
| `screen_result_*.csv`                                | 各策略原始入選快照（看「哪檔中哪些策略」用）——`{d,e,f,g}` 是 Goodinfo 四策略；`{g1_margin_expansion,g2_quality_no_history,g4_yoy_divergence,g5_valuation_gap,l6_yoy_pe_flow}` 是 docs/31 §4 本地新設計候選（不打 Goodinfo，`source=local_unvalidated`，**未過統計驗證**） |

**⚙️ 不必貼**：`theme_strength.csv`（內容已在 Section 2.8）、`screen_log.md`（檔數統計）。

**送給 Claude 的流程**：把上表 6 類貼進 Claude 網頁對話 → 套 [docs/11](./docs/11-propicks-analysis.md) 範本 prompt → Claude
產出 `pick.md`（精選進場清單，四路匯流：族群深度＋全宇宙掃描＋CP 補漲候選＋觀察清單升格）→ 對 picks 內每檔
`make report STOCK_ID=XXXX` 產個股深度報告。

## 每週指令速查

首次設定做完後，平時就這幾條（產出與貼 Claude 細節見上方「主流程」）：

```bash
make week GROUP=defg                              # ①~⑭ 一鍵跑完（尾段 week-check 缺產物自動 WARNING＋pick-outcome-brief）
# 貼給 Claude 的 6 類檔（全在 reports/YYYY-Www/，詳見上方主流程表）：
#   group_analysis.md  sector_rotation.md  candidates_enriched.csv
#   cp_candidates.md  holdings/watchlist_enriched.csv  screen_result_*.csv
# → 套 docs/11 範本 prompt → 得 pick.md（首屏 ≤60 行一頁決策卡＋尾端機器可讀區塊）
uv run tw-screener picks sync --week 2026-Www     # pick.md 定稿後整批寫底帳（閉環輸入，§10；單檔補記用 picks record）
make report STOCK_ID=2330                         # 對 picks 選出的每檔產個股深度報告（5-10 秒）
make pick-outcome                                 # （每季）pick 閉環：分層命中率×α＋偽陰性帳＋停損延遲帳 → research/pick_outcome/
make dash-dev                                     # （選配）把本週報告開成可視化儀表板瀏覽（首次先 make dash-install）→ §13
```

> **個股報告兩模式**：設了 `ANTHROPIC_API_KEY` → `make report` 直接產完整分析；沒設 → 產資料草稿，貼 Claude 對話依範本補寫。

**每週 SOP 詳解 → [docs/10-sop.md](./docs/10-sop.md)**　|　**完整挑股 prompt → [docs/11-propicks-analysis.md](./docs/11-propicks-analysis.md)**　|　**問題排解 → [docs/99-troubleshooting.md](./docs/99-troubleshooting.md)**

---

## Dashboard（投資戰情室・選配）

把 `make week` 產出的 `reports/YYYY-Www/` 開成本機可視化 HUD——**只讀報告、不抓資料也不寫檔**，
與「貼給 Claude」並行的另一條消化路徑。頁面細節（候選股／族群輪動／個股鑽取／持股損益）見下方
**§13 功能詳解**、完整規格 [docs/17-dashboard-spec.md](./docs/17-dashboard-spec.md)。

```bash
make dash-install        # 首次一次：基礎 make sync 之外多裝前端依賴（＝ uv sync ＋ cd frontend && npm install）
make week GROUP=defg      # 先有本週報告（dashboard 只是 reports/ 的瀏覽器，沒報告就沒東西看）
make dash-dev            # 起 FastAPI(:8000)＋Vite(:5173)，瀏覽器開 http://localhost:5173；Ctrl-C 一起關
```

> 自用正式跑（單一進程、免 Vite dev server）：`make dash-build && make dash`（:8000）。
> 註：首次設定的 `make sync && make init` 只裝 Python 依賴（含後端 FastAPI），**前端 npm 需另跑 `make dash-install`**。

---

## 指令總覽

### 主要（每週實際用的三個；`make help` 只列這三個）

| 指令                                                     | 做什麼                                               | 何時用                               |
| -------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------ |
| `make week GROUP=defg`                                 | 完整週流程 ①~⑭                                     | **每週一次（主入口）**         |
| `make pick-outcome`                                    | pick 閉環：分層命中率×α（vs 大盤＋族群）＋偽陰性帳＋**停損延遲帳（M3.1）** | 每季（pick 底帳變厚後）              |
| `make dash-dev`                                        | 起 dashboard 開發伺服器（FastAPI:8000＋Vite:5173）   | 視覺化瀏覽本週報告（§13）           |

### 進階（偶爾手動跑；均保留可用，`make help` 不列）

| 指令                                                     | 做什麼                                               | 何時用                               |
| -------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------ |
| `make weekend GROUP=defg`                              | week ＋ git commit/push 結果                         | 想自動存檔時                         |
| `make rotation`                                        | 次產業資金輪動報表（單獨重跑）                       | 盤後想單看資金流向                   |
| `make group`                                           | 族群分析（單獨重跑，吃既有 CSV）                     | 改 concepts.yaml 後重產報告          |
| `make l6-g4-watch`                                     | docs/31 §9 前瞻累積軌：記錄本週快照（PE/月營收YoY/投信買超），只記錄不裁決 | week 已容錯內含，可單獨重跑          |
| `make g1-g2-g5-watch`                                  | docs/31 §11 前瞻累積軌：記錄本週快照，只記錄不裁決   | week 已容錯內含，可單獨重跑          |
| `make report STOCK_ID=2330`                            | 單檔個股深度報告                                     | picks 選出後逐檔深掘                 |
| `make screen STRATEGY=d_quality_leader`                | 跑單一策略                                           | 調策略 YAML 後測試                   |
| `make screen-dry STRATEGY=…`                          | 只組 Goodinfo URL 不打網                             | 驗證 YAML 條件                       |
| `make rotation-calib`                                  | ★ 起漲點回測校準（研究軌）                          | 每季重校準訊號門檻                   |
| `make cp-value-valuation`                              | 個股相對 PE 估值表（次產業橫斷面）                   | 估值位階單獨重看                     |
| `uv run tw-screener picks sync --week …`              | 解析 pick.md 尾端區塊，整批寫 picks.csv／excluded.csv 底帳（單檔補記用 `picks record`） | 每週 pick.md 定稿後                  |
| `uv run tw-screener picks outcome --diff`              | pick-outcome ＋翻轉解剖（週對週降級＋翻轉前訊號）    | 個案覆盤                             |
| `make backtest-strategies`                             | 回測 D/E/F/G 入選後勝率/報酬/回撤 vs 大盤            | 每季（規劃書 03 V1）                 |
| `make diagnose`                                        | 抓太晚＋漏起漲診斷（研究軌）：延伸度曲線/漏抓五態雷達（docs/19） | 每季／窗變厚後重跑                   |
| `uv run tw-screener market regime`                     | 大盤 regime 姿態：進攻/中性/防禦（規劃書 03 V2）     | 盤後看大盤閘門                       |
| `uv run tw-screener market washout [--save]`           | M2 投降洗盤偵測：融資投降/廣度washout/資金極端/指數深負乖離四子項＋是否觸發（**反向 flag**、不改燈色排序；門檻未校準、前 4 週僅描述） | 洗盤疑似落底時單獨看；`make week` 已內含 |
| `uv run tw-screener market macro-risk`                 | M8 宏觀窄橋：讀輸入包 `macro_risk_latest.yaml`，印三態（ok/missing/stale/invalid）＋倉位節奏結論（**不影響選股**） | 貼輸入包前確認本週有無宏觀 gate |
| `make macro`（＝`uv run tw-screener market macro`） | 總經燈號：BAA10Y 單訊號＋揭露面板＋各指標「較上次」變化（外生風險，docs/25 M-Macro1／M-Macro4） | week 已容錯內含，可單獨重跑；`--refresh` 強抓 |
| `uv run tw-screener portfolio check`                   | 組合層風控體檢：標籤/因子簇集中度（規劃書 03 V3）    | 持股變動後                           |
| `make week-check`                                      | 產物完整性檢查（week 已內含，可單獨重跑）            | 懷疑某步無聲失敗時                   |
| `make build-panel`                                     | ground-truth 面板 parquet：前瞻報酬/位階/法人/量比＋核價（docs/22 WS-A） | 研究軌任何因子驗證前                 |
| `make factor-lab`                                      | 因子實驗台驗收：機器等價＋docs/19 對表＋面板首驗（docs/22 WS-B） | 面板重建後                           |
| `make rotation-efficacy`                               | 輪動欄效度：歷史重建→生產對表→forward basket IC/lift（docs/22 WS-C；`backtest rotation-efficacy --membership official` 出官方產業別 robustness 版） | 每季                                 |
| `make laggard-grid`                                    | 族群強弱×領先落後×位階 forward 報酬格（docs/22 WS-D；`--membership official` 同上） | 每季                                 |
| `make contrarian-efficacy`                             | 底部左側聯合桶（轉買×貼近低）forward alpha 檢驗＋§1 硬門檻裁決（docs/24 M-BR1 Phase 2） | 面板重建後／樣本變厚後重驗           |
| `make macro-regime-validate`                           | 總經燈號 as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 重測（docs/25 M-Macro2；讀 research/ raw，需先跑過三輪篩選研究） | 一次性驗證（Phase 2 已跑過）    |
| `make macro-grid-search`                               | 宏觀指標視窗/門檻/組合 grid search，對3個已知事件測早期反應（docs/31 §23.4 Part 4；已跑，0候選，結論見§23.5，不可升級為決策依據） | 一次性研究（已跑過，backlog closed） |
| `make flow-inflection`                                 | 資金流 inflection 因子族首驗（docs/22 WS-E）          | 樣本變厚後重驗                       |
| `make margin-factors`                                  | 融資減肥/大戶 WoW/margin_to_vol 三籌碼因子首驗（預註冊 docs/23 §3） | 面板重建後                    |
| `make regime-history`                                  | regime 標籤歷史化：V2 引擎逐日 as-of 重算（2022-01 起，docs/23 WS-H.3） | 面板延伸前（先產標籤）        |
| `make snapshot-week`                                    | point-in-time 週快照：凍結 concepts/宇宙/持股到 data/snapshots/（docs/23 WS-J.1） | week 已內含，可單獨重跑     |
| `make backfill-daily-history START=… END=…`         | bulk 逐日全市場歷史（TWSE MI_INDEX，一日一請求；上櫃無 bulk 走逐檔） | 面板延伸冷啟動（比逐檔快）    |
| `make backfill-institutional-history START=… END=…` | 逐日上市法人歷史（TWSE T86，一日一請求；顯式起迄不依賴 latest 錨點） | 面板法人冷啟動（build-panel 前）|
| `make doctor`                                          | Goodinfo 健康檢查（week 已內含，只診斷不擋，可單獨重跑）| 懷疑被擋/改版時                      |
| `uv run tw-screener screen run-local f_value_rebound`  | Goodinfo 被擋時的手動退路：純用 TWSE/TPEX 官方快取跑 F 策略（唯一目前可完全本地化的策略，docs/31 §19.3；D/E/G 因表外條件無法本地化，未接進 `make week`） | doctor 顯示 BLOCKED 時想至少拿到 F 的候選 |
| `make fetch-tdcc`                                      | TDCC 集保大戶持股比（week 已內含）                   | 大戶欄空值時單獨補                   |
| `make fetch-twse`                                      | 增量抓日線/法人/月營收                               | 通常不必單獨跑（week 含）            |
| `make fetch-stock STOCK_ID=2330`                       | 抓單檔完整資料                                       | 臨時看一檔沒快取的股                 |
| `make fetch-institutional-history DAYS=20`             | 回補近 N 日法人（上市 T86＋上櫃 3itrade_hedge，皆可回查歷史） | 法人快取斷檔時                 |
| `make fetch-margin-history DAYS=20`                    | 回補近 N 日上市融資融券（舊版 MI_MARGN，可回查歷史） | 融資融券快取斷檔時                   |
| `make backfill-universe-history`                       | 一次性回補全次產業成員日線（8-12 小時可續跑；`START=2022-01-01` 可指定起始月覆蓋 `MONTHS`） | 新環境冷啟動 / 面板延伸（§12）  |
| `uv run tw-screener data prune-cache`                  | 依 settings.cache 保留窗清舊快取                     | 快取肥大時                           |
| `make build-themes`                                    | 爬 Yahoo 概念股更新 concepts.yaml                    | 每月或新題材出現時（`DRY=1` 預演） |
| `make audit-concepts`                                  | 清查 concepts.yaml 無價成員（不改檔）                | 久久檢查興櫃/下市/誤標               |
| `bash scripts/fetch_cron.sh`                           | 盤後抓全市場資料（cron 用，見 §12）                 | 每交易日（排程或手動）               |
| `make dash-install`                                    | 裝 dashboard 前後端依賴（uv＋npm，首次一次）         | 第一次用儀表板                       |
| `make dash-build && make dash`                         | build 前端＋單一 FastAPI 服務（:8000）               | 自用正式跑、不需 Vite                |
| `make test` / `make lint` / `make typecheck`       | 測試 / ruff / mypy                                   | 開發時                               |
| `uv run tw-screener sector universe --list`            | 列出次產業宇宙與成員                                 | 檢查 concepts.yaml 覆蓋              |
| `uv run tw-screener sector universe --audit`           | 列出近日無價的次產業成員                             | 清 concepts.yaml 前先看              |
| `uv run tw-screener sector flows --week current --dry` | 終端機直接印資金流排名                               | 不想開報表、快速看                   |

---

## 功能詳解

### 1. 次產業資金流向輪動（`make rotation`）

對標[台股資金輪動圖](https://www.cryptocity.tw/news/taiwan-stock-sector-rotation-map)的核心功能
（[docs/12-sector-rotation.md](./docs/12-sector-rotation.md)）。與選股無關地掃**全市場**：
每個次產業（`concepts.yaml` 手標、46 個）的全部成員，加總上市+上櫃三大法人淨額，算出：

- **價格趨勢分數（排序主鍵・規劃書 05 F3）**：籃子等權指數 vs 月/季線＋成員站上季線比例＋
  領頭股 RS 跨次產業百分位——**20 日流量降為確認欄**（流量排序天然落後）；
  與 group 2.8 雷達（篩中股鏡頭）矛盾時，以價格趨勢分數裁決（讀法見 docs/11）
- **趨勢領頭板**：全市場 RS 前 N 強＋所屬族群＋位階＋旗標——過熱/土洋對作**不剔除、只標註**，
  附風險預算（部位減半、移動停損）；被核心層位階紀律擋下的延伸股在這裡有合法出口
- **資金訊號（確認欄）**：5/10/20 日淨流（張）、flow_momentum（資金加速度）、breadth（淨買超成員比）、
  力度（法人淨買股數/成交股數＝集中度）、週對週 ΔRank
- **四象限**（資金軸＝20 日淨流正負 × 價格軸＝籃子距 60 日低點位階）：
  - 🟢 **下一棒**（流入×未漲）＝重點觀察
  - 🔵 主升續勢（流入×已漲）　🔴 出貨警訊（流出×已漲）　⚪ 冷卻觀望（流出×未漲）
- **★ 校準進場訊號**：投信 20 日資金流 z>1 且動能>0——不是拍腦袋，是用 1 年歷史、34 個
  起漲點回測校準出來的（觸發後 15 日內起漲命中率 ≈ 隨機 1.3-1.5 倍、中位領先 8-10 日）
- **我的參與度**：自動把 `watchlist/holdings.csv`、`watchlist.csv`、本週命中股逐檔標上
  所屬次產業的象限與資金方向——「我有沒有參與到下一棒」一眼看到

輸出 `reports/Www/sector_rotation.md`（人讀）＋ `.csv`（下週 ΔRank 與未來 UI 用）。
`make week` 已內含；單獨跑只要快取在就行（不打 Goodinfo）。

```bash
make rotation                                      # 產本週輪動報表
uv run tw-screener sector flows --week current --dry   # 終端機快速看前 10 流入
uv run tw-screener sector universe --list          # 次產業成員清單與 28 類對照覆蓋率
```

### 2. 起漲點回測校準（`make rotation-calib`・研究軌）

輪動訊號的門檻**從歷史資料回推**，不是手設（docs/12 §2.4）：

1. 對每個次產業籃子偵測「起漲點」＝低基期（距 60 日低 ≤3%）＋ 15 日內漲 ≥10%
2. 掃描所有資金訊號 × 門檻組合，統計：命中率（precision）、episode 覆蓋率（recall）、
   **lift（vs 隨機基率）**、領先天數
3. 產出 `research/rotation/calibration_YYYYMMDD.md`（gitignore，本地研究產物）
   ＋建議寫入 `settings.yaml` `rotation.entry_signal` 的數值

```bash
make rotation-calib                          # 用 settings 預設起漲定義
uv run tw-screener sector calibrate --x-pct 12   # 敏感度測試：只算強波段
```

**建議每季資料累積後重跑一次**，把新建議值更新進 `settings.yaml`（含校準日期註記）。
目前校準結論（2026-06）：`trust_flow_20d (z>1.0)+momentum` 最穩健；對照組敏感度
X=8% 時全訊號 lift→1.0，證明訊號只領先「有意義的波段」、不領先雜訊。

### 3. 選股篩選（`make screen-all GROUP=defg`）

YAML 驅動的 Goodinfo 條件篩選：`config/strategies/*.yaml` 定義條件 → 組 URL → 限速爬蟲
（≥3 秒間隔＋抖動、24h 快取、指數退避、concurrency=1，**合規底線見 docs/02**）→ 解析成 CSV。
新增策略只要寫 YAML 不用寫 Python（流程見 docs/03「新增策略的流程」）。

### 4. 族群分析（`make group`）

讀本週篩選 CSV ＋價量/法人快取，產 `group_analysis.md`：
Section 0 策略代號/除權息/總經事件、1 入選分布、2 族群強度排名（2.5 跨族群強勢股、
2.6 次產業強度、2.7 概念股題材、**2.8 輪動雷達＋全宇宙輪動並列**）、3 各族群前 3 名、
5 Claude 次產業深度分析請求（輪動雷達驅動）、6 Claude CP 補漲候選分析請求
（個股層・讀同夾 cp_candidates.md）、7 持有/觀察清單健檢請求（你的部位・與命中策略同等深度）；
同時產 `candidates_enriched.csv`
（全候選股 × 技術/籌碼/估值/flags 排雷欄，**AI 挑股的主要宇宙**）。

### 5. AI 挑股（手動・docs/11）

跑完 week 後把報告貼給 Claude 網頁版，用 [docs/11-propicks-analysis.md](./docs/11-propicks-analysis.md)
的範本 prompt 產 `pick.md`——**首屏 ≤60 行一頁決策卡**（姿態一行 → 持股動作表 → 核心每檔 ≤5 行 →
機會表 → 本週三風險），觀察清單/族群底稿/交集分析全部後置附錄（不刪資訊、只分層；規劃書 05 F4）。
**核心層位階紀律（規劃書 05 F2）**：距季線 >+15%（`settings.picks.core_ext_ma60_max_pct`，試行值、
F1 每季校準）**硬擋入核心**，改列趨勢領頭板（部位減半＋移動停損）。**多空並陳、不下單一結論**。
定稿後用 `tw-screener picks sync` 解析 pick.md 尾端機器可讀區塊（docs/11 交付結構第三層）、
整批寫進底帳（單檔補記用 `picks record`），餵 §10 的 pick 閉環。

### 6. 個股深度報告（`make report STOCK_ID=…`）

10 段固定框架（基本面/籌碼/技術/多方/空方/進場條件/不進場情境/族群定位/資料來源），
空方論點不得少於多方、禁目標價（playbook/60-analyst-persona.md）。有 `ANTHROPIC_API_KEY` 全自動；
沒有則產資料草稿、貼 Claude 對話補寫。

### 7. 主題模型維護（`config/concepts.yaml`）

每檔股票的「次產業＋概念股」多標籤（並存於 TWSE 官方分類）。**半自動**：

- **次產業（手標）**：電子細分（記憶體/記憶體模組/IC設計/封測/晶圓代工…）＋金融＋航運，
  直接編 `concepts:` 段。Yahoo 每主題只給 ~30 檔會截斷大次產業，故手標維持完整。
  **勿用外部批次匯入整碗覆蓋**（粗分類會併掉細桶）。
- **概念股（自動）**：`make build-themes` 爬 Yahoo（5G/AI/衛星…15 主題），只動概念股標籤、
  不動手標次產業；`DRY=1` 先看 candidate 再覆蓋。
- `make group` 末尾會列出「電子股未標次產業」提醒清單，增量補標。

### 8. 庫存／觀察清單（每次必分析）

```bash
# watchlist/holdings.csv   股號,買入價,股數,備註  ← 已 gitignore，不外流
# watchlist/watchlist.csv  股號,備註
```

維護後 `make week`（或 `make group`＋`make rotation`）自動：

- enrich 成 `holdings_enriched.csv`（＋報酬率/現值/MA60 停損價）、`watchlist_enriched.csv`
- 在 `sector_rotation.md`「我的參與度」逐檔標象限與資金方向
- Step 3 貼給 Claude 時走 prompt 任務 0：庫存給續抱/加碼/減碼/停利/停損、觀察給進場時機；
  **觀察清單判「可進場」者升格入挑股四路匯流（docs/11 任務 2 來源 D），與策略命中同權競爭核心/機會層**

### 9. 總經行事曆（`config/macro_calendar.yaml`）

FOMC/CPI/台股結算/法說等市場級事件 → `group_analysis.md` Section 0.6 → picks 的事件閘門
（事件落地前控倉）。內建排程全標 `verified: false`，**請依官方公告校對後改 true**、過期清掉。

### 10. 策略回測與 pick 閉環（`make backtest-strategies`／`make pick-outcome`）

兩層裁判，皆產 `research/`（gitignore 本地研究產物）；與 `rotation-calib`（次產業資金訊號校準）是三件不同的事：

- **策略層（規劃書 03 V1）**：`make backtest-strategies` 回測 D/E/F/G 入選後 2/4/8/12 週
  勝率/中位報酬/回撤 vs 大盤（除息還原、下市 null、未到期排除）→ `research/strategy_backtest/`。
- **pick 層（規劃書 05 F1）**：每週 `pick.md` 定稿後用 `tw-screener picks sync` 把 pick
  （core/opportunity/pool 分層）與**被旗標剔除股**寫進 `reports/<week>/picks.csv`／`excluded.csv` 底帳；
  `make pick-outcome` 算分層命中率×α（**同列 vs 大盤、vs 所屬次產業兩個超額**）＋**偽陰性帳**
  （被剔除股同窗報酬——過熱/土洋對作等旗標第一次有績效裁判）→ `research/pick_outcome/`；
  另含**停損延遲帳（M3.1）**——「訊號日掛條件單」vs「等下週報覆核」的執行價差，
  量測週頻節奏的**制度性最小延遲**成本（不含連續多週續抱的行為延遲；停損欄未寫絕對價者
  記 `unparsed`，該計數即 M7 patch-2「停損一律印可掛單絕對價」的達成率指標）；
  `uv run tw-screener picks outcome --diff` 附翻轉解剖（週對週降級標的＋翻轉前訊號）。
  樣本隨週數變厚，建議每季重算，並以結果校準 F2 位階門檻。

### 11. PoC：主動式 ETF 持股（`poc/active_etf/`）

候選新訊號源（主動式 ETF 每日持股異動）。資料公開但後端 geo-fence 台灣，完整抓取需在
台灣本機跑，目前擱置、與主流程隔離（見 `poc/active_etf/README.md`）。

### 12. 每日資料排程（cron · 法人可不靠它、全市場日線密度建議常駐）

> **法人**：「上櫃法人缺日不可回補」前提已推翻（commit 57ab1f7）——上櫃法人改用
> TPEX 舊版 `3itrade_hedge` 端點（吃民國日期、逐股回傳）**可逐日回補**。`make week` 已內含
> `fetch-institutional-history`（回補近 20 日**上市＋上櫃**法人），**隔幾天沒開機/沒跑也會自動補齊**
> → 就**法人完整度**而言 cron 非必要。
>
> **全市場日線**：`STOCK_DAY_ALL` / `otc_daily_all` 的 `date` 參數被無視、**只能往未來累積、過去補不回**
> （docs/02）。rotation z 需 ~60+ 日、calibration 需 ~250 日 → 新環境若不每日累積，前幾個月歷史窗偏短、
> 訊號統計意義有限（報表頭已誠實標「歷史窗：實際 N 交易日」）。**兩種補法**：
> ① 把 `scripts/fetch_cron.sh` **排成常駐**每交易日盤後跑（建議）；或
> ② 一次性 `make backfill-universe-history`（對 concepts.yaml 全部次產業成員逐檔走可回補的單檔 `STOCK_DAY`，
> 把輪動籃子歷史補成 ~1 年；~1500 檔×13 月、8-12 小時、永久快取可中斷續跑）。

`scripts/fetch_cron.sh` 已備好（解析專案路徑、補 cron 精簡
PATH、`flock` 防重入、寫 `logs/cron_fetch.log`）。T86 法人收盤後約 90 分鐘、**15:00 起穩定**（docs/02），
故排 18:00 穩妥；crontab 加一行（交易日 18:00，盤後法人/月營收都已公布；依系統時區）：

```bash
crontab -e
# ↓ 路徑換成你的絕對路徑
0 18 * * 1-5  /bin/bash /path/to/stockpick/scripts/fetch_cron.sh
```

**WSL2 注意**：cron 預設不自動啟動，三選一——
① 每次開 WSL 後 `sudo service cron start`（關 WSL 就停）；
② `/etc/wsl.conf` 加 `[boot]` 段、設 `systemd=true` 後用 systemd 管 cron（重開 WSL 生效）；
③ 用 Windows 工作排程器呼叫 `wsl.exe -d <distro> -- /bin/bash /path/to/stockpick/scripts/fetch_cron.sh`（WSL 沒開機也會被喚起，最穩）。

漏抓自我檢查：`make rotation` / `sector flows` 會印「上櫃法人快取落後上市 N 個交易日」警告；
看到就手動 `make fetch-institutional-history DAYS=20`（含上櫃逐日回補，補回近 20 日缺口）。

### 13. 投資戰情室 Dashboard（`make dash-dev`）

把 `make week` 產出的 `reports/YYYY-Www/` 開成本機可視化 HUD——**只讀報告、不抓資料也不寫檔**，
和「貼給 Claude」是兩條並行的消化路徑（規格 [docs/17](./docs/17-dashboard-spec.md)）。FastAPI 後端
（`src/tw_screener/webapp/`）讀 `reports/` 吐 JSON、React/Vite 前端（`frontend/`）渲染。

**首次安裝（一次性）**：

```bash
make dash-install        # uv sync ＋ (cd frontend && npm install)
```

**執行流程 ＝ 先有報告、再開儀表板**（重點：dashboard 只是 `reports/` 的瀏覽器，沒報告就沒東西看）：

```bash
make week GROUP=defg     # ① 先跑主流程產 reports/YYYY-Www/（缺資料的週 → 儀表板顯空狀態）
make dash-dev            # ② 同時起 FastAPI(:8000) ＋ Vite(:5173)，Ctrl-C 一起關
#   → 瀏覽器開 http://localhost:5173 （Vite 把 /api proxy 到 :8000）
```

頁面（對應已完成的 M-Dash 0–4）：

- **候選股**：可排序/篩選表＋動能×估值散佈／法人買賣超 bar／估值分布，側欄渲染 `pick.md` 敘事
- **族群輪動**：資金熱力圖＋主題強度排行＋成員展開，`sector_rotation.md`／`group_analysis.md` 分頁
- **個股 detail**：點任一候選股/族群成員鑽取，六卡（基本面/籌碼/技術區間/族群定位/策略徽章＋Goodinfo 連結），缺資料顯「未取得」不報錯
- **持股損益**：`holdings_enriched.csv` 報酬/市值＋MA60 停損距離燈號＋除權息提醒；**Privacy 遮罩**一鍵把持股數字打碼（分享截圖友善，純前端、不影響資料）
- 週次切換器：缺資料週顯空狀態（只讀現有 `reports/`、不自動回補）

**自用正式跑法**（單一進程、不需 Vite dev server）：

```bash
make dash-build          # 前端 build → frontend/dist
make dash                # uv run tw-screener serve：單一 FastAPI 同時服務 build 後前端＋API（:8000）
```

> 全程**只讀 `reports/`**：不改報告、不觸發抓取，跑壞重開即可；`holdings` 為本機明文不入 git，
> Privacy 遮罩僅前端打碼供分享。後端 happy-path 測試 `make dash-test`。

---

## 報表產物導覽（`reports/YYYY-Www/`）

| 檔案                                                   | 誰產的                 | 內容 / 用途                                                                                                                                   |
| ------------------------------------------------------ | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `screen_result_{d,e,f,g}_*.csv`                      | ⑤ screen-all          | 各策略入選快照（純 Goodinfo 12 欄，不被後處理改寫）                                                                                           |
| `screen_log.md`                                      | ⑤ screen-all          | 各策略檔數＋交集統計                                                                                                                          |
| `sector_rotation.md` / `.csv`                      | ⑦ rotation            | **輪動地圖**：價格趨勢分數主鍵排序＋流量確認欄/四象限/★訊號/趨勢領頭板/我的參與度；CSV 供下週 ΔRank                                   |
| `cp_candidates.md` / `.csv`                        | ⑧ cp-value-candidates | 個股 CP 補漲候選＋C2 三重濾網（官方 trailing PE/PB；group Section 6 要讀）＋短窗早訊號／過熱-退潮警示（限庫存/觀察・低信心觀察，非進場/賣訊） |
| `group_analysis.md`                                  | ⑨ group               | 族群分析主報告（Section 0–7）                                                                                                                 |
| `candidates_enriched.csv`                            | ⑨ group               | 全候選股 × 完整欄位（含 flow_state/risk_kind/pullback_quality 揭露欄）＝**AI 挑股主宇宙**                                              |
| `holdings_enriched.csv` / `watchlist_enriched.csv` | ⑨ group               | 庫存/觀察 enrich（有維護才產）                                                                                                                |
| `theme_strength.csv`                                 | ⑨ group               | 2.8 雷達快照（供下週 ΔRank，不必貼給 Claude）                                                                                                |
| `pick.md`                                            | 手動 Step 3            | AI 精選進場清單（首屏 ≤60 行一頁決策卡＋尾端機器可讀區塊）                                                                                   |
| `picks.csv` / `excluded.csv`                       | 手動 picks sync        | pick／剔除底帳（pick 閉環`make pick-outcome` 的輸入）                                                                                       |
| `stocks/XXXX_名稱.md`                                | make report            | 個股深度報告                                                                                                                                  |

`reports/` 與 `research/`（校準報告）皆 gitignore——個人分析產物留本地。

---

## 策略體系

現行主流程 `make week GROUP=defg`，跑 **D/E/F/G** 四組（GROUP 必填、無預設）。

### D/E/F/G ProPicks 復刻組（現行主力）

D/E/F 對標 Investing.com ProPicks，共用「市值≥100 億」；**G 是 E 的逆勢孿生**：

| 策略                 | 條件概念                                             | 對標 / 角色          | 持有時間 |
| -------------------- | ---------------------------------------------------- | -------------------- | -------- |
| **D 品質龍頭** | 市值≥100 億 + ROE≥15 + 配息 8 年 + 連 2 季淨利     | TWCH15 台灣晶片冠軍  | 6+ 月    |
| **E 成長動能** | 市值≥100 億 + 營收 YoY≥20 + 連 2 季淨利 + 均線多頭 | Tech Titans（順勢）  | 1–3 月  |
| **F 價值反彈** | 市值≥100 億 + PER≤15 + 殖利率≥3 + 營收 YoY≥10    | Top Value Stocks     | 3–6 月  |
| **G 成長拉回** | 同 E 基本面 + 季線上揚回踩（乖離 −5%~+10%）+ 量縮   | E 的逆勢孿生（低接） | 1–3 月  |

> **E 順勢、G 逆勢**：G 的拉回過濾在分析層用快取 MA60/量比計算；G 的 CSV 是基本面宇宙，
> 有效拉回命中見 `group_analysis.md` 標 G 者。

### A/B/C 經典三角（已退役）

早期實驗，已退役（規劃書 04 A4）：YAML 移至 `config/strategies/archive/`，
`GROUP=abc` 不再可跑（會明確報退役）。僅留作歷史紀錄，
詳細條件與設計取捨見 [docs/03-strategies.md](./docs/03-strategies.md)。

## 核心設計原則

1. **半自動，不全自動**：資料抓取、選股、輪動、報告骨架自動化；下單決策保留給人。
2. **資料層與分析層分離**：數字由程式抓、Polars 算；解讀由 Claude 寫；**缺資料標「未取得」不編造**。
3. **兩宇宙互相校驗**：候選股鏡頭（精）× 全市場資金鏡頭（無偏），Section 2.8 並列防選擇偏誤。
4. **參數有依據**：輪動訊號門檻由起漲點回測校準（lift/recall/領先天數），每季重校。
5. **累積式知識庫**：每週快取與快照累積，ΔRank、回測、策略勝率都靠時間變厚。

## 開發與測試

```bash
make test        # 全部測試（~500 個，全離線：fixtures/合成資料，不打網）
make test-unit   # 排除 integration 標記
make lint        # ruff
make typecheck   # mypy
```

- 模組對應：`src/tw_screener/{data,screener,analysis,report,backtest}/`，
  測試鏡像在 `tests/`。HTML 解析測試用 `tests/fixtures/` 離線樣本。
- 慣例：Polars（不用 pandas）、httpx、loguru、type hints、參數進 `config/settings.yaml`
  不寫死（[docs/09-coding-conventions.md](./docs/09-coding-conventions.md)）。

## 技術棧

**Python 3.11+**（uv）・**Polars**・**httpx**・**jinja2**（報表模板）・**typer**（CLI）・
**Claude**（個股報告生成＋全清單挑股＋本專案的 pair programmer）

## 文件導覽

| 文件                                                                                          | 內容                                                                                                                |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| [`CLAUDE.md`](./CLAUDE.md)                                                                   | Claude Code 行為守則（工程原則 + 專案規則 + 分析師人設）                                                            |
| [`docs/00-architecture.md`](./docs/00-architecture.md)                                       | 系統架構、資料流、模組職責                                                                                          |
| [`docs/01-environment.md`](./docs/01-environment.md)                                         | 環境設定、依賴管理、devcontainer                                                                                    |
| [`docs/02-data-sources.md`](./docs/02-data-sources.md)                                       | Goodinfo 爬蟲規範、證交所 OpenAPI、合規限速                                                                         |
| [`docs/03-strategies.md`](./docs/03-strategies.md)                                           | D/E/F/G 主策略 + A/B/C（已退役）、GROUP 機制、YAML 規範                                                             |
| [`docs/04-screener-spec.md`](./docs/04-screener-spec.md)                                     | 選股模組規格                                                                                                        |
| [`docs/05-group-analysis.md`](./docs/05-group-analysis.md)                                   | 族群分析、族群內排名                                                                                                |
| [`docs/06-report-spec.md`](./docs/06-report-spec.md)                                         | 個股深度報告框架與輸出規範                                                                                          |
| [`docs/07-cli-spec.md`](./docs/07-cli-spec.md)                                               | Makefile 指令、CLI 介面                                                                                             |
| [`docs/08-milestones.md`](./docs/08-milestones.md)                                           | 建置期 M0-M7＋上線後研究軌（M-MH 多窗起漲／Part B·C／修法7 進場階梯／落後濾鏡）；M-Dash 0–4 在 docs/17-dashboard-spec |
| [`docs/09-coding-conventions.md`](./docs/09-coding-conventions.md)                           | 程式碼風格、命名、測試規範                                                                                          |
| [`docs/10-sop.md`](./docs/10-sop.md)                                                         | **每週使用 SOP**（手動 Claude 對話模式、含範本 prompt）                                                       |
| [`docs/11-propicks-analysis.md`](./docs/11-propicks-analysis.md)                             | **ProPicks 全清單分析**（Step 3 完整 prompt + 流程）                                                          |
| [`docs/12-sector-rotation.md`](./docs/12-sector-rotation.md)                                 | **次產業資金輪動**規劃書＋方法論（R0-R6、起漲點校準、四象限）                                                 |
| [`docs/13-cp-value-research.md`](./docs/13-cp-value-research.md)                             | **個股 CP 補漲研究**＋方法論（三重濾網、官方 PE 估值層、M-MH 多窗起漲/退潮校準裁決）                          |
| [`docs/14-entry-ladder-portfolio-fix.md`](./docs/14-entry-ladder-portfolio-fix.md)           | 進場階梯 × 組合層修法（M-修法7：前重後輕分批、停損脫鉤、因子簇上限）                                               |
| [`docs/15-launch-point-research-partB.md`](./docs/15-launch-point-research-partB.md)         | 起漲點研究 Part B（買方主導度／個股×族群交互／payoff·decay 穩健度）                                               |
| [`docs/16-intra-sector-laggard-research.md`](./docs/16-intra-sector-laggard-research.md)     | 族群內落後度補漲因子研究（rs_subind 落後度 × 位階 × S+ 濾鏡）                                                     |
| [`docs/18-intra-sector-laggard-production.md`](./docs/18-intra-sector-laggard-production.md) | 族群內落後濾鏡生產化（冠軍 S+ 內 rs_subind<0 進場加分上線）                                                         |
| [`docs/17-dashboard-spec.md`](./docs/17-dashboard-spec.md)                                   | **投資戰情室 Dashboard** 規劃書（讀 reports/ 的本機 HUD、M-Dash 拆解、API/頁面/Privacy 遮罩）                 |
| [`docs/19-late-entry-launch-diagnosis.md`](./docs/19-late-entry-launch-diagnosis.md)         | 抓太晚＋漏起漲診斷（M-Diag1；`make diagnose`→延伸度曲線/漏抓五態雷達/金融斷點；根＝排序追高）                 |
| [`docs/20-ranking-reform-ws5.md`](./docs/20-ranking-reform-ws5.md)                           | 排序改革 WS5：貼底揭露欄＋sector-wide 旗標降輪動＋次產業表 rotation 趨勢分/Rank 並列＋F2 +15% 校準協議         |
| [`docs/21-etf-holdings-integration.md`](./docs/21-etf-holdings-integration.md)               | ETF 持股整合：holdings ETF 輕量列（asset_type/報酬追蹤）＋組合曝險手標 etf_exposure；ETF 不進個股 alpha 框架  |
| [`docs/22-factor-lab-w28.md`](./docs/22-factor-lab-w28.md)                                   | W28 統一因子實驗台（WS-A~G 收官）：trend_score 唯一存活、inflection 族/ΔRank/退潮全否證、宇宙效應方法論        |
| [`docs/23-backtest-r2-w28.md`](./docs/23-backtest-r2-w28.md)                                 | W28 回測二輪（WS-H~L 收官）：推論硬化 Fisher-z→moving-block bootstrap＋晉升鐵則；補 docs/22 §7 四洞；trend_score 升雙 robustness、★/ambush 升級、WS-K 籌碼三因子無證據/樣本不足 |
| [`docs/24-contrarian-base-detection.md`](./docs/24-contrarian-base-detection.md)             | 底部左側偵測 M-BR1：賣壓熄火×基本面完好×貼近結構低。Phase 1 揭露欄已實作；**Phase 2 面板檢驗已否證「轉買×貼低」兩條件桶**（lift r+20 −2.30%、CI95 [−3.52,−1.26]・§3.1）；**2026-08-08 裁決 A 人工解禁**三條件桶（＋防接刀）以小注進機會層、永不核心，附 M1.6 自動回收條款（§6）——證據狀態恆為「兩條件桶已否證、三條件桶未測且先驗不利」 |
| [`docs/25-macro-regime.md`](./docs/25-macro-regime.md)                                       | MacroRegime 總經避險層（外生風險燈號，與內生 V2 regime 並列不合成）：三輪 block-bootstrap 篩出 BAA10Y 為唯一穩健主訊號，v2 改單訊號決定燈色＋揭露面板；**Phase 1（M-Macro1）已上線、Phase 2（M-Macro2）as-of 回放驗證通過**，Phase 3 共振讀法實測待排 |
| [`docs/26-macro-scan-integration.md`](./docs/26-macro-scan-integration.md)                   | 外部總經風險掃描整合評估（M-Macro4）：17 項美股情緒/籌碼指標逐項可得性對帳＋為何**不進 pipeline**（7 條硬觸發只有 2 條可求值）；採納 A 案面板「較上次」變化追蹤＋B 案人工掃描指令 `/macro-scan` |
| [`docs/proposals/`](./docs/proposals/00-index.md)                                            | 審查改善規劃書 01–05（效能技債/資料韌性/量化驗證閉環/架構瘦身/**選股有效性總改造 F1–F5**，皆已收官）        |
| [`docs/99-troubleshooting.md`](./docs/99-troubleshooting.md)                                 | 常見問題與解法                                                                                                      |

## 給 Claude Code 的使用指示

1. **先讀** `CLAUDE.md`；輪動相關開發另讀 `docs/12-sector-rotation.md`。
2. **每次只做一個 milestone**，做完停下等使用者驗收，**不要連續執行**。
3. **每個 milestone 完成時**：跑驗收指令、確認 success criteria、給「完成清單」。
4. **不確定的事先問**，不要自行假設。
