"""本地（非 Goodinfo）篩選三件套：universe（拼全市場寬表）、field_map（欄位對照）、
filter（套門檻）。Goodinfo 被 Cloudflare 擋下時，F 策略等官方資料已覆蓋的部分可走此路徑。
"""

from tw_screener.screener.local.field_map import GOODINFO_ITEM_TO_LOCAL_COL, unmapped_items
from tw_screener.screener.local.filter import UnsupportedLocalFilterError, apply_local_filters
from tw_screener.screener.local.universe import build_local_universe

__all__ = [
    "GOODINFO_ITEM_TO_LOCAL_COL",
    "UnsupportedLocalFilterError",
    "apply_local_filters",
    "build_local_universe",
    "unmapped_items",
]
