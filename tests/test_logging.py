"""单测日志必须打印输入与判定依据（可观测、可解释）。"""

from __future__ import annotations

import logging

from app.models import (
    Actor,
    DataFieldType as T,
    FieldSpec,
    LineageSpec,
    TableDef,
    TableVersionSpec,
)


def test_submit_version_logs_input_and_decision(service, caplog):
    actor = Actor(user="alice")
    service.register_table(actor, TableDef(id="t", name="t", owner="alice"))
    with caplog.at_level(logging.INFO, logger="catalog.service"):
        service.submit_version(
            actor,
            "t",
            TableVersionSpec(fields=[FieldSpec(name="id", type=T.INTEGER)]),
        )
    text = "\n".join(r.getMessage() for r in caplog.records)
    # 输入
    assert "submit_version input" in text
    assert "actor=alice" in text
    assert "table=t" in text
    assert "fields=['id']" in text
    # 判定依据
    assert "submit_version decision" in text
    assert "version=1 accepted" in text


def test_lineage_and_impact_log_input_and_reasons(service, caplog):
    actor = Actor(user="alice")
    service.register_table(actor, TableDef(id="a", name="a", owner="alice"))
    service.register_table(actor, TableDef(id="b", name="b", owner="alice"))
    service.submit_version(actor, "a", TableVersionSpec(
        fields=[FieldSpec(name="id", type=T.INTEGER)]))
    service.submit_version(actor, "b", TableVersionSpec(
        fields=[FieldSpec(name="id", type=T.INTEGER)]))
    with caplog.at_level(logging.INFO, logger="catalog.service"):
        service.add_lineage(actor, LineageSpec(upstream_table="a", downstream_table="b"))
        service.impact(actor, "a")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "add_lineage input" in text
    assert "a.* -> b.*" in text
    assert "impact decision table=a downstream=['b']" in text
