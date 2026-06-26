# 規劃書 04 — 架構重構與精簡

> 對應審查發現：§5#1（cli.py 巨石）、§5#2（研究軌＋settings 臃腫）、
> §5#3（低信心功能）、§5#4（A/B/C legacy）、§5#8（雙宇宙重疊）、
> §1#8 / §5#7（文件漂移）、§1#7（報告層配置化）。
> 性質：**收斂技術債、降認知負荷**。建議放在 01–03 之後做（需求穩定後再重構）。

---

## 背景

功能擴張速度超過收斂速度的典型徵兆：
- `cli.py` **2511 行**、`backtest/stock_calib.py` **1971 行** ＝巨石模組。
- `settings.yaml` 的 `cp_value` 段塞了數十個研究旋鈕（早偵測閘/穩健度/單調性/交互/Part B/C）。
- 多個「誠實但未驗證」功能（early_watch、overheat_watch、多窗背離）增加維護面而 edge 未證。
- A/B/C legacy 仍可跑、仍佔 `_GROUP_PREFIX`。
- 文件描述的 `analysis/indicators/` 與「Rust 預埋」**不存在**。

重構**不改對外行為**，以「既有 509 測試全綠」為等價性護欄。

---

## A6 — 文件漂移修正（最先做，順手、零風險）

### 問題
- [docs/00-architecture.md](../00-architecture.md) 模組表與「Rust 預埋位置」描述
  `src/tw_screener/analysis/indicators/`（macd.py/kd.py/rsi.py）——**該目錄不存在**
  （`ls` 確認；技術指標實際散在 `stock_panel.py` / `momentum.py` 以 polars 實作）。
- README/docs 對「STOCK_DAY_ALL 不可回補」「cron 必要性」描述偏樂觀（見規劃書 02 D2）。

### 方案
1. 刪除/改寫 docs/00 的 `indicators/` 與「Rust 預埋」段：標為「未實作／已放棄，
   指標以 Polars 向量化內嵌於 analysis/」。
2. 校正 README §12 與 docs/02 §2.2 的 cron/歷史密度描述（與規劃書 02 D2 同步）。

### 成功標準
- [ ] docs/00 不再宣稱不存在的目錄與計畫。
- [ ] `grep -r indicators docs/ README.md` 無誤導性描述。

### 可動檔案範圍
`docs/00-architecture.md`、`README.md`、`docs/02-data-sources.md`。風險：零（純文件）。

---

## A1 — 拆解 cli.py（2511 行）

### 問題
業務邏輯（enrich / orchestration）塞在 CLI command function 內，難重用、難單測。代表性熱點：
- `analysis_group`（[cli.py:685](../../src/tw_screener/cli.py#L685)）— 族群分析的編排與 enrich。
- `_enrich_named_list`（[cli.py:606](../../src/tw_screener/cli.py#L606)）、
  `_read_watchlist_csv` / `_read_holdings_csv`（[:564](../../src/tw_screener/cli.py#L564)）。
- `cp_candidates_cmd`（[:2262](../../src/tw_screener/cli.py#L2262)）、
  `sector_rotation_cmd`（[:1285](../../src/tw_screener/cli.py#L1285)）等命令內含大量計算。

### 方案（分批、每批獨立可驗）
把「計算/編排」下沉到對應模組，CLI command 只留參數解析＋呼叫＋輸出：
1. 庫存/觀察讀檔與 enrich → `analysis/watchlist.py`（新）或併入 `report/data_fetcher.py`。
2. `analysis_group` 的編排 → `report/group_report.py` 既有模組（薄化 CLI）。
3. cp / rotation 命令內計算 → 已有 `report/cp_candidates.py` / `report/rotation_report.py`，
   把殘留在 CLI 的部分移回去。
4. **一次搬一個命令**，每搬完跑該命令的回歸（產出檔逐欄對拍）。

### 成功標準
- [ ] `cli.py` 行數顯著下降（目標 < 1200 行），command function 內無重計算邏輯。
- [ ] 被下沉的邏輯有獨立單元測試（先前只能透過 CLI e2e 測）。
- [ ] `make week GROUP=defg` 產出與重構前逐欄一致。

### 可動檔案範圍
`src/tw_screener/cli.py`、`analysis/*`、`report/*`、對應 `tests/`。

### 風險
搬動時的隱性狀態（settings 載入時機、trading_date lazy resolve）→ 每命令搬完立即回歸對拍。

---

## A2 — 研究軌與 settings 瘦身（收斂已下結論的實驗）

### 問題
`stock_calib.py`（1971 行）＋ `settings.cp_value`（早偵測閘/穩健度/單調性/交互/Part B/C…）
是「已下結論的實驗」堆積。docs 多處寫「結論不變」「冠軍固定」——代表多數變體已完成探索任務。

### 方案
1. 盤點 `cp_value` 各子段，分類：**生產用**（candidate.rules、valuation、laggard 等）
   vs **已收斂的研究旋鈕**（robustness / monotonicity / interaction / early_gate 掃描網格）。
2. 已收斂者：把「冠軍結論」固化進生產設定，研究網格參數移到
   `config/research/cp_value_calib.yaml`（研究軌專用、主流程不載），或加註「凍結・每季重校才解凍」。
3. `stock_calib.py` 把一次性探索的變體函式標記/歸檔，主流程不依賴的剝離到 research helper。

### 成功標準
- [ ] `settings.yaml` 主檔的 `cp_value` 段顯著變短，只剩生產必需。
- [ ] 研究軌參數集中一處、與生產設定分離；`make cp-value-candidates` 仍正常。
- [ ] 既有測試全綠（研究函式測試隨檔移動）。

### 可動檔案範圍
`config/settings.yaml`、`config/research/`（新）、`backtest/stock_calib.py`、
`report/cp_candidates.py`、`docs/13`、`tests/`。

### 風險
別把每季重校真的會用到的旋鈕誤刪 → 用「凍結」而非「刪除」，保留可解凍註記。

---

## A3 — 低信心／未校準功能降級為 feature flag

### 問題
以下功能程式內**自承未驗證**，卻長駐主流程輸出：
- `early_watch`（短窗早訊號）— [cp_candidates.py:430](../../src/tw_screener/report/cp_candidates.py#L430)
  「**M-MH Phase 2 校準：此訊號未證實更早或更準**」。
- `overheat_watch`（過熱-退潮警示）— [cp_candidates.py:465](../../src/tw_screener/report/cp_candidates.py#L465)
  「**未校準啟發式、非賣出訊號**」。
- 多窗背離欄 `price_flow_div_{w}d`（[stock_panel.py:256](../../src/tw_screener/analysis/stock_panel.py#L256)）
  「門檻待 Phase 2 校準」。

誠實是優點，但每個都在增加維護面與報表噪音而 edge 未證。

### 方案
1. settings 已有 `enabled` 旗標（`early_watch.enabled` / `overheat_watch.enabled`）——
   **預設改 false**，需要才開；報表不再預設背這兩塊低信心區。
2. 文件（docs/13）把這些明確歸類為「研究中、預設關」。
3. 規劃書 03 的 V1 回測閉環一旦能驗證它們，再決定升級為生產或正式移除——**用資料決定去留**，
   而非一直掛著。

### 成功標準
- [ ] 預設 `make week` 的 `cp_candidates.md` 不含未校準區塊（除非顯式開啟）。
- [ ] 開啟旗標後行為與現況一致（不刪功能、只改預設）。
- [ ] 測試覆蓋「開/關」兩態。

### 可動檔案範圍
`config/settings.yaml`、`report/cp_candidates.py`、`cli.py`、`docs/13`、`tests/`。

### 風險
使用者可能正在用這些區塊 → 改的是**預設值**不是刪功能，且文件說明如何開回來。

---

## A4 — A/B/C legacy 策略退役

### 問題
A/B/C 已 legacy（README 載明），但 `config/strategies/` 仍有 `a_breakout` /
`b_growth_institutional` / `c_quality_value`，`runner._GROUP_PREFIX` 仍保
`"abc"`/`"def"`（[runner.py:22](../../src/tw_screener/screener/runner.py#L22)）。

### 方案
1. 把 a/b/c YAML 移到 `config/strategies/archive/`（已有 archive 目錄前例）。
2. `_GROUP_PREFIX` 移除 `abc`、`def`（保留主流程 `defg`），相關分支與測試簡化。
3. README/docs/03 標記 A/B/C 為「歷史紀錄、不再可跑」。

### 成功標準
- [ ] `make week GROUP=defg` 不受影響；`GROUP=abc` 明確報「已退役」而非默默跑。
- [ ] 測試與文件同步更新。

### 可動檔案範圍
`config/strategies/`、`src/tw_screener/screener/runner.py`、`docs/03`、`README.md`、`tests/`。

### 風險
低。若使用者仍想保留 abc 可跑，改為「需顯式 `--allow-legacy`」而非全移除。

---

## A5 — 雙宇宙重疊抽共用（保留設計、減重複）

### 問題
候選股族群分析（[group_report.py](../../src/tw_screener/report/group_report.py) 964 行）與
全市場 rotation 在強度計算上概念重疊（Section 2.8 已並列對照）。

### 立場
**不砍雙宇宙設計**——交叉校驗（精選宇宙 × 全市場無偏宇宙）確有防選股偏誤的價值（README 核心原則 3）。
只把兩邊**重複的強度/排名/ΔRank 計算抽成共用函式**，減 group_report 體量。

### 方案
1. 找出 group_report 與 rotation 重複的計算（如 lead_score 元件、attach_rank_delta 已共用、
   breadth/concentration 概念）。
2. 抽到 `analysis/strength.py`（新）共用純函式，兩邊呼叫。

### 成功標準
- [ ] group_report.py 行數下降、無與 rotation 重複的計算實作。
- [ ] 兩份報表產出逐欄不變（回歸對拍）。

### 可動檔案範圍
`report/group_report.py`、`analysis/rotation.py`、`analysis/strength.py`（新）、`tests/`。

### 風險
抽象別過頭——只抽「真的重複」的，不為對稱硬併（呼應 CLAUDE.md「不要為對稱抽象」）。

---

## A7 — 報告層配置化（builder 模型／長度）

### 問題
[builder.py:35](../../src/tw_screener/report/builder.py#L35) `_call_claude` 寫死
`model="claude-sonnet-4-6"`、`max_tokens=2500`。10 段框架＋多空各 3–5 點易**截斷**；
模型也該可配置（深度個股報告值得用 Opus 4.8）。

### 方案
1. `settings.yaml` 加 `report.llm`：`model` / `max_tokens` / `temperature`，builder 讀取、不寫死。
2. `max_tokens` 預設提高（如 4000+）避免截斷；`model` 預設可採最新 Opus（深度報告品質優先）。
3. 加「輸出疑似截斷」檢查（結尾無「資料來源」段 → warning）。

### 成功標準
- [ ] builder 從 settings 讀模型/長度，無寫死。
- [ ] 截斷偵測：缺尾段時記 warning。
- [ ] 既有 builder 測試（草稿模式）不受影響。

### 可動檔案範圍
`config/settings.yaml`、`src/tw_screener/report/builder.py`、`tests/report/`。

### 風險
模型/長度上調會增 API 成本 → 由 settings 控、使用者自選；草稿模式（無 API key）路徑不變。

---

## 驗收（整份規劃書）

```bash
make test && make lint && make typecheck     # 重構等價性護欄：509+ 全綠
make week GROUP=defg                          # 主流程產出與重構前逐欄一致
wc -l src/tw_screener/cli.py                   # 顯著下降
grep -rn indicators docs/ README.md            # 無誤導描述
```

## 重要原則
- **重構不改行為**：每步以「既有測試全綠＋產出對拍一致」為通過條件。
- **先收斂需求再重構**：建議 01–03 完成後再執行本份，避免重構到一半需求又變。
- 一次一個 milestone（A6 → A1 → A2 → A3 → A4 → A5 → A7），做完停下等驗收。
</content>
