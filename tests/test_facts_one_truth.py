"""M5: facts are the one truth; edges are a projection of facts."""


def _fact(brain, *, subject="person_ana", predicate="owns", obj="Acme account",
          object_entity_id=None, scope="scope_org_public"):
    return brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id=subject,
        predicate=predicate,
        object_value=obj,
        scope_id=scope,
        valid_from="2026-01-01T00:00:00+00:00",
        source_ids=[],
        object_entity_id=object_entity_id,
    )


def test_fact_with_entity_object_projects_edge_and_supersede_closes_it(brain):
    fact = _fact(brain, object_entity_id="entity_acme")
    edges = [
        e for e in brain.store.list_edges("org_acme", active_only=False)
        if (e.get("properties") or {}).get("fact_id") == fact["id"]
    ]
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "owns"
    assert edges[0]["from_id"] == "person_ana"
    assert edges[0]["to_id"] == "entity_acme"

    fact2 = _fact(brain, obj="Globex account", object_entity_id="entity_globex")
    # 'owns' defaulted single, so the stronger-or-equal second fact
    # superseded the first and closed its projected edge automatically.
    edges = [
        e for e in brain.store.list_edges("org_acme", active_only=False)
        if (e.get("properties") or {}).get("fact_id") == fact["id"]
    ]
    assert edges and edges[0].get("valid_until") == fact2["valid_from"]
    assert fact2["object_entity_id"] == "entity_globex"


def test_reserved_predicate_produces_no_fact_and_no_edge(brain):
    import logging

    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logging.getLogger("toda.reality").addHandler(_Handler())
    try:
        fact = brain.reality.apply_claim(
            "org_acme",
            {"subject": "Ana", "predicate": "member_of", "object_value": "team",
             "decision": "ADD"},
            {"id": "mem_x", "organization_id": "org_acme", "scope_id": "scope_org_public",
             "source_ids": [], "confidence": 0.9, "authority": 0.9, "entity_ids": []},
        )
    finally:
        logging.getLogger("toda.reality").handlers = [
            h for h in logging.getLogger("toda.reality").handlers
            if not isinstance(h, _Handler)
        ]
    assert fact is None
    assert any("member_of" in message for message in records)
    assert not [
        e for e in brain.store.list_edges("org_acme", active_only=False)
        if e.get("edge_type") == "member_of"
    ]


def test_memories_are_no_longer_superseded(brain):
    source = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test", source_type="test", external_id="m5-mem-1",
        content="The launch date is June 1.", scope_id="scope_org_public",
        author_entity_id="person_ajay", learn=True, processing_mode="memory",
    )
    source2 = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test", source_type="test", external_id="m5-mem-2",
        content="The launch date is June 15.", scope_id="scope_org_public",
        author_entity_id="person_ajay", learn=True, processing_mode="memory",
    )
    mems1 = source["memories"]
    mems2 = source2["memories"]
    assert mems1 and mems2
    for mem in mems1 + mems2:
        stored = brain.store.get_memory(mem["id"])
        assert stored["status"] in {"active", "confirmed"}
        assert stored["is_latest"] is True
