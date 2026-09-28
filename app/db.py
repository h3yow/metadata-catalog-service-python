"""SQLite storage.

A single process-wide connection (WAL mode) is guarded by a re-entrant lock.
Write services take the lock and run inside ``BEGIN IMMEDIATE`` transactions,
which makes concurrent registrations serialisable: duplicate submissions collapse
onto the same UNIQUE row and lost/half-written updates cannot occur.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator

DEFAULT_DB_PATH = os.environ.get("METADATA_DB_PATH", "data/catalog.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path: str = DEFAULT_DB_PATH
_tx_depth = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- One row per registered table. revision = number of accepted changesets.
CREATE TABLE IF NOT EXISTS tables (
    name            TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    revision        INTEGER NOT NULL,
    owner           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    created_seq     INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- One row per submitted (and accepted) version. Replayable forever.
CREATE TABLE IF NOT EXISTS table_versions (
    table_name     TEXT NOT NULL,
    version        INTEGER NOT NULL,
    owner          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    change_summary TEXT NOT NULL DEFAULT '{}',
    created_seq    INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (table_name, version)
);

-- Append-only field states. ``fid`` is the stable identity that survives
-- renames; every change to a field (rename/type widen/drop) closes the old row
-- (ended_in_version) and opens a new one, so any historical version can be
-- reconstructed and lineage never silently points at the wrong field.
CREATE TABLE IF NOT EXISTS fields (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name         TEXT NOT NULL,
    fid                TEXT NOT NULL,
    name               TEXT NOT NULL,
    data_type          TEXT NOT NULL,
    nullable           INTEGER NOT NULL,
    default_semantic   TEXT,
    default_value      TEXT,
    description        TEXT NOT NULL DEFAULT '',
    added_in_version   INTEGER NOT NULL,
    ended_in_version   INTEGER,
    ended_reason       TEXT,
    created_seq        INTEGER NOT NULL,
    ended_seq          INTEGER,
    UNIQUE (table_name, fid, added_in_version)
);
CREATE INDEX IF NOT EXISTS idx_fields_table ON fields(table_name, added_in_version);
CREATE INDEX IF NOT EXISTS idx_fields_live ON fields(table_name) WHERE ended_in_version IS NULL;

-- Lineage edges. '' in a field column means "table level". Snapshot columns
-- (name/version) freeze the conclusion at registration time.
CREATE TABLE IF NOT EXISTS edges (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    upstream_table     TEXT NOT NULL,
    upstream_field     TEXT NOT NULL DEFAULT '',
    downstream_table   TEXT NOT NULL,
    downstream_field   TEXT NOT NULL DEFAULT '',
    up_fid            TEXT NOT NULL DEFAULT '',
    down_fid          TEXT NOT NULL DEFAULT '',
    upstream_version   INTEGER NOT NULL,
    downstream_version INTEGER NOT NULL,
    transformation     TEXT NOT NULL DEFAULT '',
    is_cyclic          INTEGER NOT NULL DEFAULT 0,
    registered_seq     INTEGER NOT NULL,
    UNIQUE (upstream_table, upstream_field, downstream_table, downstream_field)
);
CREATE INDEX IF NOT EXISTS idx_edges_down ON edges(downstream_table, downstream_field);
CREATE INDEX IF NOT EXISTS idx_edges_up   ON edges(upstream_table, upstream_field);
CREATE INDEX IF NOT EXISTS idx_edges_seq  ON edges(registered_seq);

CREATE TABLE IF NOT EXISTS tags (
    table_name TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, key)
);

CREATE TABLE IF NOT EXISTS grants (
    table_name TEXT NOT NULL,
    grantee    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'steward',
    PRIMARY KEY (table_name, grantee)
);

CREATE TABLE IF NOT EXISTS glossary (
    term           TEXT PRIMARY KEY,
    definition     TEXT NOT NULL DEFAULT '',
    owner          TEXT NOT NULL,
    linked_tables  TEXT NOT NULL DEFAULT '[]',
    linked_fields  TEXT NOT NULL DEFAULT '[]',
    version        INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Open (or create) the database file and initialise the schema."""
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _db_path = db_path
        _conn = _connect(db_path)


def get_conn() -> sqlite3.Connection:
    """Return the process-wide connection, opening the default DB on first use."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                init_db(_db_path)
    assert _conn is not None
    return _conn


def reset(db_path: str = ":memory:") -> None:
    """Test helper: discard current state and recreate the schema."""
    init_db(db_path)


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Serialise writers.

    The outermost caller opens ``BEGIN IMMEDIATE``; nested callers (services
    composing other services inside the same thread) get a SAVEPOINT, so an
    inner failure rolls back only its own work and the transaction boundary —
    hence atomicity — is always respected.
    """
    global _tx_depth
    conn = get_conn()
    with _lock:
        depth = _tx_depth
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
            _tx_depth = 1
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                _tx_depth = 0
                raise
            else:
                conn.execute("COMMIT")
                _tx_depth = 0
        else:
            sp = f"sp_{depth}"
            conn.execute(f"SAVEPOINT {sp}")
            _tx_depth = depth + 1
            try:
                yield conn
            except Exception:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                conn.execute(f"RELEASE SAVEPOINT {sp}")
                _tx_depth = depth
                raise
            else:
                conn.execute(f"RELEASE SAVEPOINT {sp}")
                _tx_depth = depth
