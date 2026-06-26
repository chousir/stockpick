# 規劃書 01 — 效能與技術債修復

> 對應審查發現：§1#1（glob-all 載入）、§1#4 / §5#6（momentum per-stock 迴圈）、
> §1#5（OpenAPI 無 TTL/retry）、§1#6 / §5#5（快取無限增長）。
> 性質：**內部機械式修復，對外行為不變、風險低、確定性高**。建議最先做。

---

## 背景與動機

系統的分析威力建立在「逐日累積的快照」上——這是優點，但也讓幾個「讀取全部歷史再
篩近端」的寫法**隨週數線性惡化**。目前 `data/cache/twse/` 已累積：

```
institutional_DATE.parquet      ×256
institutional_otc_DATE.parquet  ×256
daily_DATE.parquet              ×15（STOCK_DAY_ALL 不可回補，僅往前累積）
stock_day_<sid>_DATE.parquet    大量（候選股月回補）
```

這些檔只會愈長愈多，而下列讀取端**每次呼叫都全讀**。現在還不痛（18 秒測試全過），
但這是「明年的你會詛咒今天的你」的典型技術債，趁早止血成本最低。

---

## Phase P1 — 快取載入改「先按檔名日期過濾，再讀內容」

### 問題（現況）
`load_institutional_history` 把**全部** `institutional_*.parquet` 讀進來 concat，
最後才 filter 近 N 日：

- [src/tw_screener/data/twse.py:1146](../../src/tw_screener/data/twse.py#L1146) `load_institutional_history`
  ```python
  files = sorted(self.cache_dir.glob("institutional_*.parquet"))
  merged = pl.concat([pl.read_parquet(f) for f in files])  # ← 讀 512 個檔
             .unique(...).sort("date")
  recent_dates = merged["date"].unique().sort(descending=True).head(n_days)...
  ```
  典型呼叫只需近 20 日，卻讀了 512 個檔。

- [src/tw_screener/analysis/rotation.py:47](../../src/tw_screener/analysis/rotation.py#L47)
  `load_market_history`：同病，glob `daily_*` + `otc_daily_*` + `stock_day_*` 三來源全讀，
  `n_days` 預設 250 但仍是讀完才篩。rotation / group / cp 主流程都會呼叫。

### 方案
檔名已內嵌日期（`institutional_20260625.parquet`），**用檔名先選出需要的檔再讀**：

1. 在 [data/cache.py](../../src/tw_screener/data/cache.py) 加工具
   `select_recent_by_filename(files, n_days, today) -> list[Path]`：
   解析檔名日期、取最近 N 個交易日對應的檔（法人含 `_otc_` 變體一起算）。
2. `load_institutional_history` / `load_market_history` 改先呼叫此工具縮檔，再 `read_parquet`。
3. 邊界：N 日內若某日缺檔（非交易日/未抓）不報錯，沿用既有「缺即補 0／降級」語意。
   `stock_day_<sid>_*` 是個股月檔、不含全市場日期語意 → 維持原樣全讀（量相對可控），
   或另以「候選股聯集」限制（見備註）。

### 成功標準
- [ ] `load_institutional_history(20)` 讀取的檔數 ≤ 30（而非全部）。
- [ ] 既有 `tests/data/test_twse.py`、`tests/analysis/test_rotation.py` 全綠。
- [ ] 新增測試：造 400 個假法人檔、斷言只讀近端、結果與「全讀後篩」逐值相同。
- [ ] `make week GROUP=defg` 產出 `sector_rotation.csv` / `candidates_enriched.csv` 與改前逐欄一致（回歸快照）。

### 可動檔案範圍
`src/tw_screener/data/cache.py`、`src/tw_screener/data/twse.py`、
`src/tw_screener/analysis/rotation.py`、對應 `tests/`。

### 風險與取捨
- **正確性風險**：縮檔邏輯若把該讀的檔漏掉 → z 分數/ΔRank 失真。緩解＝對拍「全讀」基準的等價測試。
- 連假/補班導致「最近 N 個交易日」推算偏差 → 寬鬆多取幾天（N+buffer）再以內容 filter 收斂，安全。

---

## Phase P2 — 快取保留窗與清理

### 問題
快取無上限增長（§1#6）。法人檔已 512 個且每交易日 +2。雖然單檔小，但與 P1 複合、
也讓 `data/` 備份/同步變重。

### 方案
1. `config/settings.yaml` 加 `cache.retention`：
   ```yaml
   cache:
     retention:
       daily_days: 400          # 全市場日線保留窗（rotation history_days=250 + buffer）
       institutional_days: 400
       valuation_days: 400
       stock_day_keep_all: true # 個股月檔小、與候選回補有關，預設不刪
   ```
2. 新增 CLI `data prune-cache [--dry]`：依保留窗刪超窗檔，`--dry` 只印不刪。
3. **不自動刪**（守「刪除前先看、非我建立的不刪」）；由使用者手動或排程呼叫。

### 成功標準
- [ ] `uv run tw-screener data prune-cache --dry` 正確列出超窗檔、不刪檔。
- [ ] 實際 prune 後 `make week` 仍正常（保留窗 ≥ 分析所需窗）。
- [ ] retention 全部來自 settings、無寫死天數。

### 可動檔案範圍
`config/settings.yaml`、`src/tw_screener/data/cache.py`、`src/tw_screener/cli.py`、`tests/`。

### 風險
保留窗設太短會砍掉 calibration（需 ~250 日）要用的歷史。預設給 400 日（1.6 年）緩衝，
且 `--dry` 先驗。

---

## Phase P3 — 統一動能計算為向量化（消滅 per-stock 迴圈）

### 問題
[src/tw_screener/analysis/momentum.py](../../src/tw_screener/analysis/momentum.py) 三個函式
都用 `for stock_id in stock_ids: price_history.filter(...)` 的 Python 迴圈：

- `compute_n_day_return`（[:39](../../src/tw_screener/analysis/momentum.py#L39)）
- `compute_rolling_extrema`（[:83](../../src/tw_screener/analysis/momentum.py#L83)）
- `compute_dividend_addback`（[:139](../../src/tw_screener/analysis/momentum.py#L139)）

每股各掃一次全表 ＝ O(股數 × 列數)。候選 ~150 檔尚可，但與 `stock_panel.py` 的向量化
（`over("stock_id")`）是**兩套並存的寫法**，維護心智負擔高、未來擴宇宙會痛。

### 方案
用 `group_by("stock_id")` / window function 一次算完，回傳同樣的 dict 介面（**對外簽章不變**）：
- N 日報酬 → `over("stock_id")` 的 `shift` + 取每股最後一列。
- rolling extrema → `rolling_min/max(...).over("stock_id")` 取末列。
- dividend addback → 與 panel 的除息視窗對齊，向量化 join。

保留現有函式簽章與回傳型別，純換內部實作；既有呼叫端與測試不動。

### 成功標準
- [ ] `tests/analysis/test_momentum.py` 全綠（不改測試、只換實作＝行為等價的最佳證明）。
- [ ] 新增大宇宙基準測試（1800 檔合成資料）斷言結果與舊實作逐值相同、且耗時下降。
- [ ] grep 確認 momentum.py 不再有 `for stock_id in stock_ids`。

### 可動檔案範圍
`src/tw_screener/analysis/momentum.py`、`tests/analysis/test_momentum.py`（只加不改舊案）。

### 風險
向量化的 NaN/暖機/除零邊界與迴圈版可能有細微差異 → 以「對拍舊實作」測試鎖死等價性。

---

## Phase P4 — OpenAPI `_get` 補 TTL 快取與 retry

### 問題
[twse.py:947](../../src/tw_screener/data/twse.py#L947) 的 `_get`（OpenAPI 用）**無 retry、
無 TTL 快取**；只有 `_get_legacy`（[:967](../../src/tw_screener/data/twse.py#L967)）有指數退避。
走 `_get` 的產業別、月營收等每跑必重打；單次網路抖動即整批失敗回空。

### 方案
1. 把 `_get_legacy` 的退避重試抽成共用 helper，`_get` 也套用（4xx/5xx/非 JSON/timeout）。
2. 對「日內穩定」的 OpenAPI 端點（產業別、月營收、估值比）走既有 `is_fresh` TTL 快取
   （`twse.cache_ttl_hours`，目前 settings 已有 `twse.cache_ttl_hours: 6`，但 `_get` 沒用它）。
3. `STOCK_DAY_ALL` 維持現有「以 max(date) 命名、TTL 內讀快取」邏輯不動。

### 成功標準
- [ ] `_get` 對 transient 失敗會退避重試（以 mock 驗）。
- [ ] 走 TTL 的端點在窗內重跑命中快取、不重打（log 可見「命中快取」）。
- [ ] 既有 `tests/data/test_twse.py` 全綠。

### 可動檔案範圍
`src/tw_screener/data/twse.py`、`config/settings.yaml`（若需細分各端點 TTL）、`tests/data/`。

### 風險
TTL 設太長會吃到舊產業/營收 → 用 settings 控、預設保守（6h，盤後一天內仍會重抓最新）。

---

## 驗收（整份規劃書完成後）

```bash
make test           # 509+ 全綠
make lint && make typecheck
make week GROUP=defg # 主流程跑完，產出與基準快照逐欄一致
uv run tw-screener data prune-cache --dry   # 列出超窗檔不刪
```

## 備註：不在本規劃書範圍
- `stock_day_<sid>_*` 個股月檔的整併（可另議：合併成單一 partitioned parquet）。
- 把 parquet 換成 DuckDB/實體 DB——目前規模未到，**刻意不做**（Simplicity First）。
</content>
