"""越权区分、乐观并发、并发重复登记同一批元数据：不重复、不丢失、原因可区分。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import DuplicateEntryError, ForbiddenError, NotFoundError, VersionConflictError
from app.models import (
    Actor,
    BatchItem,
    DataFieldType as T,
    FieldSpec,
    LineageSpec,
    OwnerTransfer,
    TableDef,
    TableVersionSpec,
    TagUpdate,
    TermSpec,
)

ALICE = Actor(user="alice")
BOB = Actor(user="bob")
STEWARD = Actor(user="sue", roles=["steward"])
ADMIN = Actor(user="root", roles=["admin"])


def _table(svc, t="t", owner="alice"):
    svc.register_table(Actor(user=owner), TableDef(id=t, name=t, owner=owner))
    svc.submit_version(
        Actor(user=owner),
        t,
        TableVersionSpec(fields=[FieldSpec(name="id", type=T.INTEGER)]),
    )


# ---------------- 越权 vs 不存在 ----------------

def test_forbidden_distinct_from_not_found(service):
    _table(service)
    # 资源存在但无权
    with pytest.raises(ForbiddenError):
        service.submit_version(
            BOB, "t",
            TableVersionSpec(fields=[FieldSpec(name="id", type=T.INTEGER)]),
        )
    # 资源不存在 -> NOT_FOUND，而不是 FORBIDDEN
    with pytest.raises(NotFoundError):
        service.submit_version(
            BOB, "ghost",
            TableVersionSpec(fields=[FieldSpec(name="id", type=T.INTEGER)]),
        )


def test_only_owner_or_privileged_can_tag(service):
    _table(service)
    with pytest.raises(ForbiddenError):
        service.update_tags(BOB, "t", TagUpdate(add=["pii"]))
    service.update_tags(ALICE, "t", TagUpdate(add=["pii"]))
    service.update_tags(STEWARD, "t", TagUpdate(add=["finance"]))
    assert service.get_table(ALICE, "t")["table"]["tags"] == ["pii", "finance"]


def test_transfer_owner_changes_who_can_edit(service):
    _table(service)
    service.transfer_owner(ALICE, "t", OwnerTransfer(new_owner="bob"))
    with pytest.raises(ForbiddenError):
        service.update_tags(ALICE, "t", TagUpdate(add=["x"]))
    service.update_tags(BOB, "t", TagUpdate(add=["x"]))
    # admin 始终有权
    service.update_tags(ADMIN, "t", TagUpdate(add=["y"]))


def test_term_publication_requires_privileged_role(service):
    _table(service)
    with pytest.raises(ForbiddenError):
        service.publish_term(ALICE, TermSpec(name="Revenue", definition="income"))
    service.publish_term(STEWARD, TermSpec(name="Revenue", definition="income", linked_tables=["t"]))
    assert service.list_terms(BOB)["count"] == 1


# ---------------- 乐观并发 ----------------

def test_concurrent_evolution_one_wins_other_gets_version_conflict(service):
    _table(service)
    spec = TableVersionSpec(
        fields=[FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="age", type=T.INTEGER, default_value="0")],
        expected_version=1,
    )
    outcomes: list[Exception | dict] = []
    barrier = threading.Barrier(2)

    def submit(actor) -> dict:
        barrier.wait()
        return service.submit_version(actor, "t", spec.model_copy(deep=True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, a) for a in (ALICE, ADMIN)]
        for f in futures:
            try:
                outcomes.append(f.result())
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

    accepted = [o for o in outcomes if not isinstance(o, Exception)]
    conflicts = [o for o in outcomes if isinstance(o, VersionConflictError)]
    assert len(accepted) == 1
    assert len(conflicts) == 1
    final = service.get_table(ALICE, "t")
    assert final["current_version"] == 2
    assert len(final["latest_fields"]) == 2  # 没有写一半


def test_concurrent_register_same_table_only_one_wins(service):
    table = TableDef(id="t", name="t", owner="alice")
    outcomes: list[Exception | dict] = []
    barrier = threading.Barrier(4)

    def register() -> dict:
        barrier.wait()
        return service.register_table(ALICE, table.model_copy(deep=True))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(register) for _ in range(4)]
        for f in futures:
            try:
                outcomes.append(f.result())
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

    assert sum(not isinstance(o, Exception) for o in outcomes) == 1
    assert sum(isinstance(o, DuplicateEntryError) for o in outcomes) == 3


# ---------------- 并发重复登记同一批元数据 ----------------

def _same_batch(n_tables: int = 5) -> list[BatchItem]:
    items: list[BatchItem] = []
    for i in range(n_tables):
        tid = f"tbl{i}"
        items.append(BatchItem(op="register_table",
                               body={"id": tid, "name": tid, "owner":"alice"}))
        items.append(BatchItem(op="submit_version",
                               body={"table_id": tid,
                                     "fields": [{"name": "id", "type": "integer"}]}))
    for i in range(n_tables - 1):
        items.append(BatchItem(op="add_lineage",
                               body={"upstream_table": f"tbl{i}",
                                     "downstream_table": f"tbl{i+1}"}))
    return items


def test_concurrent_same_idempotency_key_runs_once(service):
    items = _same_batch()
    n = 6
    results: list[dict | Exception] = []
    barrier = threading.Barrier(n)

    def call() -> dict:
        barrier.wait()
        return service.run_batch(ALICE, "shared-key", items)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(call) for _ in range(n)]
        for f in futures:
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

    assert not any(isinstance(r, Exception) for r in results), results
    committed = [r for r in results if not r["replayed"]]
    replayed = [r for r in results if r["replayed"]]
    assert len(committed) == 1
    assert len(replayed) == n - 1

    graph = service.lineage_graph(ALICE)
    # 不重复边
    assert graph["edge_count"] == 4
    assert len({(e["upstream_table"], e["downstream_table"]) for e in graph["edges"]}) == 4
    # 不丢表
    for i in range(5):
        assert service.get_table(ALICE, f"tbl{i}")["current_version"] == 1


def test_concurrent_different_keys_same_payload_distinguishable_failure(service):
    """不同幂等键并发提交同一批：一个成功，其余以 DUPLICATE_ENTRY 失败且整批回滚。"""
    items = _same_batch(n_tables=3)
    n = 4
    results: list[dict | Exception] = []
    barrier = threading.Barrier(n)

    def call(idx) -> dict:
        barrier.wait()
        return service.run_batch(ALICE, f"key-{idx}", items)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(call, i) for i in range(n)]
        for f in futures:
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

    ok = [r for r in results if not isinstance(r, Exception)]
    dup = [r for r in results if isinstance(r, DuplicateEntryError)]
    assert len(ok) == 1
    assert len(dup) == n - 1
    # 失败原因可区分
    assert all(r.code == "DUPLICATE_ENTRY" for r in dup)
    graph = service.lineage_graph(ALICE)
    # 失败批次回滚，没有残留半截边（只有成功批次的 2 条）
    assert graph["edge_count"] == 2


def test_failed_batch_rolls_back_everything(service):
    items = [
        BatchItem(op="register_table", body={"id": "x1", "name": "x1", "owner":"alice"}),
        BatchItem(op="add_lineage",
                  body={"upstream_table": "x1", "downstream_table": "missing"}),
    ]
    with pytest.raises(NotFoundError):
        service.run_batch(ALICE, "rollback-key", items)
    with pytest.raises(NotFoundError):
        service.get_table(ALICE, "x1")  # 第一项也被回滚
