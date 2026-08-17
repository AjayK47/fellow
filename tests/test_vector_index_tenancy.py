"""The vec0 ANN index must be tenant-partitioned and width-agnostic.

Two regressions, both invisible to the rest of the suite because
conftest.py forces the deterministic 96-dim provider for every test:

1. **Width.** The vec0 column width is fixed at CREATE time, but the real
   width is whatever the configured embedding provider returns (1536, 4096,
   ...), not TODA_EMBEDDING_DIMENSIONS -- that setting only ever
   described the offline fallback. Baking the table from the setting made
   every ingest fail with "Dimension mismatch" the moment a real provider
   was configured.

2. **Tenancy.** An unpartitioned KNN takes the global top-k across ALL
   organizations, so a large tenant's rows crowd a small tenant's
   equally-relevant rows out of the shortlist entirely -- not a disclosure
   (post-filters catch those) but a silent recall collapse where the small
   tenant gets zero semantic results.
"""

from __future__ import annotations

import pytest

from toda.storage.db import BrainStore


@pytest.fixture
def store(tmp_path):
    s = BrainStore(tmp_path / "vec.db")
    yield s
    s.close()


def _chunk(org: str, chunk_id: str, embedding: list[float]) -> dict:
    return {
        "id": chunk_id,
        "organization_id": org,
        "source_id": f"src_for_{chunk_id}",
        "scope_id": "scope_org_public",
        "position": 0,
        "content": f"content for {chunk_id}",
        "model": "test-model",
        "embedding": embedding,
    }


def test_index_adopts_whatever_width_the_provider_actually_returns(store):
    """A 4096-dim provider must just work -- no configuration, no crash."""
    wide = [0.0] * 4095 + [1.0]
    store.upsert_chunk(_chunk("org_a", "chunk_wide", wide))

    assert store.vector_dimensions() == 4096
    hits = store.vec_search_chunks("org_a", wide, 5)
    assert [row_id for row_id, _ in hits] == ["chunk_wide"]


def test_changing_embedding_width_rebuilds_instead_of_crashing(store):
    """Swapping embedding models must not wedge ingestion. The old ANN
    entries are meaningless under a new model, so they are dropped; the
    JSON embedding columns remain the source of truth."""
    store.upsert_chunk(_chunk("org_a", "chunk_narrow", [1.0] + [0.0] * 95))
    assert store.vector_dimensions() == 96

    store.upsert_chunk(_chunk("org_a", "chunk_wide", [1.0] + [0.0] * 1535))

    assert store.vector_dimensions() == 1536
    hits = store.vec_search_chunks("org_a", [1.0] + [0.0] * 1535, 5)
    assert [row_id for row_id, _ in hits] == ["chunk_wide"]


def test_a_query_vector_of_the_wrong_width_returns_nothing_rather_than_raising(store):
    """A stale query vector degrades to no semantic signal; callers still
    have their lexical/FTS signal and stay correct, just less precise."""
    store.upsert_chunk(_chunk("org_a", "chunk_a", [1.0] + [0.0] * 95))

    assert store.vec_search_chunks("org_a", [1.0] + [0.0] * 1535, 5) == []


def test_knn_is_scoped_per_organization_so_a_big_tenant_cannot_crowd_out_a_small_one(store):
    """The recall-collapse regression: identical vectors, 50 in org_big and
    1 in org_small. With a global top-k the small tenant's row never
    reaches the shortlist at all."""
    vector = [1.0] + [0.0] * 95
    for i in range(50):
        store.upsert_chunk(_chunk("org_big", f"chunk_big_{i}", vector))
    store.upsert_chunk(_chunk("org_small", "chunk_small", vector))

    small_hits = store.vec_search_chunks("org_small", vector, 3)
    assert [row_id for row_id, _ in small_hits] == ["chunk_small"]

    big_hits = store.vec_search_chunks("org_big", vector, 3)
    assert len(big_hits) == 3
    assert all(row_id.startswith("chunk_big_") for row_id, _ in big_hits)


def test_memory_and_episode_indexes_are_partitioned_too(store):
    vector = [1.0] + [0.0] * 95
    store.vec_upsert_memory("org_a", "mem_a", vector)
    store.vec_upsert_memory("org_b", "mem_b", vector)
    store.vec_upsert_episode("org_a", "episode_a", vector)
    store.vec_upsert_episode("org_b", "episode_b", vector)

    assert [i for i, _ in store.vec_search_memories("org_a", vector, 10)] == ["mem_a"]
    assert [i for i, _ in store.vec_search_episodes("org_b", vector, 10)] == ["episode_b"]


def test_reindex_skips_rows_left_at_a_stale_width_instead_of_failing(store):
    """After a model change the old JSON embeddings are the wrong width.
    Reindex must stay a safe offline repair: skip and report them, never
    raise, so the index still gets rebuilt for everything current."""
    store.upsert_chunk(_chunk("org_a", "chunk_old", [1.0] + [0.0] * 95))
    store.upsert_chunk(_chunk("org_a", "chunk_new", [1.0] + [0.0] * 1535))

    counts = store.reindex_vectors()

    assert counts["skipped_wrong_width"] == 1
    assert counts["chunks"] == 1
    assert [i for i, _ in store.vec_search_chunks("org_a", [1.0] + [0.0] * 1535, 5)] == ["chunk_new"]
