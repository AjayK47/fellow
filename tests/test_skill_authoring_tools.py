"""The model can only ever *propose* skills, never publish them directly.

skill_propose_create / skill_propose_update pause for the same approval
card every other write-risk tool uses; even once approved, the result is a
draft that stays invisible to brain_find_skills until a human separately
approves the skill version (services/skills.py).
"""

from __future__ import annotations

from docket.harness.models import InvocationEnvelope, ModelToolCall


def _envelope(brain, *, conversation: str) -> InvocationEnvelope:
    destination = {"type": "internal_agent_session", "id": "skills-test", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _run(brain, conversation: str):
    envelope = _envelope(brain, conversation=conversation)
    created = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="skill authoring probe",
        trigger_kind="reactive",
        initial_message="skill authoring probe",
    )
    return brain.store.get_task(created["id"])


def test_skill_propose_create_pauses_for_approval_and_lands_as_draft(brain):
    run = _run(brain, "skills:create")
    call = ModelToolCall(
        id="call_1",
        name="skill_propose_create",
        arguments={"name": "New Escalation Path", "description": "d", "goal": "g"},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "waiting_for_approval"

    # Not visible to any run before a human decides the approval, let alone
    # before the skill version itself is approved.
    assert "New Escalation Path" not in [s["name"] for s in brain.skills.search("org_acme", "escalation")]

    approval, resolved = brain.tool_executor.resolve_approval(
        approval_id=result.approval_id, approved=True, decided_by="person_ajay"
    )
    assert approval["status"] == "approved"
    skill = resolved.output["skill"]
    assert skill["status"] == "draft"

    # Approving the write tool call is not the same as approving the skill.
    assert "New Escalation Path" not in [s["name"] for s in brain.skills.search("org_acme", "escalation")]
    brain.skills.approve(skill["id"], skill["version"])
    assert "New Escalation Path" in [s["name"] for s in brain.skills.search("org_acme", "escalation")]


def test_skill_propose_create_has_no_status_argument_and_always_lands_draft(brain):
    definition = next(
        d for d in brain.tool_registry.definitions() if d.name == "skill_propose_create"
    )
    # The model has no schema-level channel to request any status other than
    # draft — additionalProperties is False and "status" isn't a property.
    assert "status" not in definition.input_schema["properties"]
    assert definition.input_schema["additionalProperties"] is False

    run = _run(brain, "skills:no-self-approve")
    call = ModelToolCall(
        id="call_2",
        name="skill_propose_create",
        arguments={"name": "Legit Skill", "description": "d", "goal": "g"},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    _, resolved = brain.tool_executor.resolve_approval(
        approval_id=result.approval_id, approved=True, decided_by="person_ajay"
    )
    assert resolved.output["skill"]["status"] == "draft"


def test_skill_propose_update_leaves_the_approved_skill_untouched(brain):
    skill = brain.skills.create(
        organization_id="org_acme", name="Rotate API Credentials", description="Original", goal="g", steps=[]
    )
    run = _run(brain, "skills:update")
    call = ModelToolCall(
        id="call_3",
        name="skill_propose_update",
        arguments={"skill_id": skill["id"], "description": "Updated with a new rollback step."},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "waiting_for_approval"
    _, resolved = brain.tool_executor.resolve_approval(
        approval_id=result.approval_id, approved=True, decided_by="person_ajay"
    )
    proposed = resolved.output["proposed_version"]
    assert proposed["status"] == "draft"
    assert proposed["version"] == 2

    live = brain.skills.get(skill["id"])
    assert live["version"] == 1
    assert live["description"] == "Original"
