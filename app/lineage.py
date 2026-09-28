"""Lineage edge registration, cycle marking and downstream impact analysis.

Stability rules:

* edges are deduplicated on both the frozen (table, field-name) tuple and the
  stable field identity (``fid``): duplicate submissions and process restarts
  never create duplicate or missing edges;
* every accepted edge is appended to the monotonic ``events`` log and keeps a
  frozen snapshot (names + versions at registration time), so impact reports
  can be replayed at ``as_of_seq`` and historical conclusions never change;
* traversal is keyed by ``fid`` for field edges, therefore renames preserve the
  lineage chain while the frozen names keep each conclusion explainable;
* cycles are detected *before* insertion: rejected with the exact cycle path
  (``CYCLE_DETECTED``) unless the client explicitly sets ``allow_cycle``;
* impact output ordering is deterministic (distance, then table, then field).
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque

from app.errors import CatalogError, ErrorCode
from app.models import (
    EdgeRef,
    ImpactNode,
    ImpactReport,
    LineageEdgeOut,
    RegisterLineageRequest,
)


def _err(code: str, status: int, message: str, **details) -> CatalogError:
    return CatalogError(code, status, message, details)


def _edge_out(r: sqlite3.Row) -> LineageEdgeOut:
    return LineageEdgeOut(
        upstream_table=r["upstream_table"],
        upstream_field=r["upstream_field"] or None,
        downstream_table=r["downstream_table"],
        downstream_field=r["downstream_field"] or None,
        transformation=r["transformation"],
        upstream_version=r["upstream_version"],
        downstream_version=r["downstream_version"],
        registered_seq=r["registered_seq"],
    )


def _resolve_endpoint(conn: sqlite3.Connection, ref: EdgeRef, expected_version: int | None,
                      side: str) -> tuple[int, str, str]:
    """Return (current_version, field-name-or-'', fid-or-'')."""
    t = conn.execute("SELECT current_version FROM tables WHERE name=?", (ref.table,)).fetchone()
    if t is None:
        raise _err(ErrorCode.TABLE_NOT_FOUND, 404,
                   f"{side} table '{ref.table}' does not exist", table=ref.table, side=side)
    version = t["current_version"]
    if expected_version is not None and expected_version != version:
        raise _err(ErrorCode.FIELD_VERSION_MISMATCH, 409,
                   f"{side} table '{ref.table}' expected version {expected_version} "
                   f"but current is {version}",
                   side=side, expected=expected_version, current=version)
    if not ref.field:
        return version, "", ""
    row = conn.execute(
        "SELECT fid FROM fields WHERE table_name=? AND name=? AND ended_in_version IS NULL",
        (ref.table, ref.field),
    ).fetchone()
    if row is None:
        raise _err(ErrorCode.FIELD_NOT_FOUND, 404,
                   f"{side} field '{ref.table}.{ref.field}' does not exist on the current version",
                   table=ref.table, field=ref.field, side=side)
    return version, ref.field, row["fid"]


def _node_key(r: sqlite3.Row, side: str) -> str:
    """Traversal key: table-level stays name keyed; field-level is fid keyed."""
    if side == "up":
        return f"t:{r['upstream_table']}" if not r["upstream_field"] else f"f:{r['up_fid']}"
    return f"t:{r['downstream_table']}" if not r["downstream_field"] else f"f:{r['down_fid']}"


def _all_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM edges ORDER BY registered_seq").fetchall()


def _adjacency(edges: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    adj: dict[str, list[sqlite3.Row]] = {}
    for r in edges:
        adj.setdefault(_node_key(r, "up"), []).append(r)
    for v in adj.values():
        v.sort(key=lambda r: (r["downstream_table"], r["downstream_field"], r["registered_seq"]))
    return adj


def _find_path(adj: dict[str, list[sqlite3.Row]], src: str, dst: str) -> list[str] | None:
    """Deterministic BFS path ``src`` -> ``dst``; None if unreachable."""
    if src == dst:
        return [src]
    prev: dict[str, tuple[str, sqlite3.Row] | None] = {src: None}
    q = deque([src])
    while q:
        cur = q.popleft()
        for r in adj.get(cur, []):
            nxt = _node_key(r, "down")
            if nxt in prev:
                continue
            prev[nxt] = (cur, r)
            if nxt == dst:
                path = [nxt]
                p = nxt
                while prev[p] is not None:
                    p, _ = prev[p]
                    path.append(p)
                return path[::-1]
            q.append(nxt)
    return None


def _find_cycles(edges: list[sqlite3.Row]) -> list[list[str]]:
    """Elementary cycles via smallest-vertex DFS; canonicalised for stability."""
    adj: dict[str, list[str]] = {}
    for r in edges:
        adj.setdefault(_node_key(r, "up"), []).append(_node_key(r, "down"))
    nodes = sorted(set(adj) | {v for vs in adj.values() for v in vs})
    cycles: set[tuple[str, ...]] = set()

    def dfs(start: str, node: str, stack: list[str], seen: set[str]) -> None:
        for nxt in sorted(adj.get(node, [])):
            if nxt == start:
                cycles.add(tuple(stack))
            elif nxt not in seen and nxt > start:
                seen.add(nxt)
                dfs(start, nxt, stack + [nxt], seen)
                seen.discard(nxt)

    for n in nodes:
        dfs(n, n, [n], {n})
    return [list(c) for c in sorted(cycles)]


def register_edge(conn: sqlite3.Connection, req: RegisterLineageRequest,
                  actor: str = "anonymous") -> tuple[LineageEdgeOut, bool]:
    up_v, up_fname, up_fid = _resolve_endpoint(
        conn, req.upstream, req.expected_upstream_version, "upstream")
    dn_v, dn_fname, dn_fid = _resolve_endpoint(
        conn, req.downstream, req.expected_downstream_version, "downstream")

    if (req.upstream.table == req.downstream.table
            and (req.upstream.field or "") == (req.downstream.field or "")):
        raise _err(ErrorCode.CYCLE_DETECTED, 409,
                   "an edge cannot start and end at the same node (self loop)",
                   cycle=[f"{req.upstream.table}#{up_fname}"])

    # Dedup on the frozen names AND on stable identities (rename-safe).
    existing = conn.execute(
        "SELECT * FROM edges WHERE upstream_table=? AND upstream_field=? "
        "AND downstream_table=? AND downstream_field=?",
        (req.upstream.table, up_fname, req.downstream.table, dn_fname),
    ).fetchone()
    if existing is None and (up_fid or dn_fid):
        existing = conn.execute(
            "SELECT * FROM edges WHERE up_fid=? AND down_fid=? "
            "AND upstream_table=? AND downstream_table=?",
            (up_fid, dn_fid, req.upstream.table, req.downstream.table),
        ).fetchone()
    if existing is not None:
        return _edge_out(existing), True

    edges = _all_edges(conn)
    adj = _adjacency(edges)
    src = f"t:{req.downstream.table}" if not dn_fname else f"f:{dn_fid}"
    dst = f"t:{req.upstream.table}" if not up_fname else f"f:{up_fid}"
    path = _find_path(adj, src, dst)

    is_cyclic = 0
    if path is not None:
        if not req.allow_cycle:
            states = _field_states(conn, None)
            raise _err(ErrorCode.CYCLE_DETECTED, 409,
                       "adding this edge would create a cycle; resubmit with "
                       "allow_cycle=true to record it explicitly",
                       cycle=[_label(k, states) for k in path + [src]])
        is_cyclic = 1

    cur = conn.execute(
        "INSERT INTO events(kind, payload) VALUES ('EDGE_REGISTERED',?)",
        (json.dumps({"upstream": req.upstream.model_dump(),
                     "downstream": req.downstream.model_dump(),
                     "transformation": req.transformation,
                     "upstream_version": up_v, "downstream_version": dn_v,
                     "up_fid": up_fid, "dn_fid": dn_fid,
                     "is_cyclic": bool(is_cyclic), "actor": actor}),),
    )
    seq = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO edges(upstream_table,upstream_field,downstream_table,downstream_field,
               up_fid,down_fid,upstream_version,downstream_version,transformation,
               is_cyclic,registered_seq)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (req.upstream.table, up_fname, req.downstream.table, dn_fname,
         up_fid, dn_fid, up_v, dn_v, req.transformation, is_cyclic, seq),
    )
    row = conn.execute("SELECT * FROM edges WHERE id=?", (cur.lastrowid,)).fetchone()
    return _edge_out(row), False


def _field_states(conn: sqlite3.Connection, cut: int | None) -> dict[str, dict]:
    """fid -> {table, name, active}: state visible at sequence point ``cut``."""
    rows = conn.execute("SELECT * FROM fields ORDER BY id").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        created, ended = r["created_seq"], r["ended_seq"]
        if cut is not None and created > cut:
            continue
        active = ended is None or (cut is not None and ended > cut)
        out[r["fid"]] = {"table": r["table_name"], "name": r["name"], "active": active}
    return out


def _label(key: str, states: dict[str, dict]) -> str:
    if key.startswith("t:"):
        return key[2:] + "#"
    fid = key[2:]
    st = states.get(fid)
    return f"{st['table']}#{st['name']}" if st else f"fid:{fid[:8]}"


def _canon_cycle(labels: list[str]) -> list[str]:
    """Rotate a labelled cycle so it starts at the smallest node (stable output)."""
    i = min(range(len(labels)), key=lambda k: labels[k])
    return labels[i:] + labels[:i]


def downstream_impact(conn: sqlite3.Connection, table: str, field: str | None = None,
                      as_of_seq: int | None = None, include_stale: bool = False) -> ImpactReport:
    exists = conn.execute("SELECT 1 FROM tables WHERE name=?", (table,)).fetchone()
    if exists is None:
        raise _err(ErrorCode.TABLE_NOT_FOUND, 404, f"table '{table}' does not exist", table=table)

    cut = as_of_seq
    edges = (_all_edges(conn) if cut is None else
             conn.execute("SELECT * FROM edges WHERE registered_seq<=? ORDER BY registered_seq",
                          (cut,)).fetchall())
    max_seq = max((r["registered_seq"] for r in edges), default=0)
    states = _field_states(conn, cut)
    adj = _adjacency(edges)

    # Root keys. A table query fans out to table-level edges and every field
    # identity that belongs (or belonged, in a historical query) to the table.
    roots: set[str] = set()
    if field:
        row = conn.execute(
            "SELECT fid FROM fields WHERE table_name=? AND name=? "
            "AND (ended_in_version IS NULL OR ? IS NOT NULL) ORDER BY id LIMIT 1",
            (table, field, cut),
        ).fetchone()
        if row is None:
            raise _err(ErrorCode.FIELD_NOT_FOUND, 404,
                       f"field '{table}.{field}' does not exist", table=table, field=field)
        roots.add(f"f:{row['fid']}")
    else:
        roots.add(f"t:{table}")
        for fid, st in states.items():
            if st["table"] == table:
                roots.add(f"f:{fid}")

    dist: dict[str, int] = {s: 0 for s in roots}
    edgepath: dict[str, list[sqlite3.Row]] = {s: [] for s in roots}
    q = deque(sorted(roots))
    reached: set[int] = set()
    while q:
        cur = q.popleft()
        for r in adj.get(cur, []):
            nxt = _node_key(r, "down")
            reached.add(r["id"])
            if nxt in dist:
                continue
            # For live queries, a field endpoint whose fid is no longer active
            # is a stale branch: omitted by default, explainable on request.
            if cut is None and not include_stale and nxt.startswith("f:"):
                if not states.get(nxt[2:], {}).get("active"):
                    continue
            dist[nxt] = dist[cur] + 1
            edgepath[nxt] = edgepath[cur] + [r]
            q.append(nxt)

    def _node(key: str, d: int) -> ImpactNode:
        if key.startswith("t:"):
            return ImpactNode(table=key[2:], field=None, distance=d,
                              via_edges=[_edge_out(r) for r in edgepath[key]])
        fid = key[2:]
        st = states.get(fid)
        fname = st["name"] if st else None
        return ImpactNode(table=st["table"] if st else r_table(edges, key),
                          field=fname, distance=d,
                          via_edges=[_edge_out(r) for r in edgepath[key]])

    def r_table(edges: list[sqlite3.Row], key: str) -> str:  # fallback for unknown fid
        fid = key[2:]
        for r in edges:
            if r["down_fid"] == fid:
                return r["downstream_table"]
        return "?"

    nodes = [_node(k, dist[k]) for k in sorted(
        (k for k in dist if k not in roots), key=lambda k: (dist[k], _label(k, states)))]
    raw_cycles = (_canon_cycle([_label(k, states) for k in cyc])
                  for cyc in _find_cycles([r for r in edges if r["id"] in reached]))
    cycles = sorted({tuple(c) for c in raw_cycles})
    return ImpactReport(
        root=EdgeRef(table=table, field=field),
        as_of_seq=max_seq if cut is None else min(cut, max_seq),
        nodes=nodes, cycles=[list(c) for c in cycles])


def replay_graph(conn: sqlite3.Connection, as_of_seq: int) -> dict:
    """Replay the immutable event log up to ``as_of_seq``."""
    events = conn.execute(
        "SELECT seq, kind, payload FROM events WHERE seq<=? ORDER BY seq",
        (as_of_seq,)).fetchall()
    tables: dict[str, dict] = {}
    edges: list[dict] = []
    for e in events:
        p = json.loads(e["payload"])
        if e["kind"] == "TABLE_REGISTERED":
            tables[p["name"]] = {"version": 1, "owner": p["owner"],
                                 "fields": [f["name"] for f in p["fields"]]}
        elif e["kind"] == "TABLE_EVOLVED":
            tables[p["name"]] = {"version": tables[p["name"]]["version"] + 1,
                                 "owner": p["owner"],
                                 "fields": [f["name"] for f in p["fields"]]}
        elif e["kind"] == "EDGE_REGISTERED":
            edges.append({"seq": e["seq"], **p})
    return {"as_of_seq": as_of_seq, "tables": tables, "edges": edges}
