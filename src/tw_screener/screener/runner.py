"""runner.py — 策略執行器。"""

from datetime import date
from pathlib import Path

import polars as pl
import yaml
from loguru import logger

from tw_screener.screener.goodinfo.fetcher import GoodinfoBlockedError, create_fetcher
from tw_screener.screener.goodinfo.parser import (
    GoodinfoParseError,
    GoodinfoTooManyResultsError,
    parse_screener_result,
)
from tw_screener.screener.goodinfo.url_builder import (
    build_data_url,
    build_screener_url,
    load_strategy,
)
from tw_screener.screener.log_writer import write_screen_log

_GROUP_PREFIX: dict[str, set[str]] = {
    "abc": {"a", "b", "c"},
    "def": {"d", "e", "f"},
    "defg": {"d", "e", "f", "g"},  # 現行主流程：D/E/F + G（成長拉回）
}


def derive_week_tag(settings_path: Path = Path("config/settings.yaml")) -> str:
    """用 TWSE 最近交易日推 ISO 週標籤；無 trading_date 時 fallback 到 today。

    這是 trading_date 錨點的唯一入口；CLI / runner / builder 預設 week_tag 都走這裡。
    """
    from tw_screener.data.twse import create_client

    try:
        client = create_client(settings_path)
        td = client.latest_trading_date()
    except Exception as exc:
        logger.warning("derive_week_tag fallback to today: {}", exc)
        td = None
    return (td or date.today()).strftime("%Y-W%V")


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
        self._detail_base = f"{self._goodinfo_base}/StockDetail.asp"
        # screened_at 用 trading_date 對齊整批執行；取一次避免每個策略各別查
        self._trading_date: date | None = None
        # run_all 期間累積的「本週未取得」策略 → reason（規劃書 02 D1 韌性）
        self.failures: dict[str, str] = {}

    def _resolve_trading_date(self) -> date:
        """Lazily resolve trading_date once per runner instance."""
        if self._trading_date is None:
            from tw_screener.data.twse import create_client

            try:
                client = create_client(self._settings_path)
                self._trading_date = client.latest_trading_date() or date.today()
            except Exception as exc:
                logger.warning("_resolve_trading_date fallback to today: {}", exc)
                self._trading_date = date.today()
        return self._trading_date

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

        screened_at = self._resolve_trading_date()

        if df.is_empty():
            return df.with_columns(
                [
                    pl.lit(strategy.id).alias("strategy_id"),
                    pl.lit(screened_at).alias("screened_at"),
                    pl.lit("").alias("goodinfo_url"),
                ]
            )

        return df.with_columns(
            [
                pl.lit(strategy.id).alias("strategy_id"),
                pl.lit(screened_at).alias("screened_at"),
                pl.concat_str(
                    [
                        pl.lit(f"{self._detail_base}?STOCK_ID="),
                        pl.col("stock_id"),
                    ]
                ).alias("goodinfo_url"),
            ]
        )

    def run_all(
        self, week_tag: str | None = None, group: str | None = None
    ) -> dict[str, pl.DataFrame]:
        """跑 strategies_dir 下 YAML，輸出 CSV 到 reports/YYYY-Www/。

        group: "abc" 只跑 id 開頭 a/b/c；"def" 只跑 d/e/f；None 跑全部。

        韌性（規劃書 02 D1）：單一策略 parse 改版或結果超限時降級為「本週未取得」，
        記入 self.failures 與 screen_log.md，其餘策略照跑、整批不中斷。
        GoodinfoBlockedError 為 IP 層封鎖 → 保留中斷整批的語意（再打也是被擋）。
        """
        if week_tag is None:
            week_tag = derive_week_tag(self._settings_path)

        yaml_paths = sorted(self._strategies_dir.glob("*.yaml"))
        if group is not None:
            prefixes = _GROUP_PREFIX[group]
            yaml_paths = [p for p in yaml_paths if p.stem[:1].lower() in prefixes]

        results: dict[str, pl.DataFrame] = {}
        strategy_names: dict[str, str] = {}
        self.failures = {}
        for yaml_path in yaml_paths:
            strategy = load_strategy(yaml_path)
            strategy_names[strategy.id] = strategy.name
            logger.info("Running strategy: {}", strategy.id)
            try:
                df = self.run_strategy(yaml_path)
            except GoodinfoBlockedError:
                self.write_blocked_log(strategy.id, week_tag)
                raise
            except (GoodinfoParseError, GoodinfoTooManyResultsError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                logger.error("Strategy {} 本週未取得：{}", strategy.id, reason)
                self.failures[strategy.id] = reason
                continue
            results[strategy.id] = df
            self.export_csv(df, strategy.id, week_tag)

        if results or self.failures:
            write_screen_log(
                results, strategy_names, week_tag, self._reports_dir, failures=self.failures
            )

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
