"""FastAPI wiring.

Auth model (good enough for local verification and unambiguous over HTTP):

* ``X-Principal-User`` names the caller; absence means anonymous -> 401;
* ``X-Principal-Roles`` is a comma-separated role list (admin / steward).

Every business error is rendered as::

    {"error": {"code": <STABLE_CODE>, "message": ..., "details": {...}}}

so "not found" (404), "unauthenticated" (401) and "forbidden" (403) are always
distinguishable.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import catalog, governance, lineage
from app.db import get_conn, write_tx
from app.errors import CatalogError
from app.models import (
    GlossaryTermRequest,
    Principal,
    RegisterLineageRequest,
    RegisterTableRequest,
    TagRequest,
    TransferOwnerRequest,
)


def create_app() -> FastAPI:
    app = FastAPI(title="metadata-catalog-service", version="0.1.0")

    @app.exception_handler(CatalogError)
    async def _catalog_error(_: Request, exc: CatalogError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message,
                               "details": exc.details}},
        )

    def principal(user: str | None, roles: str | None) -> Principal:
        return Principal(
            user=(user or "anonymous").strip() or "anonymous",
            roles=[r.strip() for r in (roles or "").split(",") if r.strip()],
        )

    # ------------------------------------------------------------- catalog ----
    @app.post("/api/v1/tables")
    def register(req: RegisterTableRequest,
                 x_principal_user: str | None = Header(default=None),
                 x_principal_roles: str | None = Header(default=None)):
        actor = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            version, deduped = catalog.register_table(conn, req, actor=actor.user)
        return {"version": version.model_dump(), "deduped": deduped}

    @app.get("/api/v1/tables/{name}")
    def get_table(name: str):
        with write_tx() as conn:
            return catalog.get_table(conn, name).model_dump()

    @app.get("/api/v1/tables/{name}/versions")
    def versions(name: str):
        with write_tx() as conn:
            return {"name": name,
                    "versions": [v.model_dump() for v in catalog.list_versions(conn, name)]}

    @app.get("/api/v1/tables/{name}/versions/{version}")
    def one_version(name: str, version: int):
        with write_tx() as conn:
            return catalog.get_version(conn, name, version).model_dump()

    # ------------------------------------------------------------- lineage ----
    @app.post("/api/v1/lineage/edges")
    def add_edge(req: RegisterLineageRequest,
                 x_principal_user: str | None = Header(default=None)):
        actor = principal(x_principal_user, None)
        with write_tx() as conn:
            edge, deduped = lineage.register_edge(conn, req, actor=actor.user)
        return {"edge": edge.model_dump(), "deduped": deduped}

    @app.get("/api/v1/lineage/downstream/{table}")
    def impact(table: str, field: str | None = None,
               as_of_seq: int | None = None, include_stale: bool = False):
        with write_tx() as conn:
            return lineage.downstream_impact(
                conn, table, field=field, as_of_seq=as_of_seq,
                include_stale=include_stale).model_dump()

    @app.get("/api/v1/lineage/replay/{seq}")
    def replay(seq: int):
        with write_tx() as conn:
            return lineage.replay_graph(conn, seq)

    # ---------------------------------------------------------- governance ----
    @app.post("/api/v1/tables/{name}/owner")
    def transfer(name: str, req: TransferOwnerRequest,
                 x_principal_user: str | None = Header(default=None),
                 x_principal_roles: str | None = Header(default=None)):
        p = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            return governance.transfer_owner(conn, name, req, p).model_dump()

    @app.post("/api/v1/tables/{name}/grants")
    def grant(name: str, body: dict,
              x_principal_user: str | None = Header(default=None),
              x_principal_roles: str | None = Header(default=None)):
        p = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            return governance.add_grant(conn, name, body.get("grantee", ""), p)

    @app.put("/api/v1/tables/{name}/tags")
    def put_tag(name: str, req: TagRequest,
                x_principal_user: str | None = Header(default=None),
                x_principal_roles: str | None = Header(default=None)):
        p = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            return governance.add_tag(conn, name, req, p).model_dump()

    @app.delete("/api/v1/tables/{name}/tags/{key}")
    def delete_tag(name: str, key: str,
                   x_principal_user: str | None = Header(default=None),
                   x_principal_roles: str | None = Header(default=None)):
        p = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            return governance.remove_tag(conn, name, key, p).model_dump()

    @app.get("/api/v1/tables/{name}/tags")
    def get_tags(name: str):
        with write_tx() as conn:
            return governance.list_tags(conn, name).model_dump()

    @app.post("/api/v1/glossary/terms")
    def publish_term(req: GlossaryTermRequest,
                     x_principal_user: str | None = Header(default=None),
                     x_principal_roles: str | None = Header(default=None)):
        p = principal(x_principal_user, x_principal_roles)
        with write_tx() as conn:
            return governance.publish_term(conn, req, p).model_dump()

    @app.get("/api/v1/glossary/terms/{term}")
    def get_term(term: str):
        with write_tx() as conn:
            return governance.get_term(conn, term).model_dump()

    # --------------------------------------------------------------- health ---
    @app.get("/health")
    def health():
        conn = get_conn()
        seq = conn.execute("SELECT COALESCE(MAX(seq),0) s FROM events").fetchone()["s"]
        return {"status": "ok", "event_seq": seq}

    return app
