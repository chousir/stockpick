# 10 — 模型調度守則

> 觸發條件：要派 subagent、選 model、寫委派 prompt 之前讀。模板直接抄 playbook/30。

## §0 指揮官不下場
判準是「**輸出大、而主對話只需要結論**」，不是檔案行數。出現以下任一，改派 subagent，主對話只收結論：
- 搜尋目標不明、預估要掃多個目錄或多種命名才找得到（連兩輪 Grep/Glob 沒命中就是訊號）。
- 用 WebFetch/WebSearch 跨多頁做研究。
- 批次機械修改超過 3 個檔案。
- 跑研究/掃描指令，輸出是大量明細而你只要裁決（輸出先落檔，派 agent 讀）。
- `reports/`、`research/` 下的產物檔要整份消化。

自己做（subagent 冷啟動要重讀背景，成本常高於任務本身）：已知路徑的讀檔與小改（長檔用 Grep 定位＋offset/limit 讀段，不為行數派工）、單一指令驗證、預估 ≤3 個工具呼叫能完成的事、需要與使用者來回討論的判斷。

記住：subagent 看不到你的對話。派工 prompt 必須自帶全部背景——檔案絕對路徑、術語定義、要什麼不要什麼。

## §1 環境事實（2026-09-26 查證；來源＝當日 Agent 工具 schema＋claude-api skill 模型表〔cached 2026-06-24〕）
**過期警告**：模型陣容會變。指定 model 報錯或行為不符時，以當下 Agent 工具 schema 的 enum 為準，改完把新事實更新回本段（並記 playbook/90）。

- 主對話模型（2026-09-26 起）：Opus 5.5（`claude-opus-5-5`，使用者以 /model 設為預設）。
- 內建 agent types：`general-purpose`（全工具；未指定 type 時的預設）、`Explore`（唯讀搜索，prompt 內指定 breadth："medium"／"very thorough"，以當下工具描述為準）、`Plan`（規劃）、`claude-code-guide`（查 Claude Code / Claude API 官方文件）、`claude`（泛用 catch-all）、`statusline-setup`。本 repo 自建：`verifier`（.claude/agents/verifier.md，sonnet + effort high，驗收專用）。
- **注意（2026-07-08 實測）**：session 開始後才新增/修改的 .claude/agents 定義，當前 session 看不到（報 not found），下個 session 才註冊。fallback＝改派 general-purpose 並把該定義內文整段貼進 prompt。
- Agent 工具 per-call 參數 `model`：enum 為 sonnet / opus / haiku / fable（2026-07-08 與 2026-09-26 兩次查證相同）。別名對應的確切版本**未查證**；依 API 當代陣容推定 sonnet≈Sonnet 5、haiku≈Haiku 4.5、fable≈Fable 5.1、opus≈Opus 5.x——需要確定時派 claude-code-guide 查。指定的 model 若不在組織允許清單，會**靜默退回**繼承主對話模型（不報錯）——結果品質異常時先懷疑這點。
- API 價位參考（$/1M in/out，claude-api skill 表）：Haiku 4.5 1/5、Sonnet 5 2/10、Opus 5.5 4/20、Opus 5 5/25、Fable 5.1 10/50。
- **effort 沒有 per-call 參數**。控制方式：(a) 自訂 agent 定義的 frontmatter `effort: low|medium|high|xhigh|max`（如 verifier）；(b) 不控則繼承 session 設定（settings.json，本機實測鍵名 `effortLevel`）。
- 模型解析優先序：環境變數 `CLAUDE_CODE_SUBAGENT_MODEL` > per-call `model` > agent 定義 frontmatter > 繼承主對話。
- CLAUDE.md 的 `@path` import 是**每 session 開場全部載入**（含巢狀，最多 4 層）。所以 playbook 一律用「路由指示」（要做 X 先讀 Y），**禁止在 CLAUDE.md 用 @import 引 playbook**。
- 程式內直呼 Claude API 的點（`src/tw_screener/report/builder.py`，model 由 `config/settings.yaml` 的 `report.llm.model` 決定）屬 API 範疇，不受本段 agent 別名影響；改它前先載 claude-api skill 查該模型的參數限制。

## §2 調度矩陣（任務型態 → model）
| model | 適用 | 例子 |
|---|---|---|
| haiku | 機械批次：pattern 已定型的逐檔套用、枚舉清點、格式轉換 | 「把這 20 個檔的 X 欄改名 Y（規格如下）」 |
| sonnet（預設） | 搜索結論、一般實作、測試修復、文件同步、驗收 | 「找出所有讀 candidates_enriched 的模組並回 file:line」 |
| opus | 架構設計、跨模組難 debug、需要取捨的評審、pick.md 合成 | 「這兩個修法各有什麼隱藏成本，推薦一個」 |
| fable | **僅限**高風險第二意見（研究裁決、不可逆決策、主對話與 opus 結論矛盾時）；使用者 2026-09-26 拍板，不作一般升級階 | 「兩個獨立推論矛盾，請獨立重推並指出誰的前提錯」 |
| 主對話模型（Opus 5.5） | 整合、裁決、與使用者對話 | —— |

主對話已是 opus 級：opus 級難題自己扛，不必派同級 subagent 重做一次（除非要的是 fresh context）；扛不動照 §6 處理。

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
  - 高風險判斷（研究裁決、對外報告、不可逆操作）→ 第二意見：換一個 model 獨立重推一次（主對話是 Opus 5.5，故第二意見用 fable；次要場合可用 fresh-context opus）；兩者矛盾 → 問使用者，不要自行擇一。

## §6 升降級路徑
- **haiku 錯 1 次 → 直接升 sonnet 重派。** 不給 haiku 第二次機會（重試成本高於升級差價）。
- **sonnet 同一子任務連錯 2 次 → 帶完整失敗軌跡升 opus**：原 prompt＋兩次輸出/錯誤＋你對失敗原因的猜測。不帶軌跡的升級＝讓 opus 從頭重犯一遍。
- **opus 也解不了 → 停**，整理成問題問使用者（附已試過什麼、卡在哪、你的猜測）。不做第四次重試。fable 不是這條階梯的下一階（§2），只在該題同時屬於 §5 高風險判斷時出第二意見。
- **降級**：解出可複製的「模式」後（例：同一修法要套 30 個檔），把 pattern 寫成明確規格，降回 haiku/sonnet 批次套用，verifier 抽查 2-3 個樣本。
- **重試上限**：同一件事最多兩輪（一輪＝一次派工＋一次修正機會）。第三輪之前必須換方法、換模型、或問人——三選一，不能原樣再來。

## §7 並行紀律
- 相互獨立的子任務在同一則訊息一次派出（多個 Agent call 並行）。
- 會寫同一批檔案的任務不並行。
- 同時最多 3 個 agent。想派更多＝任務沒切乾淨，先收斂。
