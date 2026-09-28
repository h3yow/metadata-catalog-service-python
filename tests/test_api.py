"""End-to-end HTTP surface: headers, error envelope, full evolution/lineage flow."""
from __future__ import annotations

import logging

from tests.conftest import logged

log = logging.getLogger("test.api")


def _table(client, name, owner="alice", fields=None):
    fields = fields or [{"name": "id", "data_type": "int64", "nullable": False}]
    r = client.post("/api/v1/tables", json={"name": name, "owner": owner, "fields": fields},
                    headers={"X-Principal-User": owner})
    assert r.status_code == 200, r.text
    return r.json()


def test_error_envelope_distinguishes_401_403_404(client):
    _table(client, "t1")
    cases = [
        ("not-found", "/api/v1/tables/nope/tags", "bob", 404, "TABLE_NOT_FOUND"),
        ("forbidden", "/api/v1/tables/t1/tags", "bob", 403, "FORBIDDEN"),
        ("anonymous", "/api/v1/tables/t1/tags", None, 401, "UNAUTHENTICATED"),
    ]
    for label, path, user, status, code in cases:
        hdr = {"X-Principal-User": user} if user else {}
        r = client.put(path, json={"key": "k"}, headers=hdr)
        logged(label, status=r.status_code, body=r.json())
        assert r.status_code == status
        assert r.json()["error"]["code"] == code


def test_full_evolution_then_impact_flow(client):
    _table(client, "ods_orders", fields=[
        {"name": "order_id", "data_type": "int32", "nullable": False}])
    # reject: new column without default semantic
    r = client.post("/api/v1/tables", headers={"X-Principal-User": "alice"}, json={
        "name": "ods_orders", "owner": "alice", "expected_revision": 1,
        "fields": [{"name": "order_id", "data_type": "int32", "nullable": False},
                   {"name": "amount", "data_type": "int64", "nullable": True}]})
    logged("missing-default", body=r.json())
    assert r.status_code == 422 and r.json()["error"]["code"] == "MISSING_DEFAULT_SEMANTIC"

    # accept: widen type + add column with a stable default
    r = client.post("/api/v1/tables", headers={"X-Principal-User": "alice"}, json={
        "name": "ods_orders", "owner": "alice", "expected_revision": 1,
        "fields": [{"name": "order_id", "data_type": "int64", "nullable": False},
                   {"name": "amount", "data_type": "int64", "nullable": True,
                    "default_semantic": "zero_value"}]})
    assert r.status_code == 200 and r.json()["version"]["version"] == 2

    _table(client, "dwd_orders", fields=[
        {"name": "order_id", "data_type": "int64", "nullable": False},
        {"name": "amount", "data_type": "int64", "nullable": True,
         "default_semantic": "zero_value"}])
    r = client.post("/api/v1/lineage/edges", json={
        "upstream": {"table": "ods_orders", "field": "amount"},
        "downstream": {"table": "dwd_orders", "field": "amount"}})
    assert r.status_code == 200
    r = client.get("/api/v1/lineage/downstream/ods_orders")
    logged("impact", nodes=r.json()["nodes"])
    assert [(n["table"], n["field"]) for n in r.json()["nodes"]] == [
        ("dwd_orders", "amount")]

    # versions remain replayable
    r = client.get("/api/v1/tables/ods_orders/versions/1")
    assert [f["data_type"] for f in r.json()["fields"]] == ["int32"]


def test_cycle_end_to_end_and_version_guard(client):
    for n in ("a", "b"):
        _table(client, n)
    client.post("/api/v1/lineage/edges", json={
        "upstream": {"table": "a", "field": "id"},
        "downstream": {"table": "b", "field": "id"}})
    r = client.post("/api/v1/lineage/edges", json={
        "upstream": {"table": "b", "field": "id"},
        "downstream": {"table": "a", "field": "id"}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CYCLE_DETECTED"
    r2 = client.post("/api/v1/lineage/edges", json={
        "upstream": {"table": "b", "field": "id"},
        "downstream": {"table": "a", "field": "id"}, "allow_cycle": True})
    assert r2.status_code == 200


def test_health_reports_event_seq(client):
    _table(client, "t1")
    body = client.get("/health").json()
    logged("health", body=body)
    assert body["status"] == "ok" and body["event_seq"] >= 1
