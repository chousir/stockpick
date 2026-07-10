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
