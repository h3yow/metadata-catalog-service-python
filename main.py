"""本地元数据目录服务入口。

环境变量：
  CATALOG_DB   持久化 JSON 文件路径（默认 ./catalog_data.json）
  CATALOG_MEM  设为 "1" 时使用纯内存存储（测试用）
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from app.routers import build_router, install_exception_handlers
from app.service import CatalogService
from app.storage import CatalogStore


def create_app() -> FastAPI:
    path = None if os.environ.get("CATALOG_MEM") == "1" else os.environ.get(
        "CATALOG_DB", "catalog_data.json"
    )
    store = CatalogStore(path)
    store.load()
    service = CatalogService(store)
    fastapi = FastAPI(title="metadata-catalog-service")
    install_exception_handlers(fastapi)
    fastapi.include_router(build_router(service))
    return fastapi


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
