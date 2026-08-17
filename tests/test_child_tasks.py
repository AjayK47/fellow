"""Phase 4's own stated deliverable: one large Task creates three parallel
child Tasks (general + pi) and receives one Manager-synthesized result --
via delegate_tasks (child_tasks.py) and the real job-queue-dispatched
ChildTaskExecutor, wired through the container exactly as production would
run it. Nothing here blocks a Python thread waiting on the children: the
root Task releases its execution lease and the Session goes idle the moment
delegation happens, only resuming once every child's terminal transition has
notified the Session.

The worker_type="pi" child in these tests exercises the real, durable
PiTaskExecutor (Phase 5) end to end -- a genuine detached subprocess, a
real exit-code marker file, real polling -- just with a fast fake `pi`
command instead of the real CLI (not installed in this sandbox).
"""

from __future__ import annotations

import time

import pytest

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall, ToolDefinition

# Shaped like a real `pi --mode json` close: an `agent_end` carrying the
# message list, then a bare `agent_settled`. See test_pi_command.py, which
# pins this against the actual CLI so the two cannot drift apart.
_FAKE_PI_SUCCESS_JSON = (
    '{"type":"agent_end","messages":[{"role":"assistant","content":'
    '[{"type":"text","text":"Applied and tested a fix for the null '
    'timestamp."}],"model":"z-ai/glm-5.2","stopReason":"stop",'
    '"usage":{"totalTokens":2699}}],"willRetry":false}'
)


def _provision_workspace(brain) -> None:
    brain.workspaces.get_or_create(
        organization_id="org_acme",
        owner_id="person_ajay",
        scope_id="scope_org_public",
    )


def _fake_pi_command(prompt: str) -> list[str]:
    # A real detached subprocess (see pi_task_executor.py's module docstring
    # for why: exit-code-marker durability requires an actual process, not
    # a mocked Popen), just standing in for the real `pi` CLI with an
    # instant, deterministic success event. Shaped like pi_command_builder's
    # output (bare executable name, no absolute path argv[0]) so it doesn't trip
    # the hardline floor's path-escape check the way an explicit /bin/sh
    # invocation would -- PiTaskExecutor.start() does its own /bin/sh -c
    # wrapping internally, after the floor check.
    return ["echo", _FAKE_PI_SUCCESS_JSON]


def _drain_pi_poll_until_terminal(brain, task_id: str, *, max_attempts: int = 20) -> dict:
    for _ in range(max_attempts):
        task = brain.store.get_task(task_id)
        if task["state"] in {"completed", "failed", "cancelled"}:
            return task
        brain.harness_worker.drain_until_idle(kinds=["task.pi_poll"])
        time.sleep(0.02)
    return brain.store.get_task(task_id)


def test_root_task_delegates_three_children_and_synthesizes_result(brain):
    # Force strictly sequential job draining so the shared ScriptedModelClient
    # queue (a plain, non-thread-safe deque) hands out responses in a
    # deterministic order across the two concurrently-dispatchable general
    # children (the pi child never touches the model at all).
    brain.harness_worker.concurrency = 1
    _provision_workspace(brain)
    brain.pi_task_executor.command_builder = _fake_pi_command
    # The default 10s poll interval is real-world durability spacing, not a
    # test-speed knob -- a rescheduled poll job isn't lease-eligible until
    # its available_at passes, so without this override the test would need
    # to sleep 10 real seconds between drains for no reason.
    brain.pi_task_executor.poll_interval_seconds = 0
    # PiTaskExecutor is only wired live by default when pi_coding_enabled is
    # set; bypass that gate here since real Pi subprocess behavior (not the
    # config flag) is what's under test.
    brain.child_task_executor.pi_executor = brain.pi_task_executor

    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_delegate",
                        name="delegate_tasks",
                        arguments={
                            "tasks": [
                                {
                                    "worker_type": "general",
                                    "prompt": "Investigate log source A for the export failure.",
                                },
                                {
                                    "worker_type": "general",
                                    "prompt": "Investigate log source B for the export failure.",
                                },
                                {
                                    "worker_type": "pi",
                                    "prompt": "Reproduce and fix the export failure. Do not push.",
                                },
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Source A: nothing unusual."),
            ModelResponse(content="Source B: found a null timestamp bug."),
            ModelResponse(
                content="Root cause was the null-timestamp bug (source B); fixed and tested."
            ),
        ]
    )

    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:children",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="investigate and fix the export failure",
        principal_id="person_ajay",
    )

    outcome = brain.session_manager.dispatch(wake["session_id"])
    assert outcome is not None
    assert outcome.state.value == "waiting"

    root_task = brain.store.get_task(outcome.task_id)
    assert root_task["state"] == "waiting"
    assert root_task["waiting_for"]["type"] == "child_task"
    assert root_task["execution_owner"] is None  # released, not held mid-wait
    child_ids = root_task["pending_child_task_ids"]
    assert len(child_ids) == 3

    session = brain.sessions.get(wake["session_id"])
    assert session["manager_state"] == "idle"

    children = [brain.store.get_task(cid) for cid in child_ids]
    assert all(child["parent_task_id"] == root_task["id"] for child in children)
    assert all(child["root_task_id"] == root_task["id"] for child in children)
    assert all(child["state"] == "queued" for child in children)
    assert all(child["max_iterations"] == 200 for child in children)
    assert all(child["max_tool_calls"] == 500 for child in children)
    assert sorted(child["worker_type"] for child in children) == [
        "general",
        "general",
        "pi",
    ]
    pi_child_id = next(c["id"] for c in children if c["worker_type"] == "pi")
    general_child_ids = [c["id"] for c in children if c["worker_type"] == "general"]

    # Simulate the background worker draining the child jobs delegate_tasks
    # enqueued -- the real "task.run_child" job kind, the real ChildTaskExecutor,
    # not a reimplementation. This runs both general children to completion
    # and starts (but does not finish) the pi child's subprocess attempt.
    drained = brain.harness_worker.drain_until_idle(kinds=["task.run_child"])
    assert drained["failed"] == 0, drained["results"]
    assert drained["completed"] == 3

    for cid in general_child_ids:
        child = brain.store.get_task(cid)
        assert child["state"] == "completed", child.get("error")
        assert child["execution_owner"] is None

    pi_task = brain.store.get_task(pi_child_id)
    assert pi_task["state"] == "running"
    assert pi_task.get("active_attempt_id")
    attempts = brain.store.list_task_attempts(pi_child_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"
    assert attempts[0]["pid"]

    # Drive the pi child's self-rescheduling poll job to its real terminal
    # state -- the fake subprocess finishes almost instantly, so this
    # converges in one or two ticks.
    pi_task = _drain_pi_poll_until_terminal(brain, pi_child_id)
    assert pi_task["state"] == "completed", pi_task.get("error")
    assert pi_task["execution_owner"] is None
    assert "active_attempt_id" not in pi_task or pi_task["active_attempt_id"] is None
    final_attempt = brain.store.list_task_attempts(pi_child_id)[-1]
    assert final_attempt["status"] == "succeeded"
    assert final_attempt["exit_code"] == 0
    assert final_attempt["terminal_event"]["type"] == "agent_end"
    assert (
        final_attempt["terminal_event"]["text"]
        == "Applied and tested a fix for the null timestamp."
    )

    # Each child's terminal transition notified the Session -- it must be
    # back to queued (work pending), not still idle.
    session = brain.sessions.get(wake["session_id"])
    assert session["manager_state"] == "queued"
    assert session["manager_revision"] > session["processed_revision"]

    final = brain.session_manager.dispatch(wake["session_id"])
    assert final is not None
    assert final.state.value == "completed"
    assert (
        final.answer
        == "Root cause was the null-timestamp bug (source B); fixed and tested."
    )

    root_task = brain.store.get_task(root_task["id"])
    assert root_task["state"] == "completed"
    assert root_task["execution_owner"] is None

    session = brain.sessions.get(wake["session_id"])
    assert session["manager_state"] == "idle"
    assert session["processed_revision"] == session["manager_revision"]


def _dispatch_delegation(brain, *, conversation_key: str, tasks: list[dict]):
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_delegate_focused",
                        name="delegate_tasks",
                        arguments={"tasks": tasks},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=conversation_key,
        destination={"type": "dm", "id": "D_CHILD_FOCUSED", "is_private": True},
        role="user",
        content="Do the delegated work",
        principal_id="person_ajay",
    )
    outcome = brain.session_manager.dispatch(wake["session_id"])
    assert outcome is not None
    assert outcome.state.value == "waiting"
    return wake, brain.store.get_task(outcome.task_id)


def test_general_child_is_temporary_specialist_with_task_local_handoff_and_tools(brain):
    wake, root = _dispatch_delegation(
        brain,
        conversation_key="slack:dm:person_ajay:temporary-specialist",
        tasks=[
            {
                "worker_type": "general",
                "prompt": "Inspect churn evidence and return cited findings.",
                "specialist": {
                    "name": "Revenue researcher",
                    "instructions": "Focus on renewals and contraction.",
                },
            }
        ],
    )
    child = brain.store.get_task(root["pending_child_task_ids"][0])
    assert child["metadata"]["specialist_name"] == "Revenue researcher"
    assert child["metadata"]["specialist_instructions"] == "Focus on renewals and contraction."
    assert child["metadata"]["suppress_delivery"] is True
    assert child["metadata"]["suppress_memory_capture"] is True

    # Its full handoff is durable and local to this child. Wider Session
    # history is still deliberately inspectable through session_search.
    local_history = brain.store.list_session_messages(
        wake["session_id"], task_id=child["id"]
    )
    assert [item["content"] for item in local_history] == [child["prompt"]]

    brain.tool_registry.register(
        ToolDefinition(
            name="child_test_read",
            description="read",
            input_schema={"type": "object", "properties": {}},
            risk="read",
        ),
        lambda _args, _runtime: {"ok": True},
    )
    brain.tool_registry.register(
        ToolDefinition(
            name="child_test_write",
            description="write",
            input_schema={"type": "object", "properties": {}},
            risk="write",
        ),
        lambda _args, _runtime: {"ok": True},
    )
    allowed = {
        definition.name for definition in brain.tool_executor.allowed_definitions(child)
    }
    assert {"child_test_read", "child_test_write", "session_search"} <= allowed
    assert not {
        "delegate_tasks",
        "manage_child_task",
        "start_parallel_task",
        "wait_for_children",
    } & allowed
    with pytest.raises(PermissionError, match="cannot delegate further"):
        brain.child_tasks.execute(
            {
                "tasks": [
                    {"worker_type": "general", "prompt": "nested work"}
                ]
            },
            {"task_id": child["id"]},
        )


def test_partial_child_completion_can_wait_then_continue_sequentially(brain):
    wake, root = _dispatch_delegation(
        brain,
        conversation_key="slack:dm:person_ajay:partial-children",
        tasks=[
            {"worker_type": "general", "prompt": "First independent investigation."},
            {"worker_type": "general", "prompt": "Second independent investigation."},
        ],
    )
    first_id, second_id = root["pending_child_task_ids"]
    assert brain.store.cancel_task(first_id)
    brain.store.bump_task_revision(first_id)
    brain.store.bump_task_revision(root["id"])
    brain.sessions.notify(wake["session_id"], reason="test.first_child.cancelled")

    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_wait_remaining",
                        name="wait_for_children",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    waiting = brain.session_manager.dispatch(wake["session_id"])
    assert waiting is not None
    assert waiting.state.value == "waiting"
    root = brain.store.get_task(root["id"])
    assert root["pending_child_task_ids"] == [first_id, second_id]
    assert root["execution_owner"] is None

    assert brain.store.cancel_task(second_id)
    brain.store.bump_task_revision(second_id)
    brain.store.bump_task_revision(root["id"])
    brain.sessions.notify(wake["session_id"], reason="test.second_child.cancelled")
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Both child results were reviewed; the sequence is complete.")]
    )
    completed = brain.session_manager.dispatch(wake["session_id"])
    assert completed is not None
    assert completed.state.value == "completed"
    root = brain.store.get_task(root["id"])
    assert "pending_child_task_ids" not in root


def test_adding_children_after_partial_result_preserves_running_siblings(brain):
    wake, root = _dispatch_delegation(
        brain,
        conversation_key="slack:dm:person_ajay:add-after-partial",
        tasks=[
            {"worker_type": "general", "prompt": "Fast investigation."},
            {"worker_type": "general", "prompt": "Slow investigation."},
        ],
    )
    first_id, still_running_id = root["pending_child_task_ids"]
    assert brain.store.cancel_task(first_id)
    brain.store.bump_task_revision(first_id)
    brain.store.bump_task_revision(root["id"])
    brain.sessions.notify(wake["session_id"], reason="test.partial.add_more")
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_delegate_followup",
                        name="delegate_tasks",
                        arguments={
                            "tasks": [
                                {
                                    "worker_type": "general",
                                    "prompt": "Follow up on the fast investigation result.",
                                }
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    outcome = brain.session_manager.dispatch(wake["session_id"])
    assert outcome is not None
    assert outcome.state.value == "waiting"
    updated = brain.store.get_task(root["id"])
    assert still_running_id in updated["pending_child_task_ids"]
    assert len(updated["pending_child_task_ids"]) == 2
