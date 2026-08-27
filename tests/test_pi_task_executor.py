"""Phase 5: PiTaskExecutor's own durability mechanics -- checkpoint wake
(plan §10.3.1: liveness is not progress), stale-heartbeat reclaim, and
cancellation -- exercised directly against the executor with real (but
short-lived, fake) subprocesses, rather than through a full delegate_tasks
round trip (already covered end-to-end in test_child_tasks.py)."""

from __future__ import annotations

import os
import signal
import time
from datetime import timedelta

import pytest

from toda.domain.models import new_id, utcnow
from toda.harness.models import AgentRunState, DestinationEnvelope, InvocationEnvelope
from toda.harness.pi_task_executor import check_hardline_floor


def _provision_workspace(brain) -> None:
    brain.workspaces.get_or_create(
        organization_id="org_acme",
        owner_id="person_ajay",
        scope_id="scope_org_public",
    )
    brain.pi_task_executor.isolation_required = False


def _session_id(brain, key: str) -> str:
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=key,
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="fix it",
        principal_id="person_ajay",
    )
    return wake["session_id"]


def _make_pi_task(brain, *, session_id: str, prompt: str = "fix it") -> dict:
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="slack_toda",
        conversation_id="slack:dm:person_ajay:pi-direct",
        destination=DestinationEnvelope(type="dm", id="D1", is_private=True),
        purpose="reactive_agent",
        permission_decision_id="permdecision_test",
        allowed_scope_ids=[],
    ).model_dump(mode="json")
    now = utcnow().isoformat()
    task_id = new_id("task")
    task = {
        "id": task_id,
        "organization_id": "org_acme",
        "session_id": session_id,
        "parent_task_id": None,
        "root_task_id": task_id,
        "state": AgentRunState.QUEUED.value,
        "worker_type": "pi",
        "trigger_kind": "tool_continuation",
        "trigger_event_id": None,
        "prompt": prompt,
        "goal": prompt,
        "envelope": envelope,
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
    brain.store.create_task(task)
    return task


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _noop_enqueue(*args, **kwargs) -> None:
    return None


def test_pi_hardline_floor_blocks_push_when_task_says_do_not_push(tmp_path):
    assert check_hardline_floor(
        "Implement the fix, but do not push anything.",
        ["git", "push", "origin", "main"],
        tmp_path,
    )
    assert check_hardline_floor(
        "Implement the fix and prepare a local commit.",
        ["git", "commit", "-am", "fix"],
        tmp_path,
    ) is None


def test_the_exit_code_decides_success_not_anything_in_the_log(brain):
    """The exit marker is the verdict.

    Success used to hinge on a `status` field in Pi's output that Pi has
    never emitted, so a clean run finalized as `failed` while a broken one
    that happened to print the right JSON would have passed. Neither run
    here writes a parseable event at all, which is the point: the marker
    alone has to separate them.
    """
    _provision_workspace(brain)
    brain.pi_task_executor.poll_interval_seconds = 0

    outcomes = {}
    for name, command in (("clean", ["true"]), ("broken", ["false"])):
        brain.pi_task_executor.command_builder = lambda prompt, c=command: c
        task = _make_pi_task(
            brain, session_id=_session_id(brain, f"slack:dm:person_ajay:exit-{name}")
        )
        brain.pi_task_executor.start(
            task["id"], owner=f"exec_{name}", enqueue_poll=_noop_enqueue
        )
        for _ in range(30):
            if brain.store.get_task(task["id"])["state"] in {"completed", "failed"}:
                break
            brain.pi_task_executor.poll(task["id"], enqueue_poll=_noop_enqueue)
            time.sleep(0.02)
        attempt = brain.store.list_task_attempts(task["id"])[-1]
        outcomes[name] = (brain.store.get_task(task["id"])["state"], attempt["status"])

    assert outcomes["clean"] == ("completed", "succeeded")
    assert outcomes["broken"] == ("failed", "failed")


def test_checkpoint_wake_notifies_session_without_ending_the_task(brain):
    _provision_workspace(brain)
    brain.pi_task_executor.command_builder = lambda prompt: ["sleep", "0.3"]
    brain.pi_task_executor.checkpoint_interval_seconds = 0
    brain.pi_task_executor.poll_interval_seconds = 0

    session_id = _session_id(brain, "slack:dm:person_ajay:checkpoint")
    task = _make_pi_task(brain, session_id=session_id)

    session_before = brain.sessions.get(session_id)
    brain.sessions.mark_processed(session_id, session_before["manager_revision"])
    session_before = brain.sessions.get(session_id)
    assert session_before["processed_revision"] == session_before["manager_revision"]

    started = brain.pi_task_executor.start(
        task["id"], owner="exec_test", enqueue_poll=_noop_enqueue
    )
    assert started["started"] is True
    live = brain.store.get_task(task["id"])
    assert live["state"] == "running"

    polled = brain.pi_task_executor.poll(task["id"], enqueue_poll=_noop_enqueue)
    assert polled.get("checkpointed") is True
    still_running = brain.store.get_task(task["id"])
    assert still_running["state"] == "running"  # checkpoint judges, never ends the task

    session_after = brain.sessions.get(session_id)
    assert session_after["manager_revision"] > session_after["processed_revision"]

    # Cleanup: let the short sleep finish on its own (it's near-instant);
    # nothing left to reap since it wasn't in our own process group wait.
    time.sleep(0.35)


def test_stale_heartbeat_reclaims_a_crashed_looking_attempt(brain):
    _provision_workspace(brain)
    brain.pi_task_executor.command_builder = lambda prompt: ["sleep", "5"]
    brain.pi_task_executor.stale_heartbeat_seconds = 1

    session_id = _session_id(brain, "slack:dm:person_ajay:stale")
    task = _make_pi_task(brain, session_id=session_id)
    brain.pi_task_executor.start(task["id"], owner="exec_test", enqueue_poll=_noop_enqueue)
    attempt = brain.store.list_task_attempts(task["id"])[0]
    pid = attempt["pid"]
    assert _pid_alive(pid)

    try:
        # Backdate the heartbeat well past the stale threshold -- proves
        # reclaim is driven by missing heartbeat, not total run time or
        # actual process death (the process is still very much alive here).
        attempt["last_heartbeat_at"] = (utcnow() - timedelta(seconds=10)).isoformat()
        attempt["updated_at"] = utcnow().isoformat()
        brain.store.update_task_attempt(attempt)

        brain.pi_task_executor.poll(task["id"], enqueue_poll=_noop_enqueue)
        task_after = brain.store.get_task(task["id"])
        assert task_after["state"] == "failed"
        final_attempt = brain.store.list_task_attempts(task["id"])[-1]
        assert final_attempt["status"] == "crashed"

        session = brain.sessions.get(session_id)
        assert session["manager_revision"] > session["processed_revision"]
    finally:
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stale_pi_owner_cannot_finalize_after_task_reclaim(brain):
    session_id = _session_id(brain, "slack:dm:person_ajay:pi-owner-loss")
    task = _make_pi_task(brain, session_id=session_id)
    assert brain.store.claim_task(task["id"], owner="old-pi", lease_seconds=-1)
    assert brain.store.claim_task(task["id"], owner="new-pi", lease_seconds=60)
    task = brain.store.get_task(task["id"])
    attempt = {
        "id": "attempt_stale_pi",
        "organization_id": task["organization_id"],
        "task_id": task["id"],
        "status": "running",
        "owner": "old-pi",
        "output_tail": "",
    }

    with pytest.raises(RuntimeError, match="lease lost while finalizing"):
        brain.pi_task_executor._finalize(
            task,
            attempt,
            owner="old-pi",
            exit_code=0,
            output=(
                '{"type":"agent_end","messages":[{"role":"assistant",'
                '"content":[{"type":"text","text":"stale answer"}]}],'
                '"willRetry":false}'
            ),
        )

    current = brain.store.get_task(task["id"])
    assert current["state"] == "running"
    assert current["execution_owner"] == "new-pi"
    assert current.get("final_answer") is None
    assert attempt["status"] == "running"


def test_cancel_terminates_process_and_finalizes_as_cancelled(brain):
    _provision_workspace(brain)
    brain.pi_task_executor.command_builder = lambda prompt: ["sleep", "5"]
    # Observed on this platform's /bin/sh: a bare SIGTERM to the process
    # group doesn't reliably reach a shell wrapper blocked synchronously on
    # its foreground child -- poll()'s grace-period SIGKILL escalation is
    # what actually guarantees termination. Zero grace means the very next
    # poll after cancel() escalates immediately, keeping this test fast.
    brain.pi_task_executor.cancel_grace_seconds = 0

    session_id = _session_id(brain, "slack:dm:person_ajay:cancel")
    task = _make_pi_task(brain, session_id=session_id)
    brain.pi_task_executor.start(task["id"], owner="exec_test", enqueue_poll=_noop_enqueue)
    attempt = brain.store.list_task_attempts(task["id"])[0]
    pid = attempt["pid"]
    assert _pid_alive(pid)

    result = brain.pi_task_executor.cancel(task["id"], reason="user asked to stop")
    assert result["cancelled"] is True

    brain.pi_task_executor.poll(task["id"], enqueue_poll=_noop_enqueue)
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.02)
    assert not _pid_alive(pid), "SIGKILL escalation should have terminated the sleep"

    task_after = brain.store.get_task(task["id"])
    assert task_after["state"] == "cancelled"
    final_attempt = brain.store.list_task_attempts(task["id"])[-1]
    assert final_attempt["status"] == "cancelled"
    assert final_attempt["cancellation_reason"] == "user asked to stop"
