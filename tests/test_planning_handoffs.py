from __future__ import annotations

import base64

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import (
    DestinationEnvelope,
    InvocationEnvelope,
    ModelResponse,
    ModelToolCall,
)
from toda.harness.prompts import build_agent_messages


def _execute(brain, task: dict, name: str, arguments: dict, call_id: str):
    return brain.tool_executor.execute(
        run=task,
        call=ModelToolCall(id=call_id, name=name, arguments=arguments),
    )


def _start_root_with_plan_and_child(brain, *, artifact_id: str | None = None):
    attachments = (
        [{"kind": "artifact", "ref": artifact_id, "label": "Large evidence"}]
        if artifact_id
        else []
    )
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_plan",
                        name="plan_create",
                        arguments={
                            "title": "Investigate",
                            "items": [
                                {
                                    "id": "analyze",
                                    "title": "Analyze evidence",
                                    "status": "active",
                                }
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_delegate",
                        name="task_delegate",
                        arguments={
                            "tasks": [
                                {
                                    "worker_type": "general",
                                    "prompt": "Analyze the referenced evidence.",
                                    "plan_step_ids": ["analyze"],
                                    "attachments": attachments,
                                }
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="api:planning-handoff",
        destination={"type": "dm", "id": "DPLAN", "is_private": True},
        role="user",
        content="Investigate this substantial issue",
        principal_id="person_ajay",
    )
    outcome = brain.session_manager.dispatch(wake["session_id"])
    assert outcome is not None
    assert outcome.state.value == "waiting"
    root = brain.store.get_task(outcome.task_id)
    child = brain.store.get_task(root["pending_child_task_ids"][0])
    return root, child


def test_root_plan_linkage_and_trusted_attachment_manifest(brain):
    artifact = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="report",
        title="Evidence",
        content="x" * 30_000,
        scope_id="scope_org_public",
        created_by="person_ajay",
    )
    root, child = _start_root_with_plan_and_child(brain, artifact_id=artifact["id"])

    assert child["metadata"]["plan_step_ids"] == ["analyze"]
    assert child["metadata"]["attachments"] == [
        {"kind": "artifact", "ref": artifact["id"], "label": "Large evidence"}
    ]
    assert "x" * 100 not in child["prompt"]

    messages = build_agent_messages(run=child, context={}, history=[])
    trusted = next(
        item["content"] for item in messages if "<trusted_attachments>" in item["content"]
    )
    assert artifact["id"] in trusted
    assert "x" * 100 not in trusted

    child_get = _execute(brain, child, "plan_get", {}, "child_plan_get")
    assert child_get.status == "completed"
    assert child_get.output["plan"]["task_id"] == root["id"]
    child_update = _execute(
        brain,
        child,
        "plan_update",
        {"title": "Child overwrite"},
        "child_plan_update",
    )
    assert child_update.status == "disabled"
    assert "capability envelope" in (child_update.error or "")
    assert brain.audit.list_events("org_acme", "task.plan.created")


def test_attachment_validation_rejects_unknown_steps_and_path_escape(brain):
    root, _child = _start_root_with_plan_and_child(brain)
    # Cancel the first child so direct service calls don't leave irrelevant work.
    for child_id in root["pending_child_task_ids"]:
        brain.store.cancel_task(child_id)

    try:
        brain.child_tasks.execute(
            {
                "tasks": [
                    {
                        "worker_type": "general",
                        "prompt": "bad link",
                        "plan_step_ids": ["missing"],
                    }
                ]
            },
            {"task_id": root["id"]},
        )
    except ValueError as exc:
        assert "unknown root plan step" in str(exc)
    else:
        raise AssertionError("unknown plan linkage was accepted")

    brain.workspaces.get_or_create(
        organization_id="org_acme",
        owner_id="person_ajay",
        scope_id="scope_org_public",
    )
    try:
        brain.child_tasks.execute(
            {
                "tasks": [
                    {
                        "worker_type": "general",
                        "prompt": "bad path",
                        "attachments": [
                            {"kind": "workspace_file", "ref": "../secret.txt"}
                        ],
                    }
                ]
            },
            {"task_id": root["id"]},
        )
    except ValueError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("workspace path traversal was accepted")


def test_delegation_batch_validation_is_atomic(brain):
    root, _child = _start_root_with_plan_and_child(brain)
    before = {
        item["id"]
        for item in brain.store.list_tasks(
            "org_acme", parent_task_id=root["id"]
        )
    }
    try:
        brain.child_tasks.execute(
            {
                "tasks": [
                    {"worker_type": "general", "prompt": "valid first item"},
                    {
                        "worker_type": "general",
                        "prompt": "invalid second item",
                        "plan_step_ids": ["not_in_plan"],
                    },
                ]
            },
            {"task_id": root["id"]},
        )
    except ValueError as exc:
        assert "unknown root plan step" in str(exc)
    else:
        raise AssertionError("invalid delegation batch was accepted")
    after = {
        item["id"]
        for item in brain.store.list_tasks(
            "org_acme", parent_task_id=root["id"]
        )
    }
    assert after == before


def test_artifact_reads_are_bounded_and_lists_are_metadata_only(brain):
    artifact = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="report",
        title="Large report",
        content="0123456789" * 3_000,
        scope_id="scope_org_public",
        created_by="person_ajay",
    )
    root, _child = _start_root_with_plan_and_child(brain)

    listed = _execute(brain, root, "artifact_list", {}, "list_artifacts")
    assert listed.status == "completed"
    listed_value = next(item for item in listed.output["artifacts"] if item["id"] == artifact["id"])
    assert "content" not in listed_value

    read = _execute(
        brain,
        root,
        "artifact_read",
        {"artifact_id": artifact["id"], "offset": 10, "max_chars": 25},
        "read_artifact",
    )
    assert read.status == "completed"
    assert read.output["returned_chars"] == 25
    assert read.output["has_more"] is True
    assert len(read.output["text"]) == 25

    binary = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="pdf",
        title="Binary report",
        content={"base64": base64.b64encode(b"secret bytes").decode()},
        scope_id="scope_org_public",
        created_by="person_ajay",
    )
    binary_read = _execute(
        brain,
        root,
        "artifact_read",
        {"artifact_id": binary["id"]},
        "read_binary_artifact",
    )
    assert binary_read.status == "completed"
    assert binary_read.output["binary"] is True
    assert binary_read.output["content_included"] is False
    assert "base64" not in str(binary_read.output)


def test_task_scratchpads_are_bounded_and_isolated(brain):
    root, child = _start_root_with_plan_and_child(brain)
    updated = _execute(
        brain, root, "scratchpad_update", {"text": "root notes"}, "root_notes"
    )
    assert updated.status == "completed"
    assert _execute(brain, root, "scratchpad_read", {}, "root_read").output["text"] == "root notes"
    assert _execute(brain, child, "scratchpad_read", {}, "child_read").output["text"] == ""

    oversized = _execute(
        brain,
        root,
        "scratchpad_update",
        {"text": "x" * 20_001},
        "root_oversized",
    )
    assert oversized.status == "failed"
    assert "character limit" in (oversized.error or "")


def test_source_references_are_permission_checked_and_read_in_slices(brain):
    source = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="message",
        external_id="handoff-source",
        content="source material " * 2_000,
        scope_id="scope_org_public",
        learn=False,
    )["source"]
    root, _child = _start_root_with_plan_and_child(brain)
    try:
        brain.child_tasks.execute(
            {
                "tasks": [
                    {
                        "worker_type": "general",
                        "prompt": "Read the source reference.",
                        "attachments": [
                            {"kind": "source", "ref": source["id"], "label": "Evidence"}
                        ],
                    }
                ]
            },
            {"task_id": root["id"]},
        )
    except Exception as exc:
        from toda.harness.child_tasks import AwaitingChildren

        assert isinstance(exc, AwaitingChildren)
        child = brain.store.get_task(exc.child_task_ids[-1])
    else:
        raise AssertionError("delegation should pause for children")
    assert child["metadata"]["attachments"][0]["ref"] == source["id"]

    read = _execute(
        brain,
        child,
        "memory_read_source",
        {"source_id": source["id"], "max_chars": 31},
        "read_source",
    )
    assert read.status == "completed"
    assert read.output["returned_chars"] == 31
    assert read.output["has_more"] is True

    foreign = dict(source)
    foreign["id"] = "source_foreign"
    foreign["organization_id"] = "org_foreign"
    brain.store.upsert_source(foreign)
    try:
        brain.child_tasks.execute(
            {
                "tasks": [
                    {
                        "worker_type": "general",
                        "prompt": "Read foreign source.",
                        "attachments": [{"kind": "source", "ref": foreign["id"]}],
                    }
                ]
            },
            {"task_id": root["id"]},
        )
    except PermissionError as exc:
        assert "not accessible" in str(exc)
    else:
        raise AssertionError("cross-organization source handoff was accepted")


def test_child_wake_summary_uses_trusted_plan_and_artifact_ids(brain):
    root, child = _start_root_with_plan_and_child(brain)
    child_artifact = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="report",
        title="Child finding",
        content="natural result body",
        scope_id="scope_org_public",
        task_id=child["id"],
        created_by="person_ajay",
    )
    assert brain.store.cancel_task(child["id"])
    brain.store.bump_task_revision(child["id"])
    brain.store.bump_task_revision(root["id"])
    brain.sessions.notify(root["session_id"], reason="test.child.summary")
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Reviewed the trusted child summary.")]
    )
    outcome = brain.session_manager.dispatch(root["session_id"])
    assert outcome is not None
    messages = brain.store.list_session_messages(root["session_id"], task_id=root["id"])
    tool_messages = [item for item in messages if item.get("role") == "tool"]
    assert tool_messages
    summary = tool_messages[-1]["content"]
    assert '"plan_step_ids": ["analyze"]' in summary
    assert child_artifact["id"] in summary
    assert '"artifact_ids"' in summary


def test_missing_app_connection_returns_exact_oauth_link_and_requires_rediscovery(
    brain, monkeypatch
):
    root, _child = _start_root_with_plan_and_child(brain)
    monkeypatch.setattr(
        brain.composio,
        "describe_toolkit",
        lambda toolkit: {
            "slug": toolkit,
            "auth_config_details": [{"mode": "OAUTH2"}],
        },
    )
    monkeypatch.setattr(
        brain.composio, "list_auth_configs", lambda **_kwargs: {"items": []}
    )
    monkeypatch.setattr(
        brain.composio,
        "create_auth_config",
        lambda **_kwargs: {"id": "auth_linear"},
    )
    monkeypatch.setattr(
        brain.composio,
        "create_connect_link",
        lambda **_kwargs: {
            "redirect_url": "https://connect.example/linear",
            "connected_account_id": "pending_linear",
        },
    )
    requested = brain.tool_executor.execute(
        run=root,
        call=ModelToolCall(
            id="request_linear_connection",
            name="request_app_connection",
            arguments={
                "toolkit": "linear",
                "missing_capability": "work tracking",
                "checked_systems": [],
            },
        ),
        approved=True,
    )
    assert requested.status == "completed"
    assert requested.output["redirect_url"] == "https://connect.example/linear"
    assert "integration_list again" in requested.output["instruction"]
    assert brain.audit.list_events(
        "org_acme", "task.integration_connection.requested"
    )

    # A returned OAuth link is not proof of connection. Only fresh connector
    # state makes the integration visible.
    before = _execute(
        brain, root, "integration_list", {}, "integrations_before"
    )
    assert before.output["integrations"] == []
    brain.store.upsert_connector_account(
        {
            "id": "connector_linear",
            "organization_id": "org_acme",
            "toolkit": "linear",
            "connected_account_id": "ca_linear",
            "status": "active",
        }
    )
    after = _execute(
        brain, root, "integration_list", {}, "integrations_after"
    )
    assert [item["toolkit"] for item in after.output["integrations"]] == ["linear"]


def test_credential_app_connection_never_collects_secrets_in_conversation(
    brain, monkeypatch
):
    root, _child = _start_root_with_plan_and_child(brain)
    monkeypatch.setattr(
        brain.composio,
        "describe_toolkit",
        lambda toolkit: {
            "slug": toolkit,
            "auth_config_details": [{"mode": "API_KEY"}],
        },
    )
    requested = brain.tool_executor.execute(
        run=root,
        call=ModelToolCall(
            id="request_api_key_connection",
            name="request_app_connection",
            arguments={
                "toolkit": "private_crm",
                "missing_capability": "customer records",
                "checked_systems": [],
            },
        ),
        approved=True,
    )
    assert requested.status == "completed"
    assert requested.output["credentials_required"] is True
    assert requested.output["redirect_url"] is None
    assert "do not request secrets" in requested.output["instruction"]


def test_plan_and_scratchpad_survive_process_restart(tmp_path):
    from toda.fixtures.seed import seed_synthetic_org
    from toda.services.container import TodaApp

    db_path = tmp_path / "planning_restart.db"
    app = TodaApp(db_path=db_path)
    seed_synthetic_org(app)
    destination = {"type": "dm", "id": "DRESTART", "is_private": True}
    decision = app.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="test",
        conversation_id="api:planning-restart",
        destination=DestinationEnvelope(**destination),
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    task = app.company_agent.create_invocation(
        envelope=envelope,
        goal="durability probe",
        trigger_kind="reactive",
        initial_message="durability probe",
    )
    task = app.store.get_task(task["id"])
    assert _execute(
        app,
        task,
        "plan_create",
        {
            "title": "Durable plan",
            "items": [{"id": "persist", "title": "Persist", "status": "active"}],
        },
        "persist_plan",
    ).status == "completed"
    assert _execute(
        app,
        task,
        "scratchpad_update",
        {"text": "durable working note"},
        "persist_scratchpad",
    ).status == "completed"
    task_id = task["id"]
    app.close()

    reopened = TodaApp(db_path=db_path)
    try:
        restored = reopened.store.get_task(task_id)
        plan = _execute(reopened, restored, "plan_get", {}, "read_persisted_plan")
        notes = _execute(
            reopened, restored, "scratchpad_read", {}, "read_persisted_scratchpad"
        )
        assert plan.output["plan"]["items"][0]["id"] == "persist"
        assert notes.output["text"] == "durable working note"
        deleted = reopened.retention.delete_organization(
            "org_acme", confirmation="org_acme"
        )
        assert deleted["deleted"]["task_scratchpads"] == 1
    finally:
        reopened.close()
