"""Self-hosted dashboard identity, membership, and admission flows."""

from __future__ import annotations

from toda.services.dashboard_auth import DashboardAuthService


OWNER_EMAIL = "tests@toda.local"
OWNER_PASSWORD = "test-password-strong"


def _owner(api_client):
    auth = DashboardAuthService(api_client.app.state.brain.store)
    user = api_client.app.state.brain.store.dashboard_user_by_email(OWNER_EMAIL)
    assert user
    return auth, user


def test_session_requires_login_by_default(api_client):
    api_client.cookies.clear()
    assert api_client.get("/v1/session").status_code == 401


def test_email_login_creates_revocable_session(api_client):
    api_client.cookies.clear()
    _, user = _owner(api_client)
    response = api_client.post(
        "/v1/auth/login",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == user["id"]
    assert response.json()["role"] == "owner"
    assert api_client.get("/v1/session").status_code == 200
    assert api_client.post("/v1/session/logout").status_code == 200
    assert api_client.get("/v1/session").status_code == 401






