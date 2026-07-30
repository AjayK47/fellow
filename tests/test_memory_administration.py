"""Memory lifecycle administration is auditable and removes retrieval visibility."""

from __future__ import annotations


def test_confirm_retract_and_forget_memory(brain):
    memory = brain.memory.list_current("org_acme")[0]
    confirmed = brain.memory.confirm(memory["id"])
    assert confirmed and confirmed["status"] == "confirmed"
    retracted = brain.memory.retract(memory["id"])
    assert retracted and retracted["status"] == "retracted"

    another = brain.memory.list_current("org_acme")[0]
    forgotten = brain.memory.forget(another["id"])
    assert forgotten and forgotten["status"] == "deleted"
    result = brain.retrieval.search(
        organization_id="org_acme", query="export", acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "test", "is_private": True}, limit=50,
    )
    assert all(item["memory"]["id"] != another["id"] for item in result["results"])
    events = brain.audit.list_events("org_acme")
    assert any(event["event_type"] == "memory.forgotten" for event in events)
