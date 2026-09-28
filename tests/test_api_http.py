"""HTTP 集成：协议状态码、错误码区分、身份头、端到端影响分析。"""

from __future__ import annotations


def _table(client, t, owner="alice", roles=""):
    h = {"X-User": owner, "X-Roles": roles}
    r = client.post("/tables", headers=h, json={"id": t, "name": t, "owner": owner})
    assert r.status_code == 201, r.text
    r = client.post(
        f"/tables/{t}/versions", headers=h,
        json={"fields": [{"name": "id", "type": "integer"},
                         {"name": "v", "type": "string", "default_value": ""}]},
    )
    assert r.status_code == 201, r.text
    return h


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_error_codes_are_distinguishable(client):
    h = _table(client, "t")
    # 404 资源不存在
    r = client.post("/tables/ghost/versions", headers={"X-User": "bob"},
                    json={"fields": [{"name": "id", "type": "integer"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"

    # 403 资源存在但越权
    r = client.post("/tables/t/versions", headers={"X-User": "bob"},
                    json={"fields": [{"name": "id", "type": "integer"}]})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"

    # 409 重复登记
    r = client.post("/tables", headers=h, json={"id": "t", "name": "t", "owner": "alice"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_ENTRY"

    # 422 演进非法（隐式改名）
    r = client.post("/tables/t/versions", headers=h,
                    json={"fields": [{"name": "id", "type": "integer"},
                                     {"name": "nick", "type": "string", "default_value": ""}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ILLEGAL_RENAME"


def test_version_conflict_status(client):
    h = _table(client, "t")
    body = {"fields": [
                {"name": "id", "type": "integer"},
                {"name": "v", "type": "string", "default_value": ""},
                {"name": "age", "type": "integer", "default_value": "0"}],
            "expected_version": 1}
    assert client.post("/tables/t/versions", headers=h, json=body).status_code == 201
    r = client.post("/tables/t/versions", headers=h, json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"


def test_lineage_cycle_and_impact_end_to_end(client):
    ha = _table(client, "ods")
    _table(client, "dwd")
    _table(client, "ads")
    assert client.post("/lineage", headers=ha,
                       json={"upstream_table": "ods", "downstream_table": "dwd"}).status_code == 201
    assert client.post("/lineage", headers=ha,
                       json={"upstream_table": "dwd", "downstream_table": "ads"}).status_code == 201
    r = client.post("/lineage", headers=ha,
                    json={"upstream_table": "ads", "downstream_table": "ods"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CYCLE_DETECTED"

    r = client.get("/tables/ods/impact", headers=ha)
    body = r.json()
    assert body["downstream_tables"] == ["ads", "dwd"]
    assert body["explanations"]["ads"] == ["ods", "dwd", "ads"]
    assert body["cycles"] == []


def test_tag_owner_and_term_role_enforcement(client):
    h = _table(client, "t")
    assert client.post("/tables/t/tags", headers={"X-User": "bob"},
                       json={"add": ["pii"]}).status_code == 403
    assert client.post("/tables/t/tags", headers=h,
                       json={"add": ["pii"]}).status_code == 200

    assert client.post("/terms", headers=h,
                       json={"name": "Revenue", "definition": "d"}).status_code == 403
    r = client.post("/terms", headers={"X-User": "sue", "X-Roles": "steward"},
                    json={"name": "Revenue", "definition": "d", "linked_tables": ["t"]})
    assert r.status_code == 201


def test_batch_idempotent_over_http(client):
    h = {"X-User": "alice"}
    client.post("/tables", headers=h, json={"id": "t", "name": "t", "owner": "alice"})
    client.post("/tables/t/versions", headers=h,
                json={"fields": [{"name": "id", "type": "integer"}]})
    items = [
        {"op": "register_table", "body": {"id": "b", "name": "b", "owner": "alice"}},
        {"op": "submit_version",
         "body": {"table_id": "b", "fields": [{"name": "id", "type": "integer"}]}},
        {"op": "add_lineage", "body": {"upstream_table": "b", "downstream_table": "t"}},
    ]
    headers = {**h, "Idempotency-Key": "abc"}
    r1 = client.post("/batch", headers=headers, json=items)
    r2 = client.post("/batch", headers=headers, json=items)
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    g = client.get("/lineage", headers=h).json()
    assert g["edge_count"] == 1


def test_replay_endpoint_returns_all_versions(client):
    h = _table(client, "t")
    r = client.get("/tables/t/replay", headers=h)
    assert r.status_code == 200
    assert r.json()["current_version"] == 1
    assert [f["name"] for f in r.json()["versions"][0]["fields"]] == ["id", "v"]
