# 23 — Backtest R2 W28 推論硬化收官檔（骨架）

> 本檔＝`feat/backtest-r2-w28` 分支收官檔骨架，WS-I（factor_lab 推論硬化：CI 從 Fisher-z
> 升級為 moving-block bootstrap 預設）完成後起筆；本輪後續工作段落持續累加，不重寫既有段落。
> 動機延續 docs/22 §7.2 誠實帳：週頻/日頻快照 × r+20 前瞻窗重疊 → 相鄰觀測自相關，
> Fisher-z 假設獨立、CI 一律偏窄——本輪把推論修硬，不改任何 gate。

## 1. 晉升鐵則（WS-I 起生效）

任何因子要從「候選」晉升進主排序提案（進 CSV 排序公式、進權重、進門檻），
**須同時滿足**：

**(a) 方向一致 ≥4/5 walk-forward 段** —— expanding-window 切分（embargo=horizon+1）後，
各測試段 IC 同號的段數達 4/5 以上。段數 <5 時比例對應下修（如 3 段需 3/3 同號），
少於門檻＝regime-dependent，維持候補、不晉升。

**(b) moving-block bootstrap CI（block ≥ horizon、B=1000）不含 0** —— 對 per-date IC
序列（`factor_lab.daily_ic_series`）算 `moving_block_bootstrap_ci`：塊長
`L = max(horizon+1, 2)`、重抽 `B=1000` 次、percentile [2.5, 97.5] 的 CI95 不可跨零。
這是本輪修正 Fisher-z 在重疊窗下偏窄的核心防線（docs/22 §7.2）；pooled 欄的 Fisher-z
CI 僅供對照連續性，**不作為晉升判準**（重疊窗下系統性偏窄，樂觀誤判風險高）。

**(c) regime 切片 ≥2 個 regime 同向** —— 需 ≥2 個不同 regime（如多頭／盤整／空頭）
下方向一致，才視為跨週期穩健。**WS-H（regime 標籤）就緒前，樣本全數落在單一多頭偏窗
（2025-01~2026-07），一律標「bull-only evidence」，只能列候補，不得晉升**——即使
(a)(b) 皆過，沒有 regime 對照就不知道是「因子有效」還是「多頭裡強者恆強」的化身
（docs/22 §1.3、§7.1）。

### 範例（假想因子，僅示範欄位格式；非真實裁決）

| factor | horizon | mean_IC | bs_CI95 | 非重疊 IC | WF 同向段數 | regime 同向數 | 裁決 |
|---|---|---|---|---|---|---|---|
| demo_factor | 20 | +0.085 | [+0.021, +0.148] | +0.079 [−0.02, +0.18] | 4/5 | 1/2（bull-only） | **候補**——(a)(b) 過，(c) 未過（WS-H 前不得晉升） |

Footer 範例（`inference_footer()` 輸出，固定三行，每張結果表尾接一份）：

- 樣本期間：2025-01-10~2026-07-09；regime 分布：regime 標籤未附（WS-H 前）＝2025-01~2026-07 多頭偏。
- 推論方法：moving-block bootstrap（block=21td・B=1000・seed=42）為 CI 預設；Fisher-z 僅供對照連續性（重疊窗下偏窄，docs/22 §7.2）。
- membership 處理：今日 concepts.yaml（非 point-in-time）。

## 2. WS-I 執行紀錄與新舊 CI 對照

（WS-H 後回填）

## 3. 待辦

（WS-H 後回填）
