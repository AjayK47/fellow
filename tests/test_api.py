"""HTTP API integration tests against real FastAPI app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import sign_in
from toda.fixtures.seed import seed_synthetic_org
from toda.services.container import TodaApp


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "api_brain.db"
    monkeypatch.setenv("TODA_DB", str(db))
    brain = TodaApp(db_path=db)
    seed_synthetic_org(brain)
    brain.close()

    from toda import main as main_mod

    app = main_mod.create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = TodaApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as c:
        sign_in(c)
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_task_api_invocation_uses_session_dispatch_and_exposes_task_tree(client):
    """The operator API is Task-native from enqueue through inspection."""
    body = {
        "organization_id": "org_acme",
        "principal_id": "person_ajay",
        "conversation_id": "api-task-session",
        "destination": {
            "type": "internal_agent_session",
            "id": "api-task-session",
            "is_private": True,
        },
        "prompt": "Summarize the launch status.",
    }
    invoked = client.post("/v1/tasks/invoke", json=body)
    assert invoked.status_code == 200
    invoked_body = invoked.json()
    assert invoked_body["job"]["kind"] == "session.dispatch"
    assert invoked_body["wake"]["session_id"] == invoked_body["session"]["id"]
    assert invoked_body["job"]["payload"]["session_id"] == invoked_body["session"]["id"]
    jobs = client.get(
        "/v1/harness/jobs", params={"organization_id": "org_acme"}
    ).json()["jobs"]
    assert all(job["kind"] != "session.dispatch.legacy" for job in jobs)

    # Drain through the durable Manager path. The deterministic test model
    # completes without a network call, so this also creates a root Task.
    invoked_now = client.post("/v1/tasks/invoke", json={**body, "run_now": True})
    assert invoked_now.status_code == 200
    tasks = invoked_now.json()["tasks"]
    assert tasks
    task = tasks[-1]
    task_id = task["id"]
    assert task["session_id"] == invoked_now.json()["session"]["id"]
    assert task["root_task_id"] == task_id

    listed = client.get(
        "/v1/tasks", params={"organization_id": "org_acme", "session_id": task["session_id"]}
    )
    assert listed.status_code == 200
    assert any(item["id"] == task_id for item in listed.json()["tasks"])

    detail = client.get(
        f"/v1/tasks/{task_id}", params={"organization_id": "org_acme"}
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["task"]["id"] == task_id
    assert detail_body["session"]["id"] == task["session_id"]
    assert isinstance(detail_body["attempts"], list)
    assert client.get(
        f"/v1/tasks/{task_id}/attempts", params={"organization_id": "org_acme"}
    ).status_code == 200
    assert client.get(
        f"/v1/tasks/{task_id}/children", params={"organization_id": "org_acme"}
    ).status_code == 200
    assert client.get(
        f"/v1/tasks/{task_id}/artifacts", params={"organization_id": "org_acme"}
    ).status_code == 200


def test_installation_rejects_a_second_organization(client):
    created = client.post(
        "/v1/organizations",
        json={"id": "org_newco", "name": "NewCo", "region": "ap-south-1"},
    )
    assert created.status_code == 409
    listed = client.get("/v1/organizations")
    assert listed.status_code == 200
    assert {"org_acme"} == {
        item["id"] for item in listed.json()["organizations"]
    }


def test_organization_about_can_be_set_and_is_surfaced_in_context(client):
    updated = client.patch(
        "/v1/organizations/org_acme", json={"about": "NewCo sells widgets."}
    )
    assert updated.status_code == 200
    assert updated.json()["about"] == "NewCo sells widgets."

    missing = client.patch(
        "/v1/organizations/org_does_not_exist", json={"about": "x"}
    )
    assert missing.status_code == 404

    fetched = client.get("/v1/organizations").json()["organizations"]
    assert next(o for o in fetched if o["id"] == "org_acme")["about"] == (
        "NewCo sells widgets."
    )




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
            "project_id": "project_toda",
            "acting_principal_id": "person_ajay",
            "destination": dest,
        },
    )
    assert proj.status_code == 200
    assert proj.json()["project"]["id"] == "project_toda"


def test_ingest_and_proactive_via_api(client):
    r = client.post(
        "/v1/ingest",
        json={
            "organization_id": "org_acme",
            "source_system": "api",
            "source_type": "message",
            "external_id": "api_test_1",
            "content": "The launch date is 20 September for Toda.",
            "scope_id": "scope_org_public",
            "author_entity_id": "person_ajay",
            "entity_ids": ["project_toda"],
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


def test_gateway_lifespan_uses_prebuilt_brain_and_stops_slack_first(tmp_path: Path):
    """H-03: `gateway` must share one TodaApp instance between the API and
    Slack Socket Mode, and must stop the Slack transport before the app's
    own database connection is closed on shutdown."""
    from toda.main import create_app

    db = tmp_path / "gateway_shared.db"
    brain = TodaApp(db_path=db)
    seed_synthetic_org(brain)

    stop_calls: list[str] = []

    def slack_stopper() -> None:
        stop_calls.append("stopped")
        # The brain must still be open and usable when Slack is told to stop
        # — stopping Slack must happen before the database is closed.
        assert brain.store.get_organization("org_acme") is not None

    app = create_app(prebuilt_brain=brain, slack_stopper=slack_stopper)
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.app.state.brain is brain
        response = client.get("/health")
        assert response.status_code == 200

    assert stop_calls == ["stopped"]


def test_network_runtime_rejects_disabled_auth(monkeypatch):
    from toda.config import settings
    from toda.main import validate_runtime_exposure

    monkeypatch.setattr(settings.server, "host", "0.0.0.0")
    monkeypatch.setattr(settings.dashboard, "auth_disabled", True)
    monkeypatch.setattr(settings.dashboard, "auto_approve_writes", False)
    with pytest.raises(RuntimeError, match="dashboard.auth_disabled"):
        validate_runtime_exposure()


def test_network_runtime_rejects_auto_approved_writes(monkeypatch):
    from toda.config import settings
    from toda.main import validate_runtime_exposure

    monkeypatch.setattr(settings.server, "host", "0.0.0.0")
    monkeypatch.setattr(settings.dashboard, "auth_disabled", False)
    monkeypatch.setattr(settings.dashboard, "auto_approve_writes", True)
    with pytest.raises(RuntimeError, match="dashboard.auto_approve_writes"):
        validate_runtime_exposure()
