"""資料密度誠實註記（規劃書 02 D2）。

STOCK_DAY_ALL / otc_daily_all 的日期參數被無視、只能往未來累積、過去補不回（docs/02），
故新環境前幾個月本地日線快取的歷史窗偏短。rotation z 分數需 ~60+ 交易日、
calibration 需 ~250 交易日才有統計意義。報表頭以本函式誠實標示「實際 / 建議」窗長，
密度不足時明標「訊號統計意義有限」——守 CLAUDE.md 誠實原則，不假裝有 edge。
"""

from __future__ import annotations

#: rotation z 分數可計算的最低交易日數
Z_MIN_DAYS = 60
#: calibration（起漲校準）建議的歷史窗交易日數
CALIB_TARGET_DAYS = 250


def data_density_note(
    actual_days: int,
    z_min: int = Z_MIN_DAYS,
    calib_target: int = CALIB_TARGET_DAYS,
) -> str:
    """回傳一行 markdown 用的資料密度註記。

    actual_days：本地日線快取實際可用的相異交易日數。
    """
    if actual_days >= calib_target:
        conf = "密度充足（達校準窗）"
    elif actual_days >= z_min:
        conf = f"可算 z 分數但未達校準窗 ~{calib_target}，統計意義中等"
    else:
        conf = f"未達 z 分數最低窗 {z_min}，訊號統計意義有限、請降低權重"
    return (
        f"歷史窗：實際 {actual_days} 交易日／建議 ~{calib_target}"
        f"（z 分數需 ≥{z_min}）— {conf}"
    )
