"""The explorer must show what the store actually holds."""

from __future__ import annotations


def _view(brain, allowed=None):
    org = "org_acme"
    sources = allowed if allowed is not None else {s["id"] for s in brain.store.list_sources(org)}
    return brain.graph.explorer_view(org, sources)


def test_memory_edges_are_shown(brain):
    """MemoryEdgeType covers SUPPORTED_BY, INVOLVES and ABOUT -- the edges
    connecting what Toda learned to who and what it is about. Skipping them
    wholesale left a graph of Slack plumbing with the knowledge removed."""
    view = _view(brain)
    kinds = {edge["edge_type"] for edge in view["edges"]}
    assert kinds & {"SUPPORTED_BY", "INVOLVES", "ABOUT"}, kinds


def test_an_edge_with_one_visible_source_is_shown(brain):
    """can_read_edge uses any(); the explorer used issubset. Two answers to
    one question, and the stricter one silently won here."""
    edges = brain.store.list_edges("org_acme")
    cited = next((e for e in edges if e.get("source_ids")), None)
    assert cited is not None
    view = _view(brain, allowed={cited["source_ids"][0]})
    assert any(e["id"] == cited["id"] for e in view["edges"])


def test_structural_edges_need_no_evidence(brain):
    """PRODUCED and USED_SKILL are facts about Toda's own state, so they
    carry no cited source to gate on."""
    view = _view(brain, allowed=set())
    for edge in view["edges"]:
        assert not (set(edge.get("source_ids") or []) - set())


def test_every_edge_endpoint_has_a_node(brain):
    """An edge to an artifact, skill or scope has no entity row. Returning
    it without a node left the renderer a dangling reference, which it
    drops -- so a Task that PRODUCED a report drew as an isolated dot."""
    view = _view(brain)
    node_ids = {node["id"] for node in view["nodes"]}
    for edge in view["edges"]:
        assert edge["from_id"] in node_ids, edge
        assert edge["to_id"] in node_ids, edge


def test_every_node_carries_a_readable_name(brain):
    view = _view(brain)
    for node in view["nodes"]:
        name = str(node.get("name") or "")
        assert name.strip(), node
        assert name != node["id"]
