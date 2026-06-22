"""uvicorn 進入點（CLI: tw-screener serve）。

預設只綁 127.0.0.1（本機自用、無驗證、含個人持股，不對外；docs/17 §8）。
"""

from __future__ import annotations

import uvicorn


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    uvicorn.run(
        "tw_screener.webapp.app:app",
        host=host,
        port=port,
        reload=reload,
    )
