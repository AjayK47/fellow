"""Document and CloudEvent API coverage."""

from __future__ import annotations


def test_document_lifecycle_is_versioned_idempotent_and_observable(api_client):
    payload = {
        "organization_id": "org_acme",
        "external_id": "doc:runbook",
        "content": "The export rollback runbook is approved by the data platform team.",
        "scope_id": "scope_org_public",
        "entity_ids": ["service_export_worker"],
    }
    created = api_client.post("/v1/documents", json=payload, headers={"Idempotency-Key": "doc-create-1"})
    assert created.status_code == 200
    first = created.json()
    assert first["job"]["status"] == "completed"
    assert first["job"]["stages"]["indexed"] == "completed"

    replay = api_client.post("/v1/documents", json=payload, headers={"Idempotency-Key": "doc-create-1"})
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["source"]["id"] == first["source"]["id"]

    status = api_client.get(f"/v1/documents/{first['source']['id']}/status")
    assert status.status_code == 200
    assert status.json()["jobs"][0]["id"] == first["job"]["id"]

    changed = {**payload, "content": "The export rollback runbook now requires an owner approval."}
    update = api_client.put("/v1/documents/doc:runbook", json=changed)
    assert update.status_code == 200
    assert update.json()["source"]["version"] == 2

    deleted = api_client.delete(f"/v1/documents/{first['source']['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["source"]["revoked"] is True


def test_cloudevents_are_idempotent_and_can_be_batched(api_client):
    event = {
        "specversion": "1.0",
        "id": "evt_export_rollback_1",
        "source": "urn:customer:harness",
        "type": "com.companybrain.task.action.completed.v1",
        "organizationid": "org_acme",
        "principalid": "person_ajay",
        "scopeid": "scope_org_public",
        "data": {"action": "rollback_deployment", "result": "success", "target_entity_id": "service_export_worker"},
    }
    first = api_client.post("/v1/events", json=event)
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    duplicate = api_client.post("/v1/events", json=event)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"

    another = {**event, "id": "evt_export_rollback_2"}
    batch = api_client.post("/v1/events:batch", json=[event, another])
    assert batch.status_code == 200
    assert batch.json()["count"] == 2
    assert batch.json()["results"][0]["status"] == "duplicate"
    assert batch.json()["results"][1]["accepted"] is True
