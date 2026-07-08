# 00 — Harness 診斷：漏 token、失焦、出錯的前三名

> 寫於 2026-07-08（Fable 5 制度建設 session）。所有數字為當日實測，非印象。
> 這份是其他 playbook 檔的立論依據；修法已落實的標【已修】，是規則的標【規則】。

## 一、最漏 token 前三名

### 1. 每-session 固定載入面過肥（最大宗，每次開場必付）
實測：CLAUDE.md 204 行（約 7KB）＋ MEMORY.md 索引 12.7KB ＝ 每個 session 開場先燒約 8–10k tokens，任何任務都逃不掉，全年重複付。主因是 MEMORY.md 單行塞整段專案史（違反「索引一行一鉤、內容進檔案」原則）。
修法：
- 【已修】CLAUDE.md 重寫為精簡路由＋鐵律（原檔備份在 `playbook/_backup/CLAUDE.md.20260708`）。
- 【已修】MEMORY.md 索引壓回一行一鉤。細節本來就在各記憶檔裡（實測 `project_cp_value_research.md` 33KB，遠厚於索引行），壓縮不失真。
- 【規則】之後每存一條 memory，索引行 ≤120 字元。想寫更多＝內容放錯位置，寫進記憶檔本體。

### 2. 大檔直讀進主對話
實測：docs/08 有 848 行、reports/ 共 2.1MB、最大單一記憶檔 33KB。主對話整檔 Read 只為找其中一段，一次燒 3–10k tokens，還把工作記憶擠掉。
修法（具體門檻，照做）：
- 預估 >200 行的檔案不整檔 Read。二選一：(a) 先 Grep 關鍵詞定位，再用 Read 的 offset/limit 只讀該段；(b) 帶著具體問題派 Explore agent，只回結論＋file:line（見 playbook/10）。
- `reports/`、`research/` 下的產物檔一律走 (b)。
- 例外：接下來要整檔改寫的檔案，才允許整檔讀。

### 3. 指令輸出不截流
make test（698 個測試）全量輸出、選股/週報指令的 DataFrame print、長 git log，全部灌進主對話。
修法（指令照抄）：
- 測試：`make test 2>&1 | tail -20`；紅了再 `uv run pytest <失敗檔路徑> -x -q` 縮小。
- 長輸出指令：導到 scratchpad 檔再 `tail -30`。
- `git log --oneline -10`，不看全量。

## 二、最容易失焦前三名

### 1. 跨主題不換 session
一個 session 從修 bug 滑到研究再滑到文件同步，context 塞滿三件事的殘渣，每件品質都降（已有 feedback memory 記錄此教訓）。
修法：一 session 一件事。使用者切換主題 → 先建議 /clear；單一長任務進行到中段 → 建議 /compact。

### 2. 研究軌自我發散
每個假設衍生三個新假設，說好跑一輪變五輪，歷史上多次靠使用者拍板止血。
修法：研究任務開工先寫死三行——「本輪唯一要回答的問題」「裁決判準（含數字門檻）」「答完即停」。判準沒過＝否證＝收穫，照實記錄，不自行加賽。詳見 playbook/20 §6。

### 3. Milestone 蔓延
「順手」做了下一個 milestone 的事，或驗收沒跑完就開新工。
修法：一次一個 milestone；驗收指令跑過＋完成清單交付後必停，等使用者說下一步（CLAUDE.md 紀律段）。

## 三、最容易出錯前三名

### 1. 文件與行為漂移（本 repo 實證最高頻的錯）
歷史至少三輪「文件同步輪」在還債：README/docs/j2 模板指向已改名的章節、已停用的段落（實例：docs/11 曾叫 Claude 去讀已不存在的「Section 5 機械基準」）。根因＝改行為時沒有文件同步 checklist。
修法：完成定義（playbook/20 §2）含文件同步項——改了輸出格式/章節編號/欄位名，就 grep 舊名在 README、docs/、src/**/prompts/ 的所有出現處，同一個 commit 內一次改完。

### 2. 從記憶編數字、憑印象寫技術參數
報告裡的財務數字、程式裡的 API/模型名/frontmatter 欄位，憑印象寫十之八九錯版本。
修法：報告數字只能來自 data/cache 或當次抓取，沒有就寫「未取得」（CLAUDE.md 鐵律）；harness/API 技術事實用 claude-code-guide agent 或官方文件查證。本 playbook 的模型清單也標了查證日期，過期要重驗（見 playbook/10 §1）。

### 3. 研究量尺錯配＋自驗
實證案例（C-P1 量尺陷阱）：問題是「起漲**機率**」，卻用「因子 vs 前瞻**報酬**的 Spearman」裁決，ρ=+0.007 差點誤判否證；換成 on-target 起漲 lift 後 z=+54 強烈成立。另一類：自己寫的東西自己驗收說沒問題。
修法：裁決量尺必須與假設同名（問機率用機率量尺、問報酬用報酬量尺），動手前先寫下量尺再跑實驗；驗收一律派 fresh-context agent，不自驗（playbook/10 §5）。
