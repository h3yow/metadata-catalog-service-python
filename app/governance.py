"""Ownership, authorization, tags and the glossary.

Authorization is always evaluated *after* existence, so callers can tell the
two failure modes apart:

* resource missing  -> 404 TABLE_NOT_FOUND / GLOSSARY_TERM_NOT_FOUND
* no identity       -> 401 UNAUTHENTICATED
* exists, not allowed -> 403 FORBIDDEN

Concurrency: every mutation runs inside one serialised ``BEGIN IMMEDIATE``
transaction. Ownership transfer additionally carries an ``expected_revision``
(optimistic lock) -> 409 REVISION_MISMATCH on stale writes, so concurrent
transfers can never silently overwrite each other. Tag writes are keyed upserts
and therefore commute into one consistent final state.
"""
from __future__ import annotations

import json
import sqlite3

from app.errors import CatalogError, ErrorCode
from app.models import (
    GlossaryTermOut,
    GlossaryTermRequest,
    OwnerOut,
    Principal,
    TagsOut,
    TagRequest,
    TransferOwnerRequest,
)

ADMIN_ROLE = "admin"
STEWARD_ROLE = "steward"


def _err(code: str, status: int, message: str, **details) -> CatalogError:
    return CatalogError(code, status, message, details)


def _table_row(conn: sqlite3.Connection, table: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tables WHERE name=?", (table,)).fetchone()
    if row is None:
        raise _err(ErrorCode.TABLE_NOT_FOUND, 404,
                   f"table '{table}' does not exist", table=table)
    return row


def _require_auth(principal: Principal) -> None:
    if not principal.user or principal.user == "anonymous":
        raise _err(ErrorCode.UNAUTHENTICATED, 401, "authentication is required")


def _is_privileged(principal: Principal) -> bool:
    return ADMIN_ROLE in principal.roles or STEWARD_ROLE in principal.roles


def _has_grant(conn: sqlite3.Connection, user: str, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM grants WHERE grantee=? AND table_name=?", (user, table)
    ).fetchone() is not None

def require_table_owner(conn: sqlite3.Connection, table: str,
                        principal: Principal, action: str = "modify") -> sqlite3.Row:
    """404 if missing, 401 if anonymous, 403 if neither owner/grantee/admin."""
    _require_auth(principal)
    row = _table_row(conn, table)  # existence first -> 404 is distinguishable
    if ADMIN_ROLE in principal.roles:
        return row
    if principal.user == row["owner"]:
        return row
    if _has_grant(conn, principal.user, table):
        return row
    raise _err(ErrorCode.FORBIDDEN, 403,
               f"user '{principal.user}' is not allowed to {action} table '{table}'",
               table=table, user=principal.user, owner=row["owner"], action=action)


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]


def _event(conn: sqlite3.Connection, kind: str, payload: dict) -> int:
    cur = conn.execute("INSERT INTO events(kind, payload) VALUES (?,?)",
                       (kind, json.dumps(payload)))
    return cur.lastrowid


def transfer_owner(conn: sqlite3.Connection, table: str,
                   req: TransferOwnerRequest, principal: Principal) -> OwnerOut:
    row = require_table_owner(conn, table, principal, action="transfer ownership of")
    if not req.new_owner or not req.new_owner.strip():
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422,
                   "new_owner is required", field="new_owner")
    if req.expected_revision != row["revision"]:
        raise _err(ErrorCode.REVISION_MISMATCH, 409,
                   f"expected revision {req.expected_revision}, current is {row['revision']}",
                   current_revision=row["revision"],
                   expected_revision=req.expected_revision)
    new_revision = row["revision"] + 1
    now = _now(conn)
    _event(conn, "OWNER_TRANSFERRED",
           {"table": table, "from": row["owner"], "to": req.new_owner,
            "base_revision": row["revision"], "actor": principal.user})
    conn.execute("UPDATE tables SET owner=?, revision=?, updated_at=? WHERE name=?",
                 (req.new_owner, new_revision, now, table))
    return OwnerOut(table=table, owner=req.new_owner, revision=new_revision)


def add_grant(conn: sqlite3.Connection, table: str, grantee: str,
              principal: Principal) -> dict:
    """Owner/admin delegates stewardship on one table."""
    row = require_table_owner(conn, table, principal, action="grant access to")
    if not grantee or not grantee.strip():
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422, "grantee is required")
    conn.execute(
        "INSERT INTO grants(table_name, grantee, role) VALUES (?,?,?) "
        "ON CONFLICT(table_name, grantee) DO UPDATE SET role=excluded.role",
        (table, grantee, STEWARD_ROLE))
    _event(conn, "GRANT_ADDED",
           {"table": table, "grantee": grantee, "actor": principal.user})
    return {"table": table, "grantee": grantee, "role": STEWARD_ROLE,
            "revision": row["revision"]}


def _tags(conn: sqlite3.Connection, table: str, revision: int) -> TagsOut:
    tags = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM tags WHERE table_name=? ORDER BY key", (table,))}
    return TagsOut(table=table, revision=revision, tags=tags)


def add_tag(conn: sqlite3.Connection, table: str, req: TagRequest,
            principal: Principal) -> TagsOut:
    row = require_table_owner(conn, table, principal, action="tag")
    if not req.key or not req.key.strip():
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422, "tag key is required")
    conn.execute(
        "INSERT INTO tags(table_name,key,value) VALUES (?,?,?) "
        "ON CONFLICT(table_name,key) DO UPDATE SET value=excluded.value",
        (table, req.key, req.value))
    _event(conn, "TAG_ADDED",
           {"table": table, "key": req.key, "value": req.value, "actor": principal.user})
    return _tags(conn, table, row["revision"])


def remove_tag(conn: sqlite3.Connection, table: str, key: str,
               principal: Principal) -> TagsOut:
    row = require_table_owner(conn, table, principal, action="untag")
    conn.execute("DELETE FROM tags WHERE table_name=? AND key=?", (table, key))
    _event(conn, "TAG_REMOVED", {"table": table, "key": key, "actor": principal.user})
    return _tags(conn, table, row["revision"])


def list_tags(conn: sqlite3.Connection, table: str) -> TagsOut:
    row = _table_row(conn, table)
    return _tags(conn, table, row["revision"])


def _linked_field_row(conn: sqlite3.Connection, table: str, field: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM fields WHERE table_name=? AND name=? AND ended_in_version IS NULL",
        (table, field)).fetchone()
    if row is None:
        raise _err(ErrorCode.FIELD_NOT_FOUND, 404,
                   f"linked field '{table}.{field}' does not exist on the current version",
                   table=table, field=field)


def publish_term(conn: sqlite3.Connection, req: GlossaryTermRequest,
                 principal: Principal) -> GlossaryTermOut:
    """Publish or update a glossary term. Requires an authorised role."""
    _require_auth(principal)
    if not _is_privileged(principal):
        raise _err(ErrorCode.FORBIDDEN, 403,
                   f"user '{principal.user}' lacks an authorised role "
                   f"(needs '{ADMIN_ROLE}' or '{STEWARD_ROLE}') to publish terms",
                   user=principal.user)
    if not req.term or not req.term.strip():
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422, "term is required")
    for t in req.linked_tables:
        _table_row(conn, t)
    for link in req.linked_fields:
        _table_row(conn, link["table"])
        _linked_field_row(conn, link["table"], link["field"])

    now = _now(conn)
    existing = conn.execute("SELECT * FROM glossary WHERE term=?", (req.term,)).fetchone()
    version = (existing["version"] + 1) if existing else 1
    owner = principal.user
    linked_tables = json.dumps(req.linked_tables)
    linked_fields = json.dumps(req.linked_fields)
    if existing:
        conn.execute(
            """UPDATE glossary SET definition=?, owner=?, linked_tables=?,
                   linked_fields=?, version=?, updated_at=? WHERE term=?""",
            (req.definition, owner, linked_tables, linked_fields, version, now, req.term))
    else:
        conn.execute(
            """INSERT INTO glossary(term,definition,owner,linked_tables,linked_fields,
                                    version,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (req.term, req.definition, owner, linked_tables, linked_fields,
             version, now, now))
    _event(conn, "TERM_PUBLISHED",
           {"term": req.term, "version": version, "actor": principal.user,
            "linked_tables": req.linked_tables, "linked_fields": req.linked_fields})
    return GlossaryTermOut(
        term=req.term, definition=req.definition, owner=owner,
        linked_tables=req.linked_tables, linked_fields=req.linked_fields, version=version)


def get_term(conn: sqlite3.Connection, term: str) -> GlossaryTermOut:
    row = conn.execute("SELECT * FROM glossary WHERE term=?", (term,)).fetchone()
    if row is None:
        raise _err(ErrorCode.GLOSSARY_TERM_NOT_FOUND, 404,
                   f"glossary term '{term}' does not exist", term=term)
    return GlossaryTermOut(
        term=row["term"], definition=row["definition"], owner=row["owner"],
        linked_tables=json.loads(row["linked_tables"]),
        linked_fields=json.loads(row["linked_fields"]), version=row["version"])
