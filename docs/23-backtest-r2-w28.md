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

## 2. WS-I 執行紀錄＋CI 回填 changelog（2026-07-11）

`make factor-lab` 以新推論重跑（面板同第一輪 2025-01-10~2026-07-09）；機器等價與
docs/19 對表皆 PASS（點估計不動、只換推論）。回填裁決：

| 因子（全市場 r+20） | Fisher CI95（舊） | bs_CI95（新・block=21・B=1000） | changelog 裁決 |
|---|---|---|---|
| ma60_dist_pct | [+0.029, +0.036] 不跨 0 | **[−0.019, +0.088] 跨 0＝翻船** | 第一輪本未列存活（docs/22 §1.3 已標 regime-dependent）——新 CI＝該降級的**額外佐證**，結論不變 |
| vol_ratio | [+0.014, +0.021] 不跨 0 | [+0.001, +0.039] 未翻、貼零邊 | 維持第一輪「效應量≈0 降級」不變 |

- **候選宇宙口徑（docs/19 §2 週頻）T=3 → block bootstrap 誠實回「無法重抽」**：
  ma60_dist −0.217 的候選宇宙結論**推論尚未硬化**（非否證），樣本增厚後補。
- trend_score 等族群層結論的 bs_CI＋regime 切片＝WS-H 面板延伸後一次重跑（§2 屆時補表），
  不做兩次半套回填。
- 附帶發現（WS-L）：W26 3293 基準異質根因＝上櫃 06-29/30 快取缺口致 entry 順延兩日，
  非 ledger 錯誤；已回補快取、W26 brief 重產四檔基準統一。同週 data_date 驗證＋
  late_entry 欄＋brief 進場日欄已上線防再犯。
- 附帶發現（除息還原覆蓋）：`load_recent_dividends`＝前瞻快照聯集，最早快取
  2026-05-21——**第一輪面板的除息加回實際只覆蓋 ex_date≥2026-05-19 的事件**，
  2025 除息季（7-9 月集中）未還原；且官方端點（TWT49U 舊制/rwd/OpenAPI）經探測
  **皆無歷史回溯**。方向性影響與圍法見 §5（面板延伸後量化）。

## 3. WS-K 預註冊（2026-07-11 寫死；首驗跑前落檔，跑後不得改假設）

資料現況：融資券（僅上市）legacy MI_MARGN 官方日檔回補中（2022-01 起）；TDCC 集保
**官方無歷史回溯**（date 參數被忽略、回應 byte-identical），實際可得起點＝快取最早
**2026-06-26**——大戶 WoW 首驗大概率「樣本不足不可判」，照實標，不硬湊。

| # | 因子 | 預註冊方向 | 機制（一句話） | 量尺 |
|---|---|---|---|---|
| K1 | 融資減肥 margin_slim | **正向** | 融資餘額下降＝浮動槓桿籌碼被洗出、上方賣壓沉澱減輕 | −1×融資餘額 20 日變化率 vs forward alpha 的 IC |
| K2 | 大戶 WoW big_holder_wow | **正向** | ≥400 張大戶佔比週增＝大資金吃貨 | WoW（pp）vs 次週起 forward alpha |
| K3 | margin_to_vol | **負向** | 融資餘額對成交量比高＝套牢槓桿供給大、反彈遇解套賣壓 | 融資餘額股數/20 日均量 vs forward alpha 的 IC |

裁決判準（同晉升鐵則語彙、先驗寫死）：|mean_IC| ≥ 0.03（效應量底線）＋bs_CI95 不含 0
＋walk-forward ≥4/5 同向＝**方向成立**；同門檻但方向與預註冊相反＝**反向顯著**（照實
記錄、禁止事後改機制說法）；其餘＝無證據。主 horizon r+20、輔 r+5/10；宇宙＝上市普通股
（margin 資料邊界）；regime 切片就緒則附（bull-only 條款照 §1c）。

## 4. 待辦

- WS-H：面板延伸 2022-01（回補批次跑完後 build）＋regime 標籤＋第一輪存活結論
  regime 切片重跑（§2 補表）。
- WS-J.2/3：TWSE 官方產業別 membership robustness＋下市/生存者偏差量化。
- §5：docs/22 §7 改寫（每項限制標 已修／已圍住／殘餘）。
