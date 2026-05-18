"""runner.py — 策略執行器。"""

from datetime import date
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

from tw_screener.screener.goodinfo.fetcher import GoodinfoBlockedError, create_fetcher
from tw_screener.screener.goodinfo.parser import (
    GoodinfoTooManyResultsError,
    parse_screener_result,
)
from tw_screener.screener.goodinfo.url_builder import (
    build_data_url,
    build_screener_url,
    load_strategy,
)
from tw_screener.screener.log_writer import write_screen_log


class ScreenerRunner:
    def __init__(self, settings_path: Path = Path("config/settings.yaml")) -> None:
        self._settings_path = settings_path
        with open(settings_path, encoding="utf-8") as fh:
            self._settings = yaml.safe_load(fh)

        cache_dir = Path(self._settings["paths"]["cache_dir"])
        self._fetcher = create_fetcher(self._settings, cache_dir)
        self._strategies_dir = Path(self._settings["paths"]["strategies_dir"])
        self._reports_dir = Path(self._settings["paths"]["reports_dir"])
        self._goodinfo_base: str = self._settings["goodinfo"]["base_url"]
        self._detail_base = f"{self._goodinfo_base}/StockInfo/StockDetail.asp"

    def run_strategy(self, strategy_path: Path) -> pl.DataFrame:
        """跑單一策略，回傳附有 metadata 欄位的結果 DataFrame。

        0 筆結果視為正常（市場大跌時 A 策略可能篩出 0 檔）。
        超過 100 筆時印警告：條件可能太寬鬆。
        篩選結果 > 300 筆時 raise GoodinfoTooManyResultsError（Goodinfo 匿名上限）。
        """
        strategy = load_strategy(strategy_path)
        display_url = build_screener_url(strategy, self._goodinfo_base)
        data_url = build_data_url(strategy, self._goodinfo_base)
        logger.info("Strategy {}: {}", strategy.id, display_url)

        html = self._fetcher.get(data_url)
        try:
            df = parse_screener_result(html)
        except GoodinfoTooManyResultsError:
            logger.error(
                "Strategy {} 篩選結果超過 300 筆（Goodinfo 匿名上限），請縮小篩選條件",
                strategy.id,
            )
            raise

        if len(df) > 100:
            logger.warning("Strategy {} 篩出 {} 檔，條件可能太寬鬆", strategy.id, len(df))

        # 套用本地 post_filter（如有），這會打 TWSE STOCK_DAY 補抓必要歷史
        if strategy.post_filter and not df.is_empty():
            df = self._apply_post_filter(df, strategy.post_filter)
            logger.info("Strategy {} post_filter 後剩 {} 檔", strategy.id, len(df))

        today = date.today()

        if df.is_empty():
            return df.with_columns(
                [
                    pl.lit(strategy.id).alias("strategy_id"),
                    pl.lit(today).alias("screened_at"),
                    pl.lit("").alias("goodinfo_url"),
                ]
            )

        return df.with_columns(
            [
                pl.lit(strategy.id).alias("strategy_id"),
                pl.lit(today).alias("screened_at"),
                pl.concat_str(
                    [
                        pl.lit(f"{self._detail_base}?STOCK_ID="),
                        pl.col("stock_id"),
                    ]
                ).alias("goodinfo_url"),
            ]
        )

    def _apply_post_filter(
        self, df: pl.DataFrame, rules: list  # list[PostFilterRule]
    ) -> pl.DataFrame:
        """套用本地 post_filter。目前支援 field='pct_from_52w_high'。

        會對每檔候選股呼叫 TWSEClient.fetch_stock_history 補抓 N 個月歷史。
        cache miss 時首次跑會慢（每檔 ~4 秒）。資料完全缺失的股票會保留並警告。
        """
        from tw_screener.analysis.grouping import is_etf_or_warrant
        from tw_screener.data.twse import create_client

        client = None  # 延後建立避免不必要的初始化
        for rule in rules:
            if rule.field != "pct_from_52w_high":
                logger.warning("post_filter 不支援的 field: {}，跳過", rule.field)
                continue

            if client is None:
                client = create_client(self._settings_path)

            months = rule.months or 6
            stock_ids: list[str] = [
                sid for sid in df["stock_id"].cast(pl.Utf8).to_list()
                if not is_etf_or_warrant(str(sid).strip())
            ]
            logger.info(
                "post_filter pct_from_52w_high: {} 檔候選股補抓 {} 個月歷史",
                len(stock_ids),
                months,
            )

            pcts: dict[str, float | None] = {}
            for idx, sid in enumerate(stock_ids, start=1):
                try:
                    history = client.fetch_stock_history(sid, months=months)
                except Exception as exc:
                    logger.warning("post_filter {} 抓歷史失敗：{}", sid, exc)
                    pcts[sid] = None
                    continue
                if history.is_empty() or "high" not in history.columns:
                    pcts[sid] = None
                    continue
                high = history["high"].max()
                latest = history.sort("date")["close"][-1]
                if high is None or latest is None or high == 0:
                    pcts[sid] = None
                    continue
                pcts[sid] = (latest / high - 1) * 100
                if idx % 20 == 0:
                    logger.info("  post_filter 進度 {}/{}", idx, len(stock_ids))

            # 把 pct 值寫成新欄位，CSV 輸出時就看得到「距高 -25%」之類的數字
            col_name = f"pct_from_{months}m_high"
            df = df.with_columns(
                pl.Series(
                    col_name,
                    [pcts.get(str(s).strip()) for s in df["stock_id"].cast(pl.Utf8).to_list()],
                    dtype=pl.Float64,
                )
            )

            # 保留：pct=None 視為資料不足，預設保留（不在 filter 範圍內）
            def _judge(sid: str) -> bool:
                v = pcts.get(str(sid).strip())
                if v is None:
                    return True
                if rule.max is not None and v > rule.max:
                    return False
                if rule.min is not None and v < rule.min:
                    return False
                return True

            before = len(df)
            mask = [_judge(s) for s in df["stock_id"].cast(pl.Utf8).to_list()]
            df = df.filter(pl.Series(mask, dtype=pl.Boolean))

            # Summary log（保留 N/M 檔 + pct 範圍）
            valid_pcts = [v for v in pcts.values() if v is not None]
            if valid_pcts:
                lo, hi = min(valid_pcts), max(valid_pcts)
                logger.info(
                    "post_filter {}: 保留 {}/{} 檔（pct 範圍 {:.1f}% ~ {:.1f}%）",
                    col_name,
                    len(df),
                    before,
                    lo,
                    hi,
                )

        return df

    def run_all(self, week_tag: str | None = None) -> dict[str, pl.DataFrame]:
        """跑 strategies_dir 下所有 YAML，輸出 CSV 到 reports/YYYY-Www/。"""
        if week_tag is None:
            week_tag = date.today().strftime("%Y-W%V")

        results: dict[str, pl.DataFrame] = {}
        strategy_names: dict[str, str] = {}
        for yaml_path in sorted(self._strategies_dir.glob("*.yaml")):
            strategy = load_strategy(yaml_path)
            strategy_names[strategy.id] = strategy.name
            logger.info("Running strategy: {}", strategy.id)
            try:
                df = self.run_strategy(yaml_path)
            except GoodinfoBlockedError:
                self.write_blocked_log(strategy.id, week_tag)
                raise
            results[strategy.id] = df
            self.export_csv(df, strategy.id, week_tag)

        if results:
            write_screen_log(results, strategy_names, week_tag, self._reports_dir)

        return results

    def write_blocked_log(self, strategy_id: str, week_tag: str) -> Path:
        """被 Goodinfo 封鎖時，附加一行到 reports/YYYY-Www/blocked.log，回傳路徑。"""
        report_dir = self._reports_dir / week_tag
        report_dir.mkdir(parents=True, exist_ok=True)
        log_path = report_dir / "blocked.log"
        ts = date.today().isoformat()
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} strategy={strategy_id} Goodinfo access blocked\n")
        logger.warning("Blocked log written → {}", log_path)
        return log_path

    def export_csv(self, df: pl.DataFrame, strategy_id: str, week_tag: str) -> Path:
        """寫入 reports/YYYY-Www/screen_result_{strategy_id}.csv。"""
        report_dir = self._reports_dir / week_tag
        report_dir.mkdir(parents=True, exist_ok=True)
        output = report_dir / f"screen_result_{strategy_id}.csv"
        df.write_csv(output)
        logger.info("Exported {} rows → {}", len(df), output)
        return output
