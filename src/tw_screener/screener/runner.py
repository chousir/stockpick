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
