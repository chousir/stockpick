# 90 — 教訓日誌（append-only）

> 用法：踩了坑、被使用者糾正、發現規則失效 → 在檔尾 append 一條，格式照下面模板。
> 不要改寫舊條目（可以在新條目引用舊條目）。超過 150 行時的精簡程序見 playbook/40 §4。

## 條目模板（照抄）
```
## YYYY-MM-DD 一句話標題
現象：發生了什麼（1-2 行）
錯誤信念：當時以為什麼是對的（1 行）
修正：實際正確做法（1-2 行）
落點：這條教訓已寫進哪個規則檔（檔名＋段落），或「尚未制度化」
```

---

## 2026-06-14 量尺陷阱：裁決量尺必須與假設同名
現象：C-P1 落後度研究用「因子 vs 前瞻報酬 Spearman」裁決「起漲機率」假設，ρ=+0.007 差點誤判否證；改用起漲 lift 後 z=+54 強烈成立。
錯誤信念：沿用上一輪（B-P2）的裁決量尺就是一致性。
修正：問機率用機率量尺、問報酬用報酬量尺；動手前先寫下量尺再跑實驗。
落點：playbook/20 §6、playbook/00 三-3。

## 2026-06-18 文件與行為漂移是本 repo 最高頻錯誤
現象：README/docs/j2 模板多次指向已改名章節（如已停用的「Section 5 機械基準」），至少三輪「文件同步輪」在還債。
錯誤信念：改完程式行為，文件「之後再補」。
修正：改輸出格式/章節/欄位的同一個 commit 內，grep 舊名在 README、docs/、src/**/prompts/ 的所有出現處一次改完。
落點：playbook/20 §2 完成定義、playbook/00 三-1。

## 2026-07-08 commit/push 問人義務收窄的依據（審查留痕）
現象：舊 CLAUDE.md §2.6 要求 commit/push/merge 前都先問；2026-07-08 重寫後只保留「merge 進 main 必問」。對抗審查指出此收窄缺落點紀錄。
錯誤信念：規則改動可以只改結果、不留依據。
修正：收窄依據＝使用者 feedback memory（feedback_commit_push）：milestone 驗收後收尾 ritual 即 commit→push→提醒 /clear，是使用者建立的慣例。merge 進 main 必問維持不變。
落點：CLAUDE.md milestone 紀律段＋鐵律 3。

## 2026-07-08 MEMORY.md 索引肥大＝每 session 固定漏 token
現象：索引長到 12.7KB（單行塞整段專案史），每個 session 開場先燒數千 tokens。
錯誤信念：索引行寫越詳細，未來 session 越省事。
修正：索引一行一鉤 ≤120 字元，細節寫進記憶檔本體（recall 時才載入）。
落點：playbook/40 §3、playbook/00 一-1。

## 2026-07-10 全市場快取含 6 位數權證碼，「非 00 開頭＋全數字」濾不掉
現象：W28 面板首建 9,770 檔——daily/otc_daily 快照混入 7,793 檔 6 位數權證（70xxxx 等），通過 is_etf_or_warrant 語義的向量檢查，污染全市場等權基準。
錯誤信念：以為 is_etf_or_warrant（00 開頭或含字母）對任何來源都足以框出普通股宇宙。
修正：ground-truth 用途一律收緊為「恰 4 位數字且非 00 開頭」（TDR/ETN 一併排除並記錄）；並抽 unique id 數對台股常識（上市+上櫃 <2,000）做 sanity check。
落點：backtest/panel.py `_non_etf_expr` docstring＋docs/22 §1；screener 管線維持原函式（其宇宙來源本就乾淨）。

## 2026-07-10 n 數萬時 CI 不跨 0 ≠ 有訊號——效應量底線要寫進 runner
現象：WS-E 資金流 inflection 因子 IC −0.009、CI [−0.016,−0.002] 不跨 0，首版 console 判「存活候選」；實際效應量≈0（docs/19 有用訊號都在 0.09–0.25）。
錯誤信念：CI 不跨 0＋跨段同號就算存活（判準漏了效應量維度）。
修正：大樣本評估一律加 |IC| 底線（flow_inflection min_effect_ic=0.03，未校準啟發式明標）；裁決三件套＝CI＋效應量＋跨段一致。
落點：playbook/20 §6.5 原則已有，本次把它變成 runner 內建防線（flow_inflection_runner）；docs/22 §0 存活判準。

## 2026-07-12 日期相依測試在長連休假紅
現象：goodinfo test_is_stale／test_stale_cache_triggers_network 假設「2 天前必早於最近交易日盤後界線」；07-10（五）休市＋週末＝連 3 非交易日，週日跑測試時 2 天前的檔案仍新鮮（實作正確、測試日曆假設破）→ 全綠 suite 無故紅 2 條。
錯誤信念：相對 time.time() 的偏移量只要「夠多天」就穩定；2 天看似夠。
修正：涉及交易日界線的測試，偏移量要蓋住最長可能連休（取 10 天）；更優解＝直接用實作自身的界線函式構造時間戳（同檔 test_fresh_aligns_to_trading_day_boundary 即此法）。另：pipeline `make test | tail` 的出口碼是 tail 的——驗證步驟不可用 pipe 吞出口碼。
落點：本條＋測試已修（tests/screener/goodinfo/test_fetcher.py）；尚未制度化其他日期相依測試的排查。

## 2026-08-21 Goodinfo 被 Cloudflare 擋＋差點被引導去用 cf_clearance 繞過
現象：Goodinfo 篩選器端點某日起回 Cloudflare「初始化失敗」JS 挑戰頁（`/cdn-cgi/` 路徑確認），多台機器多網路環境實測皆同一結果——非本機 IP 被擋、非網站改版。AI 助理一度把「手動用瀏覽器過挑戰、複製 cf_clearance cookie 給程式沿用」當成「中間地帶方案」提給使用者，使用者也一度同意；後自我糾正：這本質就是讓自動化流量冒充已驗證瀏覽器，跟鐵律4禁用 playwright/selenium 想擋的是同一件事，只是換個技術手段，沒有真的更合規。
錯誤信念：「人手動解過一次挑戰」讓後續重放這個 token 的自動化請求也算合規；反爬蟲 cookie 重放 ≠ 一般 session cookie 續用。
修正：踩到反爬蟲防護升級時，不繞過防護本身——改盤點「這條篩選邏輯的門檻，官方 API 有沒有已經在抓、只是沒解析出來的資料能覆蓋」（本次因此發現市值/累計營收YoY 兩欄，見 docs/02、screener/local/）；沒有官方替代的條件就誠實承認該策略暫時跑不了，不找繞過防護的路。
落點：docs/02「市值/累計營收YoY 官方欄位」節、`screener/local/`；memory `project_sandbox_goodinfo_blocked`。CLAUDE.md 鐵律4 的精神（不建反爬蟲防護對抗手段）本條再次確認涵蓋 cookie 重放這類手法，非僅字面上的 playwright/selenium。

## 2026-09-20 對帳判準「全未過」先查量測，再談資料——別假設兩邊口徑一致
現象：FinMind 財報 vs 本地 fundamentals 正式對帳初跑四判準全未過（Revenue 中位 0.77、覆蓋 50.6%）。實為本地 Q2 是累計 YTD 卻被當單季比、覆蓋率分母含未回補個股；還原後中位恰 1.0000、四判準全過。
錯誤信念：寫模組時以為本地 fundamentals 是單季（欄名／docs 沿用「單季基本面」），且從沒對過一檔樣本。
修正：對帳／回測類模組落地前，先抽 2–3 檔手算兩邊同一格數值（本次 2330 Q2＝Q1+Q2 一眼可見）；判準未過時先排除量測錯位再下「資料不可用」結論；修量測不動門檻，並把初跑失敗結果與失敗後才修的事實一併留痕。
落點：docs/31 §20.15〔對帳裁決〕；`finmind_financials_reconcile._decumulate_local`＋測試。未驗證：本地 fundamentals 其他「單季」命名欄（如 `roe_q_pct`）口徑，尚未制度化。

## 2026-09-20 一個欄位查出累計口徑，同表其他「單季」欄要當場全查——且要查到消費端
現象：對帳查出 `fundamentals` 的 revenue/eps 是累計 YTD 後，順著查 `roe_q_pct`／margin 也全是累計，已餵進 G2/G1（G2 通過 382→實為 251）與報告「單季」標籤數月。
錯誤信念：schema 註解與 docs 寫「單季」，就當所有衍生欄（含比率、EPS/淨值）都是單季；上一條教訓當時只標「其他欄未驗證」，靠使用者追問才查。
修正：發現一欄口徑錯，同一輪把同源所有欄逐一實測（本次 975/975 檔對照）；還原放讀取層單點（`decumulate_fundamentals`），parquet 保持原始值；累積型底帳同步加口徑標記欄（`fund_basis`），否則新舊列同名不同義。
落點：docs/08 M-Fund-SingleQ、docs/02 §單季端點註記、docs/31 §11 口徑更正；承接 2026-09-20「對帳判準全未過」條。


## 2026-09-20 回歸錨測試 RED 十天沒人查根因——「既有 RED／flaky」不是診斷，且錨必須釘住輸入
現象：`test_w35_anchor_matches_production` 自 2026-09-04 起 RED（452 檔），先後被標「flaky」「面板重建與生產路徑系統性不一致」，各 milestone 驗收都寫「唯一 FAIL＝既有 RED」帶過。實查：測試比對日取 `val_history.max()`、產業對照讀「最新月」，兩者都隨快取成長漂移；釘回 W35 當時的日期（08-28）與 `industry_202608` 後 463 檔 0 不一致。
錯誤信念：把「進本 milestone 前就是 RED」當成「與我無關、不必查」；根因未驗證就寫進 docs/31 Open item 3。
修正：回歸錨對「外部會變的輸入」（最新日期、最新月檔、glob 最新）一律釘死到產生基準當時的值；RED 超過一個 milestone 就要當場二分（換輸入日期/檔案逐一比對，一支腳本即可）而不是標註帶過；未驗證的根因不寫進 docs。
落點：`tests/backtest/test_valuation_gap_panel.py`（`_W35_DATE`／`_W35_INDUSTRY_FILES`）、docs/31 §20.14 Open item 3 更正；承接 2026-06-18「文件與行為漂移」條。

## 2026-09-20 收尾 ritual 的 push 步被使用者撤回；且「下一步」字樣過期會誤導待辦盤點
現象：使用者明說「都不要 push，我最後會自己 push」，與 CLAUDE.md 收尾 ritual「commit → push」衝突（2026-07-08 條依據的 feedback_commit_push 慣例已被覆蓋）。同 session 我依 docs/08 第 1086 行過期的「下一步：Phase 2 另立」誤報 M-Val-FinMind2「尚未開工」，實際早已 merge。
錯誤信念：既往指示永遠有效；docs/08 舊里程碑段尾的「下一步」仍反映現況。
修正：收尾只 commit、回報註明「尚未 push」；盤點待辦以 docs/08 各 milestone 的「已 merge」狀態行與 `git log` 為準，不採信舊段落尾的「下一步」，並在完工時回填舊「下一步」字樣。
落點：CLAUDE.md「Milestone 紀律」收尾 ritual；memory `feedback-no-push-user-pushes`；承接 2026-07-08 條與 2026-06-18「文件與行為漂移」條。

## 2026-09-20 「外部來源沒有這欄」只憑我們自己的快取／parser 下結論，實測一次 API 就翻案
現象：M-Fund-SingleQ／M-G1-Converge 兩度寫「FinMind 無毛利欄、無法逐檔實證」；實際 `TaiwanStockFinancialStatements` 有 `GrossProfit`，是 `_FINANCIALS_FIELD_MAP` 沒收，快取才沒有。使用者同意新抓後，一次探測請求即證實，並完成 300 檔逐檔實證。
錯誤信念：快取裡沒有的欄位＝來源沒有；把「結構推論」當作無從驗證而擱置數輪。
修正：宣稱「外部來源無此資料」前先對來源做一次最小探測（單檔單 dataset 列出全部 type／欄位），不從自家 parser 輸出推斷；探測便宜（1 request），判準事前寫死後再抓樣本。
落點：docs/31 §20.16 (c) 更正與實證；承接 2026-09-20「對帳判準全未過」「一個欄位查出累計口徑」兩條。

## 2026-09-26 換模型時，直呼 API 的程式碼與 harness 文件要一起重審
現象：2026-09-20 把 `report.llm.model` 升到 claude-opus-5 時只移除 temperature，沒查到該模型預設開 thinking——`builder.py` 仍取 `content[0].text`、`max_tokens: 4000` 未計 thinking。換代重審 harness 文件時才發現；playbook/10 §1 也停在 07-08 的查證（fable 被寫成「當日特例」，實際仍在 enum）。
錯誤信念：模型升級＝改 model 字串＋拿掉會 400 的參數；harness 文件的環境事實季檢一次就夠。
修正：改 model ID 前先載 claude-api skill 查該模型的 thinking／effort／回應區塊結構差異；主對話換模型時當場重查 10 §1。
落點：CLAUDE.md 路由表「動程式內 Claude API 呼叫」列、playbook/10 §1 末條；builder 修復待另開分支（docs/08 Harness-Opus55）。

## 2026-09-28 子代理回報「全綠」，換個環境就 7 紅——rich 輸出斷言吃到 ANSI 色碼
現象：M-Pick1 實作子代理回報 make test 1435 passed；session 中斷後以 bg job 重跑，`test_picks_sync.py` 7 條 FAIL。bg job 環境帶 `FORCE_COLOR=3`，rich 在數字前後插 ANSI 碼，子字串斷言（如「rank 必須是 ≥1 的整數」）被切斷。
錯誤信念：子代理跑綠＝任何環境都綠；對 rich console 輸出做子字串比對只需去掉換行。
修正：斷言 CLI/console 輸出前先 strip ANSI（`re.sub(r"\x1b\[[0-9;]*m", "", …)`）；驗收時在主 session 的實際環境親跑一次，不只採信子代理的綠燈。
落點：`tests/report/test_picks_sync.py::_out`；尚未制度化（其他 capsys 斷言 rich 輸出的測試未普查）。

## 2026-09-29 拿沒量過吻合率的既有產物當「獨立參照」——面板 2026-06 起列數稀疏
現象：M-Pick3a 驗證除權息還原，第一版用 `research/panel` 的 r20（TWSE 現金股利線性加回）當參考，出現 ±40～85pp 的離譜差異，且「僅面板看見事件」的窗 5,551 個，遠超事件數能產生的窗數。查證後是面板自 2026-06 起列數稀疏（與 FinMind 列對列吻合率 ≤05 月約 88%→6 月 77%→7 月 62%；列數完整股票占比 2026Q1 88%→Q2 44%→Q3 0%），面板 r20 對多數股票不是 20 個交易日（docs/29 ③ 快取密度問題）。
錯誤信念：面板是驗過的 ground truth，直接拿來當參照就好。
修正：拿既有產物當參照前，先用同鍵 join 量它跟被驗資料的列吻合率再決定能不能用；本案改用同一批 FinMind 價格列＋官方預告表股利、以 `build_price_panel` 同函式自造參考。
落點：docs/33 §6.6（M-Pick3a 發現，含 M-Pick2 受影響範圍）；memory project-m-pick3a-paused。「參照物先驗參照物」尚未制度化（playbook/20 §6 屬黃區，待使用者同意再加）。

## 2026-09-29 事前寫的判準，先推導其統計量對極端值的敏感度再定門檻
現象：事前把「還原 r+20 與線性加回 r+20 的 |差| p99 ≤ 1.0pp」寫進 settings，並註解「一般 < 0.5pp」；真實資料 p99＝1.07pp 判 FAIL。兩種還原的差是二階項 (D/entry)·(exit/after−1)，隨除息後漲幅放大，最大的窗集中在 20 日漲 +25～+60% 的少數個股。
錯誤信念：二階項「通常很小」，1.0pp 門檻夠寬（沒用公式乘上資料的漲幅分布先估量級）。
修正：門檻不動（事前寫定就不動），照實保留 FAIL；另補明標「事後補充、不設判準」的診斷——用不含二階項的比值版官方參考重比，最大差 0.18pp。以後寫判準時，若統計量的理論式含隨極端值放大的項，先用該式在資料分布上估一次量級再定門檻。
落點：docs/33 §6.5；playbook/20 §6 開工三行「裁決判準」補充尚未制度化（黃區 rubric 本體，待使用者同意）。
