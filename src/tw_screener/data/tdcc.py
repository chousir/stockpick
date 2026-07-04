"""TDCC 集保戶股權分散表資料層（規劃書 02 D3）。

來源：台灣集中保管結算所（TDCC）OpenData「集保戶股權分散表」（id=1-5），
每週公布、免費、無反爬。CSV 欄位：

    資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存數比例%

持股分級 1–15 為級距、16 為差異數調整、17 為合計。級距與張數對照（1 張＝1000 股）：
    12: 400,001–600,000   13: 600,001–800,000   14: 800,001–1,000,000   15: 1,000,001 以上
故「≥400 張大戶」＝級距 12–15、「≥1000 張（千張大戶）」＝級距 15。

衍生欄 `big_holder_pct`／`big_holder_1000_pct` ＝該級距股數 ÷ 級距17合計股數 ×100，
**口徑為占集保庫存（≈流通量）比例**，非占股本——報告/文件須據實標明。
逐週累積 `tdcc_distribution_{YYYYMMDD}.parquet`，WoW 由相鄰兩週快照相減。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import httpx
import polars as pl
import yaml
from loguru import logger

from tw_screener.data.cache import find_latest, is_fresh, load_parquet, save_parquet

# 集保戶股權分散表 CSV 的 6 個欄位（順序固定，避免 BOM/中文表頭編碼問題用位置覆寫）
_COLUMNS = ["data_date", "stock_id", "level", "holders", "shares", "pct"]

# 衍生大戶占比的「級距集合」預設（settings 可覆寫，不寫死門檻）
_DEFAULT_BIG_HOLDER_LEVELS = (12, 13, 14, 15)  # ≥400 張
_DEFAULT_BIG_HOLDER_1000_LEVELS = (15,)  # ≥1000 張（千張大戶）

# 合計列（級距 17）＝該股集保庫存總股數，做為占比分母
_TOTAL_LEVEL = 17


def parse_distribution(csv_text: str) -> pl.DataFrame:
    """把 TDCC 集保戶股權分散表 CSV 文字解析成 long DataFrame（純函式）。

    回傳欄位：data_date(Date) / stock_id(Utf8, 去空白) / level(Int64) /
    holders(Int64) / shares(Int64) / pct(Float64)。保留全部 17 級距（含 16/17）。
    以位置覆寫表頭（避開 BOM 與中文欄名編碼），缺欄/空輸入回空表（不拋）。
    """
    text = csv_text.lstrip("﻿").strip()
    if not text:
        return pl.DataFrame(schema=_schema())
    try:
        raw = pl.read_csv(
            io.BytesIO(text.encode("utf-8")),
            has_header=True,
            new_columns=_COLUMNS,
            infer_schema_length=0,  # 全讀為 Utf8，型別由下方顯式 cast 決定
        )
    except Exception as exc:  # noqa: BLE001 — 解析失敗誠實降級回空表，不炸呼叫端
        logger.warning("TDCC 集保分散表解析失敗：{}", exc)
        return pl.DataFrame(schema=_schema())

    if raw.is_empty() or raw.width < len(_COLUMNS):
        logger.warning("TDCC 集保分散表內容為空或欄數不足")
        return pl.DataFrame(schema=_schema())

    return raw.select(
        pl.col("data_date").str.strip_chars().str.strptime(pl.Date, "%Y%m%d", strict=False),
        pl.col("stock_id").str.strip_chars(),
        pl.col("level").str.strip_chars().cast(pl.Int64, strict=False),
        pl.col("holders").str.strip_chars().cast(pl.Int64, strict=False),
        pl.col("shares").str.strip_chars().cast(pl.Int64, strict=False),
        pl.col("pct").str.strip_chars().cast(pl.Float64, strict=False),
    ).filter(pl.col("stock_id").is_not_null() & (pl.col("stock_id") != ""))


def _schema():  # 回傳型別由字面值推斷（polars schema 接受 DataType 類別）
    return {
        "data_date": pl.Date,
        "stock_id": pl.Utf8,
        "level": pl.Int64,
        "holders": pl.Int64,
        "shares": pl.Int64,
        "pct": pl.Float64,
    }


def derive_big_holders(
    dist: pl.DataFrame,
    levels_400: tuple[int, ...] = _DEFAULT_BIG_HOLDER_LEVELS,
    levels_1000: tuple[int, ...] = _DEFAULT_BIG_HOLDER_1000_LEVELS,
) -> pl.DataFrame:
    """從 long 分散表算每股大戶占比（純函式，支援多日輸入）。

    big_holder_pct      ＝ Σ(級距 12–15 股數) ÷ 級距17合計股數 ×100（≥400 張）
    big_holder_1000_pct ＝ Σ(級距 15 股數)    ÷ 級距17合計股數 ×100（≥1000 張）
    占比口徑為占集保庫存（級距17＝100%）。合計股數 ≤0 → 該股占比 null（不補零）。
    回傳：data_date / stock_id / big_holder_pct / big_holder_1000_pct。
    """
    empty = {
        "data_date": pl.Date,
        "stock_id": pl.Utf8,
        "big_holder_pct": pl.Float64,
        "big_holder_1000_pct": pl.Float64,
    }
    if dist.is_empty():
        return pl.DataFrame(schema=empty)

    totals = dist.filter(pl.col("level") == _TOTAL_LEVEL).select(
        "data_date", "stock_id", pl.col("shares").alias("total_shares")
    )
    big400 = (
        dist.filter(pl.col("level").is_in(list(levels_400)))
        .group_by("data_date", "stock_id")
        .agg(pl.col("shares").sum().alias("big400_shares"))
    )
    big1000 = (
        dist.filter(pl.col("level").is_in(list(levels_1000)))
        .group_by("data_date", "stock_id")
        .agg(pl.col("shares").sum().alias("big1000_shares"))
    )

    out = (
        totals.join(big400, on=["data_date", "stock_id"], how="left")
        .join(big1000, on=["data_date", "stock_id"], how="left")
        .with_columns(
            pl.when(pl.col("total_shares") > 0)
            .then(100.0 * pl.col("big400_shares").fill_null(0) / pl.col("total_shares"))
            .otherwise(None)
            .round(2)
            .alias("big_holder_pct"),
            pl.when(pl.col("total_shares") > 0)
            .then(100.0 * pl.col("big1000_shares").fill_null(0) / pl.col("total_shares"))
            .otherwise(None)
            .round(2)
            .alias("big_holder_1000_pct"),
        )
        .select("data_date", "stock_id", "big_holder_pct", "big_holder_1000_pct")
    )
    return out.sort("stock_id")


def latest_big_holders_with_wow(derived: pl.DataFrame) -> pl.DataFrame:
    """取最新一週的大戶占比，並與「前一週」相減算 WoW（純函式）。

    只有一週資料 → WoW 欄為 null（誠實，不假裝有變化）。新上市/前週缺資料的個股
    亦為 null（左 join 自最新週）。回傳：stock_id / data_date / big_holder_pct /
    big_holder_1000_pct / big_holder_wow / big_holder_1000_wow。
    """
    empty = {
        "stock_id": pl.Utf8,
        "data_date": pl.Date,
        "big_holder_pct": pl.Float64,
        "big_holder_1000_pct": pl.Float64,
        "big_holder_wow": pl.Float64,
        "big_holder_1000_wow": pl.Float64,
    }
    if derived.is_empty():
        return pl.DataFrame(schema=empty)

    dates = derived["data_date"].unique().sort(descending=True).to_list()
    latest = derived.filter(pl.col("data_date") == dates[0])

    if len(dates) < 2:
        return latest.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("big_holder_wow"),
            pl.lit(None, dtype=pl.Float64).alias("big_holder_1000_wow"),
        ).select(list(empty.keys()))

    prev = derived.filter(pl.col("data_date") == dates[1]).select(
        "stock_id",
        pl.col("big_holder_pct").alias("_prev_400"),
        pl.col("big_holder_1000_pct").alias("_prev_1000"),
    )
    return (
        latest.join(prev, on="stock_id", how="left")
        .with_columns(
            (pl.col("big_holder_pct") - pl.col("_prev_400")).round(2).alias("big_holder_wow"),
            (pl.col("big_holder_1000_pct") - pl.col("_prev_1000"))
            .round(2)
            .alias("big_holder_1000_wow"),
        )
        .select(list(empty.keys()))
    )


class TDCCClient:
    """TDCC OpenData 集保戶股權分散表 client（含本地 parquet 快取）。"""

    def __init__(
        self,
        base_url: str,
        endpoint_id: str,
        cache_dir: Path,
        ttl_hours: float,
        user_agent: str,
        interval_sec: float,
        levels_400: tuple[int, ...],
        levels_1000: tuple[int, ...],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint_id = endpoint_id
        self.cache_dir = Path(cache_dir)
        self.ttl_hours = ttl_hours
        self.user_agent = user_agent
        self.interval_sec = interval_sec
        self.levels_400 = levels_400
        self.levels_1000 = levels_1000
        self._last_req: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)

    def fetch_distribution(self, max_retries: int = 2) -> pl.DataFrame:
        """抓集保戶股權分散表，快取到 tdcc_distribution_{資料日}.parquet（逐週累積）。

        檔名錨點為資料內 max(資料日期)（TDCC 自帶週公布日），TTL 內同檔重跑讀快取。
        transient 失敗指數退避重試；最終失敗回空表（呼叫端降級）。
        """
        latest = find_latest(self.cache_dir, "tdcc_distribution_*.parquet", by="name")
        if latest is not None and is_fresh(latest, self.ttl_hours):
            logger.info("命中快取 {}", latest)
            return load_parquet(latest)

        url = f"{self.base_url}/getOD.ashx?id={self.endpoint_id}"
        csv_text = self._download_csv(url, max_retries)
        if csv_text is None:
            logger.warning("TDCC 集保分散表下載失敗，回空表")
            return pl.DataFrame(schema=_schema())

        df = parse_distribution(csv_text)
        if df.is_empty():
            logger.warning("TDCC 集保分散表解析為空")
            return df

        date_str = df.select(pl.col("data_date").max().dt.strftime("%Y%m%d")).item()
        cache_file = self.cache_dir / f"tdcc_distribution_{date_str}.parquet"
        save_parquet(df, cache_file)
        logger.info(
            "TDCC 集保分散表快取 → {}（{} 檔個股）",
            cache_file,
            df.select(pl.col("stock_id").n_unique()).item(),
        )
        return df

    def _download_csv(self, url: str, max_retries: int) -> str | None:
        for attempt in range(max_retries + 1):
            self._throttle()
            tag = "" if attempt == 0 else f" (retry {attempt}/{max_retries})"
            logger.info("HTTP GET {}{}", url, tag)
            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=60.0,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.warning("{} HTTP error: {}", url, exc)
                if attempt < max_retries:
                    time.sleep(5 * (2**attempt))
                    continue
                return None
            self._last_req = time.monotonic()
            return resp.text
        return None

    def load_big_holders(self, n_weeks: int = 4) -> pl.DataFrame:
        """純讀近 n_weeks 份 tdcc_distribution_*.parquet，回最新週大戶占比＋WoW（不打網）。

        無快取回空表（欄位齊全），供 group/個股報告 join。WoW 取相鄰兩週相減。
        """
        files = sorted(self.cache_dir.glob("tdcc_distribution_*.parquet"))
        if not files:
            return latest_big_holders_with_wow(pl.DataFrame())
        recent = files[-max(n_weeks, 2):]
        frames = []
        for f in recent:
            try:
                frames.append(load_parquet(f))
            except Exception as exc:  # noqa: BLE001 — 單檔壞不擋整批
                logger.warning("讀取 {} 失敗：{}", f, exc)
        if not frames:
            return latest_big_holders_with_wow(pl.DataFrame())
        dist = pl.concat(frames, how="vertical_relaxed")
        derived = derive_big_holders(dist, self.levels_400, self.levels_1000)
        return latest_big_holders_with_wow(derived)


def create_tdcc_client(settings_path: Path = Path("config/settings.yaml")) -> TDCCClient:
    """依 settings.yaml 的 tdcc 區塊建立 TDCCClient（參數皆可換、不寫死）。"""
    with open(settings_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    tdcc = cfg.get("tdcc", {})
    paths = cfg.get("paths", {})
    default_ua = cfg.get("twse", {}).get("user_agent", "tw-screener/1.0")
    return TDCCClient(
        base_url=tdcc.get("base_url", "https://opendata.tdcc.com.tw"),
        endpoint_id=str(tdcc.get("endpoint_id", "1-5")),
        cache_dir=Path(paths.get("cache_dir", "data/cache")) / "twse",
        ttl_hours=float(tdcc.get("cache_ttl_hours", 18)),
        user_agent=tdcc.get("user_agent", default_ua),
        interval_sec=float(tdcc.get("interval_sec", 1.0)),
        levels_400=tuple(tdcc.get("big_holder_levels", _DEFAULT_BIG_HOLDER_LEVELS)),
        levels_1000=tuple(tdcc.get("big_holder_1000_levels", _DEFAULT_BIG_HOLDER_1000_LEVELS)),
    )
