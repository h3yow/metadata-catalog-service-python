"""结构演进：默认值语义、显式改名/删字段、类型兼容、回放与历史稳定性。"""

from __future__ import annotations

import pytest

from app.errors import (
    IllegalRenameError,
    ImplicitDropError,
    MissingDefaultError,
    RenameConflictError,
    TypeIncompatibleError,
    ValidationError,
)
from app.models import (
    DataFieldType as T,
    FieldSpec,
    RenameOp,
    DropOp,
    TableDef,
    TableVersionSpec,
)
from app.models import Actor


ALICE = Actor(user="alice")


def _v1(svc):
    svc.register_table(ALICE, TableDef(id="t", name="T", owner="alice"))
    return svc.submit_version(
        ALICE,
        "t",
        TableVersionSpec(
            fields=[
                FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="name", type=T.STRING),
            ]
        ),
    )


def test_initial_version_assigns_stable_ids(service):
    r = _v1(service)
    assert r["version"] == 1
    assert [(f["name"], f["id"]) for f in r["fields"]] == [("id", 1), ("name", 2)]


def test_new_field_requires_default(service):
    _v1(service)
    with pytest.raises(MissingDefaultError) as ei:
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(
                fields=[
                    FieldSpec(name="id", type=T.INTEGER),
                    FieldSpec(name="name", type=T.STRING),
                    FieldSpec(name="age", type=T.INTEGER),
                ]
            ),
        )
    assert ei.value.code == "MISSING_DEFAULT"
    assert ei.value.details["field"] == "age"


def test_new_field_with_default_is_accepted_and_explained(service):
    _v1(service)
    r = service.submit_version(
        ALICE,
        "t",
        TableVersionSpec(
            fields=[
                FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="name", type=T.STRING),
                FieldSpec(name="age", type=T.INTEGER, default_value="0"),
            ]
        ),
    )
    assert r["status"] == "accepted"
    assert any("default_value='0'" in x for x in r["reasons"])
    age = next(f for f in r["fields"] if f["name"] == "age")
    assert age["added_in_version"] == 2


def test_undeclared_rename_same_type_is_rejected(service):
    """名字对不上 + 同类型孤列 => ILLEGAL_RENAME，而不是普通覆盖。"""
    _v1(service)
    with pytest.raises(IllegalRenameError) as ei:
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(
                fields=[
                    FieldSpec(name="id", type=T.INTEGER),
                    FieldSpec(name="nick", type=T.STRING, default_value=""),
                ]
            ),
        )
    assert ei.value.details == {"from": "name", "to_guess": "nick"}


def test_declared_rename_preserves_field_id(service):
    _v1(service)
    r = service.submit_version(
        ALICE,
        "t",
        TableVersionSpec(
            fields=[
                FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="full_name", type=T.STRING),
            ],
            renames=[RenameOp(from_name="name", to_name="full_name", type=T.STRING)],
        ),
    )
    full = next(f for f in r["fields"] if f["name"] == "full_name")
    assert full["id"] == 2  # 与旧 name 字段相同，血缘按 id 不断裂


def test_rename_with_type_change_rejected(service):
    _v1(service)
    with pytest.raises(TypeIncompatibleError):
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(
                fields=[
                    FieldSpec(name="id", type=T.INTEGER),
                    FieldSpec(name="full_name", type=T.INTEGER),
                ],
                renames=[RenameOp(from_name="name", to_name="full_name", type=T.INTEGER)],
            ),
        )


def test_kept_field_type_change_rejected(service):
    _v1(service)
    with pytest.raises(TypeIncompatibleError):
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(
                fields=[
                    FieldSpec(name="id", type=T.STRING),
                    FieldSpec(name="name", type=T.STRING),
                ]
            ),
        )


def test_undeclared_drop_rejected_but_explicit_drop_accepted(service):
    _v1(service)
    with pytest.raises(ImplicitDropError) as ei:
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(fields=[FieldSpec(name="id", type=T.INTEGER)]),
        )
    assert ei.value.details["field"] == "name"

    r = service.submit_version(
        ALICE,
        "t",
        TableVersionSpec(
            fields=[FieldSpec(name="id", type=T.INTEGER)],
            drops=[DropOp(name="name")],
        ),
    )
    assert r["drops"] == ["name"]


def test_rename_conflict_duplicate_target(service):
    _v1(service)
    # 两个字段都 rename 到同一个目标名 => RENAME_CONFLICT
    spec = TableVersionSpec(
        fields=[FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="x", type=T.STRING)],
        renames=[
            RenameOp(from_name="name", to_name="x", type=T.STRING),
            RenameOp(from_name="id", to_name="x", type=T.INTEGER),
        ],
    )
    with pytest.raises(RenameConflictError):
        service.submit_version(ALICE, "t", spec)

    # rename 目标不在 fields 中 => RENAME_CONFLICT
    with pytest.raises(RenameConflictError):
        service.submit_version(
            ALICE,
            "t",
            TableVersionSpec(
                fields=[FieldSpec(name="id", type=T.INTEGER),
                        FieldSpec(name="name", type=T.STRING)],
                renames=[RenameOp(from_name="name", to_name="ghost", type=T.STRING)],
            ),
        )


def test_empty_fields_rejected(service):
    _v1(service)
    with pytest.raises(ValidationError):
        service.submit_version(ALICE, "t", TableVersionSpec(fields=[]))


def test_replay_is_immutable_and_historical_conclusions_stable(service):
    _v1(service)
    service.submit_version(
        ALICE,
        "t",
        TableVersionSpec(
            fields=[
                FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="full_name", type=T.STRING),
                FieldSpec(name="age", type=T.INTEGER, default_value="0"),
            ],
            renames=[RenameOp(from_name="name", to_name="full_name", type=T.STRING)],
        ),
    )
    rep = service.replay(ALICE, "t")
    assert rep["current_version"] == 2
    v1 = rep["versions"][0]
    # 历史版本不受后续改名/加列影响
    assert [f["name"] for f in v1["fields"]] == ["id", "name"]
    got = service.get_version(ALICE, "t", 1)
    assert got["version"]["fields"][1]["name"] == "name"
