"""Explorer returns a permission-safe, renderable organization graph."""

from __future__ import annotations


def test_explorer_exposes_org_graph_and_page(api_client):
    data = api_client.get("/v1/graph/explorer", params={"organization_id":"org_acme", "acting_principal_id":"person_ajay"})
    assert data.status_code == 200
    body = data.json()
    assert body["nodes"] and body["edges"] and body["permission_decision_id"]
    assert all(edge["edge_type"] not in {"UPDATES", "SUPPORTED_BY", "ABOUT"} or edge["edge_type"] == "ABOUT" for edge in body["edges"])
    page = api_client.get("/explorer")
    assert page.status_code == 200
    assert "Operations" in page.text
