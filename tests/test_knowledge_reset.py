"""`toda reset --knowledge` forgets what Toda learned, not who it is."""

from __future__ import annotations

from toda.services.knowledge_reset import _KEEP, _is_shadow, clear_knowledge


def _count(store, table: str) -> int:
    with store.cursor() as cur:
        return cur.execute(f"select count(*) from [{table}]").fetchone()[0]


def test_everything_learned_is_cleared(brain):
    for table in ("memories", "entities", "edges", "sources", "chunks", "reality_facts"):
        assert _count(brain.store, table) > 0, f"{table} should be seeded"

    result = clear_knowledge(brain.store)

    assert result["failed"] == {}
    for table in ("memories", "entities", "edges", "sources", "chunks", "reality_facts"):
        assert _count(brain.store, table) == 0


def test_identity_access_and_capability_survive(brain):
    """Clearing memory must not sign you out, disconnect Slack, or drop the
    skills -- otherwise it is just a slower `toda reset`."""
    before = {t: _count(brain.store, t) for t in
              ("organizations", "scopes", "principal_memberships", "skills", "capabilities")}
    assert all(v > 0 for v in before.values())

    clear_knowledge(brain.store)

    after = {t: _count(brain.store, t) for t in before}
    assert after == before


def test_vector_tables_are_reachable(brain):
    """vec0 is only reachable with sqlite-vec loaded; a bare sqlite3
    connection fails these with "no such module: vec0" and silently leaves
    the vectors behind. Going through BrainStore is what makes them work."""
    result = clear_knowledge(brain.store)
    assert not any(name.startswith("vec_") for name in result["failed"])


def test_new_tables_are_cleared_by_default(brain):
    """The keep list is the allowlist, so a table added later is forgotten
    unless someone deliberately preserves it. retention.py has the opposite
    default and already drifted -- it omits sixteen org-owned tables."""
    with brain.store.cursor() as cur:
        cur.execute("create table if not exists learned_later (id text primary key)")
        cur.execute("insert into learned_later values ('x')")

    clear_knowledge(brain.store)

    assert _count(brain.store, "learned_later") == 0


def test_fts_and_vec_shadow_tables_are_never_touched_directly(brain):
    """Deleting from a shadow table directly corrupts the index; they are
    managed through their virtual parent."""
    for shadow in ("memories_fts_data", "memories_fts_idx", "vec_memories_chunks",
                   "vec_chunks_rowids", "tasks_fts_config"):
        assert _is_shadow(shadow), shadow
    for parent in ("memories_fts", "vec_memories", "chunks_fts", "vec_tasks"):
        assert not _is_shadow(parent), parent

    result = clear_knowledge(brain.store)
    assert not any(_is_shadow(name) for name in result["cleared"])


def test_the_keep_list_names_only_real_tables(brain):
    """A typo in the keep list silently clears something meant to survive."""
    with brain.store.cursor() as cur:
        existing = {
            row[0] for row in cur.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
    assert _KEEP <= existing, sorted(_KEEP - existing)
