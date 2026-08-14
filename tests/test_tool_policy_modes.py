"""Organization tool modes and immediate per-tool policy overrides."""

from toda.harness.models import InvocationEnvelope, ModelToolCall, ToolDefinition


def _task(brain, conversation: str = "policy:test") -> dict:
    destination = {"type": "internal_agent_session", "id": "test", "is_private": True}
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    task = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="exercise tool policy",
        trigger_kind="reactive",
        initial_message="exercise tool policy",
    )
    return brain.store.get_task(task["id"])


def _set_mode(brain, mode: str) -> None:
    brain.store.upsert_simple_harness_record(
        "tool_policies",
        {
            "id": "tool_policy:org_acme:*",
            "organization_id": "org_acme",
            "tool_name": "*",
            "mode": mode,
        },
    )


def _set_override(brain, tool_name: str, execution: str) -> None:
    brain.store.upsert_simple_harness_record(
        "tool_policies",
        {
            "id": f"tool_policy:org_acme:{tool_name}",
            "organization_id": "org_acme",
            "tool_name": tool_name,
            "execution": execution,
        },
    )


def test_tools_api_exposes_real_readiness_and_autonomous_defaults(api_client):
    response = api_client.get("/v1/tools", params={"organization_id": "org_acme"})
    assert response.status_code == 200
    body = response.json()
    assert body["policy_mode"] == "autonomous"
    assert body["count"] == body["ready_count"] + body["setup_needed_count"]
    assert body["setup_needed_count"] > 0

    read_tool = next(tool for tool in body["tools"] if tool["risk"] == "read")
    # A write tool that leaves Toda. Work that stays inside Toda runs
    # direct, so picking whichever write tool sorts first would test the
    # rule rather than the mode.
    write_tool = next(
        tool for tool in body["tools"] if tool["name"] == "COMPOSIO_EXECUTE"
    )
    assert read_tool["policy"]["execution"] == "direct"
    assert write_tool["policy"]["execution"] == "ai_review"


def test_mode_and_tool_override_updates_are_visible_immediately(api_client):
    response = api_client.post(
        "/v1/tool-policy-mode",
        json={"organization_id": "org_acme", "mode": "restricted"},
    )
    assert response.status_code == 200
    tools = api_client.get(
        "/v1/tools", params={"organization_id": "org_acme"}
    ).json()
    assert tools["policy_mode"] == "restricted"
    # Work contained inside Toda stays direct in every mode, including
    # restricted -- see the approval rule in harness/tools.py. Work that
    # leaves Toda is gated by the mode like everything else.
    by_name = {tool["name"]: tool["policy"]["execution"] for tool in tools["tools"]}
    for contained in ("automation_create", "pdf_create", "workspace_write"):
        if contained in by_name:
            assert by_name[contained] == "direct", contained
    for external in ("COMPOSIO_EXECUTE", "skill_propose_create"):
        if external in by_name:
            assert by_name[external] in {"ask", "deny"}, external

    tool_name = tools["tools"][0]["name"]
    response = api_client.post(
        "/v1/tool-policies",
        json={
            "organization_id": "org_acme",
            "tool_name": tool_name,
            "execution": "deny",
        },
    )
    assert response.status_code == 200
    refreshed = api_client.get(
        "/v1/tools", params={"organization_id": "org_acme"}
    ).json()
    denied = next(tool for tool in refreshed["tools"] if tool["name"] == tool_name)
    assert denied["policy"] == {
        "override": "deny",
        "access": "forbidden",
        "execution": "deny",
        "source": "tool_override",
    }


def test_all_four_modes_drive_the_executor_and_deny_removes_the_tool(brain, monkeypatch):
    brain.tool_registry.register(
        ToolDefinition(
            name="policy_test_read",
            description="Policy test read",
            input_schema={"type": "object", "properties": {}},
            risk="read",
        ),
        lambda _args, _runtime: {"ok": True},
    )
    brain.tool_registry.register(
        ToolDefinition(
            name="policy_test_write",
            description="Policy test write",
            input_schema={"type": "object", "properties": {}},
            risk="write",
        ),
        lambda _args, _runtime: {"ok": True},
    )
    task = _task(brain)

    reviewed: list[str] = []
    monkeypatch.setattr(
        brain.action_reviewer,
        "review",
        lambda **kwargs: reviewed.append(kwargs["tool_name"]) or "approve",
    )
    autonomous = brain.tool_executor.execute(
        run=task,
        call=ModelToolCall(id="call_autonomous", name="policy_test_write", arguments={}),
    )
    assert autonomous.status == "completed"
    assert reviewed == ["policy_test_write"]

    _set_mode(brain, "supervised")
    supervised = brain.tool_executor.execute(
        run=task,
        call=ModelToolCall(id="call_supervised", name="policy_test_write", arguments={}),
    )
    assert supervised.status == "waiting_for_approval"
    assert reviewed == ["policy_test_write"]

    _set_mode(brain, "full_access")
    full_access = brain.tool_executor.execute(
        run=task,
        call=ModelToolCall(id="call_full_access", name="policy_test_write", arguments={}),
    )
    assert full_access.status == "completed"
    assert reviewed == ["policy_test_write"]

    _set_mode(brain, "restricted")
    restricted = brain.tool_executor.execute(
        run=task,
        call=ModelToolCall(id="call_restricted", name="policy_test_read", arguments={}),
    )
    assert restricted.status == "waiting_for_approval"

    _set_override(brain, "policy_test_write", "deny")
    allowed = {definition.name for definition in brain.tool_executor.allowed_definitions(task)}
    assert "policy_test_write" not in allowed
