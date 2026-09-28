"""结构演进：版本比对、兼容性判定、回放。

演进规则（判定依据必须稳定、可解释）
------------------------------------
一次提交声明「期望的最终字段集合」(fields)，并显式声明 renames / drops。

对「初始版本」(v1)：
- 每个字段都要求有名字和类型（pydantic 已保证）；
- 字段名唯一；允许携带 default_value，但不强制。

对「后续版本」，判定顺序（决定拒绝原因时结构问题优先）：
1. 批次内静态校验：字段名重复、rename 冲突、drop 与 fields/rename 冲突；
2. drops / renames 引用的旧字段必须存在；改名的类型必须与旧类型一致，
   且与新字段声明的类型一致，否则 TYPE_INCOMPATIBLE（不允许借改名改类型）；
3. 旧字段消失但未声明：新集合里若有同类型“孤列”则 ILLEGAL_RENAME，
   否则 IMPLICIT_DROP（防止静默改名/丢列）；
4. 同名字段类型变化 => TYPE_INCOMPATIBLE；
5. 真新增字段必须提供非空 default_value，否则 MISSING_DEFAULT。
   该默认值即“新增字段的明确稳定默认语义”，旧数据按此值补全。
6. 存活字段得到稳定 id：保留/改名字段沿用旧 id，新增字段分配
   max(existing_id)+1；血缘按字段 id 记录，改名不会让血缘断裂。

所有版本记录不可变；历史版本上的血缘结论只引用当时的版本数据。
"""

from __future__ import annotations

from .errors import (
    DuplicateEntryError,
    IllegalRenameError,
    ImplicitDropError,
    MissingDefaultError,
    RenameConflictError,
    TypeIncompatibleError,
    ValidationError,
)
from .models import (
    FieldDef,
    FieldSpec,
    TableVersionRecord,
    TableVersionSpec,
)


def _duplicate(names: list[str]) -> str | None:
    seen: set[str] = set()
    for n in names:
        if n in seen:
            return n
        seen.add(n)
    return None


def validate_spec(spec: TableVersionSpec) -> None:
    """批次内静态一致性校验。"""
    if not spec.fields:
        raise ValidationError(
            "fields must not be empty", details={"reason": "empty_fields"}
        )
    dup = _duplicate([f.name for f in spec.fields])
    if dup:
        raise DuplicateEntryError(
            f"duplicate field name in submission: {dup!r}",
            details={"field": dup},
        )

    from_names = [r.from_name for r in spec.renames]
    to_names = [r.to_name for r in spec.renames]
    if dup := _duplicate(from_names):
        raise RenameConflictError(
            f"field renamed more than once: {dup!r}", details={"field": dup}
        )
    if dup := _duplicate(to_names):
        raise RenameConflictError(
            f"multiple fields renamed to same name: {dup!r}",
            details={"field": dup},
        )
    new_names = {f.name for f in spec.fields}
    drop_names = {d.name for d in spec.drops}
    for r in spec.renames:
        if r.to_name not in new_names:
            raise RenameConflictError(
                f"rename target {r.to_name!r} is not present in fields",
                details={"from": r.from_name, "to": r.to_name},
            )
        if r.from_name in new_names and r.from_name != r.to_name:
            raise RenameConflictError(
                f"rename source {r.from_name!r} also appears unchanged in fields",
                details={"field": r.from_name},
            )
    if overlap := (drop_names & new_names):
        raise ValidationError(
            f"field both dropped and present: {sorted(overlap)}",
            details={"fields": sorted(overlap)},
        )
    if overlap := (drop_names & set(from_names)):
        raise ValidationError(
            f"field both dropped and renamed: {sorted(overlap)}",
            details={"fields": sorted(overlap)},
        )


def apply_version(
    prev: TableVersionRecord | None,
    spec: TableVersionSpec,
    new_version: int,
) -> TableVersionRecord:
    """把提交应用到上一版本，产出新的不可变版本记录。

    时间戳、created_by 由服务层在事务内填充。
    """
    validate_spec(spec)
    new_fields = {f.name: f for f in spec.fields}
    rename_from = {r.from_name: r for r in spec.renames}
    rename_to = {r.to_name: r for r in spec.renames}
    drops = {d.name for d in spec.drops}

    result: list[FieldDef] = []

    # ---------- 初始版本 ----------
    if prev is None:
        for idx, f in enumerate(spec.fields, start=1):
            result.append(_to_field_def(f, idx, new_version))
        return TableVersionRecord(
            version=new_version,
            fields=result,
            renames=list(spec.renames),
            drops=sorted(drops),
            change_note=spec.change_note,
            created_at="",
            created_by="",
        )

    old_fields = {f.name: f for f in prev.fields}

    # drops / rename 源引用必须存在
    for name in sorted(drops):
        if name not in old_fields:
            raise ValidationError(
                f"drop references unknown field {name!r}",
                details={"field": name, "reason": "drop_unknown_field"},
            )
    for r in spec.renames:
        if r.from_name not in old_fields:
            raise RenameConflictError(
                f"rename source {r.from_name!r} does not exist in previous version",
                details={"from": r.from_name},
            )
        old = old_fields[r.from_name]
        if old.type != r.type:
            raise TypeIncompatibleError(
                f"cannot change type while renaming {r.from_name!r}: "
                f"{old.type.value} -> {r.type.value}",
                details={
                    "field": r.from_name,
                    "from_type": old.type.value,
                    "to_type": r.type.value,
                },
            )
        if new_fields[r.to_name].type != r.type:
            raise TypeIncompatibleError(
                f"renamed field {r.to_name!r} declared with type "
                f"{new_fields[r.to_name].type.value}, expected {r.type.value}",
                details={"field": r.to_name},
            )

    # 结构问题优先：旧字段消失且未声明 => ILLEGAL_RENAME / IMPLICIT_DROP
    old_names = set(old_fields)
    new_names = set(new_fields)
    rename_sources = set(rename_from)
    new_orphans = sorted(n for n in new_names - old_names - set(rename_to))
    vanished = sorted(old_names - new_names - rename_sources - drops)
    for name in vanished:
        old = old_fields[name]
        guess = next(
            (n for n in new_orphans if new_fields[n].type == old.type), None
        )
        if guess is not None:
            raise IllegalRenameError(
                f"field {name!r} disappeared and {guess!r} appeared with the same "
                f"type {old.type.value}; rename must be declared explicitly",
                details={"from": name, "to_guess": guess},
            )
        raise ImplicitDropError(
            f"field {name!r} is missing from the new version; dropping requires "
            f"an explicit drops entry",
            details={"field": name},
        )

    next_id = max((f.id for f in prev.fields), default=0) + 1

    # 逐字段产出（按提交顺序，保证回放结果稳定）
    for f in spec.fields:
        if f.name in rename_to:
            r = rename_to[f.name]
            old = old_fields[r.from_name]
            result.append(
                FieldDef(
                    id=old.id,
                    name=f.name,
                    type=old.type,
                    nullable=f.nullable,
                    description=f.description,
                    default_value=old.default_value,
                    added_in_version=old.added_in_version,
                )
            )
        elif f.name in old_fields:
            old = old_fields[f.name]
            if old.type != f.type:
                raise TypeIncompatibleError(
                    f"field {f.name!r} type changed incompatibly: "
                    f"{old.type.value} -> {f.type.value}",
                    details={
                        "field": f.name,
                        "from_type": old.type.value,
                        "to_type": f.type.value,
                    },
                )
            result.append(
                FieldDef(
                    id=old.id,
                    name=f.name,
                    type=old.type,
                    nullable=f.nullable,
                    description=f.description,
                    default_value=f.default_value
                    if f.default_value is not None
                    else old.default_value,
                    added_in_version=old.added_in_version,
                )
            )
        else:
            if not f.default_value:
                raise MissingDefaultError(
                    f"new field {f.name!r} requires a non-empty default_value",
                    details={"field": f.name, "type": f.type.value},
                )
            result.append(_to_field_def(f, next_id, new_version))
            next_id += 1

    return TableVersionRecord(
        version=new_version,
        fields=result,
        renames=list(spec.renames),
        drops=sorted(drops),
        change_note=spec.change_note,
        created_at="",
        created_by="",
    )


def _to_field_def(f: FieldSpec, fid: int, version: int) -> FieldDef:
    return FieldDef(
        id=fid,
        name=f.name,
        type=f.type,
        nullable=f.nullable,
        description=f.description,
        default_value=f.default_value,
        added_in_version=version,
    )
