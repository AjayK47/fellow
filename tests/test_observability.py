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
