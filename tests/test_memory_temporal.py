"""Temporal memory supersession — drives real MemoryService + GraphService."""

from __future__ import annotations

from docket.domain.models import MemoryType


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

    # Current memories should reflect later date
    current = brain.memory.list_current(org, entity_ids=["project_x"])
    launch_current = [
        m
        for m in current
        if "launch date" in (m.get("content") or "").lower() and m.get("is_latest")
    ]
    assert launch_current, "expected current launch-date memory"
    assert any("15 September" in m["content"] for m in launch_current)
    assert not any(
        "1 September" in m["content"] and m.get("is_latest") for m in launch_current
    )

    # History preserved: older memory still in store, superseded
    all_mems = brain.store.list_memories(org, latest_only=False)
    superseded = [m for m in all_mems if m.get("status") == "superseded"]
    assert superseded, "older memory must remain with superseded status"
    assert any("1 September" in (m.get("content") or "") for m in superseded)

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
    current2 = brain.memory.list_current(org, entity_ids=["project_x"])
    launch_latest = [
        m
        for m in current2
        if "launch date" in (m.get("content") or "").lower() and m.get("is_latest")
    ]
    assert any("15 September" in m["content"] for m in launch_latest)
    superseded = [
        m
        for m in brain.store.list_memories(org, latest_only=False)
        if m.get("status") == "superseded" and "1 September" in (m.get("content") or "")
    ]
    assert superseded, "contradictory launch_date must supersede older value"


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
