"""Skill synthesis drafts concrete steps grounded in what the recurring
Tasks actually did (memory ingestion/retrieval plan §10), instead of the
empty-steps proposal shell that shipped before this."""

from __future__ import annotations

import json

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse

_EMPTY_EXTRACTION_RESPONSE = json.dumps({"proposed_entities": [], "memories": []})
_SYNTHESIS_RESPONSE = json.dumps(
    {
        "steps": [
            {
                "name": "Query the export queue depth",
                "description": "Call brain_search / the export worker's queue-depth tool and read the backlog count.",
                "source_task_ids": ["placeholder"],  # patched per-test with real task ids
            }
        ],
        "inputs": ["export queue name"],
        "preconditions": ["Requester has read access to the export worker's operational channel"],
        "expected_outputs": ["A backlog count and a flag for anything stuck longer than the SLA"],
        "failure_handling": ["If the queue tool times out, report that explicitly rather than guessing a count"],
    }
)


def _run_three_recurring_tasks(brain, goal: str) -> list[str]:
    destination = {"type": "internal_agent_session", "id": "synthesis-test", "is_private": True}
    task_ids = []
    for i in range(3):
        decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
        envelope = InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id=f"api:synthesis-test:{i}",
            destination=destination,
            purpose="reactive_agent",
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        )
        run = brain.company_agent.create_invocation(
            envelope=envelope, goal=goal, trigger_kind="reactive", initial_message=goal
        )
        outcome = brain.company_agent.execute_task(run["id"])
        assert outcome.state.value == "completed"
        task_ids.append(run["id"])
        brain.harness_memory.capture_task(run["id"])
    return task_ids


def test_proposed_skill_draft_carries_model_synthesized_steps_with_provenance(brain):
    goal = "Check the export worker queue for backlog and report anything stuck"
    # 3 capture_task calls each do one extraction generate() call; the 3rd
    # also triggers synthesis on the 3rd occurrence -- queue 3 extraction
    # responses then the synthesis response.
    scripted = ScriptedModelClient(
        [
            ModelResponse(content=_EMPTY_EXTRACTION_RESPONSE, model="test-model"),
            ModelResponse(content=_EMPTY_EXTRACTION_RESPONSE, model="test-model"),
            ModelResponse(content=_EMPTY_EXTRACTION_RESPONSE, model="test-model"),
            ModelResponse(content=_SYNTHESIS_RESPONSE, model="test-model"),
        ]
    )
    brain.harness_memory.model_extractor.model = scripted

    task_ids = _run_three_recurring_tasks(brain, goal)

    signals = brain.store.list_harness_records("task_pattern_signals", "org_acme", where={"status": "proposed"})
    assert len(signals) == 1
    skill = brain.skills.get(signals[0]["proposed_skill_id"])
    assert skill["status"] == "draft"
    assert skill["steps"]
    assert skill["steps"][0]["name"] == "Query the export queue depth"
    assert skill["inputs"] == ["export queue name"]
    assert skill["preconditions"]
    assert any("Failure handling" in w for w in skill["warnings"])

    # The synthesis call itself was given all three Tasks' real evidence.
    synthesis_request = scripted.requests[-1]
    sent_task_ids = {
        task["task_id"] for task in json.loads(synthesis_request["messages"][1]["content"])["tasks"]
    }
    assert sent_task_ids == set(task_ids)


def test_synthesis_is_skipped_when_the_extraction_model_is_not_live(brain):
    """Default fixture model (deterministic-harness) -- no model call is
    made, and the draft still gets created with empty steps exactly as
    before this feature existed."""
    goal = "Rotate the on-call schedule for the export worker pager"
    task_ids = _run_three_recurring_tasks(brain, goal)
    assert len(task_ids) == 3

    signals = brain.store.list_harness_records("task_pattern_signals", "org_acme", where={"status": "proposed"})
    assert len(signals) == 1
    skill = brain.skills.get(signals[0]["proposed_skill_id"])
    assert skill["steps"] == []
    assert any("left empty" in w for w in skill["warnings"])
