# 10 — 模型調度守則

> 觸發條件：要派 subagent、選 model、寫委派 prompt 之前讀。模板直接抄 playbook/30。

## §0 指揮官不下場
主對話（指揮官）做以下任何一件事＝下場，應改派 subagent，主對話只收結論：
- 連續超過 2 輪 Grep/Glob 還沒找到目標，仍繼續掃。
- Read 超過 200 行的檔案（目的不是整檔改寫）。
- 用 WebFetch/WebSearch 跨多頁做研究。
- 批次機械修改超過 3 個檔案。
- 跑研究/掃描指令後自己讀全量輸出。

例外（自己做反而省）：已知路徑的單檔小改、<200 行的單檔閱讀、單一指令驗證、一兩分鐘內能完成的事（subagent 冷啟動成本高於任務本身）。

記住：subagent 看不到你的對話。派工 prompt 必須自帶全部背景——檔案絕對路徑、術語定義、要什麼不要什麼。

## §1 環境事實（2026-07-08 查證；來源＝當日 Agent 工具 schema＋code.claude.com/docs/en/sub-agents.md）
**過期警告**：模型陣容會變。指定 model 報錯或行為不符時，以當下 Agent 工具 schema 的 enum 為準，改完把新事實更新回本段（並記 playbook/90）。

- 內建 agent types：`general-purpose`（全工具，預設）、`Explore`（唯讀搜索，指定 breadth："medium" 或 "very thorough"）、`Plan`（規劃）、`claude-code-guide`（查 Claude Code / Claude API 官方文件用）。本 repo 自建：`verifier`（.claude/agents/verifier.md，sonnet + effort high，驗收專用）。
- Agent 工具 per-call 參數 `model`：2026-07-08 的 enum 為 sonnet / opus / haiku / fable。**fable 是當日特例，之後不可假設存在**。指定的 model 若不在組織允許清單，會**靜默退回**繼承主對話模型（不報錯）——結果品質異常時先懷疑這點。
- **effort 沒有 per-call 參數**。控制方式：(a) 自訂 agent 定義的 frontmatter `effort: low|medium|high|xhigh|max`（如 verifier）；(b) 不控則繼承 session 設定（settings.json，本機實測鍵名 `effortLevel`，官方文件亦作 `effort`）。
- 模型解析優先序：環境變數 `CLAUDE_CODE_SUBAGENT_MODEL` > per-call `model` > agent 定義 frontmatter > 繼承主對話。
- CLAUDE.md 的 `@path` import 是**每 session 開場全部載入**（含巢狀，最多 4 層）。所以 playbook 一律用「路由指示」（要做 X 先讀 Y），**禁止在 CLAUDE.md 用 @import 引 playbook**，否則瘦身白做。

## §2 調度矩陣（任務型態 → model）
| model | 適用 | 例子 |
|---|---|---|
| haiku | 機械批次：pattern 已定型的逐檔套用、枚舉清點、格式轉換 | 「把這 20 個檔的 X 欄改名 Y（規格如下）」 |
| sonnet（預設） | 搜索結論、一般實作、測試修復、文件同步、驗收 | 「找出所有讀 candidates_enriched 的模組並回 file:line」 |
| opus | 架構設計、跨模組難 debug、第二意見、需要取捨的評審 | 「這兩個修法各有什麼隱藏成本，推薦一個」 |
| 主對話模型 | 整合、裁決、與使用者對話 | —— |

主對話本身若已是最強可用模型，難題自己扛；扛不動就照 playbook/20 §3 整理成問題問使用者，不要往更弱的模型派。

## §3 派工三件套（缺一不派）
每個 Agent prompt 必含三段：
1. **目標與動機**：要什麼＋為什麼要（動機讓 agent 在邊界情況能自行取捨，不用回來問）。
2. **驗收條件**：可判定的清單——能跑的指令、能檢查的具體事實。「做好做滿」「保持品質」不算驗收條件。
3. **回報格式**：明定結構與行數上限。

## §4 回報合約
- subagent 只回：結論、關鍵證據（file:line）、驗收條件逐條狀態。
- 長產物（報告、diff、掃描結果）落檔到 scratchpad 或指定 repo 路徑，回報只給路徑＋3 行摘要。
- 回報超過約 40 行＝派工 prompt 的回報格式沒寫好，下次修。
- subagent 的回報不要整段轉貼給使用者，消化成 2–5 句。

## §5 驗證不自驗
- **誰做的誰不驗。** 驗收一律開 fresh-context 的新 Agent call；不要 SendMessage 回原 agent（它的 context 已被自己的工作污染，會傾向說自己是對的）。
- 預設用 `verifier` agent：給它驗收條件清單＋檔案路徑＋要跑的指令。
- 分型態：
  - 檔案落地 → read-back（verifier 親讀目標段落核對）。
  - 程式碼 → 跑 `make test 2>&1 | tail -20`＋實跑受影響指令。
  - 高風險判斷（研究裁決、對外報告、不可逆操作）→ 第二意見：換一個 model（建議 opus）獨立重推一次；兩者矛盾 → 升級或問使用者，不要自行擇一。

## §6 升降級路徑
- **haiku 錯 1 次 → 直接升 sonnet 重派。** 不給 haiku 第二次機會（重試成本高於升級差價）。
- **sonnet 同一子任務連錯 2 次 → 帶完整失敗軌跡升 opus**：原 prompt＋兩次輸出/錯誤＋你對失敗原因的猜測。不帶軌跡的升級＝讓 opus 從頭重犯一遍。
- **opus 也解不了 → 停**，整理成問題問使用者（附已試過什麼、卡在哪、你的猜測）。不做第四次重試。
- **降級**：解出可複製的「模式」後（例：同一修法要套 30 個檔），把 pattern 寫成明確規格，降回 haiku/sonnet 批次套用，verifier 抽查 2-3 個樣本。
- **重試上限**：同一件事最多兩輪（一輪＝一次派工＋一次修正機會）。第三輪之前必須換方法、換模型、或問人——三選一，不能原樣再來。

## §7 並行紀律
- 相互獨立的子任務在同一則訊息一次派出（多個 Agent call 並行）。
- 會寫同一批檔案的任務不並行。
- 同時最多 3 個 agent。想派更多＝任務沒切乾淨，先收斂。
