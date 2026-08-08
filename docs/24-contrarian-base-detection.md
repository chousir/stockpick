# 24 — 底部左側偵測（M-BR1）：賣壓熄火 × 基本面完好 × 貼近結構低

> 規格代號 M-BR1。**這是一份「先揭露、後驗證、最後才 gate」的方法論筆記。**
> 任何一段把新啟發式直接寫成進出場硬規則者＝違規（重蹈 docs/22 §2 flow_turn 覆轍）。
> 對應分支 `feat/m-br1-contrarian-base`；純函式在 [analysis/contrarian.py](../src/tw_screener/analysis/contrarian.py)。

## 0. 動機與失敗前例（先讀）

現行讀法把 `risk_kind=價格已跌`（跌破季線且未收復）一律「原則剔除」，
把**基本面完好、只是被外資籌碼殺下來、已跌到結構低、外資近端開始回補**的股票
一併誤殺。實測（2026-W29／資料日 2026-07-20）：記憶體族群單月 YoY +190%~+621%、
毛利 47~76%（基本面全科技最強），但被外資 sell-the-news 倒貨、股價破線；其中十銓／
威剛／群聯外資近 5 日已翻買、且在或貼近 60 日低——這批被整桶排除＝**系統性偽陰性**。

**但務必記取教訓**：docs/22 §2 已證 `flow_turn`（退潮/回流）三窗 CI 全跨 0、無前瞻
證據，「退潮→降級」舊讀法作廢。ΔRank、CP 加速度同屬描述性欄。**「賣壓熄→可逢低」
直覺上合理，但直覺 ≠ 前瞻證據**。本規格的存在意義，是把這個直覺**做成可被回測
證偽的欄位**，而不是再加一條沒證據的 gate。

**與 roadmap 的銜接**：本規格是 (a)「近端外資反轉旗標」的**賣方鏡像**（原只做買方
20d 蓋 5d 反轉，這裡補賣方 20d 蓋 5d 熄火）；(b)「flow 從 level-based 改 inflection」
的落地；(c) PO4 偽陰性帳的擴充。不是新增獨立系統。

## 1. 三階紀律（描述 → 驗證 → gate）

| 階段 | 狀態 | 內容 |
|---|---|---|
| **Phase 1 描述** | ✅ 本分支完成 | 四欄進三份 enriched CSV（純加法、零行為變更），surface 給人看與回測 |
| **Phase 2 驗證** | ✅ 已跑・**否證**（見 §3.1） | 面板重建兩條件、lift block-bootstrap CI、跨 regime——**三門檻全數未過**，聯合桶反而顯著落後宇宙 |
| **Phase 3 gate** | ⛔ **永久未啟用**（統計路徑） | Phase 2 否證 → `contrarian_base` 及其上游四欄定案為**描述欄**，不因回測結果升 gate（同 flow_turn） |
| **人工解禁** | ✅ 2026-08-08（裁決 A・**非統計證據**） | 另開 `contrarian_ready` 欄：三條件 tag ＋防接刀兩條 → 小注進**機會層**、永不核心。詳 §6 |

> ⚠️ **Phase 3 與人工解禁是兩條不同的路，不要混讀**。Phase 3 是「拿統計證據升 gate」，
> 它**依然關著**——§3.1 的否證紀錄完全有效。§6 是委託人在知情否證的前提下，用人工裁決
> 開了一條**小注、可回收**的旁路。任何文件、報告、prompt 提到這件事，證據狀態一律寫
> **「兩條件桶已否證；三條件桶未測、且先驗不利」**，不得寫成「未驗證」或「回測完成前」。

**升 gate 硬門檻（Phase 2 通過條件）**：方向 ≥4/5 walk-forward 分段一致 ＋ block-bootstrap
CI 排除 0 ＋ **跨 ≥2 regime**（尤其須含 2022 空頭年）。三缺一 → `contrarian_base`
永遠只是描述欄＋人工觀察，標 **bull-only evidence**、不寫入 picks:。

**永不入核心（不因回測結果放寬）**：底部左側標的趨勢仍破（跌破 MA60），天生過不了
F2 位階紀律與三鏡頭，最高只能列機會/觀察、且強制小注。

## 2. Phase 1 揭露欄（已實作）

四欄同進 candidates / holdings / watchlist 三份 enriched，重疊股沿用 candidates 那筆
（`_CANONICAL_REUSE_FIELDS`）。門檻全進 `settings.contrarian_base`，不寫死。

### 2.1 `fundamental_health` ∈ {強化, 穩健, 減速, 轉差, 待查}
**價值全在「把 level 與二階導拆開」**：記憶體 YoY +621% 但加速度見頂＝`穩健/減速`
而非 `強化`，這正是外資倒貨卻股價跌的真因（sell-the-news）；聯發科 YoY +2.8%＝`減速`，
即使跌深也不該被當「基本面完好」。
- 二階導 `rev_yoy_delta`＝本月 YoY − 上月 YoY，由 [twse.load_revenue_yoy_deltas](../src/tw_screener/data/twse.py)
  純讀既有 `revenue_*.parquet` 算出（不多打網）。
- **誠實 null**：無 rev_yoy → `待查`（不猜）；delta 缺月（上月無快取）→ **不標強化**
  （「是否加速」沒量到就不能宣稱加速）；金融/缺表 net_margin=None → 不做薄利校正。
- **限制**：`revenue_*.parquet` 目前僅 3 個報告月，故 `rev_yoy_delta` **只在即時週報
  算得出、無歷史值**——Phase 2 回測不納入本欄（見 §3）。

### 2.2 `{foreign,trust,inst}_flow_inflection` ∈ {轉買, 熄火, 加速賣, 轉賣, 加速買, 平穩}
賣方鏡像・二階導偵測。**與 `flow_state`（grouping.near_flow_state）的分工**：
- `flow_state` 結構上是**買方旗標**——要求 20 日淨買超 ≥ min_shares，20 日為負時回
  None，天生表達不了「由賣轉買」。
- `flow_inflection` 是**雙向**分類，20 日買賣兩側都評，才看得見「20 日賣超但近 5 日
  翻買」這個左側佈局要抓的區間。量門檻取**兩窗較大者**（群聯 20d +210 張因買賣互抵
  而極小、5d +1,687 張才是真訊號，單看 |20d| 會誤濾）。
- 兩欄並存不互相取代：賣方鏡像若併入 flow_state，會讓部分 None 變有值、連帶改動
  `classify_risk_kind` 的 `籌碼熄火` 判定，違反 Phase 1「既有輸出逐位元不變」。
- **鐵律（沿 F5）**：近端佔比/拐點**單獨無判別力**（docs/22 已證），一律描述欄。

### 2.3 `dist_low_{20,60}d_pct` + `base_proximity` ∈ {在低(≤2%), 貼低(≤5%), 中段, 高檔}
把「跌深」與「跌到結構」區分開：左側接刀**必須貼近結構低才有支撐**；中段回檔
（華邦電距 60 日低 +72%）接刀＝無支撐、不算左側佈局。

### 2.4 `contrarian_base`（bool・描述 tag）
三條件同時成立：`fundamental_health ∈ {強化,穩健}` ∧ `foreign_flow_inflection=轉買`
∧ `base_proximity ∈ {在低,貼低}`。**Phase 2 通過前僅為描述 tag，不進 picks、不改
excluded 行為。**

## 3. Phase 2 驗證路徑（已跑 2026-07-22・裁決 2026-07-21 拍板）

**原規格設想的路徑走不通，改採面板重建**：
1. **PO4 excluded 帳撐不起檢驗**：全歷史 7 週 `reason=價格已跌` 僅 **7 筆**（vs 過熱 31／
   土洋對作 24），分兩桶對照後每桶個位數。降為個案敘事，不當裁決依據。
2. **rev_yoy 無歷史**（§2.1）→ `fundamental_health` 不進回測。
3. **可回測的只有兩條件**：`flow_inflection=轉買` × `base_proximity ∈ {在低,貼低}`，
   用 WS-C 對 rotation 欄做過的**歷史面板 point-in-time 重建**。本機面板為**就地重建**
   （F1 底帳同型的本機資料斷層）：價格 `make backfill-daily-history`（MI_INDEX bulk）＋
   法人 `make backfill-institutional-history`（新指令，T86 逐日、不依賴 latest 錨點）→
   `make regime-history` → `make build-panel`（2022-01-03～2026-07-20、749k 列、核價 100% PASS）。
   **歷史段 TPEX 上櫃回查為 no-op → 實質上市宇宙檢驗，OTC 2022-24 永久斷供**（誠實揭露）。
4. 正式因子檢驗：新 `make contrarian-efficacy`（[backtest/contrarian_efficacy.py](../src/tw_screener/backtest/contrarian_efficacy.py)，
   重用 analysis/contrarian.py 純函式）——**桶 lift** block-bootstrap CI＋walk-forward＋
   regime 切片；裁決規則同 §1 硬門檻。

**開工三行（研究軌紀律 playbook/20 §6）**：
- 唯一問題：「賣壓熄（轉買）× 貼近結構低」兩條件同時成立的桶，前瞻報酬是否顯著優於
  基率，且跨 regime 一致？
- 裁決判準：CI 排 0 ＋ |效應量| 達底線 ＋ ≥4/5 walk-forward 同號 ＋ 跨 ≥2 regime。
- 答完即停：過 → 交 F1 拍板升 Phase 3 機會層 gate；未過 → 記否證、`contrarian_base`
  永久描述欄（如 flow_turn）。

**量尺修正（跑之前踩到、記下）**：面板 `alpha{h}=r−當日中位`，報酬右偏 → 全宇宙 alpha
**均值 > 0**（r+20 全體 +2.26%）。故「桶 alpha 均值 > 0」不是「打敗市場」的檢定（隨機桶
均值就 >0，大樣本必顯著＝§6.5 陷阱）。正確量尺＝**lift{h}＝alpha{h} − 當日全宇宙 alpha
均值**（零假設才是 0）。初版 gate 誤用 pooled iid CI＋桶 alpha vs 0，會**假陽性放行**；
讀數字時抓到並改為 lift＋moving-block CI＋防禦硬化，結論才誠實。

## 3.1 結果與裁決（2026-07-22・**否證**）

面板週快照 **201 週**（2022-01-07～2026-07-20，上市宇宙）；焦點桶（轉買×貼近低）命中
**2,477 股×週**。**三門檻全數未過 → `contrarian_base` 定案為描述欄（同 flow_turn）、
永不升 Phase 3。**

> 📌 **後續（不修改上述裁決）**：2026-08-08 裁決 A 以**人工覆寫**開了 §6 那條旁路——
> 另設 `contrarian_ready`（三條件 ＋ 防接刀兩條）小注進機會層。本節「不寫入 picks」的
> 原文因此**僅適用於 `contrarian_base` 本身**（它仍不進 picks）；統計路徑（Phase 3）
> 依然關著，本節所有數字與判定完全有效。

| §1 門檻 | 結果 | 判定 |
|---|---|---|
| block-bootstrap CI 排 0 且**正** | 全樣本 lift(r+20)＝**−2.30%**，CI95 **[−3.52, −1.26]**（n_dates=174，整段在 0 以下） | ✗ 桶**顯著落後**宇宙 |
| ≥4/5 walk-forward 段 lift 為正 | **1/5**（段 lift −1.43／+0.10／−4.86／−3.71／−8.20%，近段惡化） | ✗ |
| 跨 ≥2 regime 且**含 2022 空頭**為正 | 進攻 **−3.15%** [−5.35,−1.55]、中性 **−1.94%** [−3.61,−0.60] 皆顯著**負**；防禦（2022 空頭）n_dates=25 樣本不足、CI [−1.67,+0.80] 跨 0 | ✗ 0 個可判且正切片；防禦不可判 |

**解讀**：flow×base 分解格證實 lift 集中在 **高檔（延伸）** cell（+5% 級・動能）、而焦點
的「轉買×在低/貼低」是**全格最弱之一**（轉買×在低 +0.63%／貼低 +0.58%，遠低於全體 +2.26%）。
「賣壓熄（外資轉買）→ 貼低可承接」直覺**沒有前瞻 edge、反而系統性落後**；報酬集中在動能延伸
股，與逢低承接相反。此因子與 `flow_turn`（docs/22 §2）同進否證檔案。

**誠實殘餘**：防禦（2022 空頭）僅 25 個 point-in-time 快照日、樣本不足 → 空頭年**單獨**不可
下定論；但在樣本充足的進攻/中性 regime，桶皆**顯著落後**，且無任一 regime 有正 edge → 依 §1
「三缺一即維持描述欄」與 §5「未過空頭前不得升」，裁決為**否證、維持描述欄**。若日後 OTC 歷史
或空頭樣本變厚，可重跑 `make contrarian-efficacy` 複核，但**舉證責任在證明有 edge**，非相反。

產物：`research/contrarian_efficacy/contrarian_efficacy_20260722.md`（gitignore 本地研究產物）。

## 4. 回歸測試 ground-truth（2026-07-20 資料）

判斷錯即 bug。核心意圖：**京元與聯發科必須被排除在 contrarian_base 之外**——證明
分類器沒退化成「跌深就接」。完整斷言在 [tests/analysis/test_contrarian.py](../tests/analysis/test_contrarian.py)。

| 股號 名稱 | foreign 20d/5d | YoY | 期望分類 | contrarian_base |
|---|---|---|---|---|
| 4967 十銓 | −3,324／+1,002 | +35% | 轉買＋穩健＋在低 | **TRUE** |
| 8299 群聯 | +210／+1,687 | null | 加速買（近端才是真訊號） | 候選 |
| 2449 京元電子 | +41,263／−23,442 | +28.6% | **加速賣**（20d 蓋住近端倒貨） | **FALSE** |
| 2454 聯發科 | −6,853／−2,007 | **+2.8%** | 加速賣＋**減速** | **FALSE** |
| 2344 華邦電 | −305,441／−24,885 | +190% | 穩健但中段未貼低 | **FALSE** |

> 註：華邦電近 5 日日均（4,977 張）遠低於 20 日日均（15,272 張）＝賣壓**減速**，依
> `flow_inflection` 公式判 `平穩`（規格 §6 表原標「加速賣」與其自訂加速定義矛盾，
> 依公式為準）；`contrarian_base=FALSE` 結論不受影響（中段未貼低已擋下）。

## 5. Guardrails（不要做什麼）

- ❌ 不要把三條件直接寫成進場硬規則——**統計路徑**必須先過 Phase 2（§1 硬門檻）；
  §6 的人工解禁是另一條路，且明文標為「非統計證據」。
- ❌ 不要讓 `contrarian_base`／`contrarian_ready` 進核心層（趨勢仍破、過不了 F2）。
- ❌ 不要在未過空頭 regime 前，用單一多頭樣本的正 lift 就升 gate。
- ❌ 不要 fabricate 產業/報價/接單數字進 `fundamental_health`——只吃表內 rev_yoy/margin。
  產業循環判讀（如 DRAM 報價）屬報告端外部查證層、不進管線硬編。
- ❌ 不要把左側承接階梯設成「無限攤平」——再破新低即停止承接（規格 §4.2）。

---

## 6. 人工解禁（2026-08-08・裁決 A・**非統計證據**）

> 狀態：已實作（委託書 M1，分支 `feat/enhance-v3`）。本節不覆蓋、不刪改 §3.1 的否證紀錄。

### 6.1 為什麼會有這一節（誠實敘事）

W29（資料日 2026-07-20）系統已正確判定記憶體族群基本面為全科技最強（YoY +190~621%、
毛利 47–76%）、外資殺的是 sell-the-news、Bucket A（十銓/威剛/群聯/晶豪科）可小注左側
承接——但依 §1 三階紀律「Phase 2 未過即描述欄」，全部只留附錄。一週後（7/31 起）該族群
+20~40%，同期被停損的創見本身就在受惠名單內。

**這是一次事件，不是一組證據。** §3.1 的 201 週、2,477 股×週檢驗說的是相反的話。
委託人（Steven）在知情此否證的前提下裁決解禁，理由是「機會成本的分佈不對稱」而非
「回測其實是對的」。本節如實記錄這個取捨，不替它加工成統計支持。

### 6.2 被否證的是什麼、沒被測的是什麼（**引用本節時必須整段帶走**）

| | 內容 | 證據狀態 |
|---|---|---|
| **兩條件桶** | `foreign_flow_inflection=轉買` ∧ `base_proximity ∈ {在低,貼低}` | **已否證**（§3.1：201 週、2,477 股×週，lift r+20 **−2.30%**、CI95 [−3.52,−1.26]、walk-forward 1/5 為正、進攻 −3.15%／中性 −1.94% 皆顯著負） |
| **三條件桶**（`contrarian_base`） | 上列 ∧ `fundamental_health ∈ {強化,穩健}` | **未測**——`fundamental_health` 依賴 `rev_yoy_delta`，而 `revenue_*.parquet` 只有 3 個報告月、**無歷史**（§2.1、§3 第 2 點），面板重建時根本算不出這一欄 |
| **合格左側票**（`contrarian_ready`） | 上列 ∧ 外資連 ≥2 日買 ∧ 未破 60 日新低 | **未測**（同上，且再加兩條防接刀） |

**唯一正確的一句話**：「兩條件桶已否證；三條件桶未測，且**先驗不利**——它的父集合在
2,477 樣本上顯著落後宇宙。」

**不可以寫的**：「未驗證」「回測完成前」「尚待檢驗」——這些寫法會讓讀者以為證據是空白，
而實際上證據是**反向的**。（此表述法沿用 docs/26 §3.1 對 `3-of-7` 的處理：「未測，且先驗不利」。）

### 6.3 合格條件與圍欄

`contrarian_ready = contrarian_base ∧ 外資尾端連買 ≥ min_buy_streak_days ∧ dist_low_60d_pct > new_low_eps_pct ∧ picks_unblocked`

純函式 [`contrarian_entry_ready`](../src/tw_screener/analysis/contrarian.py)；門檻全進
`settings.contrarian_base`，零寫死。`foreign_inflection_days`（外資尾端連續買超天數）由
[`trailing_buy_streak_days`](../src/tw_screener/analysis/contrarian.py) 算，與 M4.1 共用同一欄。

> **「連 ≥2 交易日」的讀法（實作取的是嚴的那個）**：算的是**尾端**連續買超天數——
> 最新一個交易日若在賣，streak 就是 0，即使近 5 日累計仍為正（＝`轉買`）。另一種讀法
> 「近 5 日**期間內曾**連 2 日買」較寬，但那正是接刀風險所在（買盤已停、只剩前段殘留）。
> 取嚴的一邊符合本條的目的（防接刀），且門檻走 settings 可調。
>
> **首週實測（2026-W32、資料日 2026-08-04）**：`contrarian_base` 命中 2 檔（勝一 1773、
> 精成科 6191，皆穩健×轉買×貼低），但兩檔 `foreign_inflection_days=0`（最新日外資在賣）
> → `contrarian_ready` **0 檔**。防接刀確實在擋東西，不是裝飾。

`dist_low_60d_pct` 用 `low_60d`（窗**含當日**）算，故 `dist=0` ＝當日收盤就是 60 日新低
＝仍在破底 → 不合格。`new_low_eps_pct` 預設 0，要更保守可調高。

| 圍欄 | 內容 | 落地位置 |
|---|---|---|
| 永不核心 | 左側票趨勢仍破，最高機會層 | §1／週報 prompt patch-1；F2 本來就只擋 core 的 >+15%，**不擋負乖離**（tests/report/test_pick_store.py 已鎖測） |
| 小注 | 單筆上限＝一般機會層的 ½（事件閘門封 ⅓ 時再疊乘） | `size_ratio_vs_opportunity`（無程式消費，prompt 引述） |
| 進場階梯 | T1 現價 20%／T2 `low_60d` 30%／T3 深殺保留 50%；**前小後大**（與右側拉回相反）；收盤再破 `low_60d` 新低＝取消後續批、停止承接 | `entry_ladder_pct`（同上） |
| 停損語意 | 收盤破 `low_60d` 新低＝築底失敗出場，**明標「非 MA60、屬結構低停損」**；MA60 對已破線股禁止套用 | prompt patch-1／patch-2 |
| 分層記帳 | `thesis` 前綴固定 `左側M-BR1` | `thesis_prefix` |
| 自動回收 | 累積 ≥10 筆或滿 12 週後，若 α **與**勝率同時劣於一般機會層 → 週報必印「左側解禁回收警告」 | `contrarian_recall_check`（`make pick-outcome` 報表 §7）；回退＝`picks_unblocked: false` |

### 6.4 回收條款的統計效力（不要高估這道煞車）

判定門檻是 **10 筆／12 週**，對上已否證的 **2,477 股×週**——效力**極不對稱**。這本帳
抓得到「明顯很糟」，抓不到「小幅為負」；它是煞車，**不是**「解禁其實安全」的證據。
且判定要求 α 與勝率**同時**劣化（單邊劣化不觸發），這是刻意設寬以免小樣本亂殺，
代價就是更遲鈍。任一組無到期樣本時**不判定**（不用空集合宣告劣化）。

### 6.5 回退怎麼做

```yaml
# config/settings.yaml
contrarian_base:
  picks_unblocked: false   # ⬅ 改這一行；contrarian_ready 整欄恆 False，回退成純描述 tag
```

不刪碼、不改 schema——`contrarian_ready` 欄仍在（恆 False），歷史底帳與 pick_outcome
的分層記帳不受影響，日後要再開也只是改回 `true`。
