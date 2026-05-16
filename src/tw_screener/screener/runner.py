"""runner.py — 策略執行器。"""

from datetime import date
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

from tw_screener.screener.goodinfo.fetcher import create_fetcher
from tw_screener.screener.goodinfo.parser import parse_screener_result
from tw_screener.screener.goodinfo.url_builder import build_screener_url, load_strategy

_DETAIL_BASE = "https://goodinfo.tw/tw/StockInfo/StockDetail.asp"


class ScreenerRunner:
    def __init__(self, settings_path: Path = Path("config/settings.yaml")) -> None:
        with open(settings_path, encoding="utf-8") as fh:
            self._settings = yaml.safe_load(fh)

        cache_dir = Path(self._settings["paths"]["cache_dir"])
        self._fetcher = create_fetcher(self._settings, cache_dir)
        self._strategies_dir = Path(self._settings["paths"]["strategies_dir"])
        self._reports_dir = Path(self._settings["paths"]["reports_dir"])
        self._goodinfo_base: str = self._settings["goodinfo"]["base_url"]

    def run_strategy(self, strategy_path: Path) -> pl.DataFrame:
        """跑單一策略，回傳附有 metadata 欄位的結果 DataFrame。

        0 筆結果視為正常（市場大跌時 A 策略可能篩出 0 檔）。
        超過 100 筆時印警告：條件可能太寬鬆。
        """
        strategy = load_strategy(strategy_path)
        url = build_screener_url(strategy, self._goodinfo_base)
        logger.info("Strategy {}: {}", strategy.id, url)

        html = self._fetcher.get(url)
        df = parse_screener_result(html)

        if len(df) > 100:
            logger.warning(
                "Strategy {} 篩出 {} 檔，條件可能太寬鬆", strategy.id, len(df)
            )

        today = date.today()

        if df.is_empty():
            return df.with_columns([
                pl.lit(strategy.id).alias("strategy_id"),
                pl.lit(today).alias("screened_at"),
                pl.lit("").alias("goodinfo_url"),
            ])

        return df.with_columns([
            pl.lit(strategy.id).alias("strategy_id"),
            pl.lit(today).alias("screened_at"),
            pl.concat_str([
                pl.lit(f"{_DETAIL_BASE}?STOCK_ID="),
                pl.col("stock_id"),
            ]).alias("goodinfo_url"),
        ])

    def run_all(self, week_tag: str | None = None) -> dict[str, pl.DataFrame]:
        """跑 strategies_dir 下所有 YAML，輸出 CSV 到 reports/YYYY-Www/。"""
        if week_tag is None:
            week_tag = date.today().strftime("%Y-W%V")

        results: dict[str, pl.DataFrame] = {}
        for yaml_path in sorted(self._strategies_dir.glob("*.yaml")):
            strategy = load_strategy(yaml_path)
            logger.info("Running strategy: {}", strategy.id)
            df = self.run_strategy(yaml_path)
            results[strategy.id] = df
            self.export_csv(df, strategy.id, week_tag)

        return results

    def export_csv(self, df: pl.DataFrame, strategy_id: str, week_tag: str) -> Path:
        """寫入 reports/YYYY-Www/screen_result_{strategy_id}.csv。"""
        report_dir = self._reports_dir / week_tag
        report_dir.mkdir(parents=True, exist_ok=True)
        output = report_dir / f"screen_result_{strategy_id}.csv"
        df.write_csv(output)
        logger.info("Exported {} rows → {}", len(df), output)
        return output
