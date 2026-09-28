---
description: 每日全量流程——make week（2026-08-28起預設不打Goodinfo，見docs/31 §20.6；含 shortlist 機器排序）＋ 總經第二意見掃描 ＋ Opus 合成當日決策卡（pick.md＋pick_detail.md）
argument-hint: ""
allowed-tools: Bash, Read, Write, Agent, Glob, Grep
---

你要跑一次**每日全量流程**：`make week`（含 `shortlist` 機器排序）→ 總經第二意見掃描 → 把掃描結果寫成
`macro_risk_latest.yaml` → 用 Opus 子代理依 docs/11 規格合成當日 `pick.md`（一頁決策卡＋機器排序 Top 5）
與 `pick_detail.md`（明細）。

**2026-08-28起 `make week` 預設流程已不再打 Goodinfo**（D/E/G結構性無法本地重建、
F改走本地等價路徑，docs/31 §20.6軟退場）——本節「使用者已拍板全量每日跑 Goodinfo」
的授權背景仍保留紀錄，但**現行預設流程不會用到它**，`doctor` 只是單頁非阻塞健康
檢查、不是掃描。若要手動跑Goodinfo原始定義，走 `make screen-all GROUP=defg`（本指令
不會自動呼叫）。

使用者已明確拍板：**每天全新產出 `pick.md`（與 `pick_detail.md`），同週內互相覆蓋**。

依序執行，任何一步失敗就停下來回報，不要跳過：

## Step 1 — 全量週流程

```
make week GROUP=defg
```

現行預設流程**不打 Goodinfo**（`screen-f-local`＋`screen-redesign-local` 皆本地
filter，`doctor` 只是單頁非阻塞健康檢查）——比純本地計算多花的時間主要在抓 TWSE/
TPEX OpenAPI、法人史、TDCC、族群分析等步驟，仍可能跑數分鐘，但不是在等 Goodinfo
速率限制。跑完後 `reports/<週次>/` 下會有 docs/11 §Step A 列的檔案——**含 `shortlist.csv`**
（M-Pick1 機器排序：`week` 在 `snapshot-week` 之後、`week-check` 之前跑 `shortlist`，容錯不擋；若缺，
先單獨 `make shortlist` 重跑一次，仍缺就照 docs/11 走「機器排序缺席」，不要自己挑股補位）。此時
`macro_risk_latest.yaml` 還不存在，`week-check` 會印它 missing——**這是預期行為，
不是錯誤**，繼續下一步。

## Step 2 — 總經第二意見掃描（Sonnet 子代理）

用 `Agent` 工具開一個 `general-purpose` 子代理，**model 指定 `sonnet`**（掃描是重複性
檢索工作、判斷含量低，用中階模型快且省——docs/28 §1 的分工理由）。

Prompt 內容要包含：「讀 `.claude/commands/macro-scan.md` 並完整依其程序執行一次外部
總經風險掃描（本專案今天的 `reports/<週次>/macro_regime.csv` 剛被 `make week` 產出，
讀得到，不要用網搜重抓那幾項）。**執行掃描前先 glob `research/macro_scan/*.md`，
排除今天日期那份，取檔名日期最新的一份當『上次掃描』基準讀進來算變化箭頭；一份都
沒有就在報告裡寫『首次掃描、無基準』，不要假裝有基準可比**。輸出到
`research/macro_scan/<今天日期 YYYY-MM-DD>.md`。完成後，把該檔最後『7. 機器摘要』
那段 6 鍵 YAML code block **逐字**回傳給我，不要摘要或改寫。」

## Step 3 — 寫入 `macro_risk_latest.yaml`

把 Step 2 子代理回傳的 YAML 印在對話裡（讓使用者看得到掃描結果），然後寫入
`reports/<週次>/macro_risk_latest.yaml`（週次＝`reports/` 下含 `-W` 的最新資料夾，
判準同 `src/tw_screener/report/pick_store.py` 的 `week_dirs()`）。檔案內容就是那段
`macro_risk:` YAML 本身（頂層鍵 `macro_risk:`，不要額外包裝）。

若 Step 2 沒能產出合法的 YAML（例如掃描大部分項目都抓不到、子代理明確回報失敗，
**或回傳的 `of` 是 0**——`analysis/macro_risk.py` 把 `of<=0` 判成 `invalid`，不是
`missing`，那會印出錯誤而不是乾淨的「掃描缺席」），**不要編一份出來**——略過這步，
讓 `macro_risk_latest.yaml` 保持不存在，下游三態容錯會正確判成 `missing`，不擋流程。

寫完後可選擇重跑一次 `uv run tw-screener report check`，讓 `week-check` 印出正確的
`ok`／`stale` 狀態（Step 1 那次跑的時候這個檔案還不存在，會印過期的 missing）。

## Step 4 — Opus 合成 `pick.md`＋`pick_detail.md`

用 `Agent` 工具再開一個 `general-purpose` 子代理，**model 指定 `opus`**（多空權衡＋估值綜合
判斷是全流程判斷含量最高的一步，用 opus 級；用子代理是為了讓它在乾淨 context 裡讀完整份
規格與產物，不受本對話前幾步干擾）。

這個子代理沒有你的對話上下文，prompt 必須完整自包含，至少要包含：
- 「讀 `docs/11-propicks-analysis.md` 全文，把裡面的『Prompt 範本』段落（含任務 1–5 與『附錄 G』節）
  當成你這次分析的完整規格——輸出結構（`pick.md`＝一頁決策卡 ≤50 行＋picks YAML；`pick_detail.md`＝
  附錄 A–H＋資料品質披露）、Top 5 照 `shortlist.csv` 的 rank 不重排、否決規則（≤ `max_vetoes`、理由只限
  三類）、多空並陳紅線、禁用詞、macro_risk gate 讀法，全部照那份規格，不要自己另創格式、不要自己挑股。」
- 「依 docs/11 §Step A 的清單，讀 `reports/<週次>/` 下這些檔案：**`shortlist.csv`（★第一頁唯一排序
  來源；不存在或過期就照 docs/11 寫『機器排序缺席』）**、`pick_outcome_brief.md`（若存在）、
  `group_analysis.md`、`sector_rotation.md`、`candidates_enriched.csv`、`holdings_enriched.csv`（若存在）、
  `watchlist_enriched.csv`（若存在）、剛寫好的 `macro_risk_latest.yaml`（若存在）、`dcf_inputs.csv`、
  `cp_candidates.md`、`inflection_ambush.md`、**所有 `screen_result_*.csv`**（2026-08-28 起為本地篩選
  F/F2/G1/G2/G4/G5/L6，檔數不固定；舊 Goodinfo D/E/G 已軟退場、不會有其 CSV，這是預期、不是缺檔）。」
- 「附錄 G 綜合估值區間：規則見 docs/11『附錄 G』節（唯一真相來源；範圍＝持股個股＋Top 5，只寫在 `pick_detail.md`、不上第一頁）。」
- 「**寫檔前自己查核 F2 位階紀律**：`picks:` 區塊裡每一筆 `layer: core` 的股票，
  對照 `candidates_enriched.csv` 的 `ma60_dist_pct` 欄（sync 落帳後即 `ext_ma60_pct`），必須 ≤
  `config/settings.yaml` 的 `picks.core_ext_ma60_max_pct`（現行 +15%）。shortlist 已用同一上限 gate，
  照抄即合規；若仍有超標（代表 shortlist 與 candidates 資料不一致），**不要自行降層或換股**——該檔依
  docs/11 否決類別①（資料異常）處理並在回報列出。`picks sync` 對這條規則是**全批次拒寫**（一筆超標，
  整份 `picks:` 都不會落帳），所以要在產出階段就擋掉。在回報裡明講『F2 已查核，N 筆 core 全數合格』
  或列出問題筆。」
- 「**寫檔前自查 Top 5 順序與否決**：① `layer: core` 各筆的 `rank` 依序等於 `shortlist.csv` 中
  `tier=top` 的 rank——扣掉被否決者、依 rank 補上遞補的 `tier=alt`——沒有 shortlist 之外的股票、沒有重排；
  ② `excluded:` 中 `reason: 機器排序否決` 的筆數 ≤ `config/settings.yaml` 的 `picks.shortlist.max_vetoes`
  （現行 2），每筆 `detail` 帶否決類別＋來源＋日期；③ 未遞補的 alt 以 `layer: pool`＋`rank` 列入；
  ④ `pick.md` 從檔首到 `<!-- picks:begin -->` 之前 ≤50 行
  （`awk '/<!-- picks:begin -->/{exit} {n++} END{print n}' reports/<週次>/pick.md`）。在回報裡明講
  『Top 5 順序＝shortlist rank、否決 N 筆 ≤ max_vetoes、第一頁 N 行』或列出不符處。」
- 「產出完成後，存到 `reports/<週次>/pick.md`（固定檔名，不可改——`week-check` 與
  F1 斷供偵測認這個名）與 `reports/<週次>/pick_detail.md`（明細）。回報時附上 F2 查核結果與
  Top 5／否決自檢結果。」

## Step 5 — 收尾

印出：
1. `reports/<週次>/pick.md` 與 `reports/<週次>/pick_detail.md` 已產出的路徑確認，附上 Step 4 回報的
   F2 查核結果與 Top 5／否決自檢結果。
2. 提醒：這份是**每日決策卡**，要正式落帳（寫入 `picks.csv`/`excluded.csv`）才會被
   `pick-outcome`／`week-check` 等底帳工具認列，指令是：
   ```
   uv run tw-screener picks sync --week <週次>
   ```
   **本指令不會自動跑這一步、`picks sync` 也沒有 `--dry-run` 可以先試跑**——落帳是
   人做最終決策的地方，交給使用者自己決定要不要跑、什麼時候跑；如果 Step 4 的自檢
   有列出不符處（F2 超標、順序不符、否決超過上限），先看過那份清單再決定要不要 sync。
