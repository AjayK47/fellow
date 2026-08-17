"""End-to-end proof for the Session Manager's Task path: an ordinary DM
request completes through Session/Task dispatch -- no legacy job -- reusing
the real CompanyAgent reasoning
loop (context prep, tool execution, compaction, skill validation) via
TaskRunStore rather than a reimplementation."""

from __future__ import annotations

import pytest

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall
from toda.harness.runtime import CompanyAgent
from toda.harness.session_manager import SessionManagerScheduler
from toda.harness.sessions import SessionService


def _scheduler(brain) -> tuple[SessionManagerScheduler, SessionService]:
    sessions = SessionService(store=brain.store, audit=brain.audit)
    old = brain.company_agent

    def agent_factory(task_store):
        return CompanyAgent(
            runs=task_store,
            context_service=old.context_service,
            model=old.model,
            tools=old.tools,
            skills=old.skills,
            skill_validator=old.skill_validator,
            audit=old.audit,
            enqueue_job=old.enqueue_job,
            graph=old.graph,
            history_compactor=old.history_compactor,
            workspaces=old.workspaces,
            max_iterations=old.max_iterations,
            max_tool_calls=old.max_tool_calls,
            context_token_budget=old.context_token_budget,
            max_total_tokens=old.max_total_tokens,
            completion_max_tokens=old.completion_max_tokens,
        )

    scheduler = SessionManagerScheduler(
        store=brain.store,
        authz=brain.authz,
        sessions=sessions,
        agent_factory=agent_factory,
    )
    return scheduler, sessions


def test_ordinary_dm_request_completes_through_the_new_session_task_path(brain):
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Hello! How can I help?")]
    )
    scheduler, sessions = _scheduler(brain)

    wake = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:new",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="hi there",
        principal_id="person_ajay",
    )

    outcome = scheduler.dispatch(wake["session_id"])

    assert outcome is not None
    assert outcome.state.value == "completed"
    assert outcome.answer == "Hello! How can I help?"

    # The whole thing happened through the tasks table.
    task = brain.store.get_task(outcome.task_id)
    assert task is not None
    assert task["worker_type"] == "session_manager"
    assert task["session_id"] == wake["session_id"]
    assert task["max_iterations"] == 50
    assert task["max_tool_calls"] == 100

    session = sessions.get(wake["session_id"])
    assert session["manager_state"] == "idle"
    assert session["processed_revision"] == session["manager_revision"] == 1


def test_a_second_message_after_completion_starts_a_fresh_root_task(brain):
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(content="First answer."),
            ModelResponse(content="Second answer."),
        ]
    )
    scheduler, sessions = _scheduler(brain)
    wake1 = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:seq",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="first question",
        principal_id="person_ajay",
    )
    outcome1 = scheduler.dispatch(wake1["session_id"])
    assert outcome1.answer == "First answer."

    wake2 = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:seq",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="second question",
        principal_id="person_ajay",
    )
    assert wake2["session_id"] == wake1["session_id"]
    outcome2 = scheduler.dispatch(wake2["session_id"])
    assert outcome2.answer == "Second answer."
    assert outcome2.task_id != outcome1.task_id

    root_tasks = brain.store.list_tasks(
        "org_acme", session_id=wake1["session_id"], worker_type="session_manager"
    )
    assert len(root_tasks) == 2
    assert all(t["state"] == "completed" for t in root_tasks)


def test_tool_call_needing_approval_pauses_and_resumes_through_the_new_path(brain):
    """The harder state-transition path: WAITING_FOR_APPROVAL -> release ->
    resume -> reclaim -> COMPLETED, exercising ToolExecutor's real approval
    gate and CompanyAgent.resume_after_approval() -- both written against
    the AgentRunStore interface, both unmodified here -- against Task
    dicts instead of legacy run rows."""
    brain.composio.api_key = None
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_skill_proposal",
                        # Any always-gated tool works -- this test is about
                        # the pause/resume state machine, not the tool.
                        # Automation tools are native-allow and never pause.
                        name="skill_propose_create",
                        arguments={
                            "name": "session-task-redteam",
                            "description": "recurring check",
                            "goal": "recurring check",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done, proposal created."),
        ]
    )
    scheduler, sessions = _scheduler(brain)
    wake = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:approve",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="propose a skill for the recurring check",
        principal_id="person_ajay",
    )
    outcome = scheduler.dispatch(wake["session_id"])
    assert outcome.state.value == "waiting"
    approval_id = outcome.approval_id
    assert approval_id

    task = brain.store.get_task(outcome.task_id)
    assert task["state"] == "waiting"
    assert task["waiting_for"]["type"] == "approval"
    assert task["execution_owner"] is None  # released, not held mid-pause

    session = sessions.get(wake["session_id"])
    assert session["manager_state"] == "idle"

    # Resolving the approval resumes the SAME task and completes it --
    # exercising CompanyAgent.resume_after_approval() unmodified.
    def agent_factory(task_store):
        old = brain.company_agent
        return CompanyAgent(
            runs=task_store, context_service=old.context_service, model=old.model,
            tools=old.tools, skills=old.skills, skill_validator=old.skill_validator,
            audit=old.audit, enqueue_job=old.enqueue_job, graph=old.graph,
            history_compactor=old.history_compactor, workspaces=old.workspaces,
            max_iterations=old.max_iterations, max_tool_calls=old.max_tool_calls,
            context_token_budget=old.context_token_budget,
            max_total_tokens=old.max_total_tokens,
            completion_max_tokens=old.completion_max_tokens,
        )

    from toda.harness.task_run_store import TaskRunStore

    resume_runs = TaskRunStore(brain.store, owner="executor-resume", lease_seconds=1800)
    resume_agent = agent_factory(resume_runs)
    resumed = resume_agent.resume_after_approval(
        approval_id=approval_id, approved=True, decided_by="person_ajay"
    )
    assert resumed.state.value == "completed", resumed.error
    assert resumed.answer == "Done, proposal created."
    final_task = brain.store.get_task(outcome.task_id)
    assert final_task["state"] == "completed"
    assert final_task["execution_owner"] is None


def test_two_sessions_manager_dispatch_does_not_double_execute(brain):
    """A second dispatch() call while the first is still (logically)
    running must not be able to claim the session -- proving the exact
    concurrent-first-message race identified in Phase 0 is now closed."""
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Answer.")]
    )
    scheduler, sessions = _scheduler(brain)
    wake = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:double",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="hi",
        principal_id="person_ajay",
    )
    # Manually claim first, simulating a slower first executor still "in
    # flight" at the moment a second dispatch is attempted.
    executor_a = "executor-a"
    assert sessions.claim(wake["session_id"], owner=executor_a)
    assert scheduler.dispatch(wake["session_id"]) is None
    # The manual claim never actually processed the pending revision, so
    # releasing it goes back to `queued` (still has work), not `idle`.
    sessions.release(wake["session_id"], owner=executor_a, idle=False)
    outcome = scheduler.dispatch(wake["session_id"])
    assert outcome.state.value == "completed"


def test_manager_and_task_leases_are_one_hour_and_renew_each_iteration(brain):
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Finished under a renewable lease.")]
    )
    scheduler, sessions = _scheduler(brain)
    renewals: list[tuple[str, int]] = []
    original_renew = sessions.renew

    def recording_renew(session_id: str, *, owner: str, lease_seconds: int) -> bool:
        renewals.append((session_id, lease_seconds))
        return original_renew(session_id, owner=owner, lease_seconds=lease_seconds)

    sessions.renew = recording_renew
    wake = sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:lease-heartbeat",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="finish this",
        principal_id="person_ajay",
    )

    outcome = scheduler.dispatch(wake["session_id"])

    assert outcome.state.value == "completed"
    assert scheduler.lease_seconds == 3600
    assert scheduler.task_lease_seconds == 3600
    # One renewal occurs before execution and at least one more at the
    # CompanyAgent iteration boundary through TaskRunStore's heartbeat.
    assert len(renewals) >= 2
    assert {lease_seconds for _, lease_seconds in renewals} == {3600}


def test_worker_retries_dispatch_while_manager_busy_but_coalesces_after_processing(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:dispatch-race",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="do this once",
        principal_id="person_ajay",
    )
    assert brain.sessions.claim(wake["session_id"], owner="executor-a")

    with pytest.raises(RuntimeError, match="session_manager_busy"):
        brain.harness_worker._dispatch_session({"session_id": wake["session_id"]})

    brain.sessions.mark_processed(wake["session_id"], wake["revision"])
    assert brain.sessions.release(wake["session_id"], owner="executor-a", idle=True)
    assert brain.harness_worker._dispatch_session(
        {"session_id": wake["session_id"]}
    ) == {"dispatched": False, "coalesced": True}
