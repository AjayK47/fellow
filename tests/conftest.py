"""Pytest fixtures using isolated temp DBs."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import itsdangerous
import pytest
from fastapi.testclient import TestClient

from toda.fixtures.seed import seed_synthetic_org
from toda.services.container import TodaApp

# Every /v1 data route is tenant-guarded (api/routes.py:enforce_tenant), so
# an HTTP test has to be signed in the same way the console is. Forging the
# real signed cookie rather than disabling auth keeps the guard on the path
# these tests exercise -- otherwise the ~40 HTTP tests would all silently
# run with enforcement switched off.
DASHBOARD_SESSION_SECRET = "test-dashboard-session-secret"


def sign_in(
    client: TestClient,
    *,
    organization_id: str = "org_acme",
    principal_id: str = "person_ajay",
    role: str = "owner",
) -> None:
    """Create a revocable dashboard session and sign its opaque cookie.

    `role` matters: owner/admin bypasses the Session participation boundary
    and may perform any mutation, so a test that signs in with the default
    exercises none of that. Pass role="member" to test as an ordinary user.
    """
    from toda.services.dashboard_auth import DashboardAuthService

    store = client.app.state.brain.store
    auth = DashboardAuthService(store)
    user = store.dashboard_user_by_email("tests@toda.local")
    if not user:
        user = auth.create_owner(
            organization_id=organization_id,
            email="tests@toda.local",
            name="Test owner",
            password="test-password-strong",
        )
    member = store.organization_member(organization_id, user["id"])
    if not member:
        member = {
            "organization_id": organization_id,
            "user_id": user["id"],
            "principal_id": principal_id,
            "role": role,
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    store.upsert_organization_member(
        {**member, "principal_id": principal_id, "role": role}
    )
    session = auth.create_session(organization_id, user["id"])
    payload = base64.b64encode(
        json.dumps({"dashboard_session_id": session["token"]}).encode()
    )
    signed = itsdangerous.TimestampSigner(DASHBOARD_SESSION_SECRET).sign(payload).decode()
    client.cookies.set("toda_session", signed)


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Tests never call configured external inference providers.

    The developer's real config.json is loaded at import time, so without
    this every test that builds the container would make live billable calls
    against whatever credentials that file holds. Replacing whole sections
    with their defaults is what makes this airtight: the credential settings
    are dicts, so clearing individual keys leaves the configured connections
    in place.

    This also covers ``enforce_tenant`` and the ingest guard, which read the
    module-level singleton directly -- previously a developer with
    dashboard auth disabled in their own environment ran the entire HTTP
    suite with tenant enforcement silently switched off.
    """
    from toda.config import (
        ComposioConfig,
        MediaConfig,
        ModelsConfig,
        OrganizationConfig,
        ServerConfig,
        SlackConfig,
        ToolsConfig,
        settings as _settings,
    )

    # The fixtures seed "org_acme", and SlackGateway falls back to the
    # configured organization for any workspace with no stored installation
    # (harness/slack.py). Without pinning this, every ambient-Slack test
    # silently files its evidence under whatever organization the developer
    # happens to have in config.json and then fails looking for it.
    monkeypatch.setattr(_settings, "organization", OrganizationConfig())
    monkeypatch.setattr(_settings, "models", ModelsConfig())
    monkeypatch.setattr(_settings, "slack", SlackConfig())
    monkeypatch.setattr(_settings, "composio", ComposioConfig())
    monkeypatch.setattr(_settings, "tools", ToolsConfig())
    monkeypatch.setattr(_settings, "media", MediaConfig())
    monkeypatch.setattr(
        _settings, "server", ServerConfig(host="127.0.0.1", port=8080)
    )
    # Tenant enforcement and write approval must stay on for the tests that
    # exercise them, whatever the developer's own config says.
    monkeypatch.setattr(_settings.dashboard, "auth_disabled", False)
    monkeypatch.setattr(_settings.dashboard, "auto_approve_writes", False)
    # Pin the cookie-signing key so sign_in() can forge the real session
    # cookie (create_app would otherwise generate a random per-process one).
    monkeypatch.setattr(_settings.dashboard, "session_secret", DASHBOARD_SESSION_SECRET)


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh seeded API client available to all HTTP integration tests."""
    db = tmp_path / "api_brain.db"
    monkeypatch.setenv("TODA_DB", str(db))
    brain = TodaApp(db_path=db)
    seed_synthetic_org(brain)
    brain.close()

    from toda.main import create_app

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = TodaApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as client:
        sign_in(client)
        yield client


@pytest.fixture
def brain(tmp_path) -> TodaApp:
    db = tmp_path / "test_brain.db"
    app = TodaApp(db_path=db)
    seed_synthetic_org(app)
    yield app
    app.close()


@pytest.fixture
def empty_brain(tmp_path) -> TodaApp:
    db = tmp_path / "empty_brain.db"
    app = TodaApp(db_path=db)
    yield app
    app.close()


@pytest.fixture
def client_authz(tmp_path: Path):
    """Seeded API client plus private memory/source ids for authz HTTP tests."""
    db = tmp_path / "authz_api.db"
    brain = TodaApp(db_path=db)
    seed_synthetic_org(brain)
    private_mems = [
        m
        for m in brain.store.list_memories("org_acme", latest_only=False)
        if m.get("scope_id") == "scope_channel_leadership_private" or m.get("is_private")
    ]
    private_sources = [
        s
        for s in brain.store.list_sources("org_acme", include_revoked=True)
        if s.get("is_private_channel") or s.get("scope_id") == "scope_channel_leadership_private"
    ]
    assert private_mems and private_sources
    mem_id = private_mems[0]["id"]
    src_id = private_sources[0]["id"]
    brain.close()

    from toda.main import create_app

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = TodaApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as c:
        sign_in(c)
        yield c, mem_id, src_id
