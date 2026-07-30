"""SQLite FTS/vector indexing integration tests."""

from __future__ import annotations


def test_ingestion_persists_chunks_vectors_and_fts_index(brain):
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="document",
        external_id="hybrid-index",
        content="The incident runbook explains how to roll back an export deployment safely.",
        scope_id="scope_org_public",
        entity_ids=["service_export_worker"],
    )
    source_id = result["source"]["id"]
    with brain.store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM chunks WHERE source_id = ?", (source_id,))
        assert cur.fetchone()["count"] == 1
        cur.execute("SELECT COUNT(*) AS count FROM chunks_fts WHERE chunk_id LIKE ?", (f"chunk_{source_id}_%",))
        assert cur.fetchone()["count"] == 1

    memory_ids = [memory["id"] for memory in result["memories"]]
    vectors = brain.store.memory_embeddings(memory_ids)
    assert set(vectors) == set(memory_ids)
    assert all(vectors[memory_id] for memory_id in memory_ids)


def test_native_fts_is_limited_to_authorized_memories(brain):
    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="public_search",
    )
    authorized = brain.authz.filter_memories(
        decision, brain.store.list_memories("org_acme", latest_only=True)
    )
    ranks = brain.store.memory_fts_ranks("launch date", [memory["id"] for memory in authorized])
    assert ranks
    private = [
        memory["id"] for memory in brain.store.list_memories("org_acme", latest_only=True)
        if memory.get("scope_id") == "scope_channel_leadership_private"
    ]
    assert not set(ranks).intersection(private)
