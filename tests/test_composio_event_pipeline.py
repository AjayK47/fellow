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
    runs = brain.store.list_harness_records("agent_runs", "org_acme")
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
        "run_id": "run_test",
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
    assert brain.tool_registry.get(
        "composio_execute"
    ).definition.approval_required is True


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
        "user_id": "company-brain:org_acme",
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
    assert trigger_args["user_id"] == "company-brain:org_acme"

    wrong_org = api_client.post(
        "/v1/composio/triggers",
        json={
            "organization_id": "org_other",
            "connected_account_id": "ca_synced_github",
            "trigger_slug": "GITHUB_ISSUE_EVENT",
        },
    )
    assert wrong_org.status_code == 404
