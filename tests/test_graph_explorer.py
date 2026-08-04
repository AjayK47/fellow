"""Explorer returns a permission-safe, renderable organization graph."""

from __future__ import annotations

from docket.domain.models import utcnow


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
