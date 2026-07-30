"""Pytest fixtures using isolated temp DBs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from company_brain.fixtures.seed import seed_synthetic_org
from company_brain.services.container import BrainApp


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Tests never call a configured external embedding provider."""
    monkeypatch.setenv("COMPANY_BRAIN_EMBEDDING_PROVIDER", "deterministic")


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh seeded API client available to all HTTP integration tests."""
    db = tmp_path / "api_brain.db"
    monkeypatch.setenv("COMPANY_BRAIN_DB", str(db))
    brain = BrainApp(db_path=db)
    seed_synthetic_org(brain)
    brain.close()

    from company_brain.main import create_app

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = BrainApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture
def brain(tmp_path) -> BrainApp:
    db = tmp_path / "test_brain.db"
    app = BrainApp(db_path=db)
    seed_synthetic_org(app)
    yield app
    app.close()


@pytest.fixture
def empty_brain(tmp_path) -> BrainApp:
    db = tmp_path / "empty_brain.db"
    app = BrainApp(db_path=db)
    yield app
    app.close()


@pytest.fixture
def client_authz(tmp_path: Path):
    """Seeded API client plus private memory/source ids for authz HTTP tests."""
    db = tmp_path / "authz_api.db"
    brain = BrainApp(db_path=db)
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

    from company_brain.main import create_app

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        b = BrainApp(db_path=db)
        app.state.brain = b
        yield
        b.close()

    app.router.lifespan_context = test_lifespan
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, mem_id, src_id
