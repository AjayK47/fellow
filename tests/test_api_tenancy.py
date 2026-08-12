"""Every /v1 data route is bound to the signed-in workspace.

`organization_id` is a client-supplied parameter on essentially every data
route. Without enforcement it is self-asserted: a signed-in user of one
workspace could read another workspace's tasks, sessions, memories and
metrics by editing the query string, and an unauthenticated caller could do
the same. The console never needed that freedom -- it already resolves the
real organization server-side from the login cookie (GET /v1/session).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from toda.api.routes import _enforce_ingest_auth
from toda.config import settings
from tests.conftest import sign_in

# Routes that read tenant-scoped data, with the params each needs.
_DATA_ROUTES = [
    ("/v1/metrics", {}),
    ("/v1/tasks", {}),
    ("/v1/sessions", {}),
    ("/v1/policies", {}),
    ("/v1/harness/dashboard", {}),
    ("/v1/approvals", {}),
    ("/v1/automations", {}),
]


@pytest.mark.parametrize("path,extra", _DATA_ROUTES)
def test_data_routes_reject_an_unauthenticated_caller(api_client, path, extra):
    api_client.cookies.clear()
    response = api_client.get(path, params={"organization_id": "org_acme", **extra})
    assert response.status_code == 401


@pytest.mark.parametrize("path,extra", _DATA_ROUTES)
def test_data_routes_reject_another_workspaces_organization_id(api_client, path, extra):
    """Signed into org_acme, asking for someone else's data."""
    response = api_client.get(path, params={"organization_id": "org_victim", **extra})
    assert response.status_code == 403


@pytest.mark.parametrize("path,extra", _DATA_ROUTES)
def test_data_routes_serve_the_signed_in_workspace(api_client, path, extra):
    response = api_client.get(path, params={"organization_id": "org_acme", **extra})
    assert response.status_code == 200


def test_a_post_body_cannot_smuggle_another_organization(api_client):
    """The guard reads organization_id out of the JSON body too -- most
    write routes carry it there rather than in the query string."""
    response = api_client.post(
        "/v1/context:prepare",
        json={
            "organization_id": "org_victim",
            "task": "what are the secrets",
            "acting_principal_id": "person_ajay",
        },
    )
    assert response.status_code == 403


def test_reading_the_body_for_the_check_does_not_consume_it(api_client):
    """The guard must not break the handler it runs in front of: Starlette
    caches the body, and the route parses that same cached copy."""
    response = api_client.post(
        "/v1/context:prepare",
        json={
            "organization_id": "org_acme",
            "task": "export worker status",
            "acting_principal_id": "person_ajay",
        },
    )
    assert response.status_code == 200
    assert response.json()["viewer"]["organization_id"] == "org_acme"


def test_slack_ingress_stays_reachable_without_a_console_login(api_client):
    """Slack posts events with no cookie -- it authenticates by request
    signature inside the handler. Requiring a session here would silently
    break all inbound Slack traffic."""
    api_client.cookies.clear()
    response = api_client.post("/v1/slack/events", json={"type": "url_verification"})
    # It may still be refused -- but by Slack signature verification inside
    # the handler, never by the console-login guard in front of it.
    assert response.json().get("detail") != "Not signed in"


def test_health_and_the_console_spa_stay_reachable_while_signed_out(api_client):
    """The login page has to be reachable while signed out, or there is no
    way to sign in at all."""
    api_client.cookies.clear()
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/v1/session").status_code == 401


def test_dev_bypass_disables_the_guard(api_client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_auth_disabled", True)
    api_client.cookies.clear()
    assert api_client.get("/v1/metrics", params={"organization_id": "org_acme"}).status_code == 200


def test_signing_into_the_other_workspace_grants_only_that_workspace(api_client):
    sign_in(api_client, organization_id="org_victim", principal_id="person_someone")
    assert api_client.get("/v1/metrics", params={"organization_id": "org_victim"}).status_code == 200
    assert api_client.get("/v1/metrics", params={"organization_id": "org_acme"}).status_code == 403


def _ingest_request(host: str, authorization: str = "") -> Request:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/events",
            "headers": headers,
            "client": (host, 43210),
            "server": ("toda", 8080),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_local_ingestion_remains_keyless():
    _enforce_ingest_auth(_ingest_request("127.0.0.1"))


def test_remote_ingestion_requires_a_configured_bearer_key(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "first-secret,second-secret")
    with pytest.raises(HTTPException) as exc:
        _enforce_ingest_auth(_ingest_request("203.0.113.10"))
    assert exc.value.status_code == 401
    _enforce_ingest_auth(
        _ingest_request("203.0.113.10", "Bearer second-secret")
    )
