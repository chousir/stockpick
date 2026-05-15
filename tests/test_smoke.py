"""Smoke tests — 確認基本 import 和 CLI 可運作。"""

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
