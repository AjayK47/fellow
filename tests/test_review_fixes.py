"""Regressions found reviewing the UX + memory work (2026-09-18)."""

from __future__ import annotations

import json

from toda.harness.delivery import DeliveryService
from toda.harness.extraction import ModelMemoryExtractor
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse, TriggerKind

PRIVATE_SCOPE = "scope_channel_leadership_private"


# -- empty-query memory search -------------------------------------------


def test_empty_query_memory_search_returns_recent_memories(brain):
    result = brain.retrieval.search(
        organization_id="org_acme",
        query=" ",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "x", "is_private": True},
    )
    assert result["results"], "the seeded org has memories to browse"


# -- reply thread mirrors the triggering message --------------------------


class _Messages:
    def __init__(self, messages):
        self.messages = messages

    def list_session_messages(self, session_id, limit=200):
        return self.messages


def _thread_for(messages, reply_id):
    delivery = DeliveryService.__new__(DeliveryService)
    delivery.store = _Messages(messages)
    return delivery._conversation_thread("session", reply_id)


def test_top_level_ask_after_a_threaded_one_replies_top_level():
    messages = [
        {"id": "u1", "role": "user", "metadata": {"thread_ts": "111.1"}},
        {"id": "a1", "role": "assistant", "metadata": {}},
        {"id": "u2", "role": "user", "metadata": {}},
        {"id": "a2", "role": "assistant", "metadata": {}},
    ]
    assert _thread_for(messages, "a1") == "111.1"
    assert _thread_for(messages, "a2") is None


# -- extraction stays inside what the source's room may see ---------------


def _private_fact(brain, subject_id):
    return brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id=subject_id,
        predicate="renewal_risk",
        object_value="board is considering layoffs",
        scope_id=PRIVATE_SCOPE,
        valid_from="2026-09-01T00:00:00+00:00",
        confidence=0.9,
        authority=0.9,
    )


def _public_source(brain, subject_id, external_id):
    return brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="test",
        external_id=external_id,
        content="Acme renewal call went fine.",
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        entity_ids=[subject_id],
        learn=False,
        processing_mode="reference",
    )["source"]


def _claim_payload(subject_id, decision, target=None):
    return {
        "proposed_entities": [],
        "memories": [
            {
                "memory_type": "fact",
                "content": "Acme renewal risk is low",
                "confidence": 0.9,
                "authority": 0.8,
                "entity_ids": [subject_id],
                "participant_ids": [],
                "claim": {
                    "subject": subject_id,
                    "predicate": "renewal_risk",
                    "object": {"value": "low"},
                    "decision": decision,
                    **({"target_fact_id": target} if target else {}),
                },
                "valid_until": None,
            }
        ],
    }


def _extractor(brain, payload):
    model = ScriptedModelClient([ModelResponse(content=json.dumps(payload), model="test")])
    extractor = ModelMemoryExtractor(
        model=model,
        memory_service=brain.memory,
        store=brain.store,
        audit=brain.audit,
        reality=brain.reality,
    )
    return extractor, model


def _subject(brain):
    return brain.store.list_entities("org_acme")[0]["id"]


def test_public_extraction_is_never_shown_private_facts(brain):
    subject = _subject(brain)
    private = _private_fact(brain, subject)
    source = _public_source(brain, subject, "review-private-1")
    extractor, model = _extractor(brain, _claim_payload(subject, "ADD"))
    extractor.extract_source(source)
    sent = json.loads(model.requests[0]["messages"][1]["content"])
    assert private["id"] not in {fact["fact_id"] for fact in sent["current_facts"]}
    assert "layoffs" not in json.dumps(sent)


def test_public_claim_cannot_supersede_a_private_fact(brain):
    subject = _subject(brain)
    private = _private_fact(brain, subject)
    source = _public_source(brain, subject, "review-private-2")
    extractor, _ = _extractor(brain, _claim_payload(subject, "UPDATE", target=private["id"]))
    memory = extractor.extract_source(source)[0]
    brain.reality.apply_claim("org_acme", memory["claim"], memory, source=source)
    after = brain.reality._get_payload("reality_facts", private["id"])
    assert after["status"] == "active"
    assert not after.get("superseded_by")


# -- opting in never rewrites anyone else's policy ------------------------


def test_opting_in_keeps_admin_setting_and_other_overrides(brain):
    brain.store.upsert_simple_harness_record(
        "tool_policies",
        {
            "id": "tp_admin_post_draft",
            "organization_id": "org_acme",
            "tool_name": "post_draft",
            "execution": "deny",
            "principal_overrides": {"person_ravi": {"C1": "direct"}},
            "updated_at": "2026-09-18T00:00:00+00:00",
        },
    )
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "idm_review_ajay",
            "organization_id": "org_acme",
            "source": "slack",
            "workspace_id": "T",
            "team_id": "T",
            "source_user_id": "U7",
            "principal_id": "person_ajay",
            "updated_at": "2026-09-18T00:00:00+00:00",
        },
    )
    brain.slack_gateway._resolve_preference_interaction(
        {"team": {"id": "T"}, "user": {"id": "U7"}},
        {"action_id": "toda_preference_yes", "value": "person_ajay::post_draft::C9"},
    )
    rows = brain.store.list_harness_records(
        "tool_policies", "org_acme", where={"tool_name": "post_draft"}, order_by="updated_at"
    )
    assert len(rows) == 1
    assert rows[0]["execution"] == "deny"
    assert rows[0]["principal_overrides"] == {
        "person_ravi": {"C1": "direct"},
        "person_ajay": {"C9": "direct"},
    }
    definition = next(
        d for d in brain.tool_executor.registry.definitions() if d.name == "post_draft"
    )
    assert (
        brain.tool_executor.effective_policy(
            "org_acme", definition, principal_id="person_ajay", channel_id="C9"
        )["execution"]
        == "deny"
    )


# -- quick replies don't pay for a compaction call every turn -------------


class _CountingCompactor:
    def __init__(self):
        self.calls = 0

    def compact(self, *, previous_summary, new_messages):
        self.calls += 1
        return previous_summary + f" [{len(new_messages)} folded]"


def _conversation_with(brain, key, count):
    destination = {"type": "internal_agent_session", "id": key, "is_private": True}
    wake = brain.sessions.wake(
        organization_id="org_acme", conversation_key=f"api:review:{key}",
        destination=destination, role="user", content="message 0",
        principal_id="person_ajay", metadata={}, message_metadata={}, may_join=True,
    )
    session_id = wake["session_id"]
    for index in range(1, count):
        brain.company_agent.runs.add_message(
            organization_id="org_acme", session_id=session_id, task_id=None,
            role="user" if index % 2 == 0 else "assistant", content=f"message {index}",
        )
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    envelope = InvocationEnvelope(
        organization_id="org_acme", principal_id="person_ajay",
        conversation_id=f"api:review:{key}", destination=destination,
        purpose="reactive_agent", permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    return session_id, envelope


def _run_turn(brain, session_id, envelope, key):
    compactor = _CountingCompactor()
    brain.conversation_agent.history_compactor = compactor
    brain.conversation_agent.model = ScriptedModelClient([ModelResponse(content="ok")])
    brain.conversation_agent.run(
        session=brain.store.get_session(session_id), envelope=envelope,
        runs=brain.company_agent.runs, trigger_kind=TriggerKind.REACTIVE,
        trigger_event_id=None, task_metadata={},
    )
    prompt = json.dumps(brain.conversation_agent.model.requests[0]["messages"])
    return compactor.calls, prompt


def test_a_few_aged_out_messages_stay_in_the_prompt_without_compaction(brain):
    session_id, envelope = _conversation_with(brain, "few", 34)
    calls, prompt = _run_turn(brain, session_id, envelope, "few")
    assert calls == 0
    assert "message 0" in prompt


def test_a_batch_of_aged_out_messages_compacts_once(brain):
    session_id, envelope = _conversation_with(brain, "batch", 45)
    calls, _ = _run_turn(brain, session_id, envelope, "batch")
    assert calls == 1
    assert "folded" in (brain.store.get_session(session_id).get("summary") or "")
