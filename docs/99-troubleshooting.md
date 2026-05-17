# 99 — 疑難排解

> M0-M6 開發與第一次完整週使用累積的常見問題清單。
> 遇到狀況先翻這份，沒有再去查 logs/ 或 source code。

---

## 1. TWSE OpenAPI 端點壞掉

### 症狀
```
make fetch-twse 拋錯：
  JSONDecodeError 或 _parse_institutional 收到 list 而非 dict
ls data/cache/twse/institutional_*.parquet → 檔案是空的或筆數異常少
```

### 原因
TWSE 的 OpenAPI（`openapi.twse.com.tw/v1/...`）部分端點已停用或改為回傳 HTML。
**T86 法人** 是已知案例：OpenAPI 版本回 HTML，legacy 版本回 JSON。

### 解法
切到 legacy URL，schema 不同需要新 parser：

```
舊 (壞)：https://openapi.twse.com.tw/v1/fund/T86
新 (可)：https://www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&selectType=ALLBUT0999
```

實作位置：[src/tw_screener/data/twse.py](../src/tw_screener/data/twse.py) 的 `fetch_institutional()` + `_parse_institutional()`。

**檢查方式**：
```bash
uv run python3 -c "
from pathlib import Path
from tw_screener.data.twse import create_client
c = create_client(Path('config/settings.yaml'))
df = c.fetch_institutional()
print(f'rows: {len(df)}')
print(df.head())
"
# 期望：> 1000 筆，含 stock_id, foreign_net, trust_net, dealer_net
```

---

## 2. STOCK_DAY_ALL 不支援歷史日期

### 症狀
```
make fetch-twse 後，data/cache/twse/daily_*.parquet 只有當天一筆
MA20、MA60 計算結果跟硬刻數字一樣（用 2-3 天當 20 天平均）
```

### 原因
`STOCK_DAY_ALL` endpoint 的 `date` 參數會被無視，永遠回傳「今天」全市場資料。
要拿歷史 OHLCV 必須用 per-stock 的 `STOCK_DAY` endpoint，一次回傳一個月。

### 解法
按需自動回補：`make report STOCK_ID=XXXX` 時呼叫 `fetch_stock_history()` 補該檔 3 個月。

```bash
# 第一次跑會額外花 5-10 秒補歷史
make report STOCK_ID=2330

# 第二次（同月份）讀快取，秒回
make report STOCK_ID=2330
```

快取檔名：`data/cache/twse/stock_day_{stock_id}_{YYYYMM}.parquet`，過去月份永久快取。

---

## 3. Goodinfo 被擋（403 或「您的瀏覽量異常」）

### 症狀
```
make screen STRATEGY=a_breakout 拋 GoodinfoBlockedError
reports/YYYY-Www/blocked.log 出現新行
連續幾分鐘任何 Goodinfo 請求都失敗
```

### 原因
1. 短時間內請求太密集
2. User-Agent 過期（瀏覽器版本太舊）
3. IP 被 Goodinfo 暫時加入黑名單（通常 30 分鐘到數小時）

### 解法

**短期**：等 30 分鐘以上再試，並調高 `config/settings.yaml`：
```yaml
goodinfo:
  request_interval_sec: 5          # 從 3 → 5
  request_interval_jitter_sec: 2   # 從 1 → 2
  backoff_base: 10                 # 從 5 → 10（指數退避）
```

**中期**：更新 User-Agent 為當前主流瀏覽器版本（看 `https://www.whatismybrowser.com/`）。

**長期**：考慮分批執行（一次只跑一個策略，間隔 10 分鐘），或改成手動下載 HTML 餵 parser。

**檢查方式**：
```bash
cat reports/$(date +%Y-W%V)/blocked.log
# 看封鎖時間戳與策略 ID，超過 1 小時前的可以重試
```

---

## 4. Goodinfo 篩選結果超過 300 筆匿名上限

### 症狀
```
make screen 拋 GoodinfoTooManyResultsError，附 count=XXX (XXX > 300)
某個策略的 CSV 是空的或部分截斷
```

### 原因
未登入 Goodinfo 帳號時，自訂篩選器最多回傳 300 筆。條件太寬鬆會碰到這個天花板。

### 解法

收緊 YAML 條件：
```yaml
# config/strategies/c_dividend_steady.yaml
filters:
  - item: 連續配息年數
    min: 8            # 從 5 → 8
  - item: 殖利率
    min: 4.0          # 加上殖利率下限
  - item: 成交金額(億)
    min: 0.5          # 過濾低流動性
```

或拆成多個策略（例如 C1/C2 分別跑大型/中型權值），再合併 CSV。

---

## 5. 大量「未分類」族群

### 症狀
```
group_analysis.md 第 2 節「未分類」族群股票一大堆（30+ 檔）
被推薦的個股很多沒有產業歸屬
```

### 原因
- TWSE 官方 `t187ap03_L` API 只涵蓋**上市股**，**上櫃股**（5xxx、6xxx、8xxx）會缺
- 上市公司名稱有星號（如 `國巨*`）時可能對不到產業

### 解法

確認上櫃 ISIN 已抓：
```bash
ls data/cache/twse/otc_industry_*.parquet
# 沒檔案：
uv run python3 -c "
from pathlib import Path
from tw_screener.data.twse import create_client
c = create_client(Path('config/settings.yaml'))
print(c.fetch_otc_industry().shape)
"
# 應該 > 800 筆
```

上櫃資料來源：ISIN 頁面 `https://isin.twse.com.tw/isin/C_public.jsp?strMode=4`（MS950 編碼）。

---

## 6. ETF / 權證污染篩選結果

### 症狀
```
group_analysis.md 第 5 節「推薦深度分析優先順序」前幾名都是 ETF（00xxxx）
策略 A（波段啟動）入選一堆 0050、00878 等指數型商品
```

### 原因
Goodinfo 自訂篩選預設包含 ETF 與權證；它們的「成交金額」「漲跌幅」常超越個股。

### 解法
已在 `src/tw_screener/analysis/grouping.py` 的 `is_etf_or_warrant()` 過濾：
```python
def is_etf_or_warrant(stock_id: str) -> bool:
    return stock_id.startswith("00") or not stock_id[0].isdigit()
```

族群分析時自動排除，但 `screen_result_*.csv` 仍會列出（供原始檢視）。

---

## 7. 個股檔名含 `*` 或斜線

### 症狀
```
make report STOCK_ID=2327 拋
  FileNotFoundError: reports/.../stocks/2327_國巨*.md
  或檔案無法在 macOS Finder 開啟
```

### 原因
台股部分股票名稱含星號（特別股、減資後）或斜線（少見），檔名不合法。

### 解法
[src/tw_screener/report/builder.py](../src/tw_screener/report/builder.py) 在寫檔前清理：
```python
safe_name = name.replace("*", "").replace("/", "-").strip()
```

如果仍遇到其他特殊字元，自行擴充清理規則。

---

## 8. uv 提示 `VIRTUAL_ENV=/usr does not match...`

### 症狀
```
warning: `VIRTUAL_ENV=/usr` does not match the project environment path `.venv`
and will be ignored; use `--active` to target the active environment instead
```

### 原因
shell 環境變數 `VIRTUAL_ENV` 指向系統 `/usr`，但 uv 用專案內 `.venv`。

### 解法
**無害警告**，可忽略。要消除可在 shell 啟動時 unset：
```bash
unset VIRTUAL_ENV
make test
```

或在 `~/.bashrc` / `~/.zshrc` 加：
```bash
[ -n "$VIRTUAL_ENV" ] && [ "$VIRTUAL_ENV" = "/usr" ] && unset VIRTUAL_ENV
```

---

## 9. `make weekend` 空 commit 失敗

### 症狀
```
make weekend
  # ... make week 完成
nothing to commit, working tree clean
make: *** [Makefile:90: weekend] Error 1
```

### 原因
本週 reports/ 沒新檔（例如先跑過一次 `make week`），`git commit` 因無變更而失敗。

### 解法
已在 M6 修正：`Makefile` 改用 `git diff --staged --quiet` 守衛：
```makefile
weekend:
  $(MAKE) week
  git add reports/ watchlist/
  @if git diff --staged --quiet; then \
    echo "無新檔可 commit，跳過 git commit/push"; \
  else \
    git commit -m "..." && git push; \
  fi
```

---

## 10. 族群強度分數小族群佔先

### 症狀
```
group_analysis.md 排名第 1 的族群只有 5 檔，半導體 48 檔卻排第 3
領頭羊推薦集中在冷門族群
```

### 原因
舊版分數用 RS min-max normalization，導致**任何**有最高 RS 的族群拿滿分，跟絕對值無關。

### 解法
已改為「絕對 RS clip(0,10) + log 規模因子」（見 [docs/05-group-analysis.md](./05-group-analysis.md) 5.2）：

```yaml
# config/settings.yaml
group_analysis:
  weights:
    entry_rate: 0.50   # 族群入選率（主要訊號）
    size: 0.15         # log1p(members)：避免小族群佔先
    rs: 0.20           # 絕對 RS, clip 0-10
    institutional: 0.15
```

可在 settings.yaml 微調比重。

---

## 一般檢查清單

每週跑完後若發現異常，按順序檢查：

```bash
# 1. 看有沒有被擋
cat reports/$(date +%Y-W%V)/blocked.log 2>/dev/null

# 2. 看快取
ls -la data/cache/twse/ | head -20

# 3. 跑測試確認核心邏輯沒壞
make test-unit

# 4. 看最近一次 fetch 的時間
ls -la data/cache/twse/daily_*.parquet | tail -3

# 5. 確認族群分類有抓到上櫃
ls data/cache/twse/otc_industry_*.parquet
```

仍找不到原因 → 看 `logs/`（如果有開），或開 issue。
