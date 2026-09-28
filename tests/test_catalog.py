"""Structure evolution: versioning, default semantics, rename/drop rejection, replay."""
from __future__ import annotations

import logging

from app.catalog import get_table, get_version, list_versions, register_table
from app.db import write_tx
from app.errors import CatalogError
from app.models import FieldSpec, RegisterTableRequest
from tests.conftest import logged

log = logging.getLogger("test.catalog")


def _reg(**kw):
    with write_tx() as c:
        return register_table(c, RegisterTableRequest(**kw))


def _expect_reject(label, payload, code):
    logged(label, code=code, payload=payload)
    try:
        _reg(**payload)
    except CatalogError as e:
        log.info("JUDGEMENT %s -> code=%s details=%s", label, e.code, e.details)
        assert e.code == code, (e.code, code)
        return e
    raise AssertionError(f"{label} should be rejected with {code}")


def test_first_version_requires_owner():
    logged("v1-missing-owner", name="t1", owner="")
    try:
        _reg(name="t1", owner="", fields=[])
    except CatalogError as e:
        assert e.code == "MISSING_REQUIRED_INFO"


def test_resubmission_is_idempotent_and_replayable():
    payload = {"name": "t1", "owner": "alice",
               "fields": [{"name": "id", "data_type": "int64", "nullable": False}]}
    v1, dup1 = _reg(**payload)
    v1b, dup2 = _reg(**payload)
    logged("idempotent-replay", v1=v1.version, second=v1b.version, deduped=dup2)
    assert (v1.version, dup1) == (1, False)
    assert (v1b.version, dup2) == (1, True)
    with write_tx() as c:
        assert len(list_versions(c, "t1")) == 1
        assert [f.name for f in get_version(c, "t1", 1).fields] == ["id"]


def test_new_field_requires_default_semantic():
    _reg(name="t1", owner="alice",
         fields=[FieldSpec(name="id", data_type="int64", nullable=False)])
    _expect_reject(
        "new-field-no-default",
        dict(name="t1", owner="alice", expected_revision=1, fields=[
            {"name": "id", "data_type": "int64", "nullable": False},
            {"name": "note", "data_type": "string"}]),
        "MISSING_DEFAULT_SEMANTIC")
    _expect_reject(
        "explicit-value-without-value",
        dict(name="t1", owner="alice", expected_revision=1, fields=[
            {"name": "id", "data_type": "int64", "nullable": False},
            {"name": "note", "data_type": "string",
             "default_semantic": "explicit_value"}]),
        "MISSING_REQUIRED_INFO")


def test_legal_evolution_adds_field_and_widens_type():
    _reg(name="t1", owner="alice",
         fields=[FieldSpec(name="id", data_type="int32", nullable=False)])
    v2, dup = _reg(name="t1", owner="alice", expected_revision=1, fields=[
        {"name": "id", "data_type": "int64", "nullable": False},
        {"name": "note", "data_type": "string", "default_semantic": "empty_string"}])
    logged("legal-evolution", version=v2.version, summary=v2.change_summary, deduped=dup)
    assert v2.version == 2 and dup is False
    assert v2.change_summary["added"] == ["note"]
    assert v2.change_summary["type_widened"] == [
        {"field": "id", "from": "int32", "to": "int64"}]


def test_silent_field_disappearance_is_rejected_not_overwritten():
    _reg(name="t1", owner="alice", fields=[
        FieldSpec(name="id", data_type="int64", nullable=False),
        FieldSpec(name="secret", data_type="string",
                  default_semantic="empty_string")])
    e = _expect_reject("silent-drop", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "int64", "nullable": False}]),
        "ILLEGAL_DROP")
    assert e.details["fields"] == ["secret"]  # reason names the missing field


def test_rename_must_be_explicit_and_non_colliding():
    _reg(name="t1", owner="alice", fields=[
        FieldSpec(name="id", data_type="int64", nullable=False),
        FieldSpec(name="name", data_type="string",
                  default_semantic="empty_string")])
    _expect_reject("rename-unknown-source", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "int64", "nullable": False},
                {"name": "name", "data_type": "string",
                 "default_semantic": "empty_string"}],
        renamed_fields={"ghost": "title"}), "FIELD_NOT_FOUND")
    _expect_reject("rename-collision", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "int64", "nullable": False},
                {"name": "id2", "data_type": "int64", "nullable": False},
                {"name": "name", "data_type": "string",
                 "default_semantic": "empty_string"}],
        renamed_fields={"name": "id"}), "ILLEGAL_RENAME")
    # legal rename keeps a stable lineage identity
    v3, _ = _reg(name="t1", owner="alice", expected_revision=1, fields=[
        {"name": "id", "data_type": "int64", "nullable": False},
        {"name": "title", "data_type": "string", "default_semantic": "empty_string"}],
        renamed_fields={"name": "title"})
    assert v3.change_summary["renamed"] == [{"from": "name", "to": "title"}]


def test_incompatible_type_and_nullable_tightening_rejected():
    _reg(name="t1", owner="alice", fields=[
        FieldSpec(name="id", data_type="int64", nullable=True)])
    _expect_reject("type-narrow", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "int32", "nullable": True}]),
        "INCOMPATIBLE_TYPE")
    _expect_reject("string-to-int", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "string", "nullable": True}]),
        "INCOMPATIBLE_TYPE")
    _expect_reject("nullable-tightened", dict(
        name="t1", owner="alice", expected_revision=1,
        fields=[{"name": "id", "data_type": "int64", "nullable": False}]),
        "INCOMPATIBLE_TYPE")


def test_optimistic_revision_guards_concurrent_evolutions():
    _reg(name="t1", owner="alice", fields=[
        FieldSpec(name="id", data_type="int64", nullable=False)])
    _reg(name="t1", owner="alice", expected_revision=1, fields=[
        {"name": "id", "data_type": "int64", "nullable": False},
        {"name": "a", "data_type": "string", "default_semantic": "empty_string"}])
    _expect_reject("stale-revision", dict(
        name="t1", owner="alice", expected_revision=1, fields=[
            {"name": "id", "data_type": "int64", "nullable": False},
            {"name": "a", "data_type": "string", "default_semantic": "empty_string"},
            {"name": "b", "data_type": "string", "default_semantic": "empty_string"}]),
        "REVISION_MISMATCH")


def test_historical_version_is_replayable_after_later_changes():
    _reg(name="t1", owner="alice", fields=[
        FieldSpec(name="id", data_type="int32", nullable=False)])
    _reg(name="t1", owner="alice", expected_revision=1, fields=[
        {"name": "id", "data_type": "int64", "nullable": False},
        {"name": "note", "data_type": "string", "default_semantic": "empty_string"}])
    _reg(name="t1", owner="alice", expected_revision=2, fields=[
        {"name": "id", "data_type": "int64", "nullable": False}],
        dropped_fields=["note"])
    with write_tx() as c:
        v1 = get_version(c, "t1", 1)
        cur = get_table(c, "t1")
        logged("replay", v1=[f.name for f in v1.fields],
               current=[f.name for f in cur.fields])
        assert [(f.name, f.data_type) for f in v1.fields] == [("id", "int32")]
        assert [f.name for f in cur.fields] == ["id"]
        # the dropped field is visible in the version where it still existed
        v2 = get_version(c, "t1", 2)
        assert {f.name for f in v2.fields} == {"id", "note"}
