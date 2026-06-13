#!/usr/bin/env bash
#
# fetch_cron.sh —— 每交易日盤後抓 TWSE/TPEX 全市場資料，寫本地 parquet 快取。
#
# 為什麼要排程：上櫃法人（TPEX）只供「最新交易日」、缺日不可回補（TPEX 端無歷史日期參數）。
# 沒在當天抓，那天的上櫃資金流就永久缺，rotation 的上櫃籃子會被低估。fetch-twse 雖會印
# 「上櫃法人落後」警告，但補不回來——所以靠這支 cron 每交易日固定抓。
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
