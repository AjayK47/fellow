from datetime import timedelta

from toda.domain.models import utcnow
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall


def _wake(brain, content: str, key: str):
    return brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=key,
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content=content,
        principal_id="person_ajay",
    )


def test_delegation_uses_one_waiting_state_with_child_reason(brain):
    brain.conversation_agent.model = ScriptedModelClient(
        [ModelResponse(tool_calls=[ModelToolCall(name="start_task", arguments={"goal": "Investigate both systems"})])]
    )
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        name="task_delegate",
                        arguments={
                            "tasks": [
                                {"worker_type": "general", "prompt": "Inspect system A"},
                                {"worker_type": "general", "prompt": "Inspect system B"},
                            ]
                        },
                    )
                ]
            )
        ]
    )
    wake = _wake(brain, "Investigate both systems", "conv:wait-child")

    outcome = brain.session_manager.dispatch(wake["session_id"])

    task = brain.store.get_task(outcome.task_id)
    assert outcome.state.value == "waiting"
    assert task["state"] == "waiting"
    assert task["waiting_for"]["type"] == "child_task"
    assert len(task["waiting_for"]["ids"]) == 2


def test_organization_memory_is_bounded_and_loaded(brain):
    organization = brain.store.get_organization("org_acme")
    organization["memory_md"] = "# Acme\n\n" + "x" * 5_000
    brain.store.put_organization(organization)
    brain.conversation_agent.model = ScriptedModelClient([ModelResponse(content="Got it")])
    wake = _wake(brain, "hello", "conv:org-memory")

    brain.session_manager.dispatch(wake["session_id"])

    request = brain.conversation_agent.model.requests[0]
    memory_block = next(
        item["content"] for item in request["messages"] if "<organization_memory>" in item["content"]
    )
    assert "# Acme" in memory_block
    assert len(memory_block) < 4_100


def test_recovery_heartbeat_requeues_an_expired_task_without_model(brain):
    brain.conversation_agent.model = ScriptedModelClient(
        [ModelResponse(tool_calls=[ModelToolCall(name="start_task", arguments={"goal": "Long task"})])]
    )
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="done")])
    wake = _wake(brain, "Do long task", "conv:recovery")
    outcome = brain.session_manager.dispatch(wake["session_id"])
    task = brain.store.get_task(outcome.task_id)
    task["state"] = "running"
    task["execution_owner"] = "dead-worker"
    task["execution_lease_expires_at"] = (utcnow() - timedelta(minutes=1)).isoformat()
    task["updated_at"] = utcnow().isoformat()
    with brain.store.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET state='running', execution_owner=?, execution_lease_expires_at=?, payload=? WHERE id=?",
            ("dead-worker", task["execution_lease_expires_at"], __import__("json").dumps(task), task["id"]),
        )

    result = brain.recovery_heartbeat.tick("org_acme")

    assert task["id"] in result["requeued_task_ids"]
    assert brain.store.get_task(task["id"])["state"] == "queued"


def test_recovery_heartbeat_uses_targeted_queries(brain, monkeypatch):
    def fail_full_scan(*args, **kwargs):
        raise AssertionError("recovery must not scan every Task")

    monkeypatch.setattr(brain.store, "list_tasks", fail_full_scan)

    result = brain.recovery_heartbeat.tick("org_acme")

    assert result["organization_id"] == "org_acme"
