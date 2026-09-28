"""HTTP 路由：把登记协议翻译成服务层调用，并统一错误响应格式。

身份通过请求头传递：X-User、X-Roles（逗号分隔）。
错误统一为 {"error": {"code", "message", "details"}}，HTTP 状态码与错误码对应，
因此 404(NOT_FOUND) 与 403(FORBIDDEN)、409(VERSION_CONFLICT/DUPLICATE_ENTRY/CYCLE...)
可以明确区分。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from .errors import CatalogError
from .models import (
    BatchItem,
    LineageSpec,
    OwnerTransfer,
    TableDef,
    TableVersionSpec,
    TagUpdate,
    TermSpec,
)
from .permissions import get_actor
from .service import CatalogService


def build_router(service: CatalogService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health():
        return {"status": "ok"}

    actor_dep = Depends(get_actor)

    # ---------- 表登记与结构演进 ----------

    @router.post("/tables", status_code=201)
    def register_table(table: TableDef, actor=actor_dep):
        return service.register_table(actor, table)

    @router.post("/tables/{table_id}/versions", status_code=201)
    def submit_version(table_id: str, spec: TableVersionSpec, actor=actor_dep):
        return service.submit_version(actor, table_id, spec)

    @router.get("/tables/{table_id}")
    def get_table(table_id: str, actor=actor_dep):
        return service.get_table(actor, table_id)

    @router.get("/tables/{table_id}/versions/{version}")
    def get_version(table_id: str, version: int, actor=actor_dep):
        return service.get_version(actor, table_id, version)

    @router.get("/tables/{table_id}/replay")
    def replay(table_id: str, actor=actor_dep):
        return service.replay(actor, table_id)

    # ---------- 血缘与影响分析 ----------

    @router.post("/lineage", status_code=201)
    def add_lineage(spec: LineageSpec, actor=actor_dep):
        return service.add_lineage(actor, spec)

    @router.get("/lineage")
    def lineage_graph(actor=actor_dep):
        return service.lineage_graph(actor)

    @router.get("/tables/{table_id}/impact")
    def impact(table_id: str, actor=actor_dep):
        return service.impact(actor, table_id)

    # ---------- 标签 / 归属 / 术语 ----------

    @router.post("/tables/{table_id}/tags")
    def update_tags(table_id: str, update: TagUpdate, actor=actor_dep):
        return service.update_tags(actor, table_id, update)

    @router.post("/tables/{table_id}/owner")
    def transfer_owner(table_id: str, body: OwnerTransfer, actor=actor_dep):
        return service.transfer_owner(actor, table_id, body)

    @router.post("/terms", status_code=201)
    def publish_term(spec: TermSpec, actor=actor_dep):
        return service.publish_term(actor, spec)

    @router.get("/terms")
    def list_terms(actor=actor_dep):
        return service.list_terms(actor)

    # ---------- 批量 / 幂等 ----------

    @router.post("/batch", status_code=200)
    def run_batch(
        items: list[BatchItem],
        actor=actor_dep,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ):
        return service.run_batch(actor, idempotency_key, items)

    return router


def install_exception_handlers(app) -> None:
    @app.exception_handler(CatalogError)
    async def _handle(_: Request, exc: CatalogError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
