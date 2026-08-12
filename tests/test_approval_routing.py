"""Approval resolution must route to the CompanyAgent for the owning Task.
Before this fix, HarnessWorker._resolve_approval always used a fixed agent,
which could look a Task id up in the wrong persistence path and fail.

This exercises the real "approval.resolve" job handler production code
actually runs, not CompanyAgent.resume_after_approval() called directly
(already covered for the Task path by test_session_manager_scheduler.py)."""

from __future__ import annotations

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall


def test_resolve_approval_job_routes_a_task_backed_approval_to_the_task_agent(brain):
    brain.composio.api_key = None
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_skill_proposal",
                        # Any always-gated tool works here -- this test is
                        # about which agent resolves the approval, not about
                        # the tool itself. Automation tools are native-allow
                        # and would never produce an approval to route.
                        name="skill_propose_create",
                        arguments={
                            "name": "approval routing test",
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

    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:dm:person_ajay:approval-routing",
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content="propose a skill for the recurring check",
        principal_id="person_ajay",
    )
    outcome = brain.session_manager.dispatch(wake["session_id"])
    assert outcome.state.value == "waiting_for_approval"
    approval_id = outcome.approval_id
    assert approval_id

    task = brain.store.get_task(outcome.task_id)
    assert task["state"] == "waiting_for_approval"

    # This is the real production job handler -- not CompanyAgent called
    # directly -- proving HarnessWorker itself resolves the right agent.
    result = brain.harness_worker._resolve_approval(
        {"approval_id": approval_id, "approved": True, "decided_by": "person_ajay"}
    )
    assert result["state"] == "completed", result.get("error")
    assert result["answer"] == "Done, proposal created."

    final_task = brain.store.get_task(outcome.task_id)
    assert final_task["state"] == "completed"
    assert final_task["execution_owner"] is None


def test_approval_agent_rejects_an_unknown_approval(brain):
    # An approval must always identify a real Task; there is no legacy-agent
    # fallback that could resume the wrong execution.
    import pytest

    with pytest.raises(KeyError):
        brain.harness_worker._approval_agent("nonexistent_approval_id")
