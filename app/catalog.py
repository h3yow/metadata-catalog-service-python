"""Table registration and schema evolution.

Evolution rules (all rejected changesets raise :class:`CatalogError` with a
distinct stable code — nothing is ever silently treated as an overwrite):

* a field present in version N but absent from N+1 MUST be declared either in
  ``dropped_fields`` or as a source of ``renamed_fields`` -> ILLEGAL_DROP;
* a rename target MUST be provided and MUST NOT collide with another field ->
  ILLEGAL_RENAME;
* type changes are only allowed along widening chains -> INCOMPATIBLE_TYPE;
* nullable=True cannot be narrowed to nullable=False -> INCOMPATIBLE_TYPE;
* genuinely new fields MUST declare ``default_semantic`` (and
  ``default_value`` when it is ``explicit_value``) -> MISSING_DEFAULT_SEMANTIC /
  MISSING_REQUIRED_INFO.

Re-submitting byte-identical content for the current version is idempotent:
the existing version is returned with ``deduped=True`` and no new revision is
created.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from app.errors import CatalogError, ErrorCode
from app.models import FieldSpec, FieldVersionOut, RegisterTableRequest, TableOut, TableVersionOut

# Widening chains: moving to the right is allowed; anything else is rejected.
WIDENING_CHAINS: list[list[str]] = [
    ["int32", "int64", "float64"],
    ["int32", "float64"],
    ["float32", "float64"],
    ["date", "timestamp"],
    ["string"],
    ["boolean"],
    ["bytes"],
    ["json"],
]
KNOWN_TYPES = {t for chain in WIDENING_CHAINS for t in chain}
VALID_DEFAULT_SEMANTICS = {
    "null_default", "zero_value", "empty_string", "false_value",
    "current_timestamp", "explicit_value",
}
VALID_DROP_REASONS = {"dropped", "renamed", "type_changed", "nullable_tightened"}


def _err(code: str, status: int, message: str, **details) -> CatalogError:
    return CatalogError(code, status, message, details)


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]


def _field_out(r: sqlite3.Row) -> FieldVersionOut:
    return FieldVersionOut(
        name=r["name"], data_type=r["data_type"], nullable=bool(r["nullable"]),
        default_semantic=r["default_semantic"], default_value=r["default_value"],
        description=r["description"], added_in_version=r["added_in_version"],
        dropped_in_version=r["ended_in_version"],
    )


def _fields_at(conn: sqlite3.Connection, table: str, version: int) -> list[sqlite3.Row]:
    """Field rows that describe exactly ``version`` (added<=v<ended)."""
    return conn.execute(
        """SELECT * FROM fields
           WHERE table_name=? AND added_in_version<=?
             AND (ended_in_version IS NULL OR ended_in_version>?)
           ORDER BY id""", (table, version, version),
    ).fetchall()


def _live_fields(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fields WHERE table_name=? AND ended_in_version IS NULL ORDER BY id",
        (table,),
    ).fetchall()


def get_table_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tables WHERE name=?", (name,)).fetchone()
    if row is None:
        raise _err(ErrorCode.TABLE_NOT_FOUND, 404, f"table '{name}' does not exist", table=name)
    return row


def _type_compatible(old: str, new: str) -> bool:
    if old == new:
        return True
    for chain in WIDENING_CHAINS:
        if old in chain and new in chain and chain.index(new) > chain.index(old):
            return True
    return False


def _payload_signature(req: RegisterTableRequest) -> dict:
    return {
        "name": req.name,
        "owner": req.owner,
        "description": req.description,
        "fields": [f.model_dump() for f in req.fields],
        "renamed_fields": req.renamed_fields,
        "dropped_fields": req.dropped_fields,
    }


def _validate_basic(req: RegisterTableRequest) -> None:
    if not req.name or not req.name.strip():
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422, "table name is required", field="name")
    names = [f.name for f in req.fields]
    if len(names) != len(set(names)):
        dups = sorted({n for n in names if names.count(n) > 1})
        raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422,
                   "duplicate field names in payload", fields=dups)
    for f in req.fields:
        if not f.name or not f.name.strip():
            raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422, "field name is required")
        if f.data_type not in KNOWN_TYPES:
            raise _err(ErrorCode.INVALID_REQUEST, 400,
                       f"unknown data_type '{f.data_type}'",
                       field=f.name, known_types=sorted(KNOWN_TYPES))
        if f.default_semantic is not None and f.default_semantic not in VALID_DEFAULT_SEMANTICS:
            raise _err(ErrorCode.INVALID_REQUEST, 400,
                       f"unknown default_semantic '{f.default_semantic}'", field=f.name)
    if len(set(req.dropped_fields)) != len(req.dropped_fields):
        raise _err(ErrorCode.INVALID_REQUEST, 400, "dropped_fields contains duplicates",
                   fields=req.dropped_fields)


def register_table(conn: sqlite3.Connection, req: RegisterTableRequest,
                   actor: str = "anonymous") -> tuple[TableVersionOut, bool]:
    """Register a table (v1) or a new version. Returns ``(version_out, deduped)``."""
    _validate_basic(req)
    row = conn.execute("SELECT * FROM tables WHERE name=?", (req.name,)).fetchone()

    # ----------------------------------------------------------- first version
    if row is None:
        if not req.owner.strip():
            raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422,
                       "owner is required when registering a new table", field="owner")
        if req.renamed_fields or req.dropped_fields:
            raise _err(ErrorCode.ILLEGAL_RENAME, 422,
                       "renames/drops are not allowed on the first version")
        now = _now(conn)
        cur = conn.execute(
            "INSERT INTO events(kind, payload) VALUES ('TABLE_REGISTERED',?)",
            (json.dumps({**_payload_signature(req), "actor": actor}),),
        )
        seq = cur.lastrowid
        conn.execute(
            """INSERT INTO tables(name,current_version,revision,owner,description,
                                  created_seq,created_at,updated_at)
               VALUES (?,1,1,?,?,?,?,?)""",
            (req.name, req.owner, req.description, seq, now, now),
        )
        conn.execute(
            """INSERT INTO table_versions(table_name,version,owner,description,
                                          change_summary,created_seq,created_at)
               VALUES (?,1,?,?,?,?,?)""",
            (req.name, req.owner, req.description,
             json.dumps({"kind": "create", "added": [f.name for f in req.fields]}), seq, now),
        )
        for f in req.fields:
            conn.execute(
                """INSERT INTO fields(table_name,fid,name,data_type,nullable,
                        default_semantic,default_value,description,
                        added_in_version,created_seq)
                   VALUES (?,?,?,?,?,?,?,?,1,?)""",
                (req.name, uuid.uuid4().hex, f.name, f.data_type, int(f.nullable),
                 f.default_semantic, f.default_value, f.description, seq),
            )
        return get_version(conn, req.name, 1), False

    # ------------------------------------------------------ existing table
    if req.expected_revision is not None and req.expected_revision != row["revision"]:
        raise _err(ErrorCode.REVISION_MISMATCH, 409,
                   f"expected revision {req.expected_revision}, current is {row['revision']}",
                   current_revision=row["revision"], expected_revision=req.expected_revision)

    submitted = {f.name: f for f in req.fields}
    declared_sources = set(req.renamed_fields) | set(req.dropped_fields)
    live = _live_fields(conn, req.name)
    live_by_name = {f["name"]: f for f in live}
    live_names = set(live_by_name)

    # idempotent replay: exactly the same content as the current version?
    if (set(submitted) == live_names and not req.renamed_fields and not req.dropped_fields
            and _same_shapes([live_by_name[n] for n in live_names],
                             [submitted[n] for n in live_names])
            and req.owner == row["owner"] and req.description == row["description"]):
        return get_version(conn, req.name, row["current_version"]), True

    if not req.fields and not req.renamed_fields and not req.dropped_fields:
        raise _err(ErrorCode.EMPTY_CHANGESET, 422,
                   "changeset changes nothing: no fields, renames or drops")

    rename_targets = list(req.renamed_fields.values())
    if len(set(rename_targets)) != len(rename_targets):
        raise _err(ErrorCode.ILLEGAL_RENAME, 422,
                   "two fields renamed to the same name", targets=rename_targets)
    for src, dst in req.renamed_fields.items():
        if src not in live_names:
            raise _err(ErrorCode.FIELD_NOT_FOUND, 404,
                       f"rename source field '{src}' does not exist on current version",
                       field=src)
        if not dst or not dst.strip():
            raise _err(ErrorCode.ILLEGAL_RENAME, 422,
                       f"rename of '{src}' has no target name", field=src)
        if dst in live_names and dst != src:
            raise _err(ErrorCode.ILLEGAL_RENAME, 422,
                       f"rename target '{dst}' already exists", field=dst)

    for d in req.dropped_fields:
        if d not in live_names:
            raise _err(ErrorCode.ILLEGAL_DROP, 422,
                       f"dropped field '{d}' does not exist on current version", field=d)

    overlap = declared_sources & set(submitted)
    if overlap:
        raise _err(ErrorCode.ILLEGAL_RENAME, 422,
                   "field cannot be renamed/dropped and also submitted as a normal field",
                   fields=sorted(overlap))

    # Any live field neither submitted nor declared as renamed/dropped would be
    # silently deleted — that is exactly what must not happen.
    unhandled = live_names - set(submitted) - declared_sources
    if unhandled:
        raise _err(ErrorCode.ILLEGAL_DROP, 422,
                   "fields missing from the new version must be declared explicitly "
                   "via renamed_fields or dropped_fields",
                   fields=sorted(unhandled))

    # Classify every submitted field and enforce per-field evolution rules.
    carried: list[tuple[sqlite3.Row, FieldSpec]] = []
    added: list[FieldSpec] = []
    renamed: list[tuple[sqlite3.Row, FieldSpec]] = []
    for name, f in submitted.items():
        if name in req.renamed_fields.values():
            src_name = next(s for s, d in req.renamed_fields.items() if d == name)
            old = live_by_name[src_name]
            renamed.append((old, f))
            if not _type_compatible(old["data_type"], f.data_type):
                raise _err(ErrorCode.INCOMPATIBLE_TYPE, 422,
                           f"renamed field '{src_name}'->'{name}' changes type "
                           f"{old['data_type']} -> {f.data_type}",
                           field=name, old_type=old["data_type"], new_type=f.data_type)
            if old["nullable"] and not f.nullable:
                raise _err(ErrorCode.INCOMPATIBLE_TYPE, 422,
                           f"renamed field '{src_name}'->'{name}' tightens nullable to false",
                           field=name)
        elif name in live_by_name:
            old = live_by_name[name]
            carried.append((old, f))
            if not _type_compatible(old["data_type"], f.data_type):
                raise _err(ErrorCode.INCOMPATIBLE_TYPE, 422,
                           f"field '{name}' has incompatible type change "
                           f"{old['data_type']} -> {f.data_type}",
                           field=name, old_type=old["data_type"], new_type=f.data_type)
            if old["nullable"] and not f.nullable:
                raise _err(ErrorCode.INCOMPATIBLE_TYPE, 422,
                           f"field '{name}' cannot be narrowed from nullable to not-null",
                           field=name)
        else:
            added.append(f)

    for f in added:
        if f.default_semantic is None:
            raise _err(ErrorCode.MISSING_DEFAULT_SEMANTIC, 422,
                       f"new field '{f.name}' must declare default_semantic",
                       field=f.name,
                       allowed=sorted(VALID_DEFAULT_SEMANTICS))
        if f.default_semantic == "explicit_value" and (
                f.default_value is None or f.default_value == ""):
            raise _err(ErrorCode.MISSING_REQUIRED_INFO, 422,
                       f"new field '{f.name}' uses explicit_value but default_value is empty",
                       field=f.name)

    # ------------------------------------------------------------ apply change
    new_version = row["current_version"] + 1
    new_revision = row["revision"] + 1
    now = _now(conn)
    cur = conn.execute(
        "INSERT INTO events(kind, payload) VALUES ('TABLE_EVOLVED',?)",
        (json.dumps({**_payload_signature(req), "actor": actor,
                     "base_revision": row["revision"]}),),
    )
    seq = cur.lastrowid

    summary = {"kind": "evolve", "added": [], "renamed": [], "dropped": [],
               "type_widened": [], "metadata_changed": False}

    def _close(old: sqlite3.Row, reason: str) -> None:
        conn.execute(
            "UPDATE fields SET ended_in_version=?, ended_reason=?, ended_seq=? WHERE id=?",
            (new_version, reason, seq, old["id"]),
        )

    def _open(old: sqlite3.Row | None, f: FieldSpec, fid: str) -> None:
        conn.execute(
            """INSERT INTO fields(table_name,fid,name,data_type,nullable,
                    default_semantic,default_value,description,
                    added_in_version,created_seq)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (req.name, fid, f.name, f.data_type, int(f.nullable),
             f.default_semantic, f.default_value, f.description, new_version, seq),
        )

    for old, f in renamed:
        _close(old, "renamed")
        _open(old, f, old["fid"])  # stable fid follows the rename
        summary["renamed"].append({"from": old["name"], "to": f.name})

    for d in req.dropped_fields:
        _close(live_by_name[d], "dropped")
        summary["dropped"].append(d)

    for old, f in carried:
        changed = (old["data_type"] != f.data_type or bool(old["nullable"]) != f.nullable
                   or old["description"] != f.description
                   or old["default_semantic"] != f.default_semantic
                   or old["default_value"] != f.default_value)
        if changed:
            reason = "type_changed" if old["data_type"] != f.data_type else "metadata_changed"
            _close(old, reason)
            _open(old, f, old["fid"])
            if old["data_type"] != f.data_type:
                summary["type_widened"].append(
                    {"field": f.name, "from": old["data_type"], "to": f.data_type})

    for f in added:
        _open(None, f, uuid.uuid4().hex)
        summary["added"].append(f.name)

    summary["metadata_changed"] = (
        req.owner != row["owner"] or req.description != row["description"])

    conn.execute(
        """INSERT INTO table_versions(table_name,version,owner,description,
                                      change_summary,created_seq,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (req.name, new_version, req.owner, req.description,
         json.dumps(summary), seq, now),
    )
    conn.execute(
        "UPDATE tables SET current_version=?, revision=?, owner=?, description=?, "
        "updated_at=? WHERE name=?",
        (new_version, new_revision, req.owner, req.description, now, req.name),
    )
    return get_version(conn, req.name, new_version), False


def _same_shapes(old_rows: list[sqlite3.Row], new: list[FieldSpec]) -> bool:
    for r, f in zip(old_rows, new):
        if (r["name"] != f.name or r["data_type"] != f.data_type
                or bool(r["nullable"]) != f.nullable
                or r["default_semantic"] != f.default_semantic
                or r["default_value"] != f.default_value
                or r["description"] != f.description):
            return False
    return True


def _version_out(conn: sqlite3.Connection, name: str, version: int) -> TableVersionOut:
    v = conn.execute(
        "SELECT * FROM table_versions WHERE table_name=? AND version=?",
        (name, version),
    ).fetchone()
    if v is None:
        raise _err(ErrorCode.VERSION_NOT_FOUND, 404,
                   f"version {version} of table '{name}' does not exist",
                   table=name, version=version)
    # Every accepted changeset produces exactly one version and bumps revision,
    # so the revision observed at historical version N is simply N.
    return TableVersionOut(
        name=name, version=version, revision=version,
        owner=v["owner"], description=v["description"],
        fields=[_field_out(r) for r in _fields_at(conn, name, version)],
        change_summary=json.loads(v["change_summary"]), created_at=v["created_at"],
    )


def get_version(conn: sqlite3.Connection, name: str, version: int) -> TableVersionOut:
    get_table_row(conn, name)
    return _version_out(conn, name, version)


def get_table(conn: sqlite3.Connection, name: str) -> TableOut:
    row = get_table_row(conn, name)
    v = _version_out(conn, name, row["current_version"])
    return TableOut(
        name=name, current_version=row["current_version"], revision=row["revision"],
        owner=row["owner"], description=row["description"], fields=v.fields,
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def list_versions(conn: sqlite3.Connection, name: str) -> list[TableVersionOut]:
    get_table_row(conn, name)
    nums = [r["version"] for r in conn.execute(
        "SELECT version FROM table_versions WHERE table_name=? ORDER BY version", (name,))]
    return [_version_out(conn, name, n) for n in nums]
