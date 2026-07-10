"""backtest/flow_inflection.py — WS-E 資金流 level→inflection 因子族（個股層）。

docs/19/20 已否證「20 日 level 排序」與「近端佔比單獨判別」——本模組**不重測
它們當新發現**；level 只留一列明標對照。新因子族全走 inflection（變化率）視角：

- f_dif_rate：日均流差分＝(Σ5/5 − Σ20/20)/vol20 —— 近端 vs 基期日均淨額差
- f_accel：加速度（二階）＝(Σ5 − 前一個 Σ5)/vol20
- f_zz：z-of-z＝roll-z60( z20 − z20.shift(5) )，z20＝roll-z60(Σ20)
- f_foreign_dif／f_trust_dif：主體拆分（外資／投信各自的 dif_rate）
- ref_level20：(Σ20/20)/vol20 —— **僅對照（docs/19 已否證 level 排序）**

正規化：全部除以自身 20 日均量（股）→ 跨股可比、無單位。
Null 語義：法人覆蓋期內（該日全市場有任何法人資料）缺紀錄＝0（T86 只列有
進出者，同生產 compute_fund_flows fill 0 語義）；覆蓋期外＝null 如實。
融資減肥×價漲、大戶 WoW：快取僅 7 日／2 週，**歷史深度不足無法回測**——
由 runner 報告如實標註，不在此臆造。

純函式；IO 由 flow_inflection_runner 負責。
"""

from __future__ import annotations

import polars as pl

_NET_COLS = ("foreign_net", "trust_net", "dealer_net")

#: 因子欄 → (中文說明, 是否僅對照)
FACTOR_SPECS: dict[str, tuple[str, bool]] = {
    "f_dif_rate": ("5d−20d 日均流差分（/vol20）", False),
    "f_accel": ("流加速度：Σ5 − 前一 Σ5（/vol20）", False),
    "f_zz": ("z-of-z：z20 的 5 日變化再標準化", False),
    "f_foreign_dif": ("外資 5d−20d 日均流差分（/vol20）", False),
    "f_trust_dif": ("投信 5d−20d 日均流差分（/vol20）", False),
    "ref_level20": ("20d 日均流 level（僅對照，docs/19 已否證）", True),
}


def build_flow_factors(panel: pl.DataFrame, z_window: int = 60) -> pl.DataFrame:
    """在面板上加 FACTOR_SPECS 全部因子欄（日頻 rolling；評估端自行抽週）。

    需要欄：date/stock_id/volume/foreign_net/trust_net/dealer_net。
    """
    need = {"date", "stock_id", "volume", *_NET_COLS}
    if panel.is_empty() or not need.issubset(panel.columns):
        return panel
    # 法人覆蓋期：該日全市場任一檔有法人紀錄
    covered = pl.col("foreign_net").is_not_null().any().over("date")
    df = panel.sort("stock_id", "date").with_columns(
        *[
            pl.when(covered)
            .then(pl.col(c).fill_null(0))
            .otherwise(None)
            .alias(f"_{c}")
            for c in _NET_COLS
        ]
    ).with_columns(
        (pl.col("_foreign_net") + pl.col("_trust_net") + pl.col("_dealer_net"))
        .alias("_total_net"),
        pl.col("volume").rolling_mean(20, min_samples=20).over("stock_id").alias("_vol20"),
    )

    def _rsum(col: str, w: int) -> pl.Expr:
        return pl.col(col).rolling_sum(w, min_samples=w).over("stock_id")

    def _norm(expr: pl.Expr) -> pl.Expr:
        return pl.when(pl.col("_vol20") > 0).then(expr / pl.col("_vol20")).otherwise(None)

    def _rz(expr_alias: str) -> pl.Expr:
        m = pl.col(expr_alias).rolling_mean(z_window, min_samples=30).over("stock_id")
        s = pl.col(expr_alias).rolling_std(z_window, min_samples=30).over("stock_id")
        return pl.when(s > 0).then((pl.col(expr_alias) - m) / s).otherwise(None)

    df = df.with_columns(
        _rsum("_total_net", 5).alias("_sum5"),
        _rsum("_total_net", 20).alias("_sum20"),
        _rsum("_foreign_net", 5).alias("_f_sum5"),
        _rsum("_foreign_net", 20).alias("_f_sum20"),
        _rsum("_trust_net", 5).alias("_t_sum5"),
        _rsum("_trust_net", 20).alias("_t_sum20"),
    )
    df = df.with_columns(
        _norm(pl.col("_sum5") / 5 - pl.col("_sum20") / 20).alias("f_dif_rate"),
        _norm(pl.col("_sum5") - pl.col("_sum5").shift(5).over("stock_id")).alias("f_accel"),
        _norm(pl.col("_f_sum5") / 5 - pl.col("_f_sum20") / 20).alias("f_foreign_dif"),
        _norm(pl.col("_t_sum5") / 5 - pl.col("_t_sum20") / 20).alias("f_trust_dif"),
        _norm(pl.col("_sum20") / 20).alias("ref_level20"),
    )
    # z-of-z：z20 → 5 日變化 → 再 roll-z
    df = df.with_columns(_rz("_sum20").alias("_z20"))
    df = df.with_columns(
        (pl.col("_z20") - pl.col("_z20").shift(5).over("stock_id")).alias("_z20_chg")
    )
    df = df.with_columns(_rz("_z20_chg").alias("f_zz"))
    return df.drop([c for c in df.columns if c.startswith("_")])
