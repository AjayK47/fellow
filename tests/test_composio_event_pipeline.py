"""End-to-end Composio trigger ingestion for the company harness."""

from __future__ import annotations

from datetime import datetime, timezone

import base64
import hashlib
import hmac
import json
import time

from tests.conftest import sign_in


def _signature(
    secret: str, webhook_id: str, timestamp: str, body: bytes
) -> str:
    signing = (
        webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    )
    digest = hmac.new(
        secret.encode(), signing, hashlib.sha256
    ).digest()
    return "v1," + base64.b64encode(digest).decode()


def _register_github(api_client, *, account_id: str = "ca_github_acme"):
    response = api_client.post(
        "/v1/connector-accounts",
        json={
            "id": f"connector_org_acme_{account_id}",
            "organization_id": "org_acme",
            "toolkit": "github",
            "connected_account_id": account_id,
            "principal_id": "person_ajay",
            "scope_id": "scope_org_public",
            "proactivity_enabled": True,
            "learn": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _deliver(api_client, payload: dict, *, secret: str):
    body = json.dumps(payload, separators=(",", ":")).encode()
    webhook_id = "webhook_test_1"
    timestamp = str(int(time.time()))
    return api_client.post(
        "/v1/composio/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(
                secret, webhook_id, timestamp, body
            ),
        },
    )


def test_signed_trigger_becomes_retrievable_evidence_and_wakes_agent(
    api_client,
):
    secret = "composio-test-webhook-secret"
    brain = api_client.app.state.brain
    brain.composio.webhook_secret = secret
    account = _register_github(api_client)
    payload = {
        "id": "msg_export_failure_1",
        "type": "composio.trigger.message",
        "metadata": {
            "log_id": "log_export_failure_1",
            "trigger_slug": "GITHUB_COMMIT_EVENT",
            "trigger_id": "ti_github_commits",
            "connected_account_id": account["connected_account_id"],
            "user_id": account["composio_user_id"],
        },
        "data": {
            "commit_sha": "0ddf00d",
            "message": "Export worker failing for Northstar customer",
            "author": "Ajay",
            "repository": "acme/export-worker",
        },
        # Must be recent: connector events older than the attention cutoff
        # are deliberately stored without a model call, so a hardcoded past
        # date would silently stop exercising the wake path this asserts.
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    accepted = _deliver(api_client, payload, secret=secret)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted"] is True

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0

    evidence = brain.retrieval.search_evidence(
        organization_id="org_acme",
        query="Northstar export worker failure",
        acting_principal_id="person_ajay",
        destination={
            "type": "internal_agent_session",
            "id": "github",
            "is_private": False,
        },
        purpose="test_connector_retrieval",
        limit=10,
    )
    assert any(
        item["source_id"]
        and brain.store.get_source(item["source_id"])["external_id"]
        == "msg_export_failure_1"
        for item in evidence["results"]
    )
    source = next(
        brain.store.get_source(item["source_id"])
        for item in evidence["results"]
        if brain.store.get_source(item["source_id"])["external_id"]
        == "msg_export_failure_1"
    )
    assert source["source_system"] == "composio:github"
    assert source["metadata"]["connected_account_id"] == "ca_github_acme"
    assert brain.store.list_harness_records(
        "attention_evaluations",
        "org_acme",
        where={"event_id": "msg_export_failure_1"},
    )
    runs = brain.store.list_tasks("org_acme", worker_type="session_manager")
    assert any(
        run.get("trigger_event_id") == "msg_export_failure_1"
        for run in runs
    )
    assert not brain.store.list_harness_records("approvals", "org_acme")

    duplicate = _deliver(api_client, payload, secret=secret)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    matching = [
        item
        for item in brain.store.list_sources("org_acme")
        if item["external_id"] == "msg_export_failure_1"
    ]
    assert len(matching) == 1


def test_webhook_rejects_invalid_signature_and_unknown_account(api_client):
    secret = "composio-test-webhook-secret"
    brain = api_client.app.state.brain
    brain.composio.webhook_secret = secret
    _register_github(api_client)
    unknown = {
        "id": "msg_unknown_account",
        "type": "composio.trigger.message",
        "metadata": {
            "trigger_slug": "GITHUB_ISSUE_EVENT",
            "trigger_id": "ti_unknown",
            "connected_account_id": "ca_other_company",
            "user_id": "other-company",
        },
        "data": {"title": "Private issue from another tenant"},
    }
    rejected = _deliver(api_client, unknown, secret=secret)
    assert rejected.status_code == 403

    body = json.dumps(unknown).encode()
    invalid = api_client.post(
        "/v1/composio/webhooks",
        content=body,
        headers={
            "webhook-id": "bad",
            "webhook-timestamp": str(int(time.time())),
            "webhook-signature": "v1,invalid",
        },
    )
    assert invalid.status_code == 401
    assert not any(
        item["external_id"] == "msg_unknown_account"
        for item in brain.store.list_sources("org_acme")
    )


def test_agent_catalog_is_scoped_to_organization_connections(
    brain, monkeypatch
):
    brain.store.upsert_connector_account(
        {
            "id": "connector_org_acme_github",
            "organization_id": "org_acme",
            "toolkit": "github",
            "connected_account_id": "ca_github_acme",
            "status": "active",
            "principal_id": "person_ajay",
            "scope_id": "scope_org_public",
        }
    )
    calls: list[dict] = []

    def fake_search_tools(**kwargs):
        calls.append(kwargs)
        return {
            "items": [
                {
                    "slug": "GITHUB_LIST_COMMITS",
                    "name": "List commits",
                    "description": "List repository commits",
                    "toolkit": {"slug": "github"},
                },
                {
                    "slug": "GMAIL_SEND_EMAIL",
                    "name": "Send email",
                    "description": "Send Gmail",
                    "toolkit": {"slug": "gmail"},
                },
            ]
        }

    monkeypatch.setattr(brain.composio, "search_tools", fake_search_tools)
    runtime = {
        "organization_id": "org_acme",
        "principal_id": "person_ajay",
        "destination": {
            "type": "internal_agent_session",
            "id": "test",
            "is_private": True,
        },
        "purpose": "test",
        "task_id": "task_test",
    }
    listing = brain.tool_registry.get(
        "integration_list"
    ).handler({}, runtime)
    assert [item["toolkit"] for item in listing["integrations"]] == ["github"]

    result = brain.tool_registry.get("integration_search").handler(
        {"query": "commits and email"}, runtime
    )
    assert [tool["slug"] for tool in result["tools"]] == [
        "GITHUB_LIST_COMMITS"
    ]
    assert calls[0]["toolkit"] == "github"

    other_tenant = {**runtime, "organization_id": "org_not_connected"}
    isolated = brain.tool_registry.get("integration_search").handler(
        {"query": "commits"}, other_tenant
    )
    assert isolated["tools"] == []


def test_connection_sync_link_and_trigger_creation_are_org_scoped(
    api_client, monkeypatch
):
    brain = api_client.app.state.brain
    brain.composio.api_key = "test-only"
    monkeypatch.setattr(
        brain.composio,
        "list_connected_accounts",
        lambda **_kwargs: {
            "items": [
                {
                    "id": "ca_synced_github",
                    "status": "ACTIVE",
                    "toolkit": {"slug": "github"},
                }
            ]
        },
    )
    synced = api_client.post(
        "/v1/composio/connections:sync",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "scope_id": "scope_org_public",
        },
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["accounts"][0]["connected_account_id"] == (
        "ca_synced_github"
    )

    linked_args: dict = {}

    def fake_link(**kwargs):
        linked_args.update(kwargs)
        return {
            "redirect_url": "https://connect.composio.test/session",
            "link_token": "link_test",
        }

    monkeypatch.setattr(brain.composio, "create_connect_link", fake_link)
    linked = api_client.post(
        "/v1/composio/connections:link",
        json={
            "organization_id": "org_acme",
            "auth_config_id": "ac_github",
            "callback_url": "https://brain.test/integrations",
        },
    )
    assert linked.status_code == 200
    assert linked_args == {
        "user_id": "toda:org_acme",
        "auth_config_id": "ac_github",
        "callback_url": "https://brain.test/integrations",
        "alias": None,
    }

    trigger_args: dict = {}

    def fake_trigger(**kwargs):
        trigger_args.update(kwargs)
        return {"id": "ti_github_issues", "enabled": True}

    monkeypatch.setattr(brain.composio, "create_trigger", fake_trigger)
    created = api_client.post(
        "/v1/composio/triggers",
        json={
            "organization_id": "org_acme",
            "connected_account_id": "ca_synced_github",
            "trigger_slug": "GITHUB_ISSUE_EVENT",
            "trigger_config": {"owner": "acme", "repo": "brain"},
        },
    )
    assert created.status_code == 200, created.text
    assert trigger_args["connected_account_id"] == "ca_synced_github"
    assert trigger_args["user_id"] == "toda:org_acme"

    wrong_org = api_client.post(
        "/v1/composio/triggers",
        json={
            "organization_id": "org_other",
            "connected_account_id": "ca_synced_github",
            "trigger_slug": "GITHUB_ISSUE_EVENT",
        },
    )
    # The tenant guard (api/routes.py:enforce_tenant) rejects a request
    # naming another organization before the handler ever runs.
    assert wrong_org.status_code == 403


def test_toolkit_browsing_is_not_limited_to_the_curated_four(api_client, monkeypatch):
    """Item: the console's Integrations page must be able to browse any of
    Composio's toolkits, not just the four hardcoded quick-connect tiles."""
    brain = api_client.app.state.brain
    brain.composio.api_key = "test-only"

    list_args: dict = {}

    def fake_list_toolkits(**kwargs):
        list_args.update(kwargs)
        return {
            "items": [
                {
                    "slug": "stripe",
                    "name": "Stripe",
                    "auth_schemes": ["OAUTH2"],
                    "no_auth": False,
                    "meta": {
                        "description": "Payments",
                        "logo": "https://logos.composio.dev/api/stripe",
                        "categories": [{"id": "payments", "name": "Payments"}],
                    },
                }
            ],
            "next_cursor": "cursor_2",
            "total_items": 1070,
        }

    monkeypatch.setattr(brain.composio, "list_toolkits", fake_list_toolkits)
    listed = api_client.get(
        "/v1/composio/toolkits", params={"search": "stripe", "limit": 8}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert list_args == {"search": "stripe", "category": None, "cursor": None, "limit": 8}
    assert body["total_items"] == 1070
    assert body["next_cursor"] == "cursor_2"
    assert body["items"] == [
        {
            "slug": "stripe",
            "name": "Stripe",
            "description": "Payments",
            "logo": "https://logos.composio.dev/api/stripe",
            "auth_schemes": ["OAUTH2"],
            "no_auth": False,
            "categories": ["Payments"],
        }
    ]

    def fake_describe_toolkit(slug):
        assert slug == "stripe"
        return {
            "slug": "stripe",
            "name": "Stripe",
            "no_auth": False,
            "meta": {"description": "Payments", "logo": "https://logos.composio.dev/api/stripe"},
            "auth_config_details": [
                {
                    "name": "stripe_oauth2",
                    "mode": "OAUTH2",
                    "fields": {"connected_account_initiation": {"required": [], "optional": []}},
                },
                {
                    "name": "stripe_api_key",
                    "mode": "API_KEY",
                    "fields": {
                        "connected_account_initiation": {
                            "required": [
                                {
                                    "name": "generic_api_key",
                                    "displayName": "API Key",
                                    "type": "string",
                                    "description": "Your Stripe secret key.",
                                    "required": True,
                                    "is_secret": True,
                                }
                            ],
                            "optional": [],
                        }
                    },
                },
            ],
        }

    monkeypatch.setattr(brain.composio, "describe_toolkit", fake_describe_toolkit)
    detail = api_client.get("/v1/composio/toolkits/stripe")
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["no_auth"] is False
    assert [s["mode"] for s in detail_body["auth_schemes"]] == ["OAUTH2", "API_KEY"]
    api_key_scheme = detail_body["auth_schemes"][1]
    assert api_key_scheme["required_fields"] == [
        {
            "name": "generic_api_key",
            "displayName": "API Key",
            "type": "string",
            "description": "Your Stripe secret key.",
            "required": True,
            "is_secret": True,
        }
    ]


def test_api_key_toolkit_connects_without_oauth_redirect(api_client, monkeypatch):
    """Item: non-OAuth toolkits (API key, bearer token, ...) must be
    connectable directly, without ever needing an OAuth redirect link."""
    brain = api_client.app.state.brain
    brain.composio.api_key = "test-only"

    monkeypatch.setattr(
        brain.composio, "list_auth_configs", lambda **_kwargs: {"items": []}
    )

    create_auth_config_args: dict = {}

    def fake_create_auth_config(**kwargs):
        create_auth_config_args.update(kwargs)
        return {"id": "ac_stripe_api_key"}

    monkeypatch.setattr(brain.composio, "create_auth_config", fake_create_auth_config)

    create_account_args: dict = {}

    def fake_create_connected_account(**kwargs):
        create_account_args.update(kwargs)
        return {"id": "ca_stripe_key", "status": "ACTIVE"}

    monkeypatch.setattr(
        brain.composio, "create_connected_account", fake_create_connected_account
    )

    response = api_client.post(
        "/v1/composio/connections:credentials",
        json={
            "organization_id": "org_acme",
            "toolkit": "stripe",
            "auth_scheme": "api_key",
            "credentials": {"generic_api_key": "sk_test_dummy"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_config_id"] == "ac_stripe_api_key"
    assert body["account"]["connected_account_id"] == "ca_stripe_key"
    assert body["account"]["status"] == "active"
    assert body["account"]["toolkit"] == "stripe"

    assert create_auth_config_args == {"toolkit": "stripe", "auth_scheme": "API_KEY"}
    assert create_account_args["auth_config_id"] == "ac_stripe_api_key"
    assert create_account_args["auth_scheme"] == "API_KEY"
    assert create_account_args["val"] == {
        "status": "ACTIVE",
        "generic_api_key": "sk_test_dummy",
    }

    stored = brain.store.get_harness_record(
        "connector_accounts", "connector:org_acme:ca_stripe_key"
    )
    assert stored is not None and stored["status"] == "active"


def test_api_key_connect_reuses_an_existing_matching_auth_config(api_client, monkeypatch):
    """A toolkit that already has an API_KEY auth config (e.g. reconnecting,
    or an OAuth2 auth config already exists for the same toolkit) must reuse
    the one matching the requested scheme, not blindly grab the first result
    or create a duplicate."""
    brain = api_client.app.state.brain
    brain.composio.api_key = "test-only"

    monkeypatch.setattr(
        brain.composio,
        "list_auth_configs",
        lambda **_kwargs: {
            "items": [
                {"id": "ac_stripe_oauth", "auth_scheme": "OAUTH2"},
                {"id": "ac_stripe_api_key_existing", "auth_scheme": "API_KEY"},
            ]
        },
    )

    def fail_create_auth_config(**_kwargs):
        raise AssertionError("should not create a new auth config when one already matches")

    monkeypatch.setattr(brain.composio, "create_auth_config", fail_create_auth_config)
    monkeypatch.setattr(
        brain.composio,
        "create_connected_account",
        lambda **_kwargs: {"id": "ca_stripe_key_2", "status": "ACTIVE"},
    )

    response = api_client.post(
        "/v1/composio/connections:credentials",
        json={
            "organization_id": "org_acme",
            "toolkit": "stripe",
            "auth_scheme": "API_KEY",
            "credentials": {"generic_api_key": "sk_test_other"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["auth_config_id"] == "ac_stripe_api_key_existing"


def test_credential_connect_rejects_an_unknown_organization(api_client, monkeypatch):
    brain = api_client.app.state.brain
    brain.composio.api_key = "test-only"
    response = api_client.post(
        "/v1/composio/connections:credentials",
        json={
            "organization_id": "org_does_not_exist",
            "toolkit": "stripe",
            "auth_scheme": "API_KEY",
            "credentials": {"generic_api_key": "sk_test_dummy"},
        },
    )
    assert response.status_code == 403  # rejected by the tenant guard first

    # Signed in AS that organization, the route's own existence check is
    # what rejects it -- both layers are load-bearing.
    sign_in(api_client, organization_id="org_does_not_exist")
    response = api_client.post(
        "/v1/composio/connections:credentials",
        json={
            "organization_id": "org_does_not_exist",
            "toolkit": "stripe",
            "auth_scheme": "API_KEY",
            "credentials": {"generic_api_key": "sk_test_dummy"},
        },
    )
    assert response.status_code == 404, response.text


def _client():
    from toda.harness.composio import ComposioClient

    return ComposioClient(
        api_key="k",
        base_url="https://example.invalid",
        connected_account_id=None,
    )


def test_auth_config_id_is_read_from_every_shape_composio_returns():
    """Reading only the top-level id turned a successful creation into a
    failure: the config was created on Composio's side, Toda raised
    "Composio did not return an auth config id", and the user got a broken
    dialog for an app that was now half set up. Reproduced with HubSpot,
    which left an ENABLED config with zero connections behind."""
    client = _client()
    assert client._auth_config_id({"id": "ac_1"}) == "ac_1"
    assert client._auth_config_id({"auth_config_id": "ac_2"}) == "ac_2"
    assert client._auth_config_id({"auth_config": {"id": "ac_3"}}) == "ac_3"
    assert client._auth_config_id({"data": {"id": "ac_4"}}) == "ac_4"
    assert client._auth_config_id({"data": {"auth_config": {"id": "ac_5"}}}) == "ac_5"
    assert client._auth_config_id({"nothing": "useful"}) is None


def test_ensure_auth_config_never_returns_another_toolkits_config():
    """`items[0]["id"]` would hand back whatever came first, which for an
    unfiltered or loosely-filtered response is an OAuth link to the wrong
    application entirely."""
    client = _client()
    client.list_auth_configs = lambda *, toolkit=None: {  # type: ignore[method-assign]
        "items": [
            {"id": "ac_github", "toolkit": {"slug": "github"}},
            {"id": "ac_hubspot", "toolkit": {"slug": "hubspot"}},
        ]
    }
    assert client.ensure_auth_config(toolkit="hubspot") == "ac_hubspot"


def test_ensure_auth_config_recovers_when_create_response_is_unreadable():
    """Creation can succeed server-side even when the response cannot be
    parsed, so look again before failing -- that is the exact case that
    stranded HubSpot."""
    client = _client()
    calls = {"n": 0}

    def _list(*, toolkit=None):
        calls["n"] += 1
        # Empty first, populated after the create landed.
        return {"items": []} if calls["n"] == 1 else {
            "items": [{"id": "ac_new", "toolkit": {"slug": toolkit}}]
        }

    client.list_auth_configs = _list  # type: ignore[method-assign]
    client.create_auth_config = lambda **_kw: {"unexpected": "shape"}  # type: ignore[method-assign]
    assert client.ensure_auth_config(toolkit="hubspot") == "ac_new"


def test_a_genuine_failure_says_what_composio_returned():
    client = _client()
    client.list_auth_configs = lambda *, toolkit=None: {"items": []}  # type: ignore[method-assign]
    client.create_auth_config = lambda **_kw: {"error": "quota", "detail": "x"}  # type: ignore[method-assign]
    try:
        client.ensure_auth_config(toolkit="hubspot")
    except RuntimeError as exc:
        assert "hubspot" in str(exc)
        assert "detail" in str(exc) and "error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_disconnecting_removes_the_triggers_it_created():
    """Otherwise Composio keeps delivering events for a connection the
    organization believes it removed."""
    client = _client()
    client.list_active_triggers = lambda *, connected_account_ids=None: {  # type: ignore[method-assign]
        "items": [{"id": "ti_1"}, {"id": "ti_2"}]
    }
    deleted: list[str] = []
    client.delete_trigger = lambda tid: deleted.append(tid) or {}  # type: ignore[method-assign]
    result = client.unsubscribe_all_triggers(connected_account_id="ca_1")
    assert deleted == ["ti_1", "ti_2"]
    assert result["removed"] == ["ti_1", "ti_2"]


def test_unsubscribe_reports_rather_than_raises_when_composio_is_unreachable():
    client = _client()

    def _boom(*, connected_account_ids=None):
        raise RuntimeError("connection refused")

    client.list_active_triggers = _boom  # type: ignore[method-assign]
    result = client.unsubscribe_all_triggers(connected_account_id="ca_1")
    assert result["removed"] == []
    assert result["failed"] and "connection refused" in result["failed"][0]["error"]


def test_only_webhook_triggers_subscribe_automatically():
    """A poll trigger's first run has seen nothing, so it emits the whole
    account history as "new" -- one GitHub PR poll produced 6,525 events in
    under an hour, every one billed. Webhooks only fire on things that
    happen after subscription, which is the point of subscribing at all."""
    client = _client()
    # `type` and `config` come back on the list item itself -- asking again
    # per slug was 46 pointless round trips per GitHub connect.
    client.list_trigger_types = lambda *, toolkit=None, limit=100: {  # type: ignore[method-assign]
        "items": [
            {"slug": "A_WEBHOOK", "type": "webhook", "config": {}},
            {"slug": "B_POLL", "type": "poll", "config": {}},
            {"slug": "C_WEBHOOK_NEEDS_CONFIG", "type": "webhook",
             "config": {"required": ["repo"]}},
        ]
    }

    def _must_not_describe(_slug):
        raise AssertionError("subscribe must not re-describe what the list already returned")

    client.describe_trigger = _must_not_describe  # type: ignore[method-assign]
    created: list[str] = []
    client.create_trigger = lambda **kw: created.append(kw["slug"]) or {"id": "ti"}  # type: ignore[method-assign]

    result = client.subscribe_all_triggers(toolkit="github", connected_account_id="ca_1")
    assert created == ["A_WEBHOOK"]
    assert result["skipped_poll"] == ["B_POLL"]
    assert [i["slug"] for i in result["needs_config"]] == ["C_WEBHOOK_NEEDS_CONFIG"]


def test_subscribing_nothing_is_an_acceptable_outcome():
    """Today every no-config trigger GitHub, Gmail and HubSpot offer is a
    poll, and every webhook they offer needs configuration -- so automatic
    subscription correctly does nothing rather than starting a firehose."""
    client = _client()
    client.list_trigger_types = lambda *, toolkit=None, limit=100: {  # type: ignore[method-assign]
        "items": [{"slug": "ONLY_POLL", "type": "poll", "config": {}}]
    }

    def _must_not_create(**_kw):
        raise AssertionError("a poll trigger must never be auto-subscribed")

    client.create_trigger = _must_not_create  # type: ignore[method-assign]
    result = client.subscribe_all_triggers(toolkit="gmail", connected_account_id="ca_1")
    assert result["subscribed"] == []
    assert result["skipped_poll"] == ["ONLY_POLL"]
