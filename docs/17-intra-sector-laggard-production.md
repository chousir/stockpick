# 17 — 族群內落後濾鏡生產化（冠軍 S+ 內 rs_subind<0 進場加分上線 規劃書）

> 承接 [docs/16 M-Part C](16-intra-sector-laggard-research.md) 的**首個全勝研究軌**：在冠軍 CP
> 補漲訊號（資金進＋貼低＝L1 埋伏）上，加「個股落後其次產業」（`rs_subind<0`）濾鏡，**起漲命中
> 近乎倍增、賺賠由負轉正**。本檔把這個 C-P2 勝出濾鏡**接進生產** `cp_candidates.py`——研究→生產
> 的對偶落地（同 docs/13 §4 研究 `stock_calib.py` ↔ 生產 `cp_candidates.py` 的慣例）。

---

## 0. 一句話目標

把 C-P2 驗證過的「冠軍 S+ 且 `rs_subind<0`」濾鏡，做成生產候選清單的**揭露欄＋輕加權**，讓
落後（補漲未發動）的埋伏股在型態內排前面、並讓分析師讀到落後旗標做進場加碼／分批——**不硬剔除
領先股、不碰篩選器、不加資料源**（守生產「揭露非扣分」哲學，比照 M-修法6 外資窗先例）。

---

## 1. 研究結論（C-P2，docs/16）——為什麼值得上線

冠軍 S+（`foreign_flow_20d_z>0.5` ＋距 60 日低 ≤15%）觸發內，再切落後／領先：

| 子集 | 起漲 lift | 命中率 | 中位報酬（40d） | 賠率 |
|---|---|---|---|---|
| 冠軍 S+ **全體** | — | — | **−0.7%** | 1.61 |
| S+ 且**領先**（rs_subind≥0） | 1.70 | 11.8% | — | — |
| S+ 且**落後**（rs_subind<0） | **2.77** | **19.1%** | **+2.3%** | **2.44** |

- precision：落後濾鏡近乎**倍增命中率**（z=+4.54 顯著）。
- 賺賠：冠軍 S+ 全體中位報酬其實**為負**，但**＋落後**轉正且隨持有期升（解 C-P1「領先股報酬高」警示
  ——那是全宇宙相對；在冠軍 S+ 子集內，落後股 lift＋報酬**雙贏**）。
- 裁決＝**升級為冠軍 S+ 進場加分**（S+ 且 rs_subind<0 提高權重／分批）。

> 證據範圍嚴格限於**冠軍 S＝foreign_flow_20d_z＋貼低＝生產的 L1 埋伏規則**；L2 追突破（投信）、
> L3 反轉（深跌＋mom）**未驗證**，故生產加權只作用於 L1 埋伏。

---

## 2. 接線可行性（已查證）

- `cp_candidates.py` 的 `build_cp_candidates` 吃 `snapshot = latest_snapshot(panel)`，而 panel
  （`analysis/stock_panel.py`）本就算 `rs_subind_{rs_window}d`（個股報酬 − 次產業籃報酬，已去族群
  beta）。→ **rs_subind 已在快照裡、純加法、不加資料源。**
- 冠軍 S+ 在生產端就是 `settings.cp_value.candidate.rules` 的 **L1埋伏**（`foreign_flow_20d_z>0.5`
  ＋`modifier: low`）——對應乾淨。
- 缺次產業標記的股 → `rs_subind` null → 不加權、誠實標（沿用既有 low_col/high_col 缺欄降級慣例）。

---

## 3. 設計決策（使用者 2026-06-21 拍板，全採推薦版）

- **D1 濾鏡形態＝揭露＋輕加權**（非純揭露、非硬閘）：新增 `rs_subind` 揭露欄＋落後旗標，並對命中
  型態的落後股 `cp_score ×(1+boost)`，讓其在型態內排前面；**不剔除領先股**（領先 lift 1.70 非無效）。
- **D2 作用範圍＝只 L1 埋伏**：加權只作用於 `apply_to` 標的型態（預設 `[ambush]`，嚴格對齊 C-P2
  證據）；其餘型態**只揭露 rs_subind 欄、不加權**（誠實：無回測證據處不加分）。
- **D3 同步改 docs/11**：prompt 加「S+ 且落後者 precision 高、可加碼／前重分批」讀法，接上 M-修法7
  進場階梯（賺賠才落地）。純 prompt、零程式。

---

## 4. 落點與形態（生產／一次一子項）

### D-P0 規劃書（本檔 docs/17）

### D-P1 code＋settings＋測試（純加法）
- `settings.cp_value.candidate.laggard`（**全門檻進 settings、不寫死**）：
  - `enabled: true`、`apply_to: [ambush]`（D2）、`threshold: 0.0`（rs_subind < 此＝落後，C-P2 驗證切點）、
    `boost: 0.3`（命中型態的落後股 cp_score ×(1+boost)＝輕加權，每季可重校）。
- `cp_candidates.build_cp_candidates` 加參數 `laggard_labels` / `laggard_boost` / `laggard_threshold`：
  - 偵測 rs_subind 欄（regex `^rs_subind_(\d+)d$`，比照 `_prefix_col` 自動抓、不寫死窗）。
  - feature 生效條件＝`boost>0 且 laggard_labels 非空 且 rs_subind 欄存在`；否則**完全不動**（回退既有行為，向後相容）。
  - 每列輸出加 `rs_subind`（值，揭露）、`cp_boosted`（bool＝該列是否被加權＝落後且型態在 apply_to）。
  - 加權在 cp_score 定案處乘上去（**早於排序/截斷**），讓落後埋伏股更可能浮上、survive 型態截斷。
- 報告（`render_cp_candidates_report`）：候選表加「族群內」欄＝`rs_subind` 值，被加權者綴 🔻（透明標示加分來源）。
- 測試：①揭露欄出現；②ambush 落後 cp_score 被乘且 `cp_boosted=True`、領先不乘；③breakout 落後僅揭露
  `cp_boosted=False`、score 不變；④缺 rs_subind 欄降級（值 null、不加權、不報錯）；⑤boost≤0 或 apply_to
  空＝停用回退（既有測試零改）。

### D-P2 docs/11 prompt（純文字）
- 候選清單讀法段加：「族群內」欄為個股相對其次產業強度（pp，負＝落後其族群）；**埋伏(L1) 且落後（🔻）
  者起漲 precision 顯著高（C-P2）**，可作為**進場加碼／前重分批**的依據，接 M-修法7 進場階梯（50/30/20
  前重）；領先埋伏不剔除、僅權重較低。圍欄同既有：多空並陳、不下單一結論、仍須個股籌碼/基本面判讀。

---

## 5. Success criteria（怎樣算做完）

- `make cp-value-candidates`（或 cli `cp candidates`）跑得出帶「族群內」欄與 🔻 的 `cp_candidates.md`／`.csv`。
- L1 埋伏內落後股 cp_score 被輕加權後排前；L2/L3 落後股只揭露、score 不變。
- 缺次產業標記股不報錯、誠實標 null。
- `make test` 全綠（新增 D-P1 測試）、ruff 乾淨、mypy 零淨增。
- docs/11 補落後旗標讀法。
- **報告不回溯**：須 W26+ 重跑才反映新欄與加權。

---

## 6. 不做 / 邊界

- **不硬剔除**領先埋伏股（違反生產揭露哲學、且領先 lift 1.70 非無效）。
- **不對 L2/L3 加權**（無 C-P2 證據；只揭露）。
- **不加資料源、不動篩選器、不碰研究軌** `stock_calib.py`（C-P2 已驗，生產只是落地）。
- 連續分級（落後越深加越多）**不做**——C-P2 只驗 binary（rs_subind<0 vs ≥0）切點，誠實對齊驗證範圍；
  boost 幅度進 settings、每季隨資料重校。
