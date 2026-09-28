"""血缘：幂等登记、重启不重复/不漏边、成环标记、影响分析稳定可解释。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import CyclicLineageError, NotFoundError
from app.models import (
    Actor,
    DataFieldType as T,
    FieldSpec,
    LineageSpec,
    TableDef,
    TableVersionSpec,
)
from app.service import CatalogService
from app.storage import CatalogStore

ALICE = Actor(user="alice")


def _table(svc, table_id: str, owner="alice"):
    svc.register_table(Actor(user=owner), TableDef(id=table_id, name=table_id, owner=owner))
    svc.submit_version(
        Actor(user=owner),
        table_id,
        TableVersionSpec(
            fields=[
                FieldSpec(name="id", type=T.INTEGER),
                FieldSpec(name="v", type=T.STRING, default_value=""),
            ]
        ),
    )


def test_duplicate_lineage_submission_is_idempotent(service):
    _table(service, "a")
    _table(service, "b")
    r1 = service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    r2 = service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    assert r1["status"] == "created"
    assert r2["status"] == "duplicate"
    assert service.lineage_graph(ALICE)["edge_count"] == 1


def test_edges_survive_restart_without_duplication_or_loss(tmp_db: Path):
    svc1 = CatalogService(CatalogStore(str(tmp_db)))
    _table(svc1, "a")
    _table(svc1, "b")
    svc1.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))

    # 模拟服务重启：新实例从同一文件加载
    svc2 = CatalogService(CatalogStore(str(tmp_db)))
    assert svc2.lineage_graph(ALICE)["edge_count"] == 1
    # 重启后重复提交仍然幂等
    r = svc2.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    assert r["status"] == "duplicate"
    assert svc2.lineage_graph(ALICE)["edge_count"] == 1


def test_cycle_is_rejected_and_marked(service):
    for t in ("a", "b", "c"):
        _table(service, t)
    service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    service.add_lineage(ALICE, LineageSpec(upstream_table="b", downstream_table="c"))
    with pytest.raises(CyclicLineageError) as ei:
        service.add_lineage(ALICE, LineageSpec(upstream_table="c", downstream_table="a"))
    cycle = ei.value.details["cycle"]
    assert cycle[0] == cycle[-1] == "c"
    assert set(cycle) == {"a", "b", "c"}
    # 被拒绝的边没有落库
    assert service.lineage_graph(ALICE)["edge_count"] == 2


def test_self_loop_rejected(service):
    _table(service, "a")
    with pytest.raises(CyclicLineageError):
        service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="a"))


def test_impact_is_stable_and_explainable(service):
    for t in ("a", "b", "c", "d"):
        _table(service, t)
    service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="c"))
    service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    service.add_lineage(ALICE, LineageSpec(upstream_table="b", downstream_table="d"))
    service.add_lineage(ALICE, LineageSpec(upstream_table="c", downstream_table="d"))

    r1 = service.impact(ALICE, "a")
    r2 = service.impact(ALICE, "a")
    # 结论稳定：多次查询一致，且按表 id 字典序
    assert r1["downstream_tables"] == ["b", "c", "d"]
    assert r1["downstream_tables"] == r2["downstream_tables"]
    # 每个下游都有解释路径
    assert r1["explanations"]["d"][0] == "a"
    assert r1["explanations"]["d"][-1] == "d"
    assert r1["cycles"] == []


def test_impact_from_different_entry_points_is_consistent(service):
    """不同入口下影响结论在共享子图上保持一致、可解释。"""
    for t in ("a", "b", "c"):
        _table(service, t)
    service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="b"))
    service.add_lineage(ALICE, LineageSpec(upstream_table="b", downstream_table="c"))
    assert service.impact(ALICE, "a")["downstream_tables"] == ["b", "c"]
    assert service.impact(ALICE, "b")["downstream_tables"] == ["c"]
    assert service.impact(ALICE, "c")["downstream_tables"] == []


def test_field_level_lineage_and_impact(service):
    _table(service, "a")
    _table(service, "b")
    r = service.add_lineage(
        ALICE,
        LineageSpec(upstream_table="a", upstream_field_id=1,
                    downstream_table="b", downstream_field_id=2),
    )
    assert r["status"] == "created"
    impact = service.impact(ALICE, "a")
    assert impact["field_impacts"]["1"] == [
        {"table": "b", "field_id": 2, "transform": "direct"}
    ]


def test_field_level_requires_paired_refs(service):
    _table(service, "a")
    _table(service, "b")
    with pytest.raises(Exception) as ei:
        service.add_lineage(
            ALICE,
            LineageSpec(upstream_table="a", upstream_field_id=1,
                        downstream_table="b"),
        )
    assert ei.value.code == "VALIDATION_ERROR"


def test_lineage_unknown_table_is_not_found(service):
    _table(service, "a")
    with pytest.raises(NotFoundError):
        service.add_lineage(ALICE, LineageSpec(upstream_table="a", downstream_table="ghost"))


def test_rename_does_not_break_field_lineage(service):
    _table(service, "a")
    _table(service, "b")
    service.add_lineage(
        ALICE,
        LineageSpec(upstream_table="a", upstream_field_id=2,
                    downstream_table="b", downstream_field_id=2),
    )
    # a.v 改名为 a.value，字段 id 仍是 2，血缘仍然有效
    service.submit_version(
        ALICE,
        "a",
        TableVersionSpec(
            fields=[FieldSpec(name="id", type=T.INTEGER),
                    FieldSpec(name="value", type=T.STRING, default_value="")],
            renames=[{"from_name": "v", "to_name": "value", "type": "string"}],
        ),
    )
    impact = service.impact(ALICE, "a")
    assert impact["field_impacts"]["2"][0]["field_id"] == 2
