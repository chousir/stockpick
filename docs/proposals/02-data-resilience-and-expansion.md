# 規劃書 02 — 資料源韌性與擴充

> 對應審查發現：§1#2（Goodinfo 單點故障）、§2.2（冷啟動歷史密度）、
> §2.3 / §4#3,#4,#5（缺資料：集保大戶、融資融券、財報細項、市值等）。
> 性質：**降低最脆弱依賴的風險、補上對勝率邊際價值最高的缺資料**。中風險（涉新資料源）。

---

## 背景

兩件事：
1. **韌性**：選股層單點押在 Goodinfo（反爬＋CLIENT_KEY 逆向＋300 筆匿名上限）。
   它一改版，整個 `make week` 的篩選步驟就停。
2. **缺料**：籌碼面只有三大法人，缺最直接的「籌碼集中」訊號（集保大戶）；
   `MI_MARGN`（融資融券）端點已知卻無人使用；財報只有營收/毛利/EPS，缺體質細項。

依「對勝率邊際價值」排序施作：**D1 韌性 → D3 集保 → D4 融資融券 → D5 財報 → D2 歷史密度 → D6 盤點**。

---

## D1 — Goodinfo 韌性：fail-loud、可重放、來源可抽換 ✅ 完成（2026-06-27，A/B/C）

> 收官：Part A（`screen doctor` 健康檢查）／Part B（`run_all` 單策略失敗不炸整批）／
> Part C（`--replay` 離線回歸）皆完成；**Part 4（ScreenerSource protocol 抽換接縫）使用者拍板延後**
> ——目前 Goodinfo 為唯一實作，單一 implementer 的 protocol 屬「為未來抽象」（Karpathy 原則 2），
> 待真要接第二來源（FinMind/自建）再抽，登記為下方 backlog。
> 落地物：`screener/goodinfo/doctor.py`（`diagnose_html` 純分類＋live/replay）、`config/doctor_probe.yaml`、
> settings `goodinfo.doctor.*`、`screen doctor` CLI、`runner.run_all` 韌性＋`screen_log` 未取得段、
> Makefile `doctor` target 並接進 `make week`（擋在 screen-all 前）。詳見 docs/08「M-R-Data1」。

### 問題（現況）
- CLIENT_KEY 由逆向 JS 常數還原，含寫死 fallback
  （[fetcher.py:113](../../src/tw_screener/screener/goodinfo/fetcher.py#L113) `_build_client_key`）。
- 封鎖偵測靠中文字串 `您的瀏覽量異常`（[fetcher.py:27](../../src/tw_screener/screener/goodinfo/fetcher.py#L27)）。
- 匿名結果上限 300 筆，超過直接 `raise GoodinfoTooManyResultsError`
  （[parser.py:43](../../src/tw_screener/screener/goodinfo/parser.py#L43)）。
- 改版時 parser 找不到 `tblStockList` → `GoodinfoParseError`，但**整批 week 會中斷**。

### 方案（不破壞合規底線 docs/02）
1. **健康檢查指令** `screen doctor`：用一個極簡固定篩選打一次 Goodinfo，判斷
   「正常／被擋／JS 結構變了／欄位改名」，回傳明確診斷碼。納入週流程前置檢查。
2. **單策略失敗不炸整批**：`runner.run_all` 對每策略 try/except，某策略 parse 失敗 →
   記 `screen_log.md`「該策略本週未取得」、其餘照跑、`make week` 不整體中斷（目前 blocked
   會 raise 中斷——保留 blocked 的中斷語意，但 parse 改版類降級為單策略略過）。
3. **可重放回歸**：每次成功抓取，把當次 HTML 落地到 `tests/fixtures/goodinfo/`（已有慣例），
   `screen doctor --replay` 用最近 fixture 驗 parser 仍能解析 → 改版時第一時間紅燈。
4. **來源抽換預留**：把「條件篩選」抽象成 `ScreenerSource` protocol（`build_url` / `fetch` /
   `parse`），Goodinfo 為其一實作。**不立刻寫第二個來源**（YAGNI），但留下換 FinMind/自建
   篩選的接縫，避免未來大改。

### 成功標準
- [x] `uv run tw-screener screen doctor` 能區分「正常／被擋／改版」並給可讀訊息。
      （診斷碼：OK／BLOCKED／JS_UNRESOLVED／STRUCTURE_CHANGED／COLUMNS_RENAMED／EMPTY_RESULT／TOO_MANY／NETWORK_ERROR）
- [x] 模擬某策略 parse 失敗時，`make week` 其餘策略仍產出、log 誠實標記。
      （`run_all` catch `GoodinfoParseError`/`GoodinfoTooManyResultsError` 降級；`GoodinfoBlockedError` 仍中斷整批）
- [x] `screen doctor --replay` 以離線 fixture 通過（不打網）。

### 可動檔案範圍
`src/tw_screener/screener/goodinfo/*`、`src/tw_screener/screener/runner.py`、
`src/tw_screener/cli.py`、`tests/screener/`。

### 風險
抽象層別過度設計（Simplicity First）——protocol 只抽真正會變的三個方法，不做萬用框架。

---

## D3 — 集保大戶／股權分散（最痛的籌碼缺口）✅ 完成（2026-06-27）

> 收官：新 `data/tdcc.py`（TDCC OpenData 集保戶股權分散表 id=1-5，免費、無反爬）——
> `parse_distribution`（純解析，吃 BOM／固定寬度尾端空白）＋`derive_big_holders`（由原始股數精算
> 大戶占比，比 TDCC 逐級截斷顯示更準）＋`latest_big_holders_with_wow`（相鄰兩週相減）＋`TDCCClient`
> （限速/退避/逐週累積 `tdcc_distribution_{YYYYMMDD}.parquet`）。**門檻採「兩個都出」（使用者拍板）**：
> `big_holder_pct`＝≥400 張（級距12-15）、`big_holder_1000_pct`＝≥1000 張（千張大戶，級距15），各帶 WoW。
> **占比口徑＝占集保庫存（≈流通量）非占股本**，報告/docs 已據實標明。接進 candidates/holdings/watchlist
> enriched.csv（`big_holder_map`，缺值誠實 null）＋個股報告籌碼段（不再要 Claude 手讀千張大戶比）。
> CLI `data fetch-tdcc`＋Makefile `fetch-tdcc`，`make week` 非阻斷接入（TDCC 異常大戶欄退化為 null、不擋主流程）。詳見 docs/08「M-R-Data3」。

### 問題
「籌碼集中」是核心訴求（情境 B），但目前只有三大法人。千張大戶比、股權分散趨勢、
董監持股**完全不在資料層**——個股報告是叫 Claude 去 Goodinfo 頁面手讀
（[builder.py:74](../../src/tw_screener/report/builder.py#L74)、prompts j2）。

### 方案
1. 新來源：**TDCC 集保戶股權分散表**（每週公布、免費 OpenData）。
   新增 `data/tdcc.py`：抓「集保戶股權分散表」→ 每股的持股級距分布 → 衍生
   `big_holder_pct`（≥400 張或 ≥1000 張級距占比）、`big_holder_wow`（週變化）。
2. 逐週累積 `tdcc_distribution_{YYYYMMDD}.parquet`（同 daily/inst 累積模式）。
3. 接進 `candidates_enriched.csv` 新欄（`big_holder_pct` / `big_holder_wow`）與個股報告
   bundle；情境 B 的「籌碼集中」從「人工讀」升級為「可篩可排序」。

### 成功標準
- [x] `data/tdcc.py` parser 以離線 fixture 測試通過。（`tests/data/test_tdcc.py` 11 測，fixture 取真實 2330/2317/0050/6182/1216 × 17 級距）
- [x] `candidates_enriched.csv` 出現大戶持股欄、缺值誠實標 null（不補零）。（4 欄 `big_holder_pct`/`big_holder_1000_pct`＋WoW，holdings/watchlist 經 `_CANONICAL_REUSE_FIELDS` 同步）
- [x] 個股報告籌碼段能引用大戶週變化（不再要求 Claude 手讀）。（`data_fetcher._format_big_holder_summary` 進 bundle，j2 模板＋inline draft 皆改）

### 可動檔案範圍
`src/tw_screener/data/tdcc.py`（新）、`report/data_fetcher.py`、`cli.py`、
`config/settings.yaml`、`tests/data/`、`tests/fixtures/tdcc/`。

### 風險
TDCC 為週頻、且公布有遞延 → meta 明標資料日，避免與日頻法人混淆時點。

---

## D4 — 啟用既有但閒置的融資融券（MI_MARGN）✅ 完成（2026-06-27）

> 收官：`data/twse.py` 加 `_parse_margin`（OpenAPI MI_MARGN list[dict]・單位張・由 latest_trading_date 錨日）
> ＋`fetch_margin`（逐日累積 `margin_{YYYYMMDD}.parquet`）＋`load_margin_signals`（最新日每股餘額/增減＋
> `margin_chg_5d`，快取不足 6 日 → null）。接進 `fetch-twse`（多一步）、candidates/holdings/watchlist
> enriched.csv（`margin_map`：margin_balance/chg/chg_5d/short_balance/short_chg/`margin_to_vol`＋併入
> `_CANONICAL_REUSE_FIELDS`）、個股報告籌碼段（`_format_margin_summary`＋券資比）。docs/11 補欄＋讀法
> （融資增減＝散戶槓桿；融資減肥＝籌碼洗清偏多）。**僅上市；上櫃融資融券為缺口、登記 D6 backlog。**
> 詳見 docs/08「M-R-Data4」。

### 問題
`docs/02` 列了 `MI_MARGN` 端點，但 `grep -r 融資|margin src/` **零使用**。
CLAUDE.md 人設籌碼段明文要求「融資增減」，目前產不出來。

### 方案
1. `data/twse.py` 加 `fetch_margin()`（OpenAPI `/exchangeReport/MI_MARGN`），逐日累積
   `margin_{YYYYMMDD}.parquet`，schema：`date / stock_id / margin_balance / margin_chg /
   short_balance / short_chg`。
2. 衍生 `margin_chg_5d`、`margin_to_volume` 進候選表與個股 bundle。

### 成功標準
- [x] `make fetch-twse` 多一步抓融資融券、parser 離線測試過。（`_parse_margin` + `tests/data/test_twse.py` 6 測，fixture `margin.json`）
- [x] 候選表/個股報告出現融資增減欄。（candidates/holdings/watchlist 6 欄＋個股報告籌碼段）

### 可動檔案範圍
`src/tw_screener/data/twse.py`、`cli.py`、`Makefile`（fetch-twse 串接）、`tests/`。
（實作另觸及 `report/group_report.py`／`report/data_fetcher.py`／`report/builder.py`／`prompts/stock_report.md.j2`／`docs/11`——成功標準「候選表/個股報告出現融資增減欄」必經，範圍清單原漏列。）

### 風險
低。端點合法、結構穩定。**實測 OpenAPI MI_MARGN 為乾淨 per-stock JSON（無「資券當沖/鉅額」section header 特殊列，那是 legacy CSV 格式的問題）**，故無需特殊列過濾；空欄位以 `_safe_int` 視為 0。

---

## D5 — 財報細項擴充（體質維度）✅ 完成（2026-06-27）

> 收官：盤點 TWSE/TPEX OpenAPI 後確認**無現金流量表端點、簡式資產負債表無存貨/應收明細**
> → 營業現金流/週轉率**不可得**（使用者拍板「改抓確定可得者」，成功標準 #2 據此修正）。
> 落地物：`_FUNDAMENTALS_SCHEMA` 加 6 欄（`pretax_margin_pct`/`net_margin_pct` 來自已抓的營益分析、
> `debt_ratio_pct`/`current_ratio`/`bvps`/`roe_q_pct` 來自新增的簡式資產負債表一般業端點）；
> `_parse_quarterly_fundamentals` 擴成 6 端點輸入（加 `t187ap07_L_ci` 上市＋`mopsfin_t187ap07_O_ci`
> 上櫃；⚠ 上市欄名「總額」上櫃「總計」、上櫃 key 用「年度/季別」）；ROE＝EPS/每股淨值（單季、歸屬母公司、未年化）；
> 接進 candidates/holdings/watchlist enriched.csv（`net_margin_pct`/`debt_ratio_pct`/`roe_q_pct` 3 欄＋
> 併入 `_CANONICAL_REUSE_FIELDS`）與個股報告基本面段（`_format_fundamentals_summary` 進 bundle、j2＋inline draft）。
> 金融業與缺表者體質欄誠實 null。docs/02 補端點表＋盤點結論、docs/11 補讀法。詳見 docs/08「M-R-Data5」。

### 問題
基本面只有 營收 YoY／毛利率／營益率／單季 EPS（`_parse_quarterly_fundamentals`）。
缺 **負債比、ROE、純益率**——D（品質龍頭）/F（價值）的體質判斷踩空。
（原列的「營業現金流、存貨/應收週轉」經盤點確認 OpenAPI 不可得，見下方修正。）

### 方案（依盤點結果，使用者拍板「只做確定可得者」）
1. ~~盤點 TWSE/TPEX OpenAPI 財報項目~~ ✅ 完成：**現金流量表無端點、簡式資產負債表無存貨/應收**
   → 營業現金流/週轉率不可得，誠實標未取得、不硬湊（不爬 MOPS、守 API-first）。
2. 擴 `fundamentals_*.parquet` schema，加**確定可取得**的欄：
   - 來自已抓的營益分析（0 新端點）：`pretax_margin_pct`（稅前純益率）、`net_margin_pct`（稅後純益率）。
   - 來自新增簡式資產負債表（一般業 `_ci`，上市+上櫃 2 端點）：`debt_ratio_pct`（負債/資產）、
     `current_ratio`（流動資產/流動負債）、`bvps`（每股淨值）、`roe_q_pct`（＝EPS/每股淨值，單季）。
3. 接進個股報告基本面段與候選表（D/F 體質欄）；不改 Goodinfo 篩選條件本身。

### 成功標準
- [x] `fundamentals` schema 擴充、parser 離線測試過、缺值誠實 null。
      （6 欄；`tests/data/test_twse.py` 2 新測含金融業/淨值非正的 null 路徑）
- [x] ~~個股報告基本面段能引用至少「營業現金流＋負債比」~~ **修正**（營業現金流 OpenAPI 不可得）→
      **個股報告基本面段能引用「負債比＋單季ROE＋稅後純益率」**，且營業現金流/週轉率誠實標未取得。
      （`_format_fundamentals_summary` 進 bundle，j2＋inline draft 皆改；`tests/report/test_data_fetcher.py` 3 新測）

### 可動檔案範圍
`src/tw_screener/data/twse.py`、`report/data_fetcher.py`、`tests/`、`docs/02`（補端點表）。
（實作另觸及 `report/group_report.py`／`report/builder.py`／`prompts/stock_report.md.j2`／`docs/11`——
成功標準「候選表/個股報告出現體質欄」必經，範圍清單原漏列，同 D4 慣例。）

### 風險
財報端點欄名/格式較雜（上市上櫃不一致：總額/總計、Year/年度）→ 沿用既有「欄名雙保險」與離線 fixture。
資產負債表/綜合損益表各分 6 種公司型態端點，D5 只取一般業 `_ci`（金融業負債比語意不同、留 null）。

---

## D2 — 全市場日線歷史密度（冷啟動）✅ 完成（2026-06-27）

> 收官：新指令 `data backfill-universe-history`（＋Makefile target）對 concepts.yaml 全部次產業成員
> （上市＋上櫃）逐檔走可回補的單檔 `STOCK_DAY`/`tradingStock`、依次產業成員數排序、永久快取可中斷續跑
> ——為 `backfill-otc-history` 的超集；新 `report/density.py`（`data_density_note` 三段信心）接進 rotation／
> group(cp) 報表頭誠實標「歷史窗：實際 N 交易日／建議 ~250（z 需 ≥60）」；docs/02＋README §12＋fetch_cron.sh
> 校正「STOCK_DAY_ALL 不可回補、cron 對全市場日線密度建議常駐」。詳見 docs/08「M-R-Data2」。

### 問題（§2.2）
`STOCK_DAY_ALL` 的 `date` 參數被無視、永遠回今天
（[docs/02](../02-data-sources.md) 已載明）→ **全市場日線只能往未來累積、過去補不回**。
rotation z 需 60+ 日、calibration 需 ~250 日 → 新環境前幾個月訊號統計意義薄弱。
README 把 cron 降「選配」對法人正確（可回補），但對**全市場日線密度**略樂觀。

### 方案
1. **誠實標示資料密度**：rotation / cp 報表頭加「歷史窗實際天數 / 所需天數」與信心註記，
   密度不足時明標「訊號統計意義有限」（守誠實原則，不假裝有 edge）。
2. **次產業成員回補**：對 `concepts.yaml` 全部次產業成員（非僅候選）用 `STOCK_DAY`（單檔月、
   可回補）批次補歷史，把 rotation 籃子的歷史密度從「snapshot 累積」補成「~1 年」。
   量大 → 限速、可斷點續抓、優先補成員多的次產業。已有 `data backfill-otc-history` 雛形可參考。
3. **正式化每日抓取**：把 `scripts/fetch_cron.sh` 從「選配」改文件定位為「建議常駐」，
   並在 README 修正 §2.2 的樂觀描述。

### 成功標準
- [x] rotation/cp 報表顯示實際歷史窗天數與信心註記。（`render_rotation_report`/`render_group_report` 加 `density_note`；`report/density.py` 三段信心）
- [x] `data backfill-universe-history` 能對次產業全成員回補日線（可中斷續跑）。（全成員、依次產業成員數排序、永久快取續跑；CLI＋Makefile）
- [x] README/docs/02 對「全市場日線不可回補」與 cron 必要性的描述校正一致。（README §12＋docs/02 端點/累積表＋fetch_cron.sh 頭註）

### 可動檔案範圍
`src/tw_screener/data/twse.py`、`cli.py`、`report/rotation_report.py`、
`README.md`、`docs/02-data-sources.md`、`tests/`。

### 風險
全成員回補請求量大 → 嚴格限速、分批、可續抓；務必不違反 TWSE 寬鬆限速禮節（建議 1 req/s 內）。

---

## D6 — 缺資料盤點（backlog，逐項評估再做）

不立即實作，但登記為待辦，每項附「來源是否免費可得／對勝率價值／實作成本」：

| 缺料 | 候選來源 | 價值 | 備註 |
|---|---|---|---|
| 市值／流通在外股數 | TWSE 基本資料 | 中 | 解鎖真周轉率、市值分層；團隊 2026-06-24 曾裁定不補，重新評估 |
| 外資持股比率 | TWSE 外資持股 | 中 | 與 D3 集保互補 |
| 上櫃融資融券（OTC margin） | TPEX OpenAPI（端點待查，obvious 名 302） | 中 | D4 僅做上市 MI_MARGN；上櫃股 margin 欄目前一律 null。約佔候選宇宙半數，補上提升覆蓋，2026-06-27 登記 |
| 分點/主力券商買賣 | 券商分點（爬蟲・灰區） | 高但風險高 | 合規與穩定度需評估，預設不做 |
| 法說會/財測行事曆 | 公開資訊觀測站 | 中 | 補 macro_calendar 的個股事件層 |
| `ScreenerSource` protocol（D1 Part 4） | — | 低（目前單一來源） | 2026-06-27 拍板延後；要接第二篩選來源（FinMind/自建）時再抽 build_url/fetch/parse 三方法，避免空殼指向 |

### 成功標準
- [ ] D6 以表格登記進本規劃書 backlog；每項待單獨提案才動工。

---

## 驗收（各 D 完成時各自驗）

```bash
uv run tw-screener screen doctor            # D1
uv run tw-screener screen doctor --replay   # D1 離線
make fetch-twse                             # D4/D5 串接後仍正常
make week GROUP=defg                        # 候選表新增欄位、缺值誠實 null
make test && make lint && make typecheck
```

## 共通原則
- 任何新來源都要：離線 fixture 測試、缺值標 null 不補零、meta 標資料日與來源、限速合規。
- **不為了補欄而編數字**（playbook/60-analyst-persona.md 資料來源原則 / docs/13 §3.3）。
</content>
