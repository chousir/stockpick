# 規劃書 07 — 多日價量軌跡訊號（Price-Trajectory Signal）

> 對應審查發現：**2026-07-01 分析師實戰回饋審查** §真問題#3
> ——「單日快照、沒有軌跡；一個『健康回踩』到底是真止穩、還是下跌第一天，我看不到」。
> 性質：**把已存在的日線歷史，蒸餾成可判『止穩 vs 破線』的軌跡欄與訊號**。
> 中價值；純衍生自既有快取，不新增資料源。

---

## 背景與根因（含對原始批評的修正）

原始批評說「我手上只有 6/30 一天的 cache、沒有連續幾天的價與量序列」。
盤點後這句**在資料層是錯的**：

- `data/cache/twse/` 有 **8,849 檔 `daily_*.parquet`**，全市場日線歷史完整。
- `analysis/stock_panel.py` 已用 `price_history` 多日序列算 RS vs 大盤。

**真正的落差**：分析師分析時讀的是 `candidates_enriched.csv` 這張**單日快照**，
它只帶了幾個派生窗（`momentum_5d_pct`、`ret_10d_pct`、`low/high_20d/60d`、`vol_ratio`）。
**逐日的價量路徑（近 N 日 close/volume 序列、連跌天數、回踩時量能是否萎縮）沒有被算成訊號、
沒有被端到分析師面前**。於是「福懋科是健康回檔還是破線第一天」只能靠 ΔRank 這種週對週粗指標猜。

**根因**：不是沒有軌跡資料，是沒有把軌跡蒸餾成「止穩 vs 破線」的可讀訊號。

---

## TR1 — 回踩品質軌跡欄（從既有日線衍生）

### 目標
對每檔候選，從既有 `daily_*.parquet` 算一組**回踩品質軌跡欄**，把「這波回踩健不健康」
從單日快照升級成有路徑依據的訊號，端進 enriched 與報告。

### 現況可用素材
- `data/cache/twse/daily_*.parquet`（8,849 檔全市場日線，含 close/volume）。
- `analysis/stock_panel.py` 的載入/夾限（`clip_daily_return_pct` 防未還原假跳動）可複用。

### 方案
1. `analysis/trajectory.py`（新，純函式）：吃單檔近 N 日（如 20 日）close/volume，輸出：
   - `down_days_streak`：連續收黑天數（判「跌第一天 vs 已跌一段」）。
   - `pullback_vol_ratio`：回踩期均量 / 前段均量——**縮量回踩＝健康、放量下殺＝危險**
     （直接對應審查的「止穩 vs 破線」核心疑問）。
   - `dist_from_pivot_pct`：距近 N 日樞紐（近高/近低）位置。
   - `above_ma_days`：連續站上/跌破 MA20 天數（比單日「距月線」多了持續性）。
   - 合成 `pullback_quality ∈ {止穩, 觀察, 破線}`（縮量+守均線→止穩；放量+連跌+破均線→破線）。
2. 接進 candidates_enriched 產出（`report/data_fetcher.py` / grouping 組裝處）新增上述欄。
3. 報告層（docs/11 技術面段）呈現 `pullback_quality` 與其依據。
4. 全參數（N、量比門檻、連跌門檻）進 `config/settings.yaml`。

### 成功標準
- [ ] `analysis/trajectory.py` 純函式、輸入日線序列輸出軌跡欄，離線可測。
- [ ] 合成兩檔測試樣本：一「縮量守均線」→ 判止穩；一「放量連跌破均線」→ 判破線。
- [ ] candidates_enriched 出現 `pullback_quality` 等欄；docs/11 補讀法。
- [ ] 不打網、純讀既有快取（守 CLAUDE.md 2.5）。

### 可動檔案範圍
`src/tw_screener/analysis/trajectory.py`（新）、`report/data_fetcher.py`（或 grouping 組裝處）、
`config/settings.yaml`、`docs/11-propicks-analysis.md`、`tests/`。

### 風險與取捨
- 上櫃日線覆蓋有缺口（同 stock_panel 註記）——軌跡欄只框上市，上櫃留 null 不臆造。
- 除權息跳空會污染連跌/量比——沿用 `clip_daily_return_pct` 夾限，並以 `ex_div_cash` 旗標排除。
- `pullback_quality` 是啟發式輔助，不是買賣訊號；判斷權在人（守人設）。

---

## TR2（選配）— 軌跡欄回饋 post-mortem 與回測

### 目標
把 TR1 的 `pullback_quality` 接進規劃書 05 的回測，驗「判止穩」的檔是否真的續強、
「判破線」的是否真的續弱——用資料確認軌跡訊號有沒有預測力，並餵給 PO3 翻轉解剖。

### 方案
1. `picks_outcome` 分桶：按入選當週 `pullback_quality` 比較前進報酬。
2. 提供給規劃書 05 PO3：翻轉標的的「破線」是否在降級前一週就先亮。

### 成功標準
- [ ] 止穩桶 vs 破線桶前進報酬對比（含樣本數，樣本少誠實標）。

### 可動檔案範圍
`src/tw_screener/backtest/picks_outcome.py`、`tests/`。

### 風險
- 選配、依賴規劃書 05；樣本稀疏，先方向性。

---

## 驗收

```bash
make week GROUP=defg     # TR1：enriched 應含 pullback_quality 等軌跡欄
make pick-outcome        # TR2（選配）：軌跡桶 edge 對比
make test && make lint && make typecheck
```

## 執行順序
**TR1（獨立、可先做）→ TR2（選配，依賴規劃書 05）**。
TR1 純衍生既有快取、無外部依賴，可與規劃書 06 NF1 平行；TR2 借 05 回測驗證價值。
