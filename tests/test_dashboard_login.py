"""Sign-in-with-Slack dashboard login: /slack/login, /v1/slack/oidc/callback,
/v1/session, /v1/session/logout."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from toda.config import settings


@pytest.fixture(autouse=True)
def signed_out(api_client):
    """This module's subject is the login lifecycle itself, so it starts
    from a genuinely signed-out client -- the shared api_client fixture
    arrives pre-authenticated for the tenant-guarded data routes."""
    api_client.cookies.clear()
    return api_client


class FakeSlackResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_session_requires_login_by_default(api_client):
    resp = api_client.get("/v1/session")
    assert resp.status_code == 401


def test_session_dev_bypass_returns_synthetic_session(api_client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "dashboard_auth_disabled", True)
    resp = api_client.get("/v1/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["organization_id"] == settings.default_org_id
    assert body["auth_disabled"] is True


def test_local_admin_token_creates_the_normal_dashboard_session(
    api_client, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "local_admin_token", "a-strong-local-token")
    methods = api_client.get("/v1/auth/methods")
    assert methods.status_code == 200
    assert methods.json()["local_token"] is True

    denied = api_client.post("/v1/session/local", json={"token": "wrong"})
    assert denied.status_code == 401
    accepted = api_client.post(
        "/v1/session/local", json={"token": "a-strong-local-token"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["organization_id"] == settings.default_org_id
    assert api_client.get("/v1/session").status_code == 200


def test_local_admin_uses_the_installed_organization(
    api_client, monkeypatch: pytest.MonkeyPatch
):
    brain = api_client.app.state.brain
    for organization in brain.store.list_organizations():
        brain.retention.delete_organization(
            organization["id"], confirmation=organization["id"]
        )
    brain.store.put_organization({"id": "org_forechain", "name": "Forechain AI"})
    monkeypatch.setattr(settings, "local_admin_token", "local-token")

    accepted = api_client.post("/v1/session/local", json={"token": "local-token"})

    assert accepted.status_code == 200
    assert accepted.json()["organization_id"] == "org_forechain"
    assert accepted.json()["workspace_name"] == "Forechain AI"


def test_slack_login_requires_configuration(api_client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "slack_dashboard_login_enabled", True)
    monkeypatch.setattr(settings, "slack_client_id", None)
    resp = api_client.get("/slack/login", follow_redirects=False)
    assert resp.status_code == 503


def test_slack_login_redirects_to_slack_authorize(api_client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "slack_dashboard_login_enabled", True)
    monkeypatch.setattr(settings, "slack_client_id", "test-client-id")
    monkeypatch.setattr(settings, "public_url", "https://gateway.example.com")
    resp = api_client.get("/slack/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://slack.com/openid/connect/authorize")
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["test-client-id"]
    assert query["scope"] == ["openid email profile"]
    assert query["state"][0]


def _do_login(api_client, monkeypatch: pytest.MonkeyPatch) -> str:
    """Drives /slack/login far enough to obtain a valid, session-bound
    CSRF state value for a subsequent callback call."""
    monkeypatch.setattr(settings, "slack_client_id", "test-client-id")
    monkeypatch.setattr(settings, "slack_dashboard_login_enabled", True)
    monkeypatch.setattr(settings, "slack_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "public_url", "https://gateway.example.com")
    resp = api_client.get("/slack/login", follow_redirects=False)
    location = resp.headers["location"]
    return parse_qs(urlparse(location).query)["state"][0]


def test_oidc_callback_rejects_missing_state(api_client, monkeypatch: pytest.MonkeyPatch):
    _do_login(api_client, monkeypatch)
    resp = api_client.get(
        "/v1/slack/oidc/callback", params={"code": "abc"}, follow_redirects=False
    )
    assert resp.status_code in (302, 307)
    assert "error=invalid_state" in resp.headers["location"]


def test_oidc_callback_rejects_mismatched_state(api_client, monkeypatch: pytest.MonkeyPatch):
    _do_login(api_client, monkeypatch)
    resp = api_client.get(
        "/v1/slack/oidc/callback",
        params={"code": "abc", "state": "not-the-real-state"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert "error=invalid_state" in resp.headers["location"]


def test_oidc_callback_no_installation_redirects_to_install(
    api_client, monkeypatch: pytest.MonkeyPatch
):
    state = _do_login(api_client, monkeypatch)
    monkeypatch.setattr(
        "toda.api.routes.httpx.post",
        lambda *a, **k: FakeSlackResponse({"ok": True, "access_token": "xoxe-fake"}),
    )
    monkeypatch.setattr(
        "toda.api.routes.httpx.get",
        lambda *a, **k: FakeSlackResponse(
            {
                "sub": "U_NEW",
                "https://slack.com/team_id": "T_NEVER_INSTALLED",
                "https://slack.com/team_name": "Brand New Workspace",
                "https://slack.com/user_id": "U_NEW",
                "name": "New Person",
                "email": "new@example.com",
            }
        ),
    )
    resp = api_client.get(
        "/v1/slack/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/slack/install"
    # No session was established.
    assert api_client.get("/v1/session").status_code == 401


def test_oidc_callback_success_sets_session_for_existing_installation(
    api_client, monkeypatch: pytest.MonkeyPatch
):
    brain = api_client.app.state.brain
    brain.store.upsert_slack_installation(
        {
            "id": "slack:org_acme:T_LOGIN",
            "organization_id": "org_acme",
            "workspace_id": "T_LOGIN",
            "workspace_name": "Acme Corp",
            "bot_user_id": "U_BOT",
            "bot_token": "xoxb-existing",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    state = _do_login(api_client, monkeypatch)
    monkeypatch.setattr(
        "toda.api.routes.httpx.post",
        lambda *a, **k: FakeSlackResponse({"ok": True, "access_token": "xoxe-fake"}),
    )
    monkeypatch.setattr(
        "toda.api.routes.httpx.get",
        lambda *a, **k: FakeSlackResponse(
            {
                "sub": "U_MEMBER",
                "https://slack.com/team_id": "T_LOGIN",
                "https://slack.com/team_name": "Acme Corp",
                "https://slack.com/user_id": "U_MEMBER",
                "name": "A Member",
                "email": "member@acme.example.com",
            }
        ),
    )
    resp = api_client.get(
        "/v1/slack/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/"

    session = api_client.get("/v1/session")
    assert session.status_code == 200
    body = session.json()
    assert body["organization_id"] == "org_acme"
    assert body["principal_id"] == "person_slack_t_login_u_member"
    assert body["workspace_name"] == "Acme Corp"
    assert body["auth_disabled"] is False

    # Logging out clears it.
    logout = api_client.post("/v1/session/logout")
    assert logout.status_code == 200
    assert api_client.get("/v1/session").status_code == 401
