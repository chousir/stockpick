#!/usr/bin/env bash
#
# fetch_cron.sh —— 每交易日盤後抓 TWSE/TPEX 全市場資料，寫本地 parquet 快取。
#
# 為什麼建議常駐排程（規劃書 02 D2）：
#  1) 全市場日線 STOCK_DAY_ALL / otc_daily_all 的 date 參數被無視、只能往未來累積、過去補不回。
#     不每日累積，rotation z(~60 日)/calibration(~250 日) 的歷史窗就偏短、訊號統計意義有限
#     （報表頭會誠實標「歷史窗：實際 N 交易日」）。另可一次性 make backfill-universe-history
#     補單檔歷史，但最新交易日仍得靠每日累積。
#  2) 上櫃法人（TPEX 3itrade_hedge）改版後雖可逐日回補（cron 對法人非必要），但每日抓最省事。
# fetch-twse 雖會印「上櫃法人落後」警告，但日線缺日補不回來——所以建議靠這支 cron 每交易日固定抓。
# 排程時段：T86 法人收盤後約 90 分鐘、15:00 起穩定（docs/02），建議排 18:00 之後。
#
# 安裝：見 README「每日資料排程（cron）」。手動跑也可：bash scripts/fetch_cron.sh
#
set -euo pipefail

# 解析專案根目錄（不論 cron 從哪個工作目錄呼叫都對；cron 預設在 $HOME 啟動）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# cron 的 PATH 很精簡（常只有 /usr/bin:/bin），uv 多裝在下列位置 → 補上避免 command not found
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

mkdir -p logs
LOG="logs/cron_fetch.log"

# 防重入：前一輪還沒跑完又被觸發時直接跳過，避免兩個 fetch 疊在一起打網
LOCK="/tmp/tw_screener_fetch.lock"
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "$(date '+%F %T')  上一輪 fetch 仍在執行，跳過本次" >>"$LOG"
  exit 0
fi

{
  echo "──────── $(date '+%F %T')  fetch-twse 開始 ────────"
  if uv run tw-screener data fetch-twse; then
    echo "──────── $(date '+%T')  完成 ────────"
  else
    rc=$?
    echo "──────── $(date '+%T')  失敗 rc=$rc（資料未更新，下次 fetch 會印落後警告）────────"
    exit "$rc"
  fi
} >>"$LOG" 2>&1
