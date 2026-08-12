"""ModelMemoryExtractor can PROPOSE new business entities from a plain-text
mention, not just link to entities that already exist.

Before this, the graph could only ever grow person/conversation/source/task
entities: resolve_source_entities (reality.py) only creates a business
entity (team/project/customer/...) from a "hard" identifier in a structured
connector payload. An ordinary Slack conversation has no such payload, so
every project, team, customer and incident anyone ever talked about was
invisible to the graph -- this is the missing half that lets it actually
grow from ordinary conversation.
"""

from __future__ import annotations

import json

from toda.harness import extraction as extraction_module
from toda.harness.extraction import (
    _PROPOSABLE_ENTITY_TYPES,
    ModelMemoryExtractor,
)
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse


def _extractor(brain, response_content: str) -> ModelMemoryExtractor:
    model = ScriptedModelClient([ModelResponse(content=response_content, model="test-model")])
    return ModelMemoryExtractor(
        model=model,
        memory_service=brain.memory,
        store=brain.store,
        audit=brain.audit,
        reality=brain.reality,
    )


def _source(brain, content: str) -> dict:
    return brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="toda_agent",
        source_type="agent_task",
        external_id="task_extraction_test",
        content=content,
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        learn=False,
        processing_mode="reference",
    )["source"]


def test_a_proposed_entity_with_no_existing_match_is_created_and_linked(brain):
    source = _source(brain, "Kickoff for Project Nightingale is next week.")
    response = json.dumps(
        {
            "proposed_entities": [
                {
                    "ref": "1",
                    "name": "Project Nightingale",
                    "entity_type": "project",
                    "aliases": [],
                }
            ],
            "memories": [
                {
                    "memory_type": "fact",
                    "content": "Project Nightingale kicks off next week.",
                    "confidence": 0.8,
                    "authority": 0.7,
                    "entity_ids": ["new:1"],
                    "participant_ids": [],
                    "canonical_claim": None,
                }
            ],
        }
    )
    committed = _extractor(brain, response).extract_source(source)

    assert len(committed) == 1
    entity_ids = committed[0]["entity_ids"]
    assert len(entity_ids) == 1
    entity = brain.graph.get_entity(entity_ids[0])
    assert entity["name"] == "Project Nightingale"
    assert entity["entity_type"] == "project"


def test_two_memories_mentioning_the_same_new_entity_resolve_to_one_id(brain):
    """The proposal is resolved ONCE per batch, not once per memory -- two
    memories mentioning the same new thing must end up pointing at the
    same entity, not two independently-created duplicates."""
    source = _source(brain, "Sanjay owns the migration. Sanjay is blocked on infra.")
    response = json.dumps(
        {
            "proposed_entities": [
                {"ref": "p", "name": "Sanjay Rao", "entity_type": "person", "aliases": []}
            ],
            "memories": [
                {
                    "memory_type": "fact",
                    "content": "Sanjay Rao owns the migration.",
                    "confidence": 0.8,
                    "authority": 0.7,
                    "entity_ids": ["new:p"],
                    "participant_ids": [],
                    "canonical_claim": None,
                },
                {
                    "memory_type": "issue",
                    "content": "Sanjay Rao is blocked on infra.",
                    "confidence": 0.8,
                    "authority": 0.7,
                    "entity_ids": ["new:p"],
                    "participant_ids": [],
                    "canonical_claim": None,
                },
            ],
        }
    )
    committed = _extractor(brain, response).extract_source(source)

    assert len(committed) == 2
    first_ids, second_ids = committed[0]["entity_ids"], committed[1]["entity_ids"]
    assert first_ids == second_ids
    assert len(first_ids) == 1


def test_a_fuzzy_matched_proposal_is_never_linked_to_the_memory(brain):
    """A merely-similar existing name queues a resolution candidate for
    human review (RealityEngine.propose_entity_mention returns entity_id=
    None, decision="candidate") -- the memory must come through with that
    reference simply dropped, never a guessed or duplicate id."""
    brain.graph.upsert_entity(
        {
            "id": "customer_acme_corp",
            "organization_id": "org_acme",
            "entity_type": "customer",
            "name": "Acme Corporation",
        }
    )
    source = _source(brain, "Acme Corp asked about pricing.")
    response = json.dumps(
        {
            "proposed_entities": [
                {"ref": "c", "name": "Acme Corp", "entity_type": "customer", "aliases": []}
            ],
            "memories": [
                {
                    "memory_type": "fact",
                    "content": "Acme Corp asked about pricing.",
                    "confidence": 0.8,
                    "authority": 0.7,
                    "entity_ids": ["new:c"],
                    "participant_ids": [],
                    "canonical_claim": None,
                }
            ],
        }
    )
    committed = _extractor(brain, response).extract_source(source)

    assert len(committed) == 1
    assert committed[0]["entity_ids"] == []
    candidates = brain.reality.list_resolution_candidates("org_acme", status="pending")
    assert any(c["left_entity_id"] == "customer_acme_corp" for c in candidates)


def test_an_unresolvable_ref_and_a_bare_unknown_id_are_both_dropped_not_guessed(brain):
    """A ref the model never declared in proposed_entities, and a bare id
    that isn't in the known-entities catalog either, must both be silently
    dropped -- never passed through as if they were real ids."""
    source = _source(brain, "Something vague happened.")
    response = json.dumps(
        {
            "proposed_entities": [],
            "memories": [
                {
                    "memory_type": "fact",
                    "content": "Something vague happened.",
                    "confidence": 0.6,
                    "authority": 0.5,
                    "entity_ids": ["new:never_declared", "entity_made_up_id"],
                    "participant_ids": [],
                    "canonical_claim": None,
                }
            ],
        }
    )
    committed = _extractor(brain, response).extract_source(source)

    assert len(committed) == 1
    assert committed[0]["entity_ids"] == []


def test_extraction_without_a_reality_engine_still_works_but_never_creates(brain):
    """reality is optional on ModelMemoryExtractor -- when absent, proposed
    entities must be silently ignored rather than raising, falling back to
    the original known-catalog-only linking behavior."""
    model = ScriptedModelClient(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "proposed_entities": [
                            {
                                "ref": "1",
                                "name": "Ghost Project",
                                "entity_type": "project",
                                "aliases": [],
                            }
                        ],
                        "memories": [
                            {
                                "memory_type": "fact",
                                "content": "Ghost Project exists.",
                                "confidence": 0.7,
                                "authority": 0.6,
                                "entity_ids": ["new:1"],
                                "participant_ids": [],
                                "canonical_claim": None,
                            }
                        ],
                    }
                ),
                model="test-model",
            )
        ]
    )
    extractor = ModelMemoryExtractor(
        model=model,
        memory_service=brain.memory,
        store=brain.store,
        audit=brain.audit,
        # reality intentionally omitted
    )
    source = _source(brain, "Ghost Project exists.")

    committed = extractor.extract_source(source)

    assert len(committed) == 1
    assert committed[0]["entity_ids"] == []
    assert not any(
        e.get("name") == "Ghost Project"
        for e in brain.store.list_entities("org_acme", entity_types=["project"])
    )


def test_known_entity_catalog_excludes_structural_event_entities(brain):
    """`source` and `conversation` entities are named from raw external ids
    ("slack event Ev0BNJ...", "slack C0BP95K48V6"), one per Slack message.
    Nothing a person writes can refer to one, so they must never occupy a
    slot in the catalog the extractor links claims against."""
    for index in range(5):
        brain.graph.upsert_entity(
            {
                "id": f"source_noise_{index}",
                "organization_id": "org_acme",
                "entity_type": "source",
                "name": f"slack event Ev0BNJFXUR{index}",
                "aliases": [],
                "attributes": {},
            }
        )
    brain.graph.upsert_entity(
        {
            "id": "conversation_noise",
            "organization_id": "org_acme",
            "entity_type": "conversation",
            "name": "slack C0BP95K48V6",
            "aliases": [],
            "attributes": {},
        }
    )
    extractor = _extractor(brain, "{}")
    catalog = extractor._known_entity_catalog("org_acme")

    assert catalog, "expected real business entities to remain in the catalog"
    assert {entry["type"] for entry in catalog} <= set(_PROPOSABLE_ENTITY_TYPES)
    ids = {entry["id"] for entry in catalog}
    assert "conversation_noise" not in ids
    assert not any(entry_id.startswith("source_noise_") for entry_id in ids)


def test_known_entity_catalog_keeps_the_most_recent_entities_when_capped(
    brain, monkeypatch
):
    """Once an organization has more entities than the cap, the newest ones
    must survive -- previously the slice took whatever order SQLite
    returned, so a just-created entity could be invisible to extraction."""
    monkeypatch.setattr(extraction_module, "_KNOWN_ENTITY_CATALOG_LIMIT", 3)
    for index in range(6):
        brain.graph.upsert_entity(
            {
                "id": f"project_cap_{index}",
                "organization_id": "org_acme",
                "entity_type": "project",
                "name": f"Project Cap {index}",
                "aliases": [],
                "attributes": {},
                "updated_at": f"2026-08-0{index + 1}T00:00:00+00:00",
            }
        )
    extractor = _extractor(brain, "{}")
    catalog = extractor._known_entity_catalog("org_acme")

    assert len(catalog) == 3
    # upsert_entity stamps updated_at itself, so the last three written win.
    assert [entry["id"] for entry in catalog] == [
        "project_cap_5",
        "project_cap_4",
        "project_cap_3",
    ]
