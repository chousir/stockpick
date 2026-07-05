# 19 — 抓太晚＋漏起漲 診斷（M-Diag1 規劃書＋診斷結論）

> 回應外部研究 prompt 的兩題：「為什麼分析常是『已漲一段、再買太貴』」與「回檔準備起漲的股
> 是不是系統性抓不到」。**研究軌**（`make diagnose` → `research/diagnostic/`），不碰生產、不加
> 資料源、不加依賴（純 Polars）。全程 walk-forward 精神、樣本薄就誠實降級。
>
> **一句話結論**：兩題是同一根——**排序系統性追高（買已延伸、棄回檔 base）**。WS1 統計上
> 診斷它；WS2＋金融輪動斷點是它最貴的一次具體代價。

---

## 0. 一句話目標

用既有前瞻報酬引擎（`strategies.compute_forward_returns`）＋候選宇宙（`candidates_enriched`）＋
`daily_all` 全市場乾淨底料，**出診斷證據**：進場延伸度 vs 前瞻報酬曲線、排序訊號 IC、全市場
漏抓起漲目錄、雷達拆解——**不動任何生產閘門**（改革須另立 milestone 走回測）。

## 1. 指令與產物

- `make diagnose`（`tw-screener backtest diagnose`）→ `research/diagnostic/`：
  - `00_cp1_data_audit.md`：資料邊界盤點（CP1）。
  - `late_entry_*.md`＋3 CSV：WS1 延伸度曲線／排序訊號 IC／組內名次 skill。
  - `missed_launch_*.md`＋2 CSV：WS2 漏抓目錄（五態雷達＋敏感度格＋具名清單）。
  - `seam_financial_rotation_W21.md`：金融輪動斷點 worked example（手寫深追，非重跑產物）。
- 設定：`config/settings.yaml` → `backtest.diagnostic`（含 `missed_launch` 子區塊）。
- 碼：`backtest/diagnostic.py`（純函式）＋`diagnostic_runner.py`（IO）＋`tests/backtest/test_diagnostic.py`。

## 2. 資料天花板（CP1，決定能宣稱什麼）

- **候選宇宙**（WS1 母體）：`candidates_enriched` 逐週 ~140 檔 × 7 週 ≈ 987 列；r+10 到期
  ~705 列（W21–W25）、**r+20 只到 W21–W22**（薄）。
- **全市場乾淨底料**（WS2 母體）：`daily_all_*` 連續 248 交易日、1042–1090 檔/日，但
  **永久停在 2026-06-09**（`STOCK_DAY_ALL` 不可回補）→ 無偏全市場掃描交叉僅 **W21–W23**、
  事件 <30＝**初步**。拼接超集（到 07-03）廣度 181↔6828 跳動，只堪算 pick 前瞻、**不能**當
  掃描母體。
- **早週舊 schema**：W21–W23 的 `candidates_enriched` 無 foreign/trust 5d/10d 窗（修法6 才加）、
  無 rotation 報表（W24 才有）——限制了 WS2 的資金歸因與 rotation 對照。

## 3. WS1「抓太晚」診斷結論

母體＝候選宇宙；target＝超額報酬 vs 大盤；IC＝Spearman＋Fisher-z 解析 95% CI。

- **越延伸→前瞻越差（追高實證）**：`ma60_dist` IC **跨三窗全顯著負**（r+5 −0.16／r+10 −0.19／
  r+20 **−0.25**，CI 全不跨 0）。
- **F2 +15% 硬擋方向對、但偏鬆**：r+20 分桶 5–10% 中位 **+3.6%** → 15–20% **−10.1%**（勝率 25%）；
  **正期望值在 10–15% 就流失**。（不釘尖銳門檻——粗桶、單一 regime。）
- **短窗動能排序也追高**：`momentum_5d` IC r+5 −0.09（CI [−0.17,−0.02] 顯著）；長窗不顯著。
- `vol_ratio` 反而正向（r+20 +0.14 顯著）；`inst_pct20d`／`foreign_net_5d`／`ret_10d` CI 跨 0＝無證據。
- 組內名次 skill 弱（r+10 +0.12±0.07，borderline）。

## 4. WS2「漏起漲」診斷結論（A 純無偏路）

錨定候選週，對 `daily_all` 全市場算前瞻，篩「data_date 處回檔且未延伸、之後漲 ≥Y%」＝起漲
事件，交叉五態雷達：**held ＞ acted ＞ considered（screener 撈到未選）＞ watchlisted（觀察
清單有沒扣扳機）＞ never_surfaced（真沒雷達，依成交額拆 liquid/illiquid）**。

- **漏抓不在排雷閘門**：considered（閘門可歸因）跨設定僅 1–4 筆——回檔起漲**幾乎沒進到閘門**，
  turn-aware 閘門改革（prompt 的 WS5）**救不到它們**。
- **WS3 二階資金 a/b/c/d 分類不可行**：never_surfaced 股不在 candidates、無法人欄可歸因；
  能歸因的 considered ≈0–4 筆，本樣本無力。
- **漏抓分三機制，各有處方**：
  1. **輪動卡排序**（金融族群，見 §5）——最貴一筆。
  2. **AI 趨勢股卡進場紀律**（廣達 2382 在觀察清單、回檔 −7% 後 +19%，watchlisted 沒買）。
  3. **價值/循環卡覆蓋**（可成 2474／豐泰 9910——不在 AI 觀察清單、策略也不框）。
- 大量 never_surfaced 是**微型投機股**（如 9110 +46% 於 0.07M），流動性濾除後才是真缺口。

## 5. 金融輪動斷點（worked example，統一 WS1×WS2）

W21 金控自 base 齊漲（國泰金 +16.6%／富邦金 +18.7%／凱基金 +23.6%／台新新光金 +19.1%）。
**管線看到了**（金融 17 檔候選、國泰金族群內 #1、group_analysis 有列）但 **0 檔入 picks**，
picks 7 檔全給科技。為什麼？

| | 距季線中位 | 5 日動能中位 |
|---|---|---|
| W21 實際 picks（科技） | **+33%**（事欣科+57／全新+50） | +12% |
| 起漲金控（丟掉的） | **+6%** base | +2% |

- **排序追延伸（＝WS1）**：挑了 +33% 延伸科技（IC 前瞻負）、丟了 +6% 金控 base（前瞻正、
  然後輪動）。CP2 pick 閉環實測 W21 核心 α **−4.2%**——延伸科技回落、金融同時起漲。
- **族群優先序追近端強**：金融=group #7，名額被前面科技群吃滿。
- **土洋對作蓋整族**：8/17 掛旗，**起漲 4 檔 100% 掛旗**＝全族群同掛＝機械/輪動足跡（prompt
  WS2 假設 (b)「假土洋對作誤殺」的實例）。
- 限制：W21 無 rotation 報表、舊 schema 無近端流窗、n=1 族群事件。

## 6. 誠實限制（防過擬合鐵律）

- 事件 <30、單一 regime、`daily_all` 06-09 窗牆——**方向性使用，不支撐硬規則**；每季／W28+ 窗
  變厚重跑。
- CI＝Fisher-z 解析近似（非 bootstrap）；偏好單調關係、不釘尖銳門檻。
- watchlist/holdings 為當前快照、非 point-in-time，近似雷達成員。

## 7. 後續（不在本 milestone，待裁決另立）

- **WS5 排序改革**（排序別追高／貼底族群內領頭羊上浮／sector-wide 旗標降為輪動訊號／
  rotation×排序接縫）——須另立 milestone 走 walk-forward，與 [docs/18](18-intra-sector-laggard-production.md)
  族群內🔻落後濾鏡同向。
- **AI 趨勢股進場紀律**（watchlisted 沒買）／**價值循環回檔掃描**（兩張網都漏那層）——另議。

> 本 milestone 的交付＝**診斷證據＋統一結論（排序追高）**，不含任何生產改動。
