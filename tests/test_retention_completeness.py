"""Every organization-scoped table must be covered by tenant deletion.

Regression for a gap introduced by making the vec0 ANN tables org-
partitioned (test_vector_index_tenancy.py): they gained an organization_id
column but RetentionService.delete_organization() didn't know about them,
so "permanently delete this org" would leave its raw embedding vectors on
disk forever while still reporting recoverable: False.
"""

from __future__ import annotations

from toda.services.retention import _TENANT_TABLES


def test_every_org_partitioned_vec_table_is_covered_by_tenant_deletion():
    for table in ("vec_chunks", "vec_memories", "vec_episodes", "episode_sources", "episodes_fts"):
        assert table in _TENANT_TABLES, f"{table} has an organization_id column but isn't deleted on tenant removal"


def test_deleting_an_organization_purges_its_vector_index(brain):
    org = "org_acme"
    brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="doc",
        external_id="retention-vec-check",
        content="Retention regression check content for the vector index.",
        scope_id="scope_org_public",
        learn=False,
        processing_mode="reference",
    )
    chunk_ids = [c["id"] for c in brain.store.list_chunks(org)]
    assert chunk_ids
    embedding = brain.embeddings.embed(["retention regression check content"])[0]
    assert brain.store.vec_search_chunks(org, embedding, 5), "sanity: chunk must be findable before deletion"

    brain.retention.delete_organization(org, confirmation=org)

    assert brain.store.vec_search_chunks(org, embedding, 5) == []
    with brain.store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM vec_chunks WHERE organization_id = ?", (org,))
        assert cur.fetchone()["n"] == 0


def test_deleting_an_organization_purges_episode_sources(brain):
    org = "org_acme"
    brain.event_router.config.episode_quiet_seconds = 0
    brain.event_router.route(
        {
            "id": "ev_retention_episode",
            "organization_id": org,
            "source": "slack",
            "type": "slack.channel_message",
            "actor": {"id": "person_ajay", "type": "person"},
            "scope": {
                "workspace_id": "TACME",
                "channel_id": "Cretention",
                "visibility": "organization_public",
                "scope_id": "scope_org_public",
            },
            "payload": {"text": "retention episode check", "conversation_key": "slack:TACME:Cretention:1"},
            "dedupe_key": "dedupe-ev_retention_episode",
        }
    )
    brain.harness_worker.drain_until_idle(kinds=brain.harness_worker.knowledge_job_kinds())
    episode = brain.store.get_episode_by_group_key(org, "slack:TACME:Cretention:1")
    assert episode is not None
    assert brain.store.list_episode_sources(episode["id"])

    brain.retention.delete_organization(org, confirmation=org)

    with brain.store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM episode_sources WHERE organization_id = ?", (org,))
        assert cur.fetchone()["n"] == 0
