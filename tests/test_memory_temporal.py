"""Temporal memory supersession — drives real MemoryService + GraphService."""

from __future__ import annotations

from toda.domain.models import MemoryType


def _launch_facts(brain, org):
    with brain.store.cursor() as cur:
        cur.execute(
            "SELECT object_value, status FROM reality_facts"
            " WHERE organization_id = ? AND predicate = 'launch_date'",
            (org,),
        )
        return {row["object_value"]: row["status"] for row in cur.fetchall()}


def test_newer_fact_supersedes_older_without_deleting_history(empty_brain):
    brain = empty_brain
    org = "org_test"
    brain.store.put_organization({"id": org, "name": "Test", "region": "us"})
    brain.store.upsert_scope(
        {
            "id": "scope_org_public",
            "organization_id": org,
            "kind": "organization_public",
            "label": "public",
            "is_public": True,
        }
    )
    brain.graph.upsert_entity(
        {
            "id": "project_x",
            "organization_id": org,
            "entity_type": "project",
            "name": "Project X",
        }
    )

    r1 = brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="launch_v1",
        content="The launch date is 1 September for Project X.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )
    r2 = brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="launch_v2",
        content="The launch date is 15 September for Project X.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )

    assert r1["memories"], "first ingest must produce memories"
    assert r2["memories"], "second ingest must produce memories"

    # Facts carry the current truth; the older value stays as history.
    current = brain.reality.current_facts(org, subject_id="project_x", predicate="launch_date")
    assert [f["object_value"] for f in current] == ["15 September for Project X"]
    assert _launch_facts(brain, org)["1 September for Project X"] == "superseded"

    # Memories are observations: both stay readable.
    contents = [m.get("content") or "" for m in brain.store.list_memories(org, latest_only=False)]
    assert any("1 September" in c for c in contents)
    assert any("15 September" in c for c in contents)

    # Provenance on memories
    for m in r1["memories"] + r2["memories"]:
        assert m.get("organization_id") == org
        assert m.get("scope_id") == "scope_org_public"
        assert m.get("source_ids"), "source lineage required"
        assert m.get("source_spans") is not None


def test_memories_have_typed_atomic_facts(brain):
    org = "org_acme"
    current = brain.memory.list_current(org)
    types = {m.get("memory_type") for m in current}
    # seed should produce several of the V1 types
    assert MemoryType.FACT.value in types or "fact" in types or any(
        "launch" in (m.get("content") or "").lower() for m in current
    )
    assert any(m.get("source_ids") for m in current)
    assert all(m.get("organization_id") == org for m in current)


def test_unrelated_is_facts_do_not_supersede_each_other(empty_brain):
    """Regression: shared predicate 'is' must not drop unrelated current facts."""
    brain = empty_brain
    org = "org_test"
    brain.store.put_organization({"id": org, "name": "Test", "region": "us"})
    brain.store.upsert_scope(
        {
            "id": "scope_org_public",
            "organization_id": org,
            "kind": "organization_public",
            "label": "public",
            "is_public": True,
        }
    )
    brain.graph.upsert_entity(
        {
            "id": "project_x",
            "organization_id": org,
            "entity_type": "project",
            "name": "Project X",
        }
    )

    r1 = brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="fact_pg",
        content="Project X is using PostgreSQL for storage.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )
    r2 = brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="fact_owner",
        content="Project X is owned by platform team.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )
    assert r1["memories"] and r2["memories"]

    current = brain.memory.list_current(org, entity_ids=["project_x"])
    contents = " ".join(m.get("content") or "" for m in current if m.get("is_latest")).lower()
    assert "postgresql" in contents or "using" in contents
    assert "owned by" in contents or "platform" in contents

    # Both should remain latest/active — no false supersession
    latest_facts = [
        m
        for m in brain.store.list_memories(org, latest_only=False)
        if m.get("memory_type") == "fact" and m.get("is_latest") and m.get("status") == "active"
    ]
    assert len(latest_facts) >= 2

    # Launch-date style still supersedes (identity claim)
    brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="launch_a",
        content="The launch date is 1 September.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )
    brain.ingestion.ingest(
        organization_id=org,
        source_system="test",
        source_type="message",
        external_id="launch_b",
        content="The launch date is 15 September.",
        scope_id="scope_org_public",
        entity_ids=["project_x"],
        learn=True,
    )
    launch = _launch_facts(brain, org)
    assert launch["15 September"] == "active"
    assert launch["1 September"] == "superseded", "contradictory launch_date must supersede"
    is_facts = brain.reality.current_facts(org, subject_id="project_x", predicate="is")
    assert {"using PostgreSQL for storage", "owned by platform team"} <= {
        f["object_value"] for f in is_facts
    }, "generic 'is' facts must not supersede each other"


def test_reingest_bumps_version_and_keeps_prior_source(empty_brain):
    brain = empty_brain
    org = "org_test"
    brain.store.put_organization({"id": org, "name": "Test", "region": "us"})
    brain.store.upsert_scope(
        {
            "id": "scope_org_public",
            "organization_id": org,
            "kind": "organization_public",
            "label": "public",
            "is_public": True,
        }
    )
    r1 = brain.ingestion.ingest(
        organization_id=org,
        source_system="slack",
        source_type="message",
        external_id="same_msg",
        content="The launch date is 1 September.",
        scope_id="scope_org_public",
        learn=True,
    )
    r2 = brain.ingestion.ingest(
        organization_id=org,
        source_system="slack",
        source_type="message",
        external_id="same_msg",
        content="The launch date is 15 September.",
        scope_id="scope_org_public",
        learn=True,
    )
    assert r1["source"]["version"] == 1
    assert r2["source"]["version"] == 2
    assert r1["source"]["id"] != r2["source"]["id"]
    # Prior evidence row still readable by id
    old = brain.store.get_source(r1["source"]["id"])
    assert old is not None
    assert old["version"] == 1
    assert "1 September" in old["content"]
    versions = brain.store.list_source_versions(org, "slack", "same_msg")
    assert len(versions) == 2
    # Memories still point at live source ids
    for m in r1["memories"]:
        assert brain.store.get_source(m["source_ids"][0]) is not None
