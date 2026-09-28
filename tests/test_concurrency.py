"""Concurrent duplicate registration: no duplicate edges/versions, no lost updates."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app import governance as g
from app.catalog import register_table
from app.db import get_conn, write_tx
from app.lineage import register_edge
from app.models import (
    EdgeRef,
    Principal,
    RegisterLineageRequest,
    RegisterTableRequest,
    TagRequest,
    TransferOwnerRequest,
)
from tests.conftest import logged

log = logging.getLogger("test.concurrent")


def test_concurrent_duplicate_table_registration_collapses(tmp_path):
    # real file-backed DB so threads contend on one BEGIN IMMEDIATE queue
    from app import db as dbmod
    dbmod.reset(str(tmp_path / "c.db"))
    payload = RegisterTableRequest(
        name="hot", owner="alice",
        fields=[{"name": "id", "data_type": "int64", "nullable": False}])

    def submit(i):
        try:
            with write_tx() as c:
                _, deduped = register_table(c, payload, actor=f"w{i}")
            return ("ok", deduped)
        except Exception as e:  # every rejected outcome must carry a reason
            return ("reject", type(e).__name__, getattr(e, "code", None))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    logged("concurrent-table", results=results)
    rows = get_conn().execute(
        "SELECT COUNT(*) c FROM table_versions WHERE table_name='hot'").fetchone()["c"]
    events = get_conn().execute(
        "SELECT COUNT(*) c FROM events WHERE kind='TABLE_REGISTERED'").fetchone()["c"]
    assert rows == 1 and events == 1
    assert sum(1 for r in results if r == ("ok", False)) == 1
    assert all(r[0] == "ok" for r in results)  # idempotent, never a half-write error


def test_concurrent_duplicate_edges_do_not_duplicate(tmp_path):
    from app import db as dbmod
    dbmod.reset(str(tmp_path / "e.db"))
    with write_tx() as c:
        for n in ("a", "b"):
            register_table(c, RegisterTableRequest(
                name=n, owner="alice",
                fields=[{"name": "id", "data_type": "int64", "nullable": False}]))
    req = RegisterLineageRequest(upstream=EdgeRef(table="a", field="id"),
                                 downstream=EdgeRef(table="b", field="id"))

    def submit(i):
        try:
            with write_tx() as c:
                _, deduped = register_edge(c, req, actor=f"w{i}")
            return deduped
        except Exception as e:
            return ("error", getattr(e, "code", str(e)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(submit, range(20)))
    logged("concurrent-edge", flags=flags)
    rows = get_conn().execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    assert rows == 1  # no duplicates and no lost edge
    assert flags.count(False) == 1 and flags.count(True) == 19


def test_concurrent_ownership_transfers_one_wins(tmp_path):
    from app import db as dbmod
    dbmod.reset(str(tmp_path / "o.db"))
    with write_tx() as c:
        register_table(c, RegisterTableRequest(
            name="t1", owner="alice",
            fields=[{"name": "id", "data_type": "int64", "nullable": False}]))

    def transfer(i):
        try:
            with write_tx() as c:
                out = g.transfer_owner(
                    c, "t1", TransferOwnerRequest(
                        new_owner=f"owner{i % 2}", expected_revision=1),
                    Principal(user="alice"))
            return ("ok", out.owner, out.revision)
        except Exception as e:
            return ("reject", getattr(e, "code", str(e)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(transfer, range(12)))
    logged("concurrent-owner", results=results)
    oks = [r for r in results if r[0] == "ok"]
    rejects = [r for r in results if r[0] == "reject"]
    assert len(oks) == 1 and oks[0][2] == 2
    # After the single winner commits, every later attempt is rejected with a
    # distinct reason: stale revision (409) or alice having lost ownership (403).
    reasons = {r[1] for r in rejects}
    assert reasons <= {"REVISION_MISMATCH", "FORBIDDEN"} and reasons
    with write_tx() as c:
        final = c.execute(
            "SELECT owner, revision FROM tables WHERE name='t1'").fetchone()
    log.info("JUDGEMENT final owner=%s revision=%s", final["owner"], final["revision"])
    assert final["revision"] == 2 and final["owner"] in {"owner0", "owner1"}


def test_concurrent_transfers_by_authorized_admin_conflict_on_revision(tmp_path):
    """Authorized caller stays the same: exactly one wins, all others 409."""
    from app import db as dbmod
    dbmod.reset(str(tmp_path / "a.db"))
    with write_tx() as c:
        register_table(c, RegisterTableRequest(
            name="t1", owner="alice",
            fields=[{"name": "id", "data_type": "int64", "nullable": False}]))

    def transfer(i):
        try:
            with write_tx() as c:
                out = g.transfer_owner(
                    c, "t1", TransferOwnerRequest(
                        new_owner=f"owner{i}", expected_revision=1),
                    Principal(user="root", roles=["admin"]))
            return ("ok", out.revision)
        except Exception as e:
            return ("reject", getattr(e, "code", str(e)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(transfer, range(10)))
    logged("concurrent-admin-transfer", results=results)
    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1 and oks[0][1] == 2
    assert {r[1] for r in results if r[0] == "reject"} == {"REVISION_MISMATCH"}


def test_concurrent_tag_writes_all_present(tmp_path):
    from app import db as dbmod
    dbmod.reset(str(tmp_path / "t.db"))
    with write_tx() as c:
        register_table(c, RegisterTableRequest(
            name="t1", owner="alice",
            fields=[{"name": "id", "data_type": "int64", "nullable": False}]))

    def tag(i):
        with write_tx() as c:
            g.add_tag(c, "t1", TagRequest(key=f"k{i}", value=str(i)), Principal(user="alice"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(tag, range(24)))
    with write_tx() as c:
        tags = g.list_tags(c, "t1").tags
    logged("concurrent-tags", count=len(tags))
    assert len(tags) == 24 and tags["k23"] == "23"  # none lost, none duplicated
