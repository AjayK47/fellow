"""HTTP requests emit request IDs and basic local metrics."""

from __future__ import annotations


def test_request_ids_and_metrics(api_client):
    response = api_client.get("/health", headers={"X-Request-ID": "req_test"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_test"
    metrics = api_client.get("/v1/metrics", params={"organization_id": "org_acme"})
    assert metrics.status_code == 200
    assert metrics.json()["requests"] >= 1
    assert metrics.json()["sources"] >= 1


def test_metrics_report_knowledge_lane_queue_age_and_episode_sizes(api_client):
    """Queue-age and episode-size visibility (memory ingestion/retrieval
    plan §7 operational hardening) -- a knowledge-lane backlog or a runaway
    episode should be readable from the same metrics surface as everything
    else, not something an operator has to query the database to notice."""
    brain = api_client.app.state.brain
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="knowledge.index_evidence",
        payload={"source_id": "does_not_matter"},
        idempotency_key="metrics-test:knowledge-queue-age",
    )
    brain.store.upsert_episode(
        {
            "id": "episode_metrics_test",
            "organization_id": "org_acme",
            "group_key": "metrics-test-group",
            "group_kind": "custom",
            "source_system": "slack",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:00:00Z",
            "last_source_id": None,
            "event_count": 5,
            "status": "quiet",
            "summary": None,
            "summary_embedding": None,
            "extraction_revision": 1,
            "scope_id": "scope_org_public",
            "privacy_class": "internal",
            "error": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )

    metrics = api_client.get("/v1/metrics", params={"organization_id": "org_acme"}).json()

    assert "knowledge.index_evidence" in metrics["harness_job_queue_age_seconds"]
    assert metrics["harness_job_queue_age_seconds"]["knowledge.index_evidence"] >= 0
    assert metrics["episodes"]["quiet"]["count"] >= 1
    assert metrics["episodes"]["quiet"]["max_events"] >= 5
