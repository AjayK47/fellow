"""Every GET route is actually called once, as an authenticated user.

test_api_tenancy already lists /v1/sessions, and it passed for three days
while the route 500'd: it asserts 401 and 403, both of which happen inside
enforce_tenant, before the handler body ever runs. A route can be
"covered" while its code has never executed.

This sweep is deliberately shallow -- it asserts only that a handler does
not blow up. Its job is to catch the class of bug that reached production
here: brain.store.session_messages(...) called three times when the method
is named list_session_messages.
"""

from __future__ import annotations

import pytest

from toda.api.routes import router

# Path params these routes need. A route whose id has no fixture value is
# skipped rather than guessed at -- a 404 from a made-up id proves nothing.
_PATH_VALUES = {
    "organization_id": "org_acme",
    "session_id": "session_missing",
    "task_id": "task_missing",
    "artifact_id": "artifact_missing",
    "skill_id": "skill_missing",
    "entity_id": "person_ajay",
    "memory_id": "mem_missing",
    "source_id": "src_missing",
    "approval_id": "approval_missing",
    "server_id": "server_missing",
    "trigger_id": "trigger_missing",
    "automation_id": "automation_missing",
    "connected_account_id": "acct_missing",
    "user_id": "user_missing",
    "request_id": "req_missing",
    "slug": "github",
    "name": "slack_send_message",
    "version": "1",
    "full_path": "index.html",
}


def _get_routes() -> list[str]:
    paths: list[str] = []
    for route in router.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if "GET" not in methods or not path.startswith("/v1/"):
            continue
        if "{" in path:
            names = [p.split("}")[0] for p in path.split("{")[1:]]
            if any(n not in _PATH_VALUES for n in names):
                continue
            for n in names:
                path = path.replace("{" + n + "}", _PATH_VALUES[n])
        paths.append(path)
    return sorted(set(paths))


@pytest.mark.parametrize("path", _get_routes())
def test_get_route_does_not_raise(api_client, path):
    response = api_client.get(path, params={"organization_id": "org_acme"})
    assert response.status_code != 500, f"{path} -> {response.text[:300]}"


def test_sessions_list_returns_real_data(api_client):
    """The specific route that was broken, asserted on its payload."""
    response = api_client.get("/v1/sessions", params={"organization_id": "org_acme"})
    assert response.status_code == 200
    body = response.json()
    assert "sessions" in body
