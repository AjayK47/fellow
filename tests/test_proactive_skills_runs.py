"""Proactive policy, skills, and execution graph."""

from __future__ import annotations

from toda.domain.models import ProactiveCandidate, ProactiveDecision
from toda.services.proactive import decide


def test_proactive_silence_when_permission_unsafe():
    c = ProactiveCandidate(
        signal_type="verified_missing_answer",
        confidence=0.99,
        evidence_authority=0.99,
        usefulness=1.0,
        urgency=1.0,
        actionability=1.0,
        novelty=1.0,
        interruption_cost=0.1,
        permission_safe=False,
    )
    assert decide(c) == ProactiveDecision.SILENT


def test_proactive_all_four_v1_signal_types(brain):
    # Scores chosen so value = u*c*a*urg*act*nov - cost clears each signal threshold
    cases = [
        (
            "verified_missing_answer",
            dict(
                confidence=0.97,
                evidence_authority=0.95,
                usefulness=1.0,
                urgency=1.0,
                actionability=1.0,
                novelty=1.0,
                interruption_cost=0.05,
                permission_safe=True,
            ),
            {ProactiveDecision.REPLY, ProactiveDecision.DIGEST, ProactiveDecision.REMEMBER_ONLY},
        ),
        (
            "verified_correction",
            dict(
                confidence=0.96,
                evidence_authority=0.95,
                usefulness=1.0,
                urgency=1.0,
                actionability=1.0,
                novelty=1.0,
                interruption_cost=0.05,
                permission_safe=True,
            ),
            {ProactiveDecision.REPLY, ProactiveDecision.DIGEST, ProactiveDecision.REMEMBER_ONLY},
        ),
        (
            "unacknowledged_customer_issue",
            dict(
                confidence=0.95,
                evidence_authority=0.95,
                usefulness=1.0,
                urgency=1.0,
                actionability=1.0,
                novelty=1.0,
                interruption_cost=0.05,
                permission_safe=True,
            ),
            {ProactiveDecision.PRIVATE_ROUTE, ProactiveDecision.DIGEST, ProactiveDecision.ESCALATE, ProactiveDecision.REMEMBER_ONLY, ProactiveDecision.REPLY},
        ),
        (
            "verified_operational_incident",
            dict(
                confidence=0.95,
                evidence_authority=0.95,
                usefulness=1.0,
                urgency=1.0,
                actionability=1.0,
                novelty=1.0,
                interruption_cost=0.05,
                permission_safe=True,
            ),
            {ProactiveDecision.ESCALATE, ProactiveDecision.DIGEST, ProactiveDecision.PRIVATE_ROUTE, ProactiveDecision.REMEMBER_ONLY, ProactiveDecision.REPLY},
        ),
    ]
    allowed = {
        ProactiveDecision.SILENT,
        ProactiveDecision.REMEMBER_ONLY,
        ProactiveDecision.DIGEST,
        ProactiveDecision.REPLY,
        ProactiveDecision.PRIVATE_ROUTE,
        ProactiveDecision.ESCALATE,
    }
    for signal_type, kwargs, expected_set in cases:
        c = ProactiveCandidate(signal_type=signal_type, **kwargs)
        d = decide(c)
        assert d in allowed
        assert d in expected_set
        # high-quality missing answer should reply
        if signal_type == "verified_missing_answer":
            assert d == ProactiveDecision.REPLY
        if signal_type == "unacknowledged_customer_issue":
            assert d == ProactiveDecision.PRIVATE_ROUTE
        if signal_type == "verified_operational_incident":
            assert d == ProactiveDecision.ESCALATE

        result = brain.proactive.evaluate_candidate(c)
        assert result.decision == d
        assert result.reason
        assert result.policy_explanation
        assert "value_score" in result.policy_explanation


def test_proactive_evaluate_api_path_audited(brain):
    payload = brain.proactive.evaluate(
        organization_id="org_acme",
        trigger={
            "type": "conversation.updated",
            "text": "Did we ever confirm the launch date?",
        },
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s", "is_private": True},
        mode="dry_run",
        candidate={
            "signal_type": "verified_missing_answer",
            "confidence": 0.97,
            "evidence_authority": 0.95,
            "usefulness": 1.0,
            "urgency": 1.0,
            "actionability": 1.0,
            "novelty": 1.0,
            "interruption_cost": 0.05,
            "permission_safe": True,
            "content": "The launch date is 15 September.",
            "evidence": [{"memory_id": "mem_x", "content": "The launch date is 15 September."}],
        },
    )
    assert payload["decision"] in {
        "silent",
        "remember_only",
        "digest",
        "reply_in_context",
        "private_route",
        "escalate",
    }
    assert payload["reason"]
    assert payload["policy_explanation"]
    assert payload["permission_decision_id"]
    events = brain.audit.list_events("org_acme", event_type="proactive.evaluated")
    assert events


def test_skill_store_and_retrieve(brain):
    skill = brain.skills.create(
        organization_id="org_acme",
        name="Deploy Backend",
        description="Safely deploy backend",
        goal="Release and verify health",
        steps=[{"order": 1, "name": "Build", "description": "CI build"}],
    )
    got = brain.skills.get(skill["id"])
    assert got is not None
    assert got["name"] == "Deploy Backend"
