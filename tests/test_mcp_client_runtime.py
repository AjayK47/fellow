from __future__ import annotations


def _server(brain, *, organization_id: str = "org_acme", name: str = "Demo"):
    return brain.mcp.create(
        organization_id=organization_id,
        name=name,
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        auth_type="api_key",
        api_key_header="Authorization",
        api_key="Bearer secret-value",
    )


def test_discovery_registers_namespaced_write_tools_and_redacts_secrets(brain, monkeypatch):
    created = _server(brain)

    async def discover(_server, oauth_provider=None):
        return [{
            "name": "create_issue",
            "description": "Create an issue",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        }]

    monkeypatch.setattr(brain.mcp, "_discover", discover)
    tested = brain.mcp.test(created["id"], "org_acme")

    assert tested["status"] == "ready"
    assert tested["auth"] == {
        "type": "api_key",
        "header_name": "Authorization",
        "scope": None,
        "configured": True,
    }
    assert "secret-value" not in str(tested)
    definitions = [
        definition for definition in brain.tool_registry.definitions()
        if definition.capability_id == f"mcp.{created['id']}.create_issue"
    ]
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.name.startswith("mcp_demo_create_issue_")
    assert definition.risk == "write"
    assert definition.allowed_organization_ids == ["org_acme"]
    assert definition.toolkits == ["mcp:Demo"]


def test_mcp_tools_are_tenant_scoped_and_inherit_native_write_policy(brain, monkeypatch):
    created = _server(brain)

    async def discover(_server, oauth_provider=None):
        return [{"name": "mutate", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(brain.mcp, "_discover", discover)
    brain.mcp.test(created["id"], "org_acme")
    definition = next(
        item for item in brain.tool_registry.definitions()
        if item.capability_id == f"mcp.{created['id']}.mutate"
    )
    acme_run = {
        "organization_id": "org_acme",
        "trigger_kind": "reactive",
        "metadata": {},
        "envelope": {"principal_id": "person_ajay", "destination": {}},
    }
    other_run = {**acme_run, "organization_id": "org_other"}

    assert definition in brain.tool_executor.allowed_definitions(acme_run)
    assert definition not in brain.tool_executor.allowed_definitions(other_run)
    assert brain.tool_executor.effective_policy("org_acme", definition)["execution"] == "ai_review"

    brain.store.upsert_simple_harness_record("tool_policies", {
        "id": "tool_policy:org_acme:*", "organization_id": "org_acme",
        "tool_name": "*", "mode": "supervised",
    })
    assert brain.tool_executor.effective_policy("org_acme", definition)["execution"] == "ask"
    brain.store.upsert_simple_harness_record("tool_policies", {
        "id": "tool_policy:org_acme:*", "organization_id": "org_acme",
        "tool_name": "*", "mode": "full_access",
    })
    assert brain.tool_executor.effective_policy("org_acme", definition)["execution"] == "direct"


def test_disabling_and_deleting_server_unloads_dynamic_tools(brain, monkeypatch):
    created = _server(brain)

    async def discover(_server, oauth_provider=None):
        return [{"name": "do_work", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(brain.mcp, "_discover", discover)
    brain.mcp.test(created["id"], "org_acme")
    local_name = next(
        item.name for item in brain.tool_registry.definitions()
        if item.capability_id == f"mcp.{created['id']}.do_work"
    )
    assert brain.tool_registry.get(local_name)

    brain.mcp.set_enabled(created["id"], "org_acme", False)
    assert brain.tool_registry.get(local_name) is None
    brain.mcp.set_enabled(created["id"], "org_acme", True)
    assert brain.tool_registry.get(local_name)
    brain.mcp.delete(created["id"], "org_acme")
    assert brain.tool_registry.get(local_name) is None
    assert brain.store.get_harness_record("mcp_servers", created["id"]) is None


def test_mcp_admin_api_persists_redacted_servers_and_can_delete(api_client):
    response = api_client.post("/v1/mcp/servers", json={
        "organization_id": "org_acme",
        "name": "Remote API",
        "transport": "streamable_http",
        "url": "https://mcp.example.test/mcp",
        "auth_type": "api_key",
        "api_key_header": "X-API-Key",
        "api_key": "top-secret",
    })
    assert response.status_code == 200
    created = response.json()
    assert created["auth"]["configured"] is True
    assert "top-secret" not in response.text

    listed = api_client.get(
        "/v1/mcp/servers", params={"organization_id": "org_acme"}
    )
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert "top-secret" not in listed.text

    deleted = api_client.delete(
        f"/v1/mcp/servers/{created['id']}",
        params={"organization_id": "org_acme"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
