---
description: 每日全量流程——make week 全量 Goodinfo 掃描 ＋ 總經第二意見掃描 ＋ Opus 合成當日推薦（pick.md）
argument-hint: ""
allowed-tools: Bash, Read, Write, Agent, Glob, Grep
---

你要跑一次**每日全量流程**：全量 `make week` → 總經第二意見掃描 → 把掃描結果寫成
`macro_risk_latest.yaml` → 用 Opus 子代理依 docs/11 規格合成當日 `pick.md`。

使用者已明確拍板兩件事（不要再問）：**接受全量每日跑 Goodinfo**（週選股系統改成每日
跑，流量比原設計大，使用者已知情同意）；**每天全新產出 `pick.md`，同週內互相覆蓋**。

依序執行，任何一步失敗就停下來回報，不要跳過：

## Step 1 — 全量週流程

```
make week GROUP=defg
```

這一步會全量打 Goodinfo（`screen-all`），可能跑一段時間。跑完後 `reports/<週次>/` 下
會有 docs/11 §Step A 列的檔案。此時 `macro_risk_latest.yaml` 還不存在，`week-check`
會印它 missing——**這是預期行為，不是錯誤**，繼續下一步。

## Step 2 — 總經第二意見掃描（Sonnet 子代理）

用 `Agent` 工具開一個 `general-purpose` 子代理，**model 指定 `sonnet`**（掃描是重複性
檢索工作，不需要頂級推理，比照 docs/28 §1 原本「Sonnet 4.6：快且省」的分工理由）。

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

## Step 4 — Opus 合成 `pick.md`

用 `Agent` 工具再開一個 `general-purpose` 子代理，**model 指定 `opus`**（比照 docs/11
「選 Claude Opus，最強模型，這步值得用」——這是全流程唯一值得用最貴模型的地方）。

這個子代理沒有你的對話上下文，prompt 必須完整自包含，至少要包含：
- 「讀 `docs/11-propicks-analysis.md` 全文，把裡面的『Prompt 範本』段落與『任務 0-5』
  當成你這次分析的完整規格——輸出結構（一頁決策卡→附錄→機器可讀區塊）、多空並陳紅線、
  禁用詞、macro_risk gate 讀法，全部照那份規格，不要自己另創格式。」
- 「依 docs/11 §Step A 的清單，讀 `reports/<週次>/` 下這些檔案：`group_analysis.md`、
  `sector_rotation.md`、`candidates_enriched.csv`、`cp_candidates.md`、
  `inflection_ambush.md`、`holdings_enriched.csv`（若存在）、`watchlist_enriched.csv`
  （若存在）、4 個 `screen_result_*.csv`、`pick_outcome_brief.md`（若存在）、
  剛寫好的 `macro_risk_latest.yaml`（若存在）。」
- 「**寫檔前自己查核 F2 位階紀律**：`picks:` 區塊裡每一筆 `layer: core` 的股票，
  對照 `candidates_enriched.csv` 的 `ext_ma60_pct` 欄，必須 ≤ `config/settings.yaml`
  的 `picks.core_ext_ma60_max_pct`（現行 +15%）。超過的股票**不能放進 core 層**——
  要嘛降到 opportunity 層並改寫理由，要嘛不選。`picks sync` 對這條規則是**全批次
  拒寫**（一筆超標，整份 `picks:` 都不會落帳），所以要在產出階段就擋掉，不要留給
  `sync` 事後打回票。在回報裡明講『F2 已查核，N 筆 core 全數合格』或列出哪幾筆被
  降層/剔除。」
- 「產出完成後，存到 `reports/<週次>/pick.md`（固定檔名，不可改——`week-check` 與
  F1 斷供偵測認這個名）。回報時附上 F2 查核結果。」

## Step 5 — 收尾

印出：
1. `reports/<週次>/pick.md` 已產出的路徑確認，附上 Step 4 回報的 F2 查核結果。
2. 提醒：這份是**每日推薦**，要正式落帳（寫入 `picks.csv`/`excluded.csv`）才會被
   `pick-outcome`／`week-check` 等底帳工具認列，指令是：
   ```
   uv run tw-screener picks sync --week <週次>
   ```
   **本指令不會自動跑這一步、`picks sync` 也沒有 `--dry-run` 可以先試跑**——落帳是
   人做最終決策的地方，交給使用者自己決定要不要跑、什麼時候跑；如果 Step 4 的 F2
   查核有列出被剔除/降層的股票，先看過那份清單再決定要不要 sync。
