"""Evidence-backed corrections preserve temporal history and support as-of reads."""

from __future__ import annotations


def test_correction_preserves_prior_fact_for_as_of_reads(empty_brain):
    brain = empty_brain
    org = "org_temporal"
    brain.store.put_organization({"id": org, "name": "Temporal"})
    brain.store.upsert_scope({"id":"scope_org_public", "organization_id":org, "kind":"organization_public", "label":"public", "is_public":True})
    brain.store.add_membership(org, "nobody", "scope_org_public")
    first = brain.ingestion.ingest(organization_id=org, source_system="test", source_type="message", external_id="before", content="The launch date is 1 September.", scope_id="scope_org_public")
    old = first["memories"][0]
    evidence = brain.ingestion.ingest(organization_id=org, source_system="test", source_type="message", external_id="correction", content="The launch date is 15 September.", scope_id="scope_org_public", learn=False)["source"]
    corrected = brain.memory.correct(old["id"], content="The launch date is 15 September.", source_id=evidence["id"])
    assert corrected and corrected["id"] != old["id"]
    edges = brain.graph.list_edges(org, edge_type="CORRECTS", from_id=corrected["id"])
    assert edges and edges[0]["to_id"] == old["id"]
    historical = brain.retrieval.search(organization_id=org, query="launch date", acting_principal_id="nobody", as_of=old["valid_from"], include_superseded=True)
    assert any("1 September" in item["memory"]["content"] for item in historical["results"])
