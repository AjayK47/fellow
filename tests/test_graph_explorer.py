"""Explorer returns a permission-safe, renderable organization graph."""

from __future__ import annotations

from toda.domain.models import utcnow


def test_superseded_edge_stops_being_current_but_stays_queryable_historically(brain):
    org = "org_acme"
    brain.graph.upsert_entity({"id": "person_a", "organization_id": org, "entity_type": "person", "name": "A"})
    brain.graph.upsert_entity({"id": "person_b", "organization_id": org, "entity_type": "person", "name": "B"})
    brain.graph.upsert_entity({"id": "service_x", "organization_id": org, "entity_type": "service", "name": "X"})

    original = brain.graph.add_edge(org, "OWNS", "person_a", "service_x")
    before_transfer = utcnow().isoformat()
    transferred = brain.graph.add_edge(
        org, "OWNS", "person_b", "service_x", replaces=original["id"]
    )

    current = brain.graph.list_edges(org, edge_type="OWNS", to_id="service_x")
    assert [e["id"] for e in current] == [transferred["id"]]

    owners_now = brain.graph.resolve_owners(org, ["service_x"])
    assert [o["owner_id"] for o in owners_now] == ["person_b"]

    historical = brain.graph.edges_as_of(org, valid_at=before_transfer, edge_type="OWNS", to_id="service_x")
    assert [e["id"] for e in historical] == [original["id"]]

    closed = brain.graph.get_edge(original["id"])
    assert closed["valid_until"] and closed["system_to"]


def test_paths_between_finds_a_direct_and_a_multi_hop_path(brain):
    """paths_between() is a recursive SQL CTE scoped to the actual query,
    not a Python BFS over every edge in the org -- these tests are the
    correctness baseline for that rewrite (no prior tests existed)."""
    org = "org_acme"
    brain.graph.add_edge(org, "OWNS", "person_a", "service_x")
    brain.graph.add_edge(org, "OWNS", "person_b", "service_y")
    brain.graph.add_edge(org, "DEPENDS_ON", "service_x", "service_y")

    direct = brain.graph.paths_between(org, "person_a", "service_x", max_depth=3)
    assert len(direct) == 1
    assert [e["id"] for e in direct[0]] == [
        e["id"]
        for e in brain.graph.list_edges(org, edge_type="OWNS", from_id="person_a")
    ]

    multi_hop = brain.graph.paths_between(org, "person_a", "person_b", max_depth=3)
    assert len(multi_hop) == 1
    path = multi_hop[0]
    assert len(path) == 3
    # person_a -[OWNS]-> service_x -[DEPENDS_ON]-> service_y <-[OWNS]- person_b
    assert path[0]["from_id"] == "person_a" and path[0]["to_id"] == "service_x"
    assert path[1]["from_id"] == "service_x" and path[1]["to_id"] == "service_y"
    # The third edge is stored as person_b -> service_y but traversed
    # backward -- must come back normalized to service_y -> person_b with
    # _reversed set, matching the original BFS's undirected semantics.
    assert path[2]["from_id"] == "service_y" and path[2]["to_id"] == "person_b"
    assert path[2]["_reversed"] is True


def test_paths_between_respects_max_depth(brain):
    org = "org_acme"
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_a", "svc_b")
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_b", "svc_c")
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_c", "svc_d")

    assert brain.graph.paths_between(org, "svc_a", "svc_d", max_depth=2) == []
    found = brain.graph.paths_between(org, "svc_a", "svc_d", max_depth=3)
    assert len(found) == 1
    assert len(found[0]) == 3


def test_paths_between_filters_by_edge_type(brain):
    org = "org_acme"
    brain.graph.add_edge(org, "OWNS", "person_a", "service_x")
    brain.graph.add_edge(org, "DEPENDS_ON", "service_x", "service_y")

    all_types = brain.graph.paths_between(org, "person_a", "service_y", max_depth=3)
    assert len(all_types) == 1

    filtered = brain.graph.paths_between(
        org, "person_a", "service_y", max_depth=3, edge_types=["DEPENDS_ON"]
    )
    assert filtered == []  # the only path requires the OWNS edge first, filtered out


def test_paths_between_terminates_and_stays_bounded_with_a_cycle(brain):
    """A cycle in the (undirected) graph must not cause infinite recursion,
    and every returned path must still respect max_depth and never revisit
    a node."""
    org = "org_acme"
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_a", "svc_b")
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_b", "svc_c")
    brain.graph.add_edge(org, "DEPENDS_ON", "svc_c", "svc_a")  # closes the cycle

    found = brain.graph.paths_between(org, "svc_a", "svc_c", max_depth=3)
    assert found  # at least one path exists (directly, and via svc_b)
    for path in found:
        assert len(path) <= 3
        nodes = [path[0]["from_id"]] + [e["to_id"] for e in path]
        assert len(nodes) == len(set(nodes))  # no node visited twice


def test_paths_between_returns_empty_when_unreachable(brain):
    org = "org_acme"
    brain.graph.add_edge(org, "OWNS", "person_a", "service_x")
    assert brain.graph.paths_between(org, "person_a", "service_never_connected") == []


def test_supersede_edge_endpoint_closes_without_replacement(api_client):
    api_client.post(
        "/v1/entities",
        json={"id": "team_x", "organization_id": "org_acme", "entity_type": "team", "name": "X"},
    )
    api_client.post(
        "/v1/entities",
        json={"id": "person_leaver", "organization_id": "org_acme", "entity_type": "person", "name": "Leaver"},
    )
    created = api_client.post(
        "/v1/edges",
        json={
            "organization_id": "org_acme",
            "edge_type": "MEMBER_OF",
            "from_id": "person_leaver",
            "to_id": "team_x",
        },
    ).json()

    response = api_client.post(f"/v1/edges/{created['id']}:supersede")
    assert response.status_code == 200
    assert response.json()["valid_until"]

    missing = api_client.post("/v1/edges/edge_does_not_exist:supersede")
    assert missing.status_code == 404


def test_explorer_exposes_org_graph_and_page(api_client):
    data = api_client.get("/v1/graph/explorer", params={"organization_id":"org_acme", "acting_principal_id":"person_ajay"})
    assert data.status_code == 200
    body = data.json()
    assert body["nodes"] and body["edges"] and body["permission_decision_id"]
    assert all(edge["edge_type"] not in {"UPDATES", "SUPPORTED_BY", "ABOUT"} or edge["edge_type"] == "ABOUT" for edge in body["edges"])
    page = api_client.get("/explorer")
    assert page.status_code == 200
    assert "<div id=\"root\"></div>" in page.text or "Operations" in page.text
