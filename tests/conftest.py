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


def sign_in(client: TestClient, *, organization_id: str = "org_acme", principal_id: str = "person_ajay") -> None:
    """Set the console login cookie SessionMiddleware would issue."""
    payload = base64.b64encode(
        json.dumps({"organization_id": organization_id, "principal_id": principal_id}).encode()
    )
    signed = itsdangerous.TimestampSigner(DASHBOARD_SESSION_SECRET).sign(payload).decode()
    client.cookies.set("toda_session", signed)


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Tests never call configured external inference providers."""
    monkeypatch.setenv("TODA_EMBEDDING_PROVIDER", "deterministic")
    monkeypatch.setenv("TODA_MODEL_PROVIDER", "deterministic")
    monkeypatch.setenv("TODA_IMAGE_PROVIDER", "disabled")
    monkeypatch.setenv("TODA_TRANSCRIPTION_PROVIDER", "disabled")
    monkeypatch.setenv("TODA_TTS_PROVIDER", "disabled")
    monkeypatch.setenv("TODA_SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("TODA_SLACK_APP_TOKEN", "")
    monkeypatch.setenv("TODA_BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setenv("TODA_FIRECRAWL_API_KEY", "")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "")
    # Pin the cookie-signing key so sign_in() can forge the real session
    # cookie (create_app would otherwise generate a random per-process one).
    from toda.config import settings as _settings

    monkeypatch.setattr(_settings, "dashboard_session_secret", DASHBOARD_SESSION_SECRET)


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
