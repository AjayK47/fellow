"""M4: extraction decisions + predicate catalog."""

import json

from toda.harness.extraction import ModelMemoryExtractor
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse


def _extractor(brain, *responses: str) -> ModelMemoryExtractor:
    model = ScriptedModelClient(
        [ModelResponse(content=r, model="test-model") for r in responses]
    )
    return ModelMemoryExtractor(
        model=model,
        memory_service=brain.memory,
        store=brain.store,
        audit=brain.audit,
        reality=brain.reality,
    )


def _source(brain, content: str, external_id: str) -> dict:
    return brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="test",
        external_id=external_id,
        content=content,
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        learn=False,
        processing_mode="reference",
    )["source"]


def _memory_payload(*, decision, subject, predicate, obj, target=None):
    return {
        "proposed_entities": [],
        "memories": [
            {
                "memory_type": "fact",
                "content": f"{subject} {predicate} {obj}",
                "confidence": 0.9,
                "authority": 0.8,
                "entity_ids": [],
                "participant_ids": [],
                "claim": {
                    "subject": subject,
                    "predicate": predicate,
                    "object": {"value": obj},
                    "decision": decision,
                    **({"target_fact_id": target} if target else {}),
                },
                "valid_until": None,
            }
        ],
    }


def _apply(brain, extractor, source):
    memories = extractor.extract_source(source)
    assert len(memories) == 1
    return brain.reality.apply_claim(
        "org_acme", memories[0]["claim"], memories[0], source=source
    )


def test_standup_update_supersedes_old_time(brain):
    source1 = _source(brain, "Standup is at 9am.", "m4-standup-1")
    extractor = _extractor(brain, json.dumps(_memory_payload(
        decision="ADD", subject="standup", predicate="meeting_time", obj="9am")))
    fact1 = _apply(brain, extractor, source1)
    assert fact1 is not None and fact1["status"] == "active"

    source2 = _source(brain, "Standup moved to 10am.", "m4-standup-2")
    extractor2 = _extractor(brain, json.dumps(_memory_payload(
        decision="UPDATE", subject="standup", predicate="meeting_time",
        obj="10am", target=fact1["id"])))
    fact2 = _apply(brain, extractor2, source2)
    assert fact2 is not None and fact2["status"] == "active"
    old = brain.reality._get_payload("reality_facts", fact1["id"])
    assert old["status"] == "superseded"
    assert old["valid_to"] == fact2["valid_from"]
    current = brain.reality.current_facts("org_acme", subject_id=fact2["subject_id"])
    assert [f["id"] for f in current] == [fact2["id"]]


def _conflicts_about(brain, subject_id):
    """The seeded org has its own (resolved) launch-date conflict."""
    return [
        conflict
        for conflict in brain.reality.list_conflicts("org_acme")
        if any(
            brain.reality._get_payload("reality_facts", fact_id)["subject_id"] == subject_id
            for fact_id in conflict["fact_ids"]
        )
    ]


def test_multi_valued_add_keeps_both_with_no_conflict(brain):
    source1 = _source(brain, "Priya likes ice cream.", "m4-likes-1")
    extractor = _extractor(brain, json.dumps(_memory_payload(
        decision="ADD", subject="Priya", predicate="likes", obj="ice cream")))
    fact1 = _apply(brain, extractor, source1)
    assert fact1 is not None

    source2 = _source(brain, "Priya likes coffee.", "m4-likes-2")
    extractor2 = _extractor(brain, json.dumps(_memory_payload(
        decision="ADD", subject="Priya", predicate="likes", obj="coffee")))
    fact2 = _apply(brain, extractor2, source2)
    current = brain.reality.current_facts("org_acme", subject_id=fact2["subject_id"])
    assert {f["object_value"] for f in current} == {"ice cream", "coffee"}
    assert _conflicts_about(brain, fact2["subject_id"]) == []
    record = brain.store.get_predicate("org_acme", "likes")
    assert record is not None and record["cardinality"] == "multi"


def test_contradicts_creates_contested_fact_and_conflict(brain):
    source1 = _source(brain, "Ana owns Acme.", "m4-owns-1")
    extractor = _extractor(brain, json.dumps(_memory_payload(
        decision="ADD", subject="Acme", predicate="owner", obj="Ana")))
    fact1 = _apply(brain, extractor, source1)
    assert fact1 is not None

    source2 = _source(brain, "Rumor: Ben owns Acme.", "m4-owns-2")
    extractor2 = _extractor(brain, json.dumps(_memory_payload(
        decision="CONTRADICTS", subject="Acme", predicate="owner",
        obj="Ben", target=fact1["id"])))
    fact2 = _apply(brain, extractor2, source2)
    assert fact2 is not None and fact2["status"] == "contested"
    assert len(_conflicts_about(brain, fact2["subject_id"])) == 1


def test_noop_attaches_source_without_new_fact(brain):
    source1 = _source(brain, "Standup is at 9am.", "m4-noop-1")
    extractor = _extractor(brain, json.dumps(_memory_payload(
        decision="ADD", subject="standup", predicate="meeting_time", obj="9am")))
    fact1 = _apply(brain, extractor, source1)

    source2 = _source(brain, "Standup is at 9am.", "m4-noop-2")
    extractor2 = _extractor(brain, json.dumps(_memory_payload(
        decision="NOOP", subject="standup", predicate="meeting_time",
        obj="9am", target=fact1["id"])))
    assert _apply(brain, extractor2, source2) is None
    updated = brain.reality._get_payload("reality_facts", fact1["id"])
    assert source2["id"] in (updated.get("source_ids") or [])


def test_predicate_merge_by_embedding_similarity(brain):
    class FakeEmbedder:
        def __init__(self, vectors):
            self.vectors = vectors

        def embed(self, texts):
            return [self.vectors[text] for text in texts]

    brain.reality.embeddings = FakeEmbedder(
        {
            "likes": [1.0, 0.0, 0.0],
            "enjoys": [0.99, 0.01, 0.0],
            "standup_time": [0.0, 1.0, 0.0],
        }
    )
    assert brain.reality.normalize_predicate("org_acme", "likes") == "likes"
    assert brain.reality.normalize_predicate("org_acme", "enjoys") == "likes"
    assert brain.reality.normalize_predicate("org_acme", "standup_time") == "standup_time"


def test_invalid_decision_values_are_rejected(brain):
    source = _source(brain, "Something happened.", "m4-invalid-1")
    payload = _memory_payload(
        decision="ADD", subject="x", predicate="y", obj="z")
    payload["memories"][0]["claim"]["decision"] = "MAYBE"
    extractor = _extractor(brain, json.dumps(payload))
    assert extractor.extract_source(source) == []
