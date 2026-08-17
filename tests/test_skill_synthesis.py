"""The restricted Skill Curator owns learned Skill proposals."""

from __future__ import annotations

import json

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse


def _completed_meaningful_task(brain, goal: str) -> str:
    destination = {
        "type": "internal_agent_session",
        "id": "curator-test",
        "is_private": True,
    }
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="api:curator-test",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    run = brain.company_agent.create_invocation(
        envelope=envelope, goal=goal, trigger_kind="reactive", initial_message=goal
    )
    with brain.store.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET state = 'completed', completed_at = ?, payload = ? WHERE id = ?",
            (
                run["updated_at"],
                json.dumps(
                    {
                        **run,
                        "state": "completed",
                        "final_answer": "The queue was checked and no jobs were stuck.",
                        "completion_summary": f"Goal: {goal}\nOutcome: no stuck jobs",
                        "tool_call_count": 1,
                    }
                ),
                run["id"],
            ),
        )
    brain.store.index_root_task(brain.store.get_task(run["id"]))
    return run["id"]


def test_curator_creates_grounded_draft_with_task_provenance(brain):
    task_id = _completed_meaningful_task(
        brain, "Check the export worker queue and report anything stuck"
    )
    brain.skill_curator.model = ScriptedModelClient(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "outcome": "NEW_SKILL_DRAFT",
                        "reason": "A reusable queue check was verified.",
                        "name": "Export queue health check",
                        "description": "Check the export queue for stuck jobs.",
                        "instructions": "Read queue depth and oldest-job age; report facts only.",
                        "existing_skill_id": None,
                    }
                )
            )
        ]
    )

    result = brain.skill_curator.review(task_id)

    skill = brain.skills.get(result["skill_id"])
    assert result["outcome"] == "NEW_SKILL_DRAFT"
    assert skill["status"] == "draft"
    assert skill["origin"]["source_task_ids"] == [task_id]
    assert "queue depth" in skill["instructions"]


def test_curator_degrades_to_no_change_when_model_output_is_invalid(brain):
    task_id = _completed_meaningful_task(brain, "Rotate the export pager")
    brain.skill_curator.model = ScriptedModelClient(
        [ModelResponse(content="not valid JSON")]
    )

    result = brain.skill_curator.review(task_id)

    assert result["outcome"] == "NO_CHANGE"
    assert result["reason"].startswith("curator_unavailable:")
    assert not [
        skill for skill in brain.skills.list("org_acme") if skill.get("status") == "draft"
    ]
