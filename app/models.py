"""领域模型与请求/响应 schema（先放类型，后填行为）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------- 枚举 ----------

class DataFieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    BIGINT = "bigint"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


class ActorRole(str, Enum):
    ADMIN = "admin"
    STEWARD = "steward"      # 被授权的数据管理员角色
    VIEWER = "viewer"


# ---------- 鉴权 ----------

class Actor(BaseModel):
    user: str
    roles: list[str] = Field(default_factory=list)


# ---------- 字段 / 表 ----------

class FieldSpec(BaseModel):
    name: str
    type: DataFieldType
    nullable: bool = True
    description: str = ""
    default_value: str | None = None  # 新增字段的稳定默认值（按字面量文本存储）


class FieldDef(BaseModel):
    id: int
    name: str
    type: DataFieldType
    nullable: bool = True
    description: str = ""
    default_value: str | None = None
    added_in_version: int = 1


class RenameOp(BaseModel):
    from_name: str
    to_name: str
    type: DataFieldType  # 类型必须保持一致，否则非法改名


class DropOp(BaseModel):
    name: str


class TableVersionSpec(BaseModel):
    """一次新版本登记。

    fields: 期望的最终字段集合（新增字段必须带 default_value）。
    renames / drops: 必须显式声明，不允许静默改名/删字段。
    """

    fields: list[FieldSpec]
    renames: list[RenameOp] = Field(default_factory=list)
    drops: list[DropOp] = Field(default_factory=list)
    change_note: str = ""
    expected_version: int | None = None  # 乐观并发控制


class TableDef(BaseModel):
    id: str
    name: str
    owner: str
    current_version: int = 0
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TableVersionRecord(BaseModel):
    version: int
    fields: list[FieldDef]
    renames: list[RenameOp] = Field(default_factory=list)
    drops: list[str] = Field(default_factory=list)
    change_note: str
    created_at: str
    created_by: str


# ---------- 血缘 ----------

class FieldLineageRef(BaseModel):
    table: str
    field_id: str


class LineageSpec(BaseModel):
    upstream_table: str
    upstream_field_id: int | None = None
    downstream_table: str
    downstream_field_id: int | None = None
    transform: str = "direct"


class LineageEdge(BaseModel):
    id: str
    upstream_table: str
    upstream_field_id: int | None
    downstream_table: str
    downstream_field_id: int | None
    transform: str
    seq: int
    created_at: str
    created_by: str


# ---------- 标签 / 术语 / 归属 ----------

class TagUpdate(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class OwnerTransfer(BaseModel):
    new_owner: str


class TermSpec(BaseModel):
    name: str
    definition: str
    linked_tables: list[str] = Field(default_factory=list)
    linked_fields: list[FieldLineageRef] = Field(default_factory=list)


class Term(BaseModel):
    id: str
    name: str
    definition: str
    linked_tables: list[str] = Field(default_factory=list)
    linked_fields: list[FieldLineageRef] = Field(default_factory=list)
    published_by: str
    published_at: str


# ---------- 批量登记 ----------

class BatchItem(BaseModel):
    op: str  # register_table / submit_version / add_lineage
    body: dict


class BatchReport(BaseModel):
    idempotency_key: str
    results: list[dict]
