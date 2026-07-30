"""Proactive signals have a deduplicated, auditable issue-routing lifecycle."""

from __future__ import annotations


def test_signal_deduplication_routing_and_acknowledgement(brain):
    first = brain.proactive.evaluate(
        organization_id="org_acme", trigger={"type": "issue.reported", "text": "Acme export stuck"},
        acting_principal_id="person_ajay", destination={"type": "internal_agent_session", "id": "ops", "is_private": True},
        candidate={"signal_type": "unacknowledged_customer_issue", "confidence": 0.99, "evidence_authority": 0.99,
                   "usefulness": 1, "urgency": 1, "actionability": 1, "novelty": 1, "interruption_cost": 0,
                   "permission_safe": True, "entity_ids": ["service_export_worker"]},
    )
    second = brain.proactive.evaluate(
        organization_id="org_acme", trigger={"type": "issue.reported", "text": "Acme export stuck"},
        acting_principal_id="person_ajay", destination={"type": "internal_agent_session", "id": "ops", "is_private": True},
        candidate={"signal_type": "unacknowledged_customer_issue", "confidence": 0.99, "evidence_authority": 0.99,
                   "usefulness": 1, "urgency": 1, "actionability": 1, "novelty": 1, "interruption_cost": 0,
                   "permission_safe": True, "entity_ids": ["service_export_worker"]},
    )
    assert first["signal_id"] == second["signal_id"]
    routed = brain.signals.route(first["signal_id"])
    assert routed and routed["recipient_id"] == "person_ravi"
    acknowledged = brain.signals.acknowledge(first["signal_id"], "person_ravi")
    assert acknowledged and acknowledged["acknowledged"] is True
    assert acknowledged["state"] == "acknowledged"
