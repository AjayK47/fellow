"""Phase 6 (plan §9): steering and follow-ups.

- an explicit stop/cancel message interrupts active work immediately --
  the root Task and every active child, pi included -- without waiting for
  a Manager model call (§9.3);
- an ordinary steering message is pushed as a signal the root Task's own
  _loop() picks up at its next iteration boundary if it's genuinely
  in flight right now (§9.1);
- the Session Manager can revise or cancel an *existing* child Task via
  manage_child_task instead of only ever being able to create new ones
  (§9.2), including the pi-specific cancel-then-restart-same-workspace
  flow plan §10.5 describes for a runtime with no live redirect protocol.
"""

from __future__ import annotations

import time

from toda.domain.models import new_id, utcnow
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import AgentRunState, DestinationEnvelope, InvocationEnvelope
from toda.harness.models import ModelResponse


def _envelope() -> dict:
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="slack_toda",
        conversation_id="slack:dm:person_ajay:steering",
        destination=DestinationEnvelope(type="dm", id="D1", is_private=True),
        purpose="reactive_agent",
        permission_decision_id="permdecision_test",
        allowed_scope_ids=[],
    ).model_dump(mode="json")


def _session_id(brain, key: str) -> str:
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=key,
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="start the work",
        principal_id="person_ajay",
        may_join=True,
    )
    return wake["session_id"]


def _make_task(
    brain,
    *,
    session_id: str,
    worker_type: str,
    state: str,
    parent_task_id: str | None = None,
    root_task_id: str | None = None,
    prompt: str = "do the work",
    waiting_for_type: str = "child_task",
) -> dict:
    now = utcnow().isoformat()
    task_id = new_id("task")
    task = {
        "id": task_id,
        "organization_id": "org_acme",
        "session_id": session_id,
        "parent_task_id": parent_task_id,
        "root_task_id": root_task_id or task_id,
        "state": state,
        "worker_type": worker_type,
        "trigger_kind": "reactive",
        "trigger_event_id": None,
        "prompt": prompt,
        "goal": prompt,
        "envelope": _envelope(),
        "trace_id": "trace_test",
        "max_iterations": 10,
        "max_tool_calls": 16,
        "iteration_count": 0,
        "tool_call_count": 0,
        "final_answer": None,
        "result": None,
        "error": None,
        "events": [],
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    if state == AgentRunState.WAITING.value:
        task["waiting_for"] = {
            "type": waiting_for_type,
            "ids": [],
        }
    brain.store.create_task(task)
    if state == AgentRunState.RUNNING.value:
        brain.store.claim_task(task_id, owner="exec_seed", lease_seconds=1800)
    return task


def _provision_workspace(brain) -> None:
    brain.workspaces.get_or_create(
        organization_id="org_acme",
        owner_id="person_ajay",
        scope_id="scope_org_public",
    )
    brain.pi_task_executor.isolation_required = False


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_explicit_stop_cancels_root_and_active_children_without_a_model_call(brain):
    _provision_workspace(brain)
    brain.pi_task_executor.command_builder = lambda prompt: ["sleep", "5"]
    brain.pi_task_executor.cancel_grace_seconds = 0
    brain.sessions.pi_executor = brain.pi_task_executor

    session_id = _session_id(brain, "slack:dm:person_ajay:stop")
    root = _make_task(
        brain, session_id=session_id, worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )
    general_child = _make_task(
        brain, session_id=session_id, worker_type="general", state=AgentRunState.RUNNING.value,
        parent_task_id=root["id"], root_task_id=root["id"],
    )
    pi_child = _make_task(
        brain, session_id=session_id, worker_type="pi", state=AgentRunState.QUEUED.value,
        parent_task_id=root["id"], root_task_id=root["id"],
    )
    brain.pi_task_executor.start(pi_child["id"], owner="exec_pi", enqueue_poll=lambda *a, **k: None)
    pi_attempt = brain.store.list_task_attempts(pi_child["id"])[0]
    assert _pid_alive(pi_attempt["pid"])

    session_before = brain.sessions.get(session_id)
    brain.sessions.mark_processed(session_id, session_before["manager_revision"])

    result = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:stop",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="stop",
        principal_id="person_ajay",
        may_join=True,
    )
    assert result["steer_signal"] == "abort"

    root_after = brain.store.get_task(root["id"])
    assert root_after["state"] == "cancelled"
    assert root_after["execution_owner"] is None
    general_after = brain.store.get_task(general_child["id"])
    assert general_after["state"] == "cancelled"

    # The pi child's cancellation is requested synchronously inside wake()
    # itself -- no poll, no model call needed to trigger it. (Actual OS-level
    # termination timing, including the SIGKILL escalation for a /bin/sh -c
    # wrapper that defers SIGTERM, is covered by test_pi_task_executor.py.)
    pi_attempt_after = brain.store.list_task_attempts(pi_child["id"])[-1]
    assert pi_attempt_after["cancel_requested_at"] is not None
    assert pi_attempt_after["cancellation_reason"] == "stop"

    # The Session must be woken so the Manager can explain what happened.
    session_after = brain.sessions.get(session_id)
    assert session_after["manager_revision"] > session_after["processed_revision"]


def test_ordinary_steer_message_is_injected_into_the_active_roots_next_iteration(brain):
    session_id = _session_id(brain, "slack:dm:person_ajay:steer")
    root = _make_task(
        brain, session_id=session_id, worker_type="session_manager",
        state=AgentRunState.RUNNING.value,
    )

    result = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:steer",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="also check the billing channel while you're at it",
        principal_id="person_ajay",
        may_join=True,
    )
    assert result["steer_signal"] == "steer"

    signal = brain.store.pop_task_signal(root["id"])
    assert signal is not None
    assert signal["kind"] == "steer"
    assert signal["message"] is None
    # The instruction already exists exactly once in the durable Session
    # transcript. The signal only causes the live loop to notice the new
    # revision at its next boundary; copying the content into the signal
    # would append the same user instruction twice.
    messages = brain.sessions.messages(session_id)
    assert [m["content"] for m in messages].count(
        "also check the billing channel while you're at it"
    ) == 1
    # Root itself was never touched -- steering never mechanically cancels.
    assert brain.store.get_task(root["id"])["state"] == "running"


def test_wake_with_no_active_root_task_does_not_signal_anything(brain):
    _session_id(brain, "slack:dm:person_ajay:idle")
    result = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:idle",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="stop",
        principal_id="person_ajay",
        may_join=True,
    )
    assert result["steer_signal"] is None


def test_actor_change_interrupts_active_root_for_fresh_authorization(brain):
    session_id = _session_id(brain, "slack:channel:shared")
    root = _make_task(
        brain,
        session_id=session_id,
        worker_type="session_manager",
        state=AgentRunState.RUNNING.value,
    )

    result = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:channel:shared",
        destination={"type": "slack_channel", "id": "C1", "is_private": False},
        role="user",
        content="now check my request",
        principal_id="person_maya",
        may_join=True,
    )

    assert result["steer_signal"] == "reauthorize"
    signal = brain.store.pop_task_signal(root["id"])
    assert signal and signal["kind"] == "reauthorize"
    assert brain.sessions.get(session_id)["last_actor_principal_id"] == "person_maya"


def test_actor_change_on_waiting_root_does_not_leave_a_stale_pause_signal(brain):
    session_id = _session_id(brain, "slack:channel:shared-waiting")
    root = _make_task(
        brain,
        session_id=session_id,
        worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )

    result = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:channel:shared-waiting",
        destination={"type": "slack_channel", "id": "C1", "is_private": False},
        role="user",
        content="use my permissions for this update",
        principal_id="person_maya",
        may_join=True,
    )

    assert result["steer_signal"] == "reauthorize"
    assert brain.store.pop_task_signal(root["id"]) is None


def test_waiting_root_resumes_under_current_actor_envelope(brain):
    session_id = _session_id(brain, "slack:channel:shared-resume")
    root = _make_task(
        brain,
        session_id=session_id,
        worker_type="session_manager",
        state=AgentRunState.WAITING.value,
        waiting_for_type="user",
    )
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Handled using the current actor.")]
    )
    brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:channel:shared-resume",
        destination={"type": "slack_channel", "id": "C1", "is_private": False},
        role="user",
        content="continue as me",
        principal_id="person_maya",
        may_join=True,
    )

    outcome = brain.session_manager.dispatch(session_id)

    assert outcome.state == AgentRunState.COMPLETED
    refreshed = brain.store.get_task(root["id"])
    assert refreshed["envelope"]["principal_id"] == "person_maya"
    assert refreshed["envelope"]["permission_decision_id"] != _envelope()[
        "permission_decision_id"
    ]


def test_session_manager_can_start_unrelated_root_in_same_session(brain):
    session_id = _session_id(brain, "slack:dm:person_ajay:parallel-root")
    session = brain.sessions.get(session_id)
    assert brain.sessions.claim(session_id, owner="setup")
    brain.sessions.mark_processed(session_id, session["manager_revision"])
    assert brain.sessions.release(session_id, owner="setup", idle=True)
    original = _make_task(
        brain,
        session_id=session_id,
        worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )

    created = brain.child_tasks.start_root(
        {
            "prompt": "Investigate the unrelated invoice question",
            "reason": "This is independent of the active implementation task",
        },
        {"task_id": original["id"]},
    )

    parallel = brain.store.get_task(created["task_id"])
    assert parallel["session_id"] == session_id
    assert parallel["parent_task_id"] is None
    assert parallel["root_task_id"] == parallel["id"]
    assert parallel["state"] == AgentRunState.QUEUED.value
    assert parallel["prompt"] == "Investigate the unrelated invoice question"
    assert brain.store.get_task(original["id"])["state"] == "waiting"
    assert brain.sessions.get(session_id)["manager_state"] == "queued"


def test_manage_child_task_cancels_a_general_child(brain):
    session_id = _session_id(brain, "slack:dm:person_ajay:cancel-child")
    root = _make_task(
        brain, session_id=session_id, worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )
    child = _make_task(
        brain, session_id=session_id, worker_type="general", state=AgentRunState.RUNNING.value,
        parent_task_id=root["id"], root_task_id=root["id"],
    )

    result = brain.child_tasks.manage(
        {"child_task_id": child["id"], "action": "cancel", "reason": "no longer needed"},
        {"task_id": root["id"]},
    )
    assert result["state"] == "cancelled"
    assert brain.store.get_task(child["id"])["state"] == "cancelled"


def test_manage_child_task_rejects_a_task_that_is_not_actually_a_child(brain):
    session_id = _session_id(brain, "slack:dm:person_ajay:not-a-child")
    root = _make_task(
        brain, session_id=session_id, worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )
    unrelated = _make_task(
        brain, session_id=session_id, worker_type="general", state=AgentRunState.RUNNING.value,
    )
    try:
        brain.child_tasks.manage(
            {"child_task_id": unrelated["id"], "action": "cancel"},
            {"task_id": root["id"]},
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_manage_child_task_revises_a_pi_child_and_restarts_with_new_prompt_same_workspace(brain):
    _provision_workspace(brain)
    brain.pi_task_executor.cancel_grace_seconds = 0
    original_prompt = "reproduce and fix the flaky test"
    revised_prompt = "actually just skip the flaky test for now"

    def fake_command(prompt: str) -> list[str]:
        if prompt == revised_prompt:
            return [
                "echo",
                '{"type":"agent_end","messages":[{"role":"assistant",'
                '"content":[{"type":"text","text":"skipped it"}]}],'
                '"willRetry":false}',
            ]
        return ["sleep", "5"]

    brain.pi_task_executor.command_builder = fake_command
    brain.pi_task_executor.poll_interval_seconds = 0
    brain.child_tasks.pi_executor = brain.pi_task_executor

    session_id = _session_id(brain, "slack:dm:person_ajay:revise-pi")
    root = _make_task(
        brain, session_id=session_id, worker_type="session_manager",
        state=AgentRunState.WAITING.value,
    )
    child = _make_task(
        brain, session_id=session_id, worker_type="pi", state=AgentRunState.QUEUED.value,
        parent_task_id=root["id"], root_task_id=root["id"], prompt=original_prompt,
    )
    brain.pi_task_executor.start(child["id"], owner="exec_pi", enqueue_poll=lambda *a, **k: None)
    assert brain.store.get_task(child["id"])["state"] == "running"

    result = brain.child_tasks.manage(
        {
            "child_task_id": child["id"],
            "action": "revise",
            "revised_prompt": revised_prompt,
            "reason": "priorities changed",
        },
        {"task_id": root["id"]},
    )
    assert result["state"] == "revising"
    revising_task = brain.store.get_task(child["id"])
    assert revising_task["prompt"] == revised_prompt
    assert revising_task["metadata"]["pending_revision"] is True

    # Drive the poll loop: cancellation of the original sleep-5 attempt
    # (escalating to SIGKILL immediately, cancel_grace_seconds=0), the
    # automatic restart with the revised prompt, and that new attempt's
    # own near-instant success.
    final_task = None
    for _ in range(30):
        final_task = brain.store.get_task(child["id"])
        if final_task["state"] in {"completed", "failed", "cancelled"}:
            break
        brain.pi_task_executor.poll(child["id"], enqueue_poll=lambda *a, **k: None)
        time.sleep(0.02)

    assert final_task["state"] == "completed", final_task.get("error")
    assert final_task["prompt"] == revised_prompt
    assert "pending_revision" not in (final_task.get("metadata") or {})

    attempts = brain.store.list_task_attempts(child["id"])
    assert len(attempts) == 2
    assert attempts[0]["status"] == "cancelled"
    assert attempts[1]["status"] == "succeeded"
    assert attempts[0]["workspace_path"] == attempts[1]["workspace_path"]
