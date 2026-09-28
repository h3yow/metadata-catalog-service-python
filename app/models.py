"""Pydantic request/response models (shape only; semantic rules live in services)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- catalog ----

DefaultSemantic = Literal[
    "null_default",
    "zero_value",
    "empty_string",
    "false_value",
    "current_timestamp",
    "explicit_value",
]


class FieldSpec(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    default_semantic: DefaultSemantic | None = None
    default_value: str | None = None
    description: str = ""


class RegisterTableRequest(BaseModel):
    name: str
    owner: str = ""
    description: str = ""
    fields: list[FieldSpec] = Field(default_factory=list)
    renamed_fields: dict[str, str] = Field(default_factory=dict)
    dropped_fields: list[str] = Field(default_factory=list)
    expected_revision: int | None = None


class FieldVersionOut(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default_semantic: str | None
    default_value: str | None
    description: str
    added_in_version: int
    dropped_in_version: int | None = None


class TableVersionOut(BaseModel):
    name: str
    version: int
    revision: int
    owner: str
    description: str
    fields: list[FieldVersionOut]
    change_summary: dict
    created_at: str


class TableOut(BaseModel):
    name: str
    current_version: int
    revision: int
    owner: str
    description: str
    fields: list[FieldVersionOut]
    created_at: str
    updated_at: str


# ---------------------------------------------------------------- lineage ----

class EdgeRef(BaseModel):
    table: str
    field: str | None = None


class RegisterLineageRequest(BaseModel):
    upstream: EdgeRef
    downstream: EdgeRef
    transformation: str = ""
    expected_upstream_version: int | None = None
    expected_downstream_version: int | None = None
    allow_cycle: bool = False


class LineageEdgeOut(BaseModel):
    upstream_table: str
    upstream_field: str | None
    downstream_table: str
    downstream_field: str | None
    transformation: str
    upstream_version: int
    downstream_version: int
    registered_seq: int


class ImpactNode(BaseModel):
    table: str
    field: str | None = None
    via_edges: list[LineageEdgeOut] = Field(default_factory=list)
    distance: int


class ImpactReport(BaseModel):
    root: EdgeRef
    as_of_seq: int
    nodes: list[ImpactNode]
    cycles: list[list[str]]


# ------------------------------------------------------------- governance ----

class Principal(BaseModel):
    user: str
    roles: list[str] = Field(default_factory=list)


class TransferOwnerRequest(BaseModel):
    new_owner: str
    expected_revision: int


class TagRequest(BaseModel):
    key: str
    value: str = ""


class GlossaryTermRequest(BaseModel):
    term: str
    definition: str = ""
    linked_tables: list[str] = Field(default_factory=list)
    linked_fields: list[dict] = Field(default_factory=list)


class GlossaryTermOut(BaseModel):
    term: str
    definition: str
    owner: str
    linked_tables: list[str]
    linked_fields: list[dict]
    version: int


class TagsOut(BaseModel):
    table: str
    revision: int
    tags: dict[str, str]


class OwnerOut(BaseModel):
    table: str
    owner: str
    revision: int
