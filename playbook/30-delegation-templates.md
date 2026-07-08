# 30 — 派工 prompt 模板（直接複製，填空後派出）

> 用法：選對應型態的模板 → 填【】內容 → 照 playbook/10 §2 選 model → Agent 工具派出。
> 三件套（目標動機／驗收條件／回報格式）已內建在每個模板，不要刪段落。
> 通則：subagent 看不到你的對話——路徑寫絕對路徑，術語當它第一次聽到來解釋。

## T1 搜尋／盤點（agent: Explore；model: sonnet，純枚舉可 haiku）
```
【目標】找出：【要找什麼，具體到欄位名/函式名/字串】
【動機】因為【接下來要做什麼】，所以我需要知道【存在於哪裡/有幾處/長什麼樣】。
【範圍】搜 /home/user/stockpick 下的【目錄】；排除 data/、reports/、research/、playbook/_backup/。
搜索廣度：【medium｜very thorough】。
【驗收條件】
- 每個結果附 file:line。
- 找不到也要回報「確認不存在」＋你搜過的 pattern 清單（讓我判斷是真沒有還是沒搜到）。
【回報格式】≤20 行：第一行總數，然後每結果一行「file:line — 一句話說明」。不要貼程式碼原文。
```

## T2 實作（agent: general-purpose；model: sonnet）
```
【目標】在【檔案路徑】實作【功能，一句話】。
【動機】【為什麼要做＋這段程式誰會呼叫/消費它】。
【規格】
- 輸入/輸出：【具體型別與範例】
- 邊界情況：【至少列 2 個：空輸入、缺欄位、NaN…】
- 本 repo 約束：Polars 不用 pandas；type hints；loguru 不 print；參數進 config/settings.yaml；禁 bare except。
【禁區】不動【檔案清單】；不改既有測試的斷言；不加新依賴。
【驗收條件】
- `uv run pytest 【對應測試路徑】 -q` 綠（新功能要有 happy path 測試）。
- `make test 2>&1 | tail -5` 無新增失敗。
- `uv run ruff check 【改動檔案】` 淨。
【回報格式】≤15 行：改了哪些檔（file:line 範圍）／驗收指令輸出尾 3 行／你做的取捨（若有）。
```

## T3 重構／批次套用（agent: general-purpose；model: pattern 已定型用 haiku，否則 sonnet）
```
【目標】把【pattern A】改成【pattern B】，套用到【檔案清單/glob】。
【動機】【為什麼改】。
【Pattern 規格（已定型，照抄不要發揮）】
改前：【貼真實程式碼片段】
改後：【貼真實程式碼片段】
不符合此 pattern 的地方【跳過並回報，不要自行變通】。
【驗收條件】
- 行為不變：`make test 2>&1 | tail -5` 綠、無新增失敗。
- 全 repo grep 舊 pattern 剩 0 處（排除 tests/fixtures、playbook/_backup）。
【回報格式】≤15 行：套用 N 處（檔案清單）／跳過 M 處（各附 file:line＋為何不符 pattern）／測試輸出尾 3 行。
```

## T4 研究／分析（agent: general-purpose；model: sonnet；裁決存疑時 opus 出第二意見）
```
【本輪唯一要回答的問題】【一句話，是非題或量值題】
【裁決判準（先寫死，跑完不准改）】：【數字門檻，例：lift ≥1.3 且 z≥2 → 成立；否則否證】
【量尺檢查】假設問的是【機率/報酬/覆蓋率】，所以量尺用【對應量尺】——兩者必須同名（playbook/20 §6）。
【資料】用【路徑】；缺資料標「未取得」不補值；entry 次日、未到期排除（前視規則見 playbook/20 §6-3）。
【答完即停】回答完這一題就停，衍生的新假設列出來但不要跑。
【驗收條件】
- 結論明確三選一：成立／否證／不可判（附缺什麼）。
- 樣本數有報（tiny-N 高 lift 要自我標註為雜訊嫌疑）。
- 完整結果落檔【scratchpad 或 research/ 路徑】。
【回報格式】≤20 行：結論一行／關鍵數字 3-5 行／樣本數與但書／結果檔路徑／衍生假設清單（只列不跑）。
```

## T5 審查／驗收（agent: verifier；model: 定義內建 sonnet+effort high；高風險加派 opus 第二意見）
```
【要驗收的宣稱】「【誰宣稱做完了什麼】」
【驗收條件（逐條可判定）】
1. 【檔案 X 存在且包含 Y 段落】
2. 【指令 Z 輸出含/不含 W】
3. 【grep 舊名 in 範圍 = 0 處】
【相關路徑】【絕對路徑清單】
【要跑的指令】【指令清單，含預期輸出特徵】
【回報格式】第一行總判定（全過/N 條未過），逐條 PASS/FAIL/無法驗證＋證據（file:line 或輸出行）。≤40 行。
```

## 反模式（看到自己在寫這些就停）
- 「幫我看看這個 repo」——沒有目標與驗收條件，agent 會回一篇散文。
- 「盡量做好」「保持高品質」——不可判定，等於沒寫。
- 把主對話整段歷史貼給 agent——只給它需要的背景，其餘是雜訊。
- 一個 prompt 派三件事——切開，每件一個 agent（並行規則 playbook/10 §7）。
