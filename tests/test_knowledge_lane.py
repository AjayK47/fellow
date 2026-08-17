"""The event-routing control path must never wait on remote knowledge work
(memory ingestion/retrieval plan §2 Phase A / §5 Phase 2 acceptance
criteria): a slow embedding provider must not delay wake, and the
knowledge lane must run on capacity fully separate from the main pool."""

from __future__ import annotations

import time


def _ambient_event_payload(*, event_id: str, channel_id: str, text: str) -> dict:
    return {
        "id": event_id,
        "organization_id": "org_acme",
        "source": "slack",
        "type": "slack.channel_message",
        "actor": {"id": "person_ajay", "type": "person"},
        "scope": {
            "workspace_id": "TACME",
            "channel_id": channel_id,
            "visibility": "organization_public",
            "scope_id": "scope_org_public",
        },
        "payload": {"text": text, "conversation_key": f"slack:TACME:{channel_id}:1"},
        "dedupe_key": f"dedupe-{event_id}",
    }


def test_route_does_not_call_the_embedding_provider_inline(brain, monkeypatch):
    calls: list[str] = []

    def slow_embed(texts):
        calls.append("embed")
        time.sleep(0.3)
        return [[0.0] * brain.store.vector_dimensions for _ in texts]

    monkeypatch.setattr(brain.embeddings, "embed", slow_embed)

    started = time.monotonic()
    routed = brain.event_router.route(
        _ambient_event_payload(
            event_id="ev_knowledge_lane_1",
            channel_id="Cknowledge",
            text="Deploy went out at 3pm, watching for errors",
        )
    )
    elapsed = time.monotonic() - started

    assert routed["route"] == "ambient_attention"
    assert elapsed < 0.3, f"route() waited on the embedding provider ({elapsed:.3f}s)"
    assert calls == []  # embeddings only get called once the knowledge job runs

    source_id = routed["source_id"]
    assert brain.store.has_chunks_for_source(source_id) is False


def test_knowledge_job_indexes_the_evidence_asynchronously(brain):
    routed = brain.event_router.route(
        _ambient_event_payload(
            event_id="ev_knowledge_lane_2",
            channel_id="Cknowledge2",
            text="Northstar renewal call moved to Thursday",
        )
    )
    source_id = routed["source_id"]
    assert brain.store.has_chunks_for_source(source_id) is False

    drained = brain.harness_worker.drain_until_idle(kinds=brain.harness_worker.knowledge_job_kinds())
    assert drained["failed"] == 0, drained

    assert brain.store.has_chunks_for_source(source_id) is True


def test_direct_request_persists_evidence_before_reasoning_and_is_not_blocked_by_knowledge_lane(
    brain, monkeypatch
):
    """A direct DM/mention wakes the Session Manager immediately; the
    evidence row it's built from is written by the fast persist() path, not
    the slow ingest() one, regardless of embedding provider latency."""

    def slow_embed(texts):
        time.sleep(0.3)
        return [[0.0] * brain.store.vector_dimensions for _ in texts]

    monkeypatch.setattr(brain.embeddings, "embed", slow_embed)

    started = time.monotonic()
    routed = brain.event_router.route(
        {
            "id": "ev_direct_knowledge_lane",
            "organization_id": "org_acme",
            "source": "slack",
            "type": "slack.dm",
            "actor": {"id": "person_ajay", "type": "person"},
            "scope": {
                "workspace_id": "TACME",
                "channel_id": "Ddirect",
                "visibility": "dm",
                "scope_id": "scope_org_public",
            },
            "payload": {"text": "status?", "conversation_key": "slack:TACME:Ddirect:1"},
            "dedupe_key": "dedupe-ev_direct_knowledge_lane",
            "is_direct_agent_request": True,
        }
    )
    elapsed = time.monotonic() - started

    assert routed["route"] == "session_wake"
    assert elapsed < 0.3, f"direct-request routing waited on the embedding provider ({elapsed:.3f}s)"
