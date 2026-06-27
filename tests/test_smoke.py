"""Smoke tests — 確認基本 import 和 CLI 可運作。"""

from pathlib import Path

from typer.testing import CliRunner

from tw_screener import __version__
from tw_screener.cli import app


def test_version_defined() -> None:
    assert __version__ == "0.1.0"


def test_hello_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "Hello from tw-stock-screener" in result.output


def _write_prune_fixture(tmp_path: Path) -> Path:
    """造 cache 檔 + 指向它的 settings；回傳 settings 路徑。"""
    twse = tmp_path / "cache" / "twse"
    twse.mkdir(parents=True)
    (twse / "institutional_20260625.parquet").write_bytes(b"")   # 錨、窗內
    (twse / "institutional_20240101.parquet").write_bytes(b"")   # 超窗 → 該刪
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\n"
        "cache:\n  retention:\n    institutional_days: 400\n",
        encoding="utf-8",
    )
    return settings


def test_prune_cache_dry_lists_without_deleting(tmp_path: Path) -> None:
    settings = _write_prune_fixture(tmp_path)
    old = tmp_path / "cache" / "twse" / "institutional_20240101.parquet"
    result = CliRunner().invoke(app, ["data", "prune-cache", "--dry", "--settings", str(settings)])
    assert result.exit_code == 0
    assert "institutional_20240101.parquet" in result.output
    assert old.exists()  # --dry 不刪


def test_prune_cache_deletes_over_window(tmp_path: Path) -> None:
    settings = _write_prune_fixture(tmp_path)
    twse = tmp_path / "cache" / "twse"
    result = CliRunner().invoke(app, ["data", "prune-cache", "--settings", str(settings)])
    assert result.exit_code == 0
    assert not (twse / "institutional_20240101.parquet").exists()  # 超窗 → 刪
    assert (twse / "institutional_20260625.parquet").exists()      # 窗內 → 留
