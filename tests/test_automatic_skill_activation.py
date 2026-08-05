"""Automatic destination skills, prompt injection, and response validation."""

from __future__ import annotations

import json

import pytest

from docket.harness.model_client import ScriptedModelClient
from docket.harness.models import InvocationEnvelope, ModelResponse
from docket.harness.skill_validation import SkillResponseValidator


def _slack_envelope(brain, *, conversation: str) -> InvocationEnvelope:
    destination = {
        "type": "slack_channel",
        "id": "channel_eng",
        "is_private": False,
    }
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def test_resolver_separates_destination_mandatory_and_task_optional_skills(brain):
    resolved = brain.skills.resolve_for_task(
        organization_id="org_acme",
        task="Investigate the export failure before fixing it",
        destination={
            "type": "private_channel",
            "id": "channel_leadership",
            "is_private": True,
        },
        trigger_kind="reactive",
        purpose="reactive_agent",
        allowed_scope_ids=["scope_channel_leadership_private"],
    )

    slack = next(skill for skill in resolved if skill["name"] == "Write for Slack")
    investigation = next(
        skill for skill in resolved if skill["name"] == "Investigate Before Fixing"
    )
    assert slack["activation_mode"] == "mandatory"
    assert slack["activation_reason"] == "destination_mandatory"
    assert investigation["activation_mode"] == "optional"
    assert investigation["activation_reason"] == "task_relevant"


def test_active_skill_versions_are_pinned_to_the_task(brain):
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain, conversation="slack:T:channel_eng:event:pin"),
        goal="Investigate the export failure before fixing it",
        trigger_kind="reactive",
        initial_message="Investigate the export failure before fixing it",
    )
    first = brain.company_agent._active_skills(task)
    pinned_procedure = next(
        skill for skill in first if skill["name"] == "Handle Export Failure"
    )
    proposal = brain.skills.propose_update(
        pinned_procedure["id"], {"description": "A newly proposed procedure."}
    )
    assert proposal
    brain.skills.approve(pinned_procedure["id"], proposal["version"])

    persisted = brain.store.get_task(task["id"])
    pinned = brain.company_agent._active_skills(persisted)
    pinned_again = next(
        skill for skill in pinned if skill["name"] == "Handle Export Failure"
    )
    assert pinned_again["version"] == pinned_procedure["version"]


def test_slack_skill_is_injected_and_validates_the_final_response(brain):
    main_model = ScriptedModelClient(
        [
            ModelResponse(
                content=(
                    "Spain won the **2026 FIFA World Cup**. "
                    "Source: [FIFA](https://www.fifa.com/final)"
                )
            )
        ]
    )
    validation_model = ScriptedModelClient(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "valid": False,
                        "violations": [
                            "Used Markdown bold and Markdown link syntax."
                        ],
                        "corrected_response": (
                            "Spain won the *2026 FIFA World Cup*. "
                            "Source: <https://www.fifa.com/final|FIFA>"
                        ),
                    }
                )
            )
        ]
    )
    brain.company_agent.model = main_model
    brain.company_agent.skill_validator = SkillResponseValidator(
        model=validation_model
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(
            brain, conversation="slack:T:channel_eng:event:validation"
        ),
        goal="Who won the World Cup?",
        trigger_kind="reactive",
        initial_message="Who won the World Cup?",
        metadata={
            "slack": {"workspace_id": "T", "channel_id": "channel_eng"}
        },
    )

    outcome = brain.company_agent.execute_task(task["id"])

    assert outcome.answer == (
        "Spain won the *2026 FIFA World Cup*. "
        "Source: <https://www.fifa.com/final|FIFA>"
    )
    active_block = next(
        message["content"]
        for message in main_model.requests[0]["messages"]
        if message["content"].startswith("<active_skills>")
    )
    assert "Write for Slack" in active_block
    assert '"activation_mode": "mandatory"' in active_block
    validation_input = json.loads(
        validation_model.requests[0]["messages"][-1]["content"]
    )
    assert validation_input["destination"]["type"] == "slack_channel"
    assert validation_input["active_skill_contracts"][0]["name"] == (
        "Write for Slack"
    )
    completed = brain.store.get_task(task["id"])
    assert completed["metadata"]["last_skill_validation"]["valid"] is False
    audit = brain.store.list_audit(
        "org_acme", event_type="agent.response.skill_validated"
    )
    assert audit[-1]["payload"]["corrected"] is True


def test_platform_skills_are_visible_without_memory_public_scope(brain):
    # A fresh/reset tenant may not have materialized its organization-public
    # memory scope yet. Destination skills must still be active.
    active = brain.skills.resolve_for_task(
        organization_id="org_acme",
        task="read and summarize a PDF",
        destination={"type": "dm", "id": "D1", "is_private": True},
        trigger_kind="reactive",
        purpose="answer_question",
        allowed_scope_ids=[],
    )
    names = {skill["name"] for skill in active}
    assert "Write for Slack" in names
    assert "PDF and Document Handling" in names


def test_document_skills_activate_with_real_run_scoped_capabilities(brain):
    """H-31: the builtin document skills (pdf-documents, docx, xlsx,
    powerpoint, ocr-documents, nano-pdf) declare required_capabilities in
    terms of coarse `documents.read`/`documents.write` markers, not the
    granular `document.<family>.<op>` capability_ids typed_tools.py actually
    emits. resolve_for_task's own `ready()` gate only lets an optional skill
    through when it can't prove a required capability is missing -- passing
    `available_capabilities=None` (as the sibling test above does) skips
    that check entirely. This test passes the *real* Task-scoped capability
    set (`ToolExecutor.available_capabilities()`, exactly what
    CompanyAgent._active_skills() passes in production) so a regression in
    that capability-marker wiring actually fails a test instead of silently
    leaving every document skill permanently inert."""
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain, conversation="skills:documents:real-capabilities"),
        goal="Create a PDF report",
        trigger_kind="reactive",
        initial_message="Create a PDF report",
    )
    task = brain.store.get_task(task["id"])
    resolved = brain.skills.resolve_for_task(
        organization_id="org_acme",
        task="Create a PDF document report artifact",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        trigger_kind="reactive",
        purpose="reactive_agent",
        allowed_scope_ids=["scope_channel_eng"],
        optional_limit=20,
        available_capabilities=brain.tool_executor.available_capabilities(task),
    )
    names = {skill["name"] for skill in resolved}
    assert "PDF and Document Handling" in names


def test_platform_skills_cannot_be_changed_or_deprecated(brain):
    slack = next(
        skill
        for skill in brain.skills.ensure_platform_skills("org_acme")
        if skill["name"] == "Write for Slack"
    )
    with pytest.raises(ValueError, match="immutable"):
        brain.skills.propose_update(slack["id"], {"goal": "changed"})
    with pytest.raises(ValueError, match="cannot be deprecated"):
        brain.skills.deprecate(slack["id"])
