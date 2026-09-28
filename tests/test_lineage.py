"""Lineage: dedup on resubmit, cycle marking, determinism, rename continuity, replay."""
from __future__ import annotations

import logging

from app.catalog import register_table
from app.db import get_conn, write_tx
from app.errors import CatalogError
from app.lineage import downstream_impact, register_edge, replay_graph
from app.models import EdgeRef, RegisterLineageRequest, RegisterTableRequest
from tests.conftest import logged

log = logging.getLogger("test.lineage")


def _table(name, fields=("id",)):
    with write_tx() as c:
        register_table(c, RegisterTableRequest(
            name=name, owner="alice",
            fields=[{"name": f, "data_type": "int64", "nullable": False} for f in fields]))


def _edge(up, down, **kw):
    with write_tx() as c:
        return register_edge(c, RegisterLineageRequest(
            upstream=EdgeRef(table=up[0], field=up[1]),
            downstream=EdgeRef(table=down[0], field=down[1]), **kw))


def _edges_count():
    return get_conn().execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]


def test_duplicate_edge_submission_collapses():
    _table("a"), _table("b")
    e1, d1 = _edge(("a", "id"), ("b", "id"))
    e2, d2 = _edge(("a", "id"), ("b", "id"))
    logged("dup-edge", seq1=e1.registered_seq, seq2=e2.registered_seq,
           deduped=(d1, d2), rows=_edges_count())
    assert (d1, d2) == (False, True)
    assert e1.registered_seq == e2.registered_seq
    assert _edges_count() == 1  # neither duplicate nor missing


def test_persistence_survives_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    from app import db as dbmod
    dbmod.reset(db)
    _table("a"), _table("b")
    _edge(("a", "id"), ("b", "id"))
    # simulate process restart: drop module-held connection and reopen
    import sqlite3
    dbmod._conn.close()
    dbmod._conn = None
    dbmod.init_db(db)
    logged("restart", rows=_edges_count())
    assert _edges_count() == 1
    # resubmit after restart still dedupes
    _, deduped = _edge(("a", "id"), ("b", "id"))
    assert deduped is True
    assert _edges_count() == 1


def test_missing_endpoint_is_distinct_404():
    _table("a")
    logged("missing-downstream", upstream="a.id", downstream="x.id")
    try:
        _edge(("a", "id"), ("x", "id"))
    except CatalogError as e:
        assert e.status_code == 404 and e.code == "TABLE_NOT_FOUND"
    else:
        raise AssertionError("expected TABLE_NOT_FOUND")


def test_cycle_is_rejected_then_marked_on_request():
    for n in ("a", "b", "c"):
        _table(n)
    _edge(("a", "id"), ("b", "id"))
    _edge(("b", "id"), ("c", "id"))
    try:
        _edge(("c", "id"), ("a", "id"))
    except CatalogError as e:
        logged("cycle-reject", code=e.code, cycle=e.details["cycle"])
        assert e.code == "CYCLE_DETECTED"
        assert e.details["cycle"][0] == e.details["cycle"][-1]
    else:
        raise AssertionError("expected CYCLE_DETECTED")
    assert _edges_count() == 2  # rejected edge was not inserted
    edge, _ = _edge(("c", "id"), ("a", "id"), allow_cycle=True)
    logged("cycle-marked", seq=edge.registered_seq)
    assert _edges_count() == 3
    flagged = get_conn().execute(
        "SELECT is_cyclic FROM edges WHERE registered_seq=?",
        (edge.registered_seq,)).fetchone()["is_cyclic"]
    assert flagged == 1
    with write_tx() as c:
        report = downstream_impact(c, "a")
    # traversal terminates (no infinite loop); the elementary cycle is listed,
    # and the closing edge c->a proves it actually loops back to the first node
    assert [n.table for n in report.nodes] == ["b", "c"]
    assert report.cycles == [["a#id", "b#id", "c#id"]]
    closing = get_conn().execute(
        "SELECT 1 FROM edges WHERE upstream_table='c' AND downstream_table='a'").fetchone()
    assert closing is not None


def test_impact_is_stable_and_explainable():
    for n in ("a", "b", "c"):
        _table(n)
    _edge(("a", "id"), ("b", "id"))
    _edge(("b", "id"), ("c", "id"))
    with write_tx() as c:
        r1 = downstream_impact(c, "a").model_dump()
        r2 = downstream_impact(c, "a").model_dump()
    logged("impact-stable", nodes=r1["nodes"])
    assert r1 == r2
    assert [(n["table"], n["distance"]) for n in r1["nodes"]] == [("b", 1), ("c", 2)]
    # each node carries the explaining edge path
    far = r1["nodes"][1]
    assert [e["upstream_table"] for e in far["via_edges"]] == ["a", "b"]


def test_rename_preserves_field_lineage_chain():
    _table("a", fields=("id", "name")), _table("b", fields=("id", "name"))
    e, _ = _edge(("a", "name"), ("b", "name"))
    # rename on both tables
    for t in ("a", "b"):
        with write_tx() as c:
            register_table(c, RegisterTableRequest(
                name=t, owner="alice", expected_revision=1,
                fields=[{"name": "id", "data_type": "int64", "nullable": False},
                        {"name": "full_name", "data_type": "int64", "nullable": False}],
                renamed_fields={"name": "full_name"}))
    # resubmit using NEW names: identity dedup recognises the same edge
    _, deduped = _edge(("a", "full_name"), ("b", "full_name"))
    logged("rename-dedup", deduped=deduped, rows=_edges_count())
    assert deduped is True and _edges_count() == 1
    with write_tx() as c:
        report = downstream_impact(c, "a", "full_name")
    logged("rename-chain", nodes=[(n.table, n.field) for n in report.nodes])
    assert [(n.table, n.field) for n in report.nodes] == [("b", "full_name")]
    # historical report keeps the frozen old names and has no stale branch
    with write_tx() as c:
        hist = downstream_impact(c, "a", as_of_seq=e.registered_seq)
    assert [(n.table, n.field) for n in hist.nodes] == [("b", "name")]


def test_replay_matches_historical_conclusions():
    for n in ("a", "b"):
        _table(n)
    e, _ = _edge(("a", "id"), ("b", "id"))
    with write_tx() as c:
        before = replay_graph(c, e.registered_seq - 1)
        at = replay_graph(c, e.registered_seq)
    logged("replay", before_edges=len(before["edges"]), at_edges=len(at["edges"]))
    assert len(before["edges"]) == 0 and len(at["edges"]) == 1
