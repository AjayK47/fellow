"""Graph traversal must not bypass scope authorization (memory
ingestion/retrieval plan §5 / §11 trust boundary #3: private evidence is
never exposed to a public or unrelated destination).

Regression coverage for a real gap: `retrieval.search`'s memory-list side
already ran every result through `authz.filter_memories`, but the graph
edges attached to those same results (`GraphService.neighbors`/
`paths_between`, called from retrieval.py and context.py) were never
authorization-checked at all -- an edge backed only by a private source
was returned to any caller regardless of destination.
"""

from __future__ import annotations


def _public_decision(brain):
    return brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="public_search",
    )


def _private_decision(brain):
    return brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "private_channel", "id": "channel_leadership", "is_private": True},
        purpose="private_search",
    )


def _private_sourced_edge(brain):
    private = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="document",
        external_id="graph-authz-private-source",
        content="Confidential: person_secret_a is being performance-managed by person_secret_b.",
        scope_id="scope_channel_leadership_private",
        is_private_channel=True,
        channel_id="channel_leadership",
        learn=False,
        processing_mode="reference",
    )
    private_source_id = private["source"]["id"]
    brain.graph.upsert_entity(
        {"id": "person_secret_a", "organization_id": "org_acme", "entity_type": "person", "name": "Secret A"}
    )
    brain.graph.upsert_entity(
        {"id": "person_secret_b", "organization_id": "org_acme", "entity_type": "person", "name": "Secret B"}
    )
    edge = brain.graph.add_edge(
        "org_acme", "MANAGED_BY", "person_secret_a", "person_secret_b", source_ids=[private_source_id]
    )
    return edge, private_source_id


def test_can_read_edge_denies_a_privately_sourced_edge_to_a_public_decision(brain):
    edge, _ = _private_sourced_edge(brain)
    public_decision = _public_decision(brain)
    assert brain.authz.can_read_edge(public_decision, edge) is False


def test_can_read_edge_allows_the_same_edge_to_the_matching_private_decision(brain):
    edge, _ = _private_sourced_edge(brain)
    private_decision = _private_decision(brain)
    assert brain.authz.can_read_edge(private_decision, edge) is True


def test_can_read_edge_allows_structural_edges_with_no_source_ids(brain):
    """Org-chart / ownership edges have no evidentiary backing to check --
    explorer_view has always shown these regardless of destination; the
    fix must not regress that."""
    brain.graph.upsert_entity({"id": "person_x", "organization_id": "org_acme", "entity_type": "person", "name": "X"})
    brain.graph.upsert_entity({"id": "team_y", "organization_id": "org_acme", "entity_type": "team", "name": "Y"})
    edge = brain.graph.add_edge("org_acme", "MEMBER_OF", "person_x", "team_y")
    public_decision = _public_decision(brain)
    assert brain.authz.can_read_edge(public_decision, edge) is True


def test_neighbors_result_from_retrieval_search_excludes_privately_sourced_edges(brain):
    edge, private_source_id = _private_sourced_edge(brain)
    memory = brain.graph.commit_memory(
        {
            "id": "mem_graph_authz_1",
            "organization_id": "org_acme",
            "memory_type": "fact",
            "content": "person_secret_a reporting line changed",
            "status": "active",
            "confidence": 0.9,
            "authority": 0.9,
            "is_latest": True,
            "scope_id": "scope_org_public",
            "source_ids": [],
            "entity_ids": ["person_secret_a"],
            "participant_ids": [],
        }
    )
    result = brain.retrieval.search(
        organization_id="org_acme",
        query="person_secret_a reporting line",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="public_search",
    )
    matched = [r for r in result["results"] if r["memory"]["id"] == memory["id"]]
    assert matched, "expected the public memory itself to be returned"
    leaked_edge_ids = {p["id"] for p in matched[0]["graph_paths"]}
    assert edge["id"] not in leaked_edge_ids


def test_relationship_context_drops_a_path_containing_an_unauthorized_edge(brain):
    edge, _ = _private_sourced_edge(brain)
    result = brain.context.relationship_context(
        organization_id="org_acme",
        from_entity_id="person_secret_a",
        to_entity_id="person_secret_b",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="public_search",
    )
    all_edge_ids = {e["id"] for path in result["graph_paths"] for e in path}
    assert edge["id"] not in all_edge_ids


def test_relationship_context_shows_the_path_to_an_authorized_destination(brain):
    edge, _ = _private_sourced_edge(brain)
    result = brain.context.relationship_context(
        organization_id="org_acme",
        from_entity_id="person_secret_a",
        to_entity_id="person_secret_b",
        acting_principal_id="person_ajay",
        destination={"type": "private_channel", "id": "channel_leadership", "is_private": True},
        purpose="private_search",
    )
    all_edge_ids = {e["id"] for path in result["graph_paths"] for e in path}
    assert edge["id"] in all_edge_ids
