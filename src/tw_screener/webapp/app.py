"""FastAPI app：dev 開 CORS 給 Vite；prod 掛載 frontend/dist 靜態檔。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from tw_screener.webapp.routers import narrative, tables, weeks

# repo 根的 frontend/dist（prod build 產物）；存在才掛載
_DIST_DIR = Path("frontend/dist")
# dev：Vite 開在 5173，允許跨埠呼叫 :8000
_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app() -> FastAPI:
    app = FastAPI(
        title="投資戰情室 Dashboard",
        description="只讀 reports/ 的本機自用儀表板（docs/17）",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # API 路由（必須在靜態掛載之前註冊，/api/* 才會優先匹配）
    app.include_router(weeks.router)
    app.include_router(tables.router)
    app.include_router(narrative.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # prod：把 build 後前端掛在 "/"（html=True → 進站給 index.html）
    if _DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="frontend")

    return app


app = create_app()
