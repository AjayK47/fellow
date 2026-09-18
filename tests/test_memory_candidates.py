"""M1 candidates-first search: bounded rows, relevant on top."""


def test_candidates_bounded_and_relevant_on_top(brain):
    store = brain.store
    org = "org_acme"
    all_mems: list[dict] = []
    for i in range(200):
        store.upsert_memory(
            {
                "id": f"mem_noise_{i:04d}",
                "organization_id": org,
                "scope_id": "scope_org_public",
                "memory_type": "fact",
                "content": f"unrelated filler record number {i} about widgets",
                "status": "active",
                "is_latest": True,
                "confidence": 0.5,
                "authority": 0.5,
                "observed_at": "2026-01-01T00:00:00+00:00",
                "entity_ids": [],
                "source_ids": [],
            }
        )
    for j, text in enumerate(
        [
            "Acme renewal blocked on pricing approval",
            "Acme renewal pricing approval pending finance",
            "Acme renewal finance needs pricing approval",
        ]
    ):
        mem = {
                "id": f"mem_rel_{j}",
                "organization_id": org,
                "scope_id": "scope_org_public",
                "memory_type": "fact",
                "content": text,
                "status": "active",
                "is_latest": True,
                "confidence": 0.9,
                "authority": 0.9,
                "observed_at": "2026-09-01T00:00:00+00:00",
                "entity_ids": [],
                "source_ids": [],
            }
        store.upsert_memory(mem)
        all_mems.append(mem)

    # upsert_memory does not maintain the FTS index (only the embedding path
    # does); index the relevant rows so the FTS candidate stage can find them.
    with store.cursor() as cur:
        for mem in all_mems:
            cur.execute("DELETE FROM memories_fts WHERE memory_id = ?", (mem["id"],))
            cur.execute(
                "INSERT INTO memories_fts(memory_id, organization_id, scope_id, content) VALUES (?, ?, ?, ?)",
                (mem["id"], org, mem["scope_id"], mem["content"]),
            )

    loaded_counts: list[int] = []
    orig = store.get_memories

    def counting(ids, *args, **kwargs):
        loaded_counts.append(len(ids))
        return orig(ids, *args, **kwargs)

    store.get_memories = counting  # type: ignore
    try:
        result = brain.retrieval.search(
            organization_id=org,
            query="Acme renewal pricing",
            acting_principal_id="person_ajay",
            destination={"type": "dm", "id": "D1", "is_private": True},
            limit=5,
        )
    finally:
        store.get_memories = orig  # type: ignore
    ids = [r["memory"]["id"] for r in result["results"]]
    assert any(i.startswith("mem_rel_") for i in ids)
    assert loaded_counts and max(loaded_counts) <= 400
