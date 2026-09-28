"""服务层：编排存储、演进、血缘、权限，保证并发安全与可解释结果。

所有写操作都在 store.transaction()（跨进程文件锁 + 进程内锁 + 进入时重读盘）
内完成“校验-判定-落盘”，因此：
- 多进程/多线程并发提交不会丢更新、不会写到一半；
- expected_version 乐观锁让“基于同一旧版本的并发演进”只有一个成功，
  其余得到可区分的 VERSION_CONFLICT。

判定依据会以 reasons / explanation 字段回传，单测可直接断言。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .errors import (
    DuplicateEntryError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from .evolve import apply_version
from .lineage import downstream_closure, edge_key, ensure_acyclic
from .models import (
    Actor,
    BatchItem,
    LineageSpec,
    OwnerTransfer,
    TableDef,
    TableVersionRecord,
    TableVersionSpec,
    TagUpdate,
    TermSpec,
)
from .permissions import is_privileged, require_owner_or_role
from .storage import CatalogStore

log = logging.getLogger("catalog.service")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _model_dump(model) -> dict:
    return model.model_dump(mode="json")


class CatalogService:
    def __init__(self, store: CatalogStore):
        self.store = store

    # ================= 表登记与演进 =================

    def register_table(self, actor: Actor, table: TableDef) -> dict:
        log.info("register_table input actor=%s table=%s", actor.user, table.model_dump())
        with self.store.transaction() as data:
            if table.id in data["tables"]:
                existing = data["tables"][table.id]
                raise DuplicateEntryError(
                    f"table {table.id!r} already registered",
                    details={"table_id": table.id, "registered_at": existing.get("created_at")},
                )
            ts = _now()
            record = TableDef(
                id=table.id,
                name=table.name,
                owner=table.owner or actor.user,
                current_version=0,
                tags=list(dict.fromkeys(table.tags)),
                created_at=ts,
                updated_at=ts,
            )
            data["tables"][table.id] = _model_dump(record)
            data["versions"][table.id] = []
            result = {
                "table": _model_dump(record),
                "status": "registered",
                "reasons": [f"new table registered by {actor.user}"],
            }
        log.info("register_table decision %s status=registered", table.id)
        return result

    def submit_version(self, actor: Actor, table_id: str, spec: TableVersionSpec) -> dict:
        log.info(
            "submit_version input actor=%s table=%s fields=%s renames=%s drops=%s expected=%s",
            actor.user, table_id, [f.name for f in spec.fields],
            [f"{r.from_name}->{r.to_name}" for r in spec.renames],
            [d.name for d in spec.drops], spec.expected_version,
        )
        with self.store.transaction() as data:
            table = self._get_table_or_404(data, table_id)
            require_owner_or_role(actor, table["owner"])

            versions = data["versions"].setdefault(table_id, [])
            current_no = len(versions)
            if spec.expected_version is not None and spec.expected_version != current_no:
                raise VersionConflictError(
                    f"expected_version={spec.expected_version} but current_version={current_no}",
                    details={
                        "table_id": table_id,
                        "expected_version": spec.expected_version,
                        "current_version": current_no,
                    },
                )

            prev = TableVersionRecord(**versions[-1]) if versions else None
            new_no = current_no + 1
            new_record = apply_version(prev, spec, new_no)
            new_record.created_at = _now()
            new_record.created_by = actor.user

            versions.append(_model_dump(new_record))
            table["current_version"] = new_no
            table["updated_at"] = new_record.created_at

            result = {
                "table_id": table_id,
                "version": new_no,
                "fields": _model_dump(new_record)["fields"],
                "renames": _model_dump(new_record)["renames"],
                "drops": new_record.drops,
                "status": "accepted",
                "reasons": self._evolution_reasons(prev, spec),
            }
        log.info("submit_version decision table=%s version=%s accepted", table_id, new_no)
        return result

    @staticmethod
    def _evolution_reasons(prev, spec: TableVersionSpec) -> list[str]:
        if prev is None:
            return ["initial version; fields assigned stable ids 1..N"]
        reasons = []
        old = {f.name for f in prev.fields}
        new = {f.name for f in spec.fields}
        for r in spec.renames:
            reasons.append(f"rename declared: {r.from_name} -> {r.to_name} (type {r.type.value}, id preserved)")
        for d in spec.drops:
            reasons.append(f"drop declared: {d.name}")
        for f in spec.fields:
            if f.name not in old and f.name not in {r.to_name for r in spec.renames}:
                reasons.append(f"new field {f.name} default_value={f.default_value!r} applies to historical rows")
            elif f.name in old:
                reasons.append(f"field {f.name} kept with stable id and compatible type {f.type.value}")
        for name in sorted(old - new - {d.name for d in spec.drops} - {r.from_name for r in spec.renames}):
            reasons.append(f"field {name} removed via declared drop/rename")
        return reasons

    def get_table(self, actor: Actor, table_id: str) -> dict:
        with self.store.read() as data:
            table = self._get_table_or_404(data, table_id)
            versions = data["versions"].get(table_id, [])
            return {
                "table": table,
                "current_version": table["current_version"],
                "latest_fields": versions[-1]["fields"] if versions else [],
            }

    def get_version(self, actor: Actor, table_id: str, version: int) -> dict:
        with self.store.read() as data:
            self._get_table_or_404(data, table_id)
            versions = data["versions"].get(table_id, [])
            if version < 1 or version > len(versions):
                raise NotFoundError(
                    f"version {version} of table {table_id!r} not found",
                    details={"table_id": table_id, "version": version,
                             "current_version": len(versions)},
                )
            return {"table_id": table_id, "version": versions[version - 1]}

    def replay(self, actor: Actor, table_id: str) -> dict:
        """回放全部版本：每个版本记录不可变，可独立重放。"""
        with self.store.read() as data:
            self._get_table_or_404(data, table_id)
            versions = data["versions"].get(table_id, [])
            return {
                "table_id": table_id,
                "current_version": len(versions),
                "versions": versions,
            }

    # ================= 血缘 =================

    def add_lineage(self, actor: Actor, spec: LineageSpec) -> dict:
        u_ref = "*" if spec.upstream_field_id is None else f"f{spec.upstream_field_id}"
        d_ref = "*" if spec.downstream_field_id is None else f"f{spec.downstream_field_id}"
        log.info(
            "add_lineage input actor=%s %s.%s -> %s.%s transform=%s",
            actor.user, spec.upstream_table, u_ref,
            spec.downstream_table, d_ref, spec.transform,
        )
        with self.store.transaction() as data:
            up = self._get_table_or_404(data, spec.upstream_table)
            down = self._get_table_or_404(data, spec.downstream_table)
            require_owner_or_role(actor, down["owner"])

            field_level = (spec.upstream_field_id is not None or
                           spec.downstream_field_id is not None)
            if field_level and (spec.upstream_field_id is None or
                                spec.downstream_field_id is None):
                raise ValidationError(
                    "field-level lineage requires both upstream_field_id "
                    "and downstream_field_id",
                    details={"reason": "field_refs_must_be_paired"},
                )
            if field_level:
                self._require_field(data, spec.upstream_table, spec.upstream_field_id)
                self._require_field(data, spec.downstream_table, spec.downstream_field_id)

            # 环检测在表级图上做（任何新表级父子关系都不许成环）
            ensure_acyclic(data["edges"], spec.upstream_table, spec.downstream_table)

            key = edge_key(
                spec.upstream_table, spec.upstream_field_id,
                spec.downstream_table, spec.downstream_field_id, spec.transform,
            )
            if key in data["edges"]:
                existing = data["edges"][key]
                log.info("add_lineage decision key=%s duplicate_idempotent seq=%s",
                         key, existing["seq"])
                return {
                    "status": "duplicate",
                    "edge": existing,
                    "reasons": ["identical lineage edge already exists; resubmission is idempotent"],
                }

            seq = self.store.next_seq(data)
            edge = {
                "id": f"edge-{seq}",
                "upstream_table": spec.upstream_table,
                "upstream_field_id": spec.upstream_field_id,
                "downstream_table": spec.downstream_table,
                "downstream_field_id": spec.downstream_field_id,
                "transform": spec.transform,
                "seq": seq,
                "created_at": _now(),
                "created_by": actor.user,
            }
            data["edges"][key] = edge
            log.info("add_lineage decision key=%s created seq=%s", key, seq)
            return {
                "status": "created",
                "edge": edge,
                "reasons": [
                    f"edge {spec.upstream_table} -> {spec.downstream_table} accepted; "
                    "no cycle introduced; deterministic key prevents duplicates",
                ],
            }

    def impact(self, actor: Actor, table_id: str) -> dict:
        with self.store.read() as data:
            self._get_table_or_404(data, table_id)
            downstream, paths, cycles = downstream_closure(data["edges"], table_id)
            field_impacts: dict[str, list[dict]] = {}
            # 字段级影响：该表字段（当前版本 id）经字段边可达的下游字段
            for key, e in data["edges"].items():
                if e["upstream_table"] == table_id and e["upstream_field_id"] is not None:
                    field_impacts.setdefault(str(e["upstream_field_id"]), []).append(
                        {"table": e["downstream_table"], "field_id": e["downstream_field_id"],
                         "transform": e["transform"]}
                    )
            result = {
                "table_id": table_id,
                "downstream_tables": downstream,
                "explanations": paths,
                "cycles": cycles,
                "field_impacts": field_impacts,
                "decided_at": _now(),
                "reasons": [
                    "downstream list is reachable tables in the table-level lineage graph",
                    "ordering is lexicographic by table id; paths show propagation edges",
                    "cycles (should never be registered) are reported explicitly, never dropped",
                ],
            }
        log.info("impact decision table=%s downstream=%s cycles=%s",
                 table_id, downstream, cycles)
        return result

    def lineage_graph(self, actor: Actor) -> dict:
        with self.store.read() as data:
            edges = sorted(data["edges"].values(), key=lambda e: e["seq"])
            return {"edge_count": len(edges), "edges": edges}

    @staticmethod
    def _require_field(data: dict, table_id: str, field_id: int) -> dict:
        versions = data["versions"].get(table_id, [])
        if not versions:
            raise NotFoundError(f"table {table_id!r} has no versions",
                                details={"table_id": table_id})
        latest = versions[-1]
        for f in latest["fields"]:
            if f["id"] == field_id:
                return f
        raise NotFoundError(
            f"field id={field_id} not found in current version of table {table_id!r}",
            details={"table_id": table_id, "field_id": field_id,
                     "current_version": latest["version"]},
        )

    # ================= 标签 / 归属 / 术语 =================

    def update_tags(self, actor: Actor, table_id: str, update: TagUpdate) -> dict:
        log.info("update_tags input actor=%s table=%s add=%s remove=%s",
                 actor.user, table_id, update.add, update.remove)
        with self.store.transaction() as data:
            table = self._get_table_or_404(data, table_id)
            require_owner_or_role(actor, table["owner"])
            tags = list(table["tags"])
            known = set(tags)
            actions = []
            for t in update.add:
                if t not in known:
                    tags.append(t)
                    known.add(t)
                    actions.append(f"added tag {t!r}")
            for t in update.remove:
                if t in known:
                    tags.remove(t)
                    known.discard(t)
                    actions.append(f"removed tag {t!r}")
            table["tags"] = tags
            table["updated_at"] = _now()
            return {"table_id": table_id, "tags": tags, "actions": actions}

    def transfer_owner(self, actor: Actor, table_id: str, body: OwnerTransfer) -> dict:
        log.info("transfer_owner input actor=%s table=%s new_owner=%s",
                 actor.user, table_id, body.new_owner)
        if not body.new_owner.strip():
            raise ValidationError("new_owner must not be empty",
                                  details={"reason": "empty_owner"})
        with self.store.transaction() as data:
            table = self._get_table_or_404(data, table_id)
            require_owner_or_role(actor, table["owner"])
            old = table["owner"]
            table["owner"] = body.new_owner
            table["updated_at"] = _now()
            return {"table_id": table_id, "previous_owner": old,
                    "owner": body.new_owner,
                    "reasons": [f"ownership transferred from {old} to {body.new_owner}"]}

    def publish_term(self, actor: Actor, spec: TermSpec) -> dict:
        log.info("publish_term input actor=%s term=%s tables=%s",
                 actor.user, spec.name, spec.linked_tables)
        if not is_privileged(actor):
            raise ForbiddenError(
                f"term publication requires admin/steward role; user={actor.user}",
                details={"user": actor.user, "roles": actor.roles},
            )
        with self.store.transaction() as data:
            for t in spec.linked_tables:
                self._get_table_or_404(data, t)
            for ref in spec.linked_fields:
                self._require_field(data, ref.table, ref.field_id)
            term_id = f"term-{spec.name.strip().lower().replace(' ', '_')}"
            if term_id in data["terms"]:
                raise DuplicateEntryError(
                    f"term {spec.name!r} already published",
                    details={"term_id": term_id},
                )
            term = {
                "id": term_id,
                "name": spec.name,
                "definition": spec.definition,
                "linked_tables": list(dict.fromkeys(spec.linked_tables)),
                "linked_fields": [f.model_dump(mode="json") for f in spec.linked_fields],
                "published_by": actor.user,
                "published_at": _now(),
            }
            data["terms"][term_id] = term
            return {"status": "published", "term": term}

    def list_terms(self, actor: Actor) -> dict:
        with self.store.read() as data:
            terms = sorted(data["terms"].values(), key=lambda t: t["id"])
            return {"terms": terms, "count": len(terms)}

    # ================= 批量 / 幂等 =================

    def run_batch(self, actor: Actor, idempotency_key: str, items: list[BatchItem]) -> dict:
        log.info("run_batch input actor=%s key=%s items=%d -> %s",
                 actor.user, idempotency_key, len(items),
                 [(i.op, sorted(i.body)) for i in items])
        if not idempotency_key.strip():
            raise ValidationError("idempotency_key header is required",
                                  details={"reason": "missing_idempotency_key"})
        with self.store.transaction() as data:
            cached = data["idempotency"].get(idempotency_key)
            if cached is not None:
                # 同 key 重放：返回首次结果，绝不重复执行
                log.info("run_batch decision key=%s replayed_from_cache", idempotency_key)
                cached = dict(cached)
                cached["replayed"] = True
                return cached

            results: list[dict] = []
            try:
                for idx, item in enumerate(items):
                    outcome = self._dispatch(data, actor, item.op, item.body)
                    results.append({"index": idx, "op": item.op, "status": "ok",
                                    "result": outcome})
            except Exception as exc:  # 整批回滚：异常退出事务 => 不 persist
                log.info("run_batch decision key=%s aborted op=%s error=%s",
                         idempotency_key, item.op, getattr(exc, "code", type(exc).__name__))
                raise

            report = {
                "idempotency_key": idempotency_key,
                "results": results,
                "replayed": False,
                "reasons": [
                    "batch applied atomically; any failure rolls back the whole batch",
                    "same idempotency_key returns the original result without re-executing",
                ],
            }
            data["idempotency"][idempotency_key] = {
                "idempotency_key": idempotency_key,
                "results": results,
                "replayed": False,
            }
            log.info("run_batch decision key=%s committed items=%d",
                     idempotency_key, len(results))
            return report

    def _dispatch(self, data: dict, actor: Actor, op: str, body: dict) -> dict:
        """在已打开的事务 data 上执行单项操作（失败抛异常，整批回滚）。"""
        if op == "register_table":
            table = TableDef(**body)
            if table.id in data["tables"]:
                raise DuplicateEntryError(
                    f"table {table.id!r} already registered",
                    details={"table_id": table.id},
                )
            ts = _now()
            record = TableDef(
                id=table.id, name=table.name, owner=table.owner or actor.user,
                tags=list(dict.fromkeys(table.tags)),
                created_at=ts, updated_at=ts,
            )
            data["tables"][table.id] = _model_dump(record)
            data["versions"].setdefault(table.id, [])
            return {"table_id": table.id, "status": "registered"}

        if op == "submit_version":
            table_id = body["table_id"]
            spec = TableVersionSpec(**{k: v for k, v in body.items() if k != "table_id"})
            table = self._get_table_or_404(data, table_id)
            require_owner_or_role(actor, table["owner"])
            versions = data["versions"].setdefault(table_id, [])
            current_no = len(versions)
            if spec.expected_version is not None and spec.expected_version != current_no:
                raise VersionConflictError(
                    f"expected_version={spec.expected_version} but current_version={current_no}",
                    details={"table_id": table_id,
                             "expected_version": spec.expected_version,
                             "current_version": current_no})
            prev = TableVersionRecord(**versions[-1]) if versions else None
            new_no = current_no + 1
            record = apply_version(prev, spec, new_no)
            record.created_at = _now()
            record.created_by = actor.user
            versions.append(_model_dump(record))
            table["current_version"] = new_no
            table["updated_at"] = record.created_at
            return {"table_id": table_id, "version": new_no, "status": "accepted"}

        if op == "add_lineage":
            spec = LineageSpec(**body)
            self._get_table_or_404(data, spec.upstream_table)
            down = self._get_table_or_404(data, spec.downstream_table)
            require_owner_or_role(actor, down["owner"])
            field_level = (spec.upstream_field_id is not None or
                           spec.downstream_field_id is not None)
            if field_level and not (spec.upstream_field_id is not None and
                                    spec.downstream_field_id is not None):
                raise ValidationError("field-level lineage requires both field ids",
                                      details={"reason": "field_refs_must_be_paired"})
            if field_level:
                self._require_field(data, spec.upstream_table, spec.upstream_field_id)
                self._require_field(data, spec.downstream_table, spec.downstream_field_id)
            ensure_acyclic(data["edges"], spec.upstream_table, spec.downstream_table)
            key = edge_key(spec.upstream_table, spec.upstream_field_id,
                           spec.downstream_table, spec.downstream_field_id, spec.transform)
            if key in data["edges"]:
                return {"status": "duplicate", "edge": data["edges"][key]}
            seq = self.store.next_seq(data)
            edge = {
                "id": f"edge-{seq}",
                "upstream_table": spec.upstream_table,
                "upstream_field_id": spec.upstream_field_id,
                "downstream_table": spec.downstream_table,
                "downstream_field_id": spec.downstream_field_id,
                "transform": spec.transform, "seq": seq,
                "created_at": _now(), "created_by": actor.user,
            }
            data["edges"][key] = edge
            return {"status": "created", "edge": edge}

        raise ValidationError(f"unknown batch op {op!r}", details={"op": op})

    # ================= 工具 =================

    @staticmethod
    def _get_table_or_404(data: dict, table_id: str) -> dict:
        table = data["tables"].get(table_id)
        if table is None:
            raise NotFoundError(
                f"table {table_id!r} not found",
                details={"table_id": table_id},
            )
        return table
