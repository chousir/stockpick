---
description: 週報系統覆盤（docs/30）——季頻，跑 pick-outcome/diagnose 底稿後委派 Opus 子代理依 docs/30 規格產出事後評定報告
argument-hint: ""
allowed-tools: Bash, Read, Write, Agent, Glob, Grep
---

你要跑一次**週報系統覆盤**——回答「照 `pick.md` 買會不會賺、哪裡該改、漏抓了什麼賺錢股、
停損政策合不合理、抓到的是起漲點還是已經尾聲」。這是**季頻**任務（docs/30 §0），不是每次
呼叫都該跑；若你不確定使用者是不是真的要跑一次全新覆盤（而不是只是問個問題），先確認。

`docs/30-pick-outcome-retro-review.md` 是**唯一規格來源**——本檔只負責跑前置指令＋委派，
不重複規則內容。

## Step 1 — 產生底稿

```
uv run tw-screener picks outcome --diff
make diagnose
```

第一個指令產出 `research/pick_outcome/outcome_<YYYYMMDD>.md`、`picks_returns_<YYYYMMDD>.csv`、
`stop_delay_<YYYYMMDD>.csv`。第二個產出 `research/diagnostic/late_entry_<YYYYMMDD>.md` 及
其 3 個 CSV（WS1 可重跑部分；WS2 漏抓目錄凍結在 2026-06-09，不會更新，這是已知限制不是本次
指令跑壞了）。

任何一步失敗就停下來回報，不要跳過或用舊檔頂替。

## Step 2 — Opus 子代理產出覆盤報告

用 `Agent` 工具開一個 `general-purpose` 子代理，**model 指定 `opus`**（覆盤要下對/錯判斷、
排優先順序，值得用最強模型——比照 docs/11 對 ProPicks 合成步驟的同一個理由）。

這個子代理沒有你的對話上下文，prompt 必須完整自包含，至少要包含：
- 「讀 `docs/30-pick-outcome-retro-review.md` 全文，把裡面的 Persona／規則（§2）與任務
  1-9（§3）當成這次分析的完整規格，逐項依序輸出，不要自創格式或跳過任務。」
- 「讀 Step 1 剛產出的底稿：`research/pick_outcome/outcome_<日期>.md`、
  `picks_returns_<日期>.csv`、`stop_delay_<日期>.csv`、`research/diagnostic/
  late_entry_<日期>.md` 及其 3 個 CSV（日期填 Step 1 實際產出的 YYYYMMDD）。」
- 「讀 `research/pick_outcome/self_review_20260808.md` 當格式範例（若存在）——尤其任務 9
  的信心分級小結表格式，比照它做。」
- 「讀 `docs/08-milestones.md` 第 832 行的 F2 季度校準協議（docs/30 §2 已借用成一般判準），
  任何規則變更建議都要先過那三個條件，且就算三個條件都滿足，只要任務 4 的反事實檢驗顯示會
  誤殺真正的領頭股就要否決，過不了/被否決只能寫『記錄、樣本不足以改規則』。」
- 「任務 5（停損政策）依 docs/30 規定寫『未取得』並說明現有 `stop_delay_ledger` 的量測邊
  界，**不要**自己去讀價格快取臨場算一個停損後前瞻報酬的數字——那個數字沒有 `make` 目標可
  重現，以後會被誤引成正式產物。」
- 「輸出存到 `research/pick_outcome/retro_review_<今天日期 YYYY-MM-DD>.md`
  （`research/` 已 gitignored，這是研究軌產出，不寫進 `reports/`）。」

## Step 3 — 收尾

印出：
1. `research/pick_outcome/retro_review_<日期>.md` 路徑確認。
2. 任務 9 信心分級小結表的內容（直接印在對話裡，讓使用者不用開檔也能看到結論一覽）。
3. 若任務 8 的改進建議裡有任何一條標「可直接做」，列出來提醒使用者——這些是唯一不需要更多
   樣本就能考慮的行動項；「需要更多樣本」的那些只是記錄，不必現在決定。
