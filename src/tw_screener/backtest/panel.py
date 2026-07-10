"""backtest/panel.py — WS-A ground-truth 日頻面板（W28 統一因子實驗台資料底盤）。

date × stock_id 面板，任何因子研究 join 一次即可取得前瞻報酬與基準（≤3 行）：

    panel = pl.read_parquet("research/panel/panel.parquet")
    df = my_factor.join(panel, on=["date", "stock_id"], how="inner")
    ic = spearman(df, "my_factor", "alpha20")

欄位與約定（與 strategies.py V1 前瞻引擎同一套防偏誤慣例）：
- close/volume：原始收盤/量（close>0 過濾髒資料；ETF/權證整檔排除）。
- r{h}（h∈horizons，交易日）：前瞻報酬%。entry＝**次一交易日**收盤（防前視）、
  exit＝entry 後第 h 個交易日收盤；現金股利 ex_date∈(entry,exit] 依 V1 慣例線性加回。
  **未到期與下市一律 null**（不補 0、不污染統計）。
- mkt_ew_r{h}：同日全面板 r{h} 中位數＝全市場等權同窗基準；alpha{h}=r{h}−mkt_ew_r{h}。
- sub_industry：concepts.yaml 手標次產業（一檔多屬取 membership 表第一筆；
  族群層研究請 join list_subindustries() 長表，勿依賴本欄的多屬摺疊）。
- ma20_dist_pct/ma60_dist_pct：收盤距自身均線%（原始收盤；窗不足 → null）。
- foreign_net/trust_net/dealer_net：三大法人日淨額（單位＝股；上市 T86＋上櫃 3itrade；
  該日無法人資料 → null，不填 0）。
- vol_ratio：今日量／前 vol_lookback 日均量（**窗不足 → null**；與 grouping 篩選層
  fill 0 的降級慣例不同——面板是 ground truth，缺就是缺）。

純函式；IO（快取載入、parquet 落地、核價抽查取樣）由 panel_runner 負責。
"""

from __future__ import annotations

import polars as pl

_ID_COLS = ("date", "stock_id")


def _non_etf_expr() -> pl.Expr:
    """ETF/權證排除（與 grouping.is_etf_or_warrant 同語義的向量式）。"""
    sid = pl.col("stock_id")
    return ~sid.str.starts_with("00") & sid.str.contains(r"^\d+$")


def _cumulative_dividends(px: pl.DataFrame, dividends: pl.DataFrame | None) -> pl.DataFrame:
    """把現金股利對齊到各股交易日序列 → 累計股利欄 `_cumdiv`。

    ex_date 對齊到該股**第一個 ≥ ex_date 的交易日**（停牌跨日自動後移）；
    落在序列外（最後交易日之後）的股利不進任何窗、直接捨棄。
    無股利資料 → _cumdiv 全 0（報酬退化為純價差，與 V1 dividends=None 同義）。
    """
    if dividends is None or dividends.is_empty() or not {
        "stock_id", "ex_date", "cash_dividend",
    }.issubset(dividends.columns):
        return px.with_columns(pl.lit(0.0).alias("_cumdiv"))
    div = (
        dividends.filter(
            pl.col("cash_dividend").is_not_null() & (pl.col("cash_dividend") > 0)
        )
        .with_columns(pl.col("stock_id").cast(pl.Utf8))
        .group_by("stock_id", "ex_date")
        .agg(pl.col("cash_dividend").sum())
        .sort("ex_date")
        .join_asof(
            px.select("stock_id", "date").sort("date"),
            left_on="ex_date",
            right_on="date",
            by="stock_id",
            strategy="forward",
            check_sortedness=False,  # 兩側已明確 sort（by 群組下 polars 無法自檢）
        )
        .drop_nulls("date")
        .group_by("stock_id", "date")
        .agg(pl.col("cash_dividend").sum().alias("_div"))
    )
    return (
        px.join(div, on=["stock_id", "date"], how="left")
        .with_columns(
            pl.col("_div").fill_null(0.0).cum_sum().over("stock_id").alias("_cumdiv")
        )
        .drop("_div")
    )


def build_price_panel(
    price: pl.DataFrame,
    dividends: pl.DataFrame | None = None,
    institutional: pl.DataFrame | None = None,
    membership: pl.DataFrame | None = None,
    horizons: tuple[int, ...] = (5, 10, 20, 40),
    ma_windows: tuple[int, ...] = (20, 60),
    vol_lookback: int = 20,
) -> pl.DataFrame:
    """組 ground-truth 面板（欄位約定見模組 docstring）。

    Args:
        price: 日線長表（date/stock_id/close，volume 可缺）。
        dividends: load_recent_dividends() 輸出（stock_id/ex_date/cash_dividend）。
        institutional: 三大法人日淨額長表（date/stock_id/foreign_net/trust_net/dealer_net）。
        membership: list_subindustries() 輸出（sub_industry/stock_id）。
        horizons: 前瞻窗（交易日）。
        ma_windows: 位階均線窗。
        vol_lookback: 量比回看窗。

    Returns:
        date × stock_id 面板；輸入空 → 空表。
    """
    if price.is_empty() or not {"date", "stock_id", "close"}.issubset(price.columns):
        return pl.DataFrame()

    px = (
        price.with_columns(pl.col("stock_id").cast(pl.Utf8))
        .filter(_non_etf_expr())
        .drop_nulls("close")
        .filter(pl.col("close") > 0)
        .unique(subset=list(_ID_COLS), keep="first")
        .sort("stock_id", "date")
    )
    if "volume" not in px.columns:
        px = px.with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    px = px.select("date", "stock_id", "close", pl.col("volume").cast(pl.Float64))
    if px.is_empty():
        return pl.DataFrame()

    px = _cumulative_dividends(px, dividends)

    # 前瞻報酬：entry=次一交易列、exit=entry+h 列；shift 出界（未到期/下市）自然為 null
    fwd_exprs: list[pl.Expr] = []
    for h in horizons:
        entry = pl.col("close").shift(-1).over("stock_id")
        exit_ = pl.col("close").shift(-(1 + h)).over("stock_id")
        div_in_window = (
            pl.col("_cumdiv").shift(-(1 + h)) - pl.col("_cumdiv").shift(-1)
        ).over("stock_id")
        fwd_exprs.append(
            ((exit_ - entry + div_in_window) / entry * 100).alias(f"r{h}")
        )

    # 位階與量比（原始收盤；窗不足 null）
    level_exprs: list[pl.Expr] = []
    for w in ma_windows:
        ma = pl.col("close").rolling_mean(window_size=w, min_samples=w).over("stock_id")
        level_exprs.append(((pl.col("close") / ma - 1) * 100).alias(f"ma{w}_dist_pct"))
    prior_vol = (
        pl.col("volume")
        .shift(1)
        .rolling_mean(window_size=vol_lookback, min_samples=vol_lookback)
        .over("stock_id")
    )
    level_exprs.append(
        pl.when(prior_vol > 0).then(pl.col("volume") / prior_vol).alias("vol_ratio")
    )

    panel = px.with_columns(*fwd_exprs, *level_exprs).drop("_cumdiv")

    # 全市場等權同窗基準（同日中位；null 不計）＋超額
    panel = panel.with_columns(
        *[pl.col(f"r{h}").median().over("date").alias(f"mkt_ew_r{h}") for h in horizons]
    ).with_columns(
        *[(pl.col(f"r{h}") - pl.col(f"mkt_ew_r{h}")).alias(f"alpha{h}") for h in horizons]
    )

    if institutional is not None and not institutional.is_empty():
        inst = (
            institutional.with_columns(pl.col("stock_id").cast(pl.Utf8))
            .select("date", "stock_id", "foreign_net", "trust_net", "dealer_net")
            .unique(subset=list(_ID_COLS), keep="first")
        )
        panel = panel.join(inst, on=["date", "stock_id"], how="left")
    else:
        panel = panel.with_columns(
            *[pl.lit(None, dtype=pl.Int64).alias(c)
              for c in ("foreign_net", "trust_net", "dealer_net")]
        )

    if membership is not None and not membership.is_empty():
        first_sub = membership.group_by("stock_id", maintain_order=True).agg(
            pl.col("sub_industry").first()
        )
        panel = panel.join(first_sub, on="stock_id", how="left")
    else:
        panel = panel.with_columns(pl.lit(None, dtype=pl.Utf8).alias("sub_industry"))

    return panel.sort("stock_id", "date")


def panel_summary(panel: pl.DataFrame, horizons: tuple[int, ...] = (5, 10, 20, 40)) -> pl.DataFrame:
    """面板體檢表：各欄非 null 覆蓋率＋整體規模（給 build 報告，不做任何裁決）。"""
    schema = {"metric": pl.Utf8, "value": pl.Utf8}
    if panel.is_empty():
        return pl.DataFrame(schema=schema)
    d0, d1 = str(panel["date"].min()), str(panel["date"].max())
    rows = [
        {"metric": "rows", "value": str(panel.height)},
        {"metric": "stocks", "value": str(panel["stock_id"].n_unique())},
        {"metric": "date_range", "value": f"{d0} ~ {d1}"},
        {"metric": "trading_days", "value": str(panel["date"].n_unique())},
        {
            "metric": "with_sub_industry",
            "value": str(panel.filter(pl.col("sub_industry").is_not_null())["stock_id"].n_unique()),
        },
    ]
    check_cols = [
        *(f"r{h}" for h in horizons),
        "ma20_dist_pct", "ma60_dist_pct", "vol_ratio",
        "foreign_net", "trust_net", "dealer_net",
    ]
    for c in check_cols:
        if c in panel.columns:
            cov = panel[c].is_not_null().mean()
            pct = float(cov) if isinstance(cov, (int, float)) else 0.0
            rows.append({"metric": f"coverage_{c}", "value": f"{pct:.1%}"})
    return pl.DataFrame(rows, schema=schema)


def reconcile_close(
    panel: pl.DataFrame,
    reference: pl.DataFrame,
    tol_pct: float = 0.5,
) -> pl.DataFrame:
    """核價抽查：面板 close vs 獨立來源 reference（date/stock_id/close_ref）。

    回傳逐筆對照（diff_pct＝|panel−ref|/ref×100、within_tol）；配不到的 ref 筆不入表
    （由呼叫端決定要不要當缺口回報）。
    """
    schema = {
        "date": pl.Date, "stock_id": pl.Utf8, "close_panel": pl.Float64,
        "close_ref": pl.Float64, "diff_pct": pl.Float64, "within_tol": pl.Boolean,
    }
    need = {"date", "stock_id", "close_ref"}
    if panel.is_empty() or reference.is_empty() or not need.issubset(reference.columns):
        return pl.DataFrame(schema=schema)
    ref = reference.with_columns(pl.col("stock_id").cast(pl.Utf8)).drop_nulls("close_ref")
    return (
        panel.select("date", "stock_id", pl.col("close").alias("close_panel"))
        .join(ref.select("date", "stock_id", "close_ref"), on=["date", "stock_id"], how="inner")
        .with_columns(
            ((pl.col("close_panel") - pl.col("close_ref")).abs() / pl.col("close_ref") * 100)
            .alias("diff_pct")
        )
        .with_columns((pl.col("diff_pct") < tol_pct).alias("within_tol"))
        .select(list(schema))
        .sort("diff_pct", descending=True)
    )
