"""End-to-end Composio trigger ingestion for the company harness."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


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
        "timestamp": "2026-07-30T04:00:00Z",
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
        "composio_list_integrations"
    ).handler({}, runtime)
    assert [item["toolkit"] for item in listing["integrations"]] == ["github"]

    result = brain.tool_registry.get("composio_search").handler(
        {"query": "commits and email"}, runtime
    )
    assert [tool["slug"] for tool in result["tools"]] == [
        "GITHUB_LIST_COMMITS"
    ]
    assert calls[0]["toolkit"] == "github"

    other_tenant = {**runtime, "organization_id": "org_not_connected"}
    isolated = brain.tool_registry.get("composio_search").handler(
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
        "user_id": "docket:org_acme",
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
    assert trigger_args["user_id"] == "docket:org_acme"

    wrong_org = api_client.post(
        "/v1/composio/triggers",
        json={
            "organization_id": "org_other",
            "connected_account_id": "ca_synced_github",
            "trigger_slug": "GITHUB_ISSUE_EVENT",
        },
    )
    assert wrong_org.status_code == 404


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
    assert response.status_code == 404
