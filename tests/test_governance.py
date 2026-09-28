"""Governance: 404/401/403 distinction, grants, tags, glossary, ownership locking."""
from __future__ import annotations

import logging

from app import governance as g
from app.catalog import register_table
from app.db import write_tx
from app.errors import CatalogError
from app.models import (
    GlossaryTermRequest,
    Principal,
    RegisterTableRequest,
    TagRequest,
    TransferOwnerRequest,
)
from tests.conftest import logged

log = logging.getLogger("test.gov")
ALICE = Principal(user="alice")
BOB = Principal(user="bob")
ANON = Principal(user="anonymous")
ADMIN = Principal(user="root", roles=["admin"])
STEWARD = Principal(user="sue", roles=["steward"])


def _table():
    with write_tx() as c:
        register_table(c, RegisterTableRequest(
            name="t1", owner="alice",
            fields=[{"name": "id", "data_type": "int64", "nullable": False}]))


def _try(label, fn, code, status=None):
    logged(label)
    try:
        with write_tx() as c:
            fn(c)
    except CatalogError as e:
        log.info("JUDGEMENT %s -> %s/%s details=%s", label, e.status_code, e.code, e.details)
        assert e.code == code and (status is None or e.status_code == status)
        return
    raise AssertionError(f"{label}: expected {code}")


def test_not_found_unauth_forbidden_are_distinct():
    _table()
    _try("missing-table", lambda c: g.require_table_owner(c, "nope", BOB),
         "TABLE_NOT_FOUND", 404)
    _try("anonymous", lambda c: g.transfer_owner(
        c, "t1", TransferOwnerRequest(new_owner="x", expected_revision=1), ANON),
        "UNAUTHENTICATED", 401)
    _try("outsider-tag", lambda c: g.add_tag(c, "t1", TagRequest(key="k"), BOB),
         "FORBIDDEN", 403)
    # owner succeeds, proving the same resource exists for an authorised caller
    with write_tx() as c:
        out = g.add_tag(c, "t1", TagRequest(key="domain", value="sales"), ALICE)
    assert out.tags == {"domain": "sales"}


def test_granted_steward_can_tag_and_owner_can_delegate():
    _table()
    _try("bob-cannot-grant", lambda c: g.add_grant(c, "t1", "mallory", BOB),
         "FORBIDDEN", 403)
    with write_tx() as c:
        g.add_grant(c, "t1", "bob", ALICE)
        out = g.add_tag(c, "t1", TagRequest(key="pii", value="true"), BOB)
    logged("granted-tag", tags=out.tags)
    assert out.tags == {"pii": "true"}


def test_ownership_transfer_uses_optimistic_revision():
    _table()
    with write_tx() as c:
        g.transfer_owner(c, "t1", TransferOwnerRequest(new_owner="carol",
                                                       expected_revision=1), ALICE)
    # alice's stale handle is now rejected, both by permission and by revision
    _try("old-owner-locked-out", lambda c: g.add_tag(c, "t1", TagRequest(key="x"), ALICE),
         "FORBIDDEN", 403)
    _try("stale-revision-transfer", lambda c: g.transfer_owner(
        c, "t1", TransferOwnerRequest(new_owner="dave", expected_revision=1),
        Principal(user="carol")), "REVISION_MISMATCH", 409)
    with write_tx() as c:
        out = g.transfer_owner(c, "t1", TransferOwnerRequest(
            new_owner="dave", expected_revision=2), Principal(user="carol"))
    assert (out.owner, out.revision) == ("dave", 3)


def test_glossary_requires_role_and_validates_links():
    _table()
    _try("term-anon", lambda c: g.publish_term(
        c, GlossaryTermRequest(term="Revenue"), ANON), "UNAUTHENTICATED", 401)
    _try("term-plain-user", lambda c: g.publish_term(
        c, GlossaryTermRequest(term="Revenue"), BOB), "FORBIDDEN", 403)
    _try("term-broken-link", lambda c: g.publish_term(
        c, GlossaryTermRequest(term="Revenue", linked_tables=["ghost"]), ADMIN),
        "TABLE_NOT_FOUND", 404)
    with write_tx() as c:
        t1 = g.publish_term(c, GlossaryTermRequest(
            term="Revenue", definition="gross income", linked_tables=["t1"]), STEWARD)
        t2 = g.publish_term(c, GlossaryTermRequest(
            term="Revenue", definition="refined", linked_tables=["t1"]), ADMIN)
    logged("term-versioning", v1=t1.version, v2=t2.version, owner=t2.owner)
    assert (t1.version, t2.version, t2.owner) == (1, 2, "root")


def test_tag_upserts_commute_to_consistent_state():
    _table()
    with write_tx() as c:
        g.add_tag(c, "t1", TagRequest(key="env", value="dev"), ALICE)
        g.add_tag(c, "t1", TagRequest(key="env", value="prod"), ALICE)
        g.add_tag(c, "t1", TagRequest(key="team", value="data"), ALICE)
        tags = g.list_tags(c, "t1").tags
    logged("tag-upsert", tags=tags)
    assert tags == {"env": "prod", "team": "data"}  # last write wins, nothing duplicated
