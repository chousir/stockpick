"""CLI 入口：tw-screener 命令。"""

import typer
from rich.console import Console

from tw_screener import __version__

app = typer.Typer(help="台股波段選股與分析工具", no_args_is_help=True)
console = Console()


@app.command()
def hello() -> None:
    """確認安裝正常。"""
    console.print("Hello from tw-stock-screener")


@app.command()
def version() -> None:
    """顯示版本。"""
    console.print(f"tw-stock-screener {__version__}")


if __name__ == "__main__":
    app()
