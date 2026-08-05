"""Workspace folders are keyed by Slack workspace, not by Task."""

from __future__ import annotations

from docket.harness.models import InvocationEnvelope


def _envelope(brain, *, conversation: str, is_private: bool = True) -> InvocationEnvelope:
    destination = {
        "type": "internal_agent_session",
        "id": conversation,
        "is_private": is_private,
    }
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _runtime_for(task: dict) -> dict:
    envelope = task["envelope"]
    return {
        "organization_id": task["organization_id"],
        "principal_id": envelope["principal_id"],
        "destination": envelope["destination"],
        "purpose": envelope["purpose"],
        "task_id": task["id"],
        "task_metadata": task.get("metadata") or {},
    }


def test_two_tasks_in_the_same_slack_workspace_share_one_workspace_folder(brain):
    task_a = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="slack:team-x:channel-a"),
        goal="do something in channel a",
        trigger_kind="reactive",
        metadata={"slack": {"workspace_id": "T_TEAM_X", "channel_id": "C_A"}},
    )
    task_b = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="slack:team-x:channel-b"),
        goal="do something in channel b",
        trigger_kind="reactive",
        metadata={"slack": {"workspace_id": "T_TEAM_X", "channel_id": "C_B"}},
    )

    workspace_a = brain.workspaces.get_for_task("org_acme", task_a)
    workspace_b = brain.workspaces.get_for_task("org_acme", task_b)
    assert workspace_a is not None
    assert workspace_a["id"] == workspace_b["id"]
    assert workspace_a["path"] == workspace_b["path"]

    write_handler = brain.tool_registry.get("workspace_write").handler
    read_handler = brain.tool_registry.get("workspace_read").handler
    write_handler({"path": "notes.txt", "content": "hello from a"}, _runtime_for(task_a))
    result = read_handler({"path": "notes.txt"}, _runtime_for(task_b))
    assert result["content"] == "hello from a"


def test_two_different_slack_workspaces_get_separate_folders(brain):
    task_a = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="slack:team-x:channel-a"),
        goal="team x task",
        trigger_kind="reactive",
        metadata={"slack": {"workspace_id": "T_TEAM_X", "channel_id": "C_A"}},
    )
    task_c = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="slack:team-y:channel-c"),
        goal="team y task",
        trigger_kind="reactive",
        metadata={"slack": {"workspace_id": "T_TEAM_Y", "channel_id": "C_C"}},
    )

    workspace_a = brain.workspaces.get_for_task("org_acme", task_a)
    workspace_c = brain.workspaces.get_for_task("org_acme", task_c)
    assert workspace_a["path"] != workspace_c["path"]


def test_task_with_no_slack_destination_falls_back_to_org_default_without_crashing(brain):
    task = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:internal:task-1"),
        goal="internal API-triggered task",
        trigger_kind="reactive",
    )

    workspace = brain.workspaces.get_for_task("org_acme", task)
    assert workspace is not None
    assert workspace["slack_workspace_id"] is None

    write_handler = brain.tool_registry.get("workspace_write").handler
    write_handler({"path": "notes.txt", "content": "no slack here"}, _runtime_for(task))

    other_task = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:internal:task-2"),
        goal="another internal task",
        trigger_kind="reactive",
    )
    other_workspace = brain.workspaces.get_for_task("org_acme", other_task)
    assert other_workspace["path"] == workspace["path"]
