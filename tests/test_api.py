"""HTTP API integration tests against real FastAPI app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from company_brain.fixtures.seed import seed_synthetic_org
from company_brain.services.container import BrainApp


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "api_brain.db"
    monkeypatch.setenv("COMPANY_BRAIN_DB", str(db))
    brain = BrainApp(db_path=db)
    seed_synthetic_org(brain)
    brain.close()

    from company_brain import main as main_mod

    app = main_mod.create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = BrainApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_five_reactive_operations(client):
    org = "org_acme"
    dest = {"type": "internal_agent_session", "id": "sess", "is_private": True}

    search = client.post(
        "/v1/search",
        json={
            "organization_id": org,
            "query": "launch date export",
            "acting_principal_id": "person_ajay",
            "destination": dest,
            "limit": 10,
        },
    )
    assert search.status_code == 200
    body = search.json()
    assert body["count"] >= 1
    assert body["results"][0]["memory"]["content"]
    assert body["permission_decision_id"]

    ctx = client.post(
        "/v1/context:prepare",
        json={
            "organization_id": org,
            "task": "Investigate Acme export failures",
            "acting_principal_id": "person_ajay",
            "destination": dest,
            "entity_ids": ["customer_acme"],
            "token_budget": 4000,
        },
    )
    assert ctx.status_code == 200
    cbody = ctx.json()
    assert cbody["permission_decision_id"]
    assert cbody["request_id"]
    assert isinstance(cbody["current_facts"], list)
    assert cbody["summary"]

    rel = client.post(
        "/v1/relationships:query",
        json={
            "organization_id": org,
            "from_entity_id": "person_ajay",
            "to_entity_id": "person_ravi",
            "acting_principal_id": "person_ajay",
            "destination": dest,
        },
    )
    assert rel.status_code == 200
    assert rel.json()["permission_decision_id"]

    owner = client.post(
        "/v1/owners:resolve",
        json={
            "organization_id": org,
            "issue_text": "Acme exports stuck",
            "acting_principal_id": "person_support_agent",
            "entity_ids": ["customer_acme", "service_export_worker"],
            "destination": dest,
        },
    )
    assert owner.status_code == 200
    obody = owner.json()
    assert obody["primary_owner"]
    assert obody["evidence"]

    proj = client.post(
        "/v1/projects:context",
        json={
            "organization_id": org,
            "project_id": "project_company_brain",
            "acting_principal_id": "person_ajay",
            "destination": dest,
        },
    )
    assert proj.status_code == 200
    assert proj.json()["project"]["id"] == "project_company_brain"


def test_ingest_and_proactive_via_api(client):
    r = client.post(
        "/v1/ingest",
        json={
            "organization_id": "org_acme",
            "source_system": "api",
            "source_type": "message",
            "external_id": "api_test_1",
            "content": "The launch date is 20 September for Company Brain.",
            "scope_id": "scope_org_public",
            "author_entity_id": "person_ajay",
            "entity_ids": ["project_company_brain"],
        },
    )
    assert r.status_code == 200
    assert r.json()["source"]["id"]
    assert r.json()["memories"]

    p = client.post(
        "/v1/proactive:evaluate",
        json={
            "organization_id": "org_acme",
            "trigger": {"type": "conversation.updated", "text": "When is launch?"},
            "acting_principal_id": "person_ajay",
            "destination": {"type": "internal_agent_session", "id": "s", "is_private": True},
            "mode": "dry_run",
            "candidate": {
                "signal_type": "verified_missing_answer",
                "confidence": 0.97,
                "evidence_authority": 0.95,
                "usefulness": 1.0,
                "urgency": 1.0,
                "actionability": 1.0,
                "novelty": 1.0,
                "interruption_cost": 0.05,
                "permission_safe": True,
                "content": "The launch date is 15 September.",
                "evidence": [],
            },
        },
    )
    assert p.status_code == 200
    assert p.json()["decision"]
    assert p.json()["policy_explanation"]
