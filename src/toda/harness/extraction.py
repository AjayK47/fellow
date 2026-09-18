"""Asynchronous model-assisted organizational memory extraction."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from toda.harness.prompting.catalog import load
from toda.domain.models import (
    EXTRACTABLE_EDGE_TYPES,
    CanonicalClaim,
    ClaimDecision,
    Memory,
    MemoryClaim,
    MemoryRelation,
    MemoryType,
    utcnow,
)

# Common entity kinds, offered to the extractor as examples rather than as
# a closed set. This was an enum in the response schema, and the omission
# was total: no company, technology, place, product or regulation, so a
# 53,000-word report on semiconductor policy yielded no entity for TSMC,
# Taiwan or the CHIPS Act -- nothing it described could be represented.
#
# Supermemory's graph is built the same way: "you do not define schema or
# draw edges". Entities emerge from the content while the relations stay a
# small fixed vocabulary, which Toda already has in MemoryEdgeType.
_COMMON_ENTITY_TYPES = (
    "person", "team", "project", "service", "customer", "incident",
    "document", "issue", "decision", "commitment",
    "company", "technology", "product", "place", "regulation", "event",
)

# Kinds the harness mints per event rather than learning from content.
# `source` ("slack event Ev0BNJ...") and `conversation` ("slack C0BP95K48V6")
# are named from raw external ids, so nothing a person writes can refer to
# one, and one is created for every Slack message -- left in, they become
# the overwhelming majority of the catalog and push real subjects past the
# cap. Excluded by name rather than by allow-listing business types, so a
# kind nobody has thought of yet still reaches the model.
_STRUCTURAL_ENTITY_TYPES = frozenset(
    {"source", "conversation", "channel", "task", "agent", "application", "skill"}
)

# How many known entities the extractor is shown per source. Bounded to keep
# the request small; see _known_entity_catalog for what fills it and why the
# ordering matters once an organization has more entities than this.
_KNOWN_ENTITY_CATALOG_LIMIT = 500

# A memory's entity_ids/participant_ids may reference either a real id from
# the supplied known_entities catalog, or one of THIS batch's own proposed
# entities by its local ref -- letting one proposal be reused across
# several memories in the same extraction instead of repeating the full
# entity definition inline on every memory that mentions it.
_PROPOSED_REF_PREFIX = "new:"

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposed_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "name": {"type": "string"},
                    "entity_type": {
                        "type": "string",
                        "description": (
                            "What kind of thing this is, lowercase and singular. "
                            "Common values: " + ", ".join(_COMMON_ENTITY_TYPES)
                            + ". Use another word when none of those fit -- the "
                            "set is deliberately open."
                        ),
                    },
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ref", "name", "entity_type", "aliases"],
                "additionalProperties": False,
            },
        },
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "memory_type": {
                        "type": "string",
                        "enum": [
                            "fact",
                            "decision",
                            "commitment",
                            "issue",
                            "relationship",
                            "preference",
                            "episode_summary",
                            "outcome",
                            "derived",
                        ],
                    },
                    "content": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "authority": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "participant_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "claim": {
                        "type": ["object", "null"],
                        "description": (
                            "The update decision for this claim against the "
                            "current facts supplied: ADD a new fact, UPDATE the "
                            "target fact, CONTRADICTS the target fact, or NOOP "
                            "when already recorded. The object is either an "
                            "entity_ref (a known id or new: ref) or a plain "
                            "value, never both."
                        ),
                        "properties": {
                            "subject": {"type": "string"},
                            "predicate": {"type": "string"},
                            "object": {
                                "type": "object",
                                "properties": {
                                    "entity_ref": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "additionalProperties": False,
                            },
                            "decision": {
                                "type": "string",
                                "enum": ["ADD", "UPDATE", "CONTRADICTS", "NOOP"],
                            },
                            "target_fact_id": {"type": ["string", "null"]},
                        },
                        "required": ["subject", "predicate", "object", "decision"],
                        "additionalProperties": False,
                    },
                    "valid_until": {
                        "type": ["string", "null"],
                        "description": (
                            "ISO 8601 timestamp for when this claim STOPPED being "
                            "true, if the content states or clearly implies an end "
                            "-- a leave that runs to a date, a contract that "
                            "expires, a role someone left, a deadline that passes. "
                            "Null when no end is stated, which is the normal case. "
                            "Never guess a lifetime and never infer a date from "
                            "unrelated events: a claim with no stated end is open, "
                            "not short-lived."
                        ),
                    },
                },
                "required": [
                    "memory_type",
                    "content",
                    "confidence",
                    "authority",
                    "entity_ids",
                    "participant_ids",
                    "claim",
                    "valid_until",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposed_entities", "memories"],
    "additionalProperties": False,
}


def _parse_timestamp(value: Any) -> "datetime | None":
    """An ISO timestamp the model supplied, or None.

    Anything unparseable is dropped rather than defaulted: a bad date here
    would close a fact's validity interval at the wrong moment, which is
    worse than leaving it open.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ModelMemoryExtractor:
    def __init__(
        self,
        *,
        model: Any,
        memory_service: Any,
        store: Any,
        audit: Any,
        reality: Any | None = None,
    ) -> None:
        self.model = model
        self.memory = memory_service
        self.store = store
        self.audit = audit
        # Optional: without it, entity_ids/participant_ids fall back to
        # known-catalog-only linking (the original, narrower behavior) --
        # proposed_entities are still requested from the model but silently
        # dropped, same as an unresolvable id always was.
        self.reality = reality

    @property
    def live(self) -> bool:
        return getattr(self.model, "model", "").lower() not in {
            "deterministic-harness-v1",
            "",
        }

    def _known_entities(self, organization_id: str) -> list[dict[str, Any]]:
        """Every entity a claim may legitimately be linked to.

        Excludes only the structural kinds the harness mints per event (see
        _STRUCTURAL_ENTITY_TYPES). Everything a person or a document could
        actually name stays referenceable, including kinds nobody enumerated
        in advance.

        Ordered newest-updated first so that when the cap does bite, it
        drops the least recently relevant entity rather than whichever row
        SQLite happened to return last.
        """
        entities = [
            entity
            for entity in self.store.list_entities(organization_id)
            if str(entity.get("entity_type") or "").lower()
            not in _STRUCTURAL_ENTITY_TYPES
        ]
        entities.sort(
            key=lambda entity: (
                entity.get("updated_at") or entity.get("created_at") or "",
                entity.get("id") or "",
            ),
            reverse=True,
        )
        return entities

    def _known_entity_catalog(self, organization_id: str) -> list[dict[str, Any]]:
        """The bounded `known_entities` projection actually sent to the model."""
        return [
            {
                "id": entity["id"],
                "type": entity.get("entity_type"),
                "name": entity.get("name"),
                "aliases": entity.get("aliases") or [],
            }
            for entity in self._known_entities(organization_id)[
                :_KNOWN_ENTITY_CATALOG_LIMIT
            ]
        ]

    def extract_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        # Linking stays lenient against the full filtered set, not just the
        # capped projection: a real entity the model referenced from its own
        # context is still a valid link even if it fell outside the catalog.
        known_entities = self._known_entities(source["organization_id"])
        entity_catalog = [
            {
                "id": entity["id"],
                "type": entity.get("entity_type"),
                "name": entity.get("name"),
                "aliases": entity.get("aliases") or [],
            }
            for entity in known_entities[:_KNOWN_ENTITY_CATALOG_LIMIT]
        ]
        current_facts: list[dict[str, Any]] = []
        predicate_catalog: list[dict[str, Any]] = []
        if self.reality is not None:
            try:
                source_entity_ids = list(source.get("entity_ids") or [])[:20]
                for entity_id in source_entity_ids:
                    for fact in self.reality.current_facts(
                        source["organization_id"], subject_id=entity_id
                    )[:10]:
                        current_facts.append(
                            {
                                "fact_id": fact["id"],
                                "subject": fact.get("subject_id"),
                                "predicate": fact.get("predicate"),
                                "object": fact.get("object_value"),
                            }
                        )
                used_predicates = sorted(
                    {fact["predicate"] for fact in current_facts if fact.get("predicate")}
                )
                seen = set(used_predicates)
                for predicate in self.store.predicates_by_usage(
                    source["organization_id"], limit=50
                ):
                    predicate_catalog.append(
                        {"name": predicate["name"], "cardinality": predicate["cardinality"]}
                    )
                    seen.add(predicate["name"])
                for predicate in self.store.predicates_for_names(
                    source["organization_id"], [p for p in used_predicates if p not in seen]
                ):
                    predicate_catalog.append(
                        {"name": predicate["name"], "cardinality": predicate["cardinality"]}
                    )
            except Exception:
                current_facts, predicate_catalog = [], []
        response = self.model.generate(
            messages=[
                {"role": "system", "content": load("memory_extraction")},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_id": source["id"],
                            "content": source.get("content") or "",
                            "known_entities": entity_catalog,
                            "current_facts": current_facts,
                            "predicates": predicate_catalog,
                        },
                        sort_keys=True,
                    ),
                },
            ],
            response_schema=EXTRACTION_SCHEMA,
            temperature=0.0,
            max_tokens=1800,
        )
        payload = json.loads(response.content)
        known_ids = {entity["id"] for entity in known_entities}
        # Resolve every proposed entity ONCE, up front, into a ref -> real
        # id map -- not per memory, so two memories mentioning the same
        # new entity in this batch resolve to the same id (and hit the
        # exact-match fast path, not two independent fuzzy matches that
        # could disagree with each other).
        ref_to_id: dict[str, str] = {}
        if self.reality is not None:
            for proposal in payload.get("proposed_entities") or []:
                ref = str(proposal.get("ref") or "").strip()
                name = str(proposal.get("name") or "").strip()
                # Normalized, not validated against a list: rejecting an
                # unfamiliar kind here would discard exactly the entities
                # the open schema exists to capture. A proposal still needs
                # a ref, a name, and some stated kind.
                entity_type = str(proposal.get("entity_type") or "").strip().lower()
                if not ref or not name or not entity_type:
                    continue
                entity_id, _decision = self.reality.propose_entity_mention(
                    source["organization_id"],
                    name=name,
                    entity_type=entity_type,
                    source_id=source["id"],
                    aliases=[
                        str(alias) for alias in (proposal.get("aliases") or [])
                    ],
                )
                if entity_id:
                    # "matched"/"created" both yield a usable id; "candidate"
                    # (entity_id is None) is left unresolved on purpose --
                    # linking a memory to an entity that's still pending
                    # human review would make that review meaningless.
                    ref_to_id[f"{_PROPOSED_REF_PREFIX}{ref}"] = entity_id

        def _resolve_ids(raw_ids: list[Any]) -> list[str]:
            resolved: list[str] = []
            for raw_id in raw_ids or []:
                raw_id = str(raw_id)
                if raw_id in known_ids:
                    resolved.append(raw_id)
                elif raw_id in ref_to_id:
                    resolved.append(ref_to_id[raw_id])
            return resolved

        committed: list[dict[str, Any]] = []
        for item in payload.get("memories") or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            entity_ids = _resolve_ids(item.get("entity_ids") or [])
            participant_ids = _resolve_ids(item.get("participant_ids") or [])
            claim_data = item.get("claim")
            claim = None
            if isinstance(claim_data, dict):
                try:
                    raw_object = claim_data.get("object") or {}
                    decision = str(claim_data.get("decision") or "").upper()
                    if decision not in {"ADD", "UPDATE", "CONTRADICTS", "NOOP"}:
                        continue
                    claim = MemoryClaim(
                        subject=str(claim_data.get("subject") or ""),
                        predicate=str(claim_data.get("predicate") or ""),
                        object_entity_ref=(
                            str(raw_object.get("entity_ref") or "") or None
                            if isinstance(raw_object, dict)
                            else None
                        ),
                        object_value=(
                            str(raw_object.get("value") or "") or None
                            if isinstance(raw_object, dict)
                            else None
                        ),
                        decision=ClaimDecision(decision),
                        target_fact_id=claim_data.get("target_fact_id"),
                    )
                except Exception:
                    continue
            legacy_claim_data = item.get("canonical_claim")
            claim_legacy = CanonicalClaim(**legacy_claim_data) if legacy_claim_data else None
            # Only keep the edge when BOTH ends resolve to entities that exist
            # (or were proposed in this same pass). A half-resolved edge would
            # point at a name string and be unqueryable.
            relation = None
            raw_relation = item.get("relation") or {}
            if raw_relation.get("edge_type") in EXTRACTABLE_EDGE_TYPES:
                ends = _resolve_ids([raw_relation.get("from"), raw_relation.get("to")])
                if len(ends) == 2 and ends[0] != ends[1]:
                    relation = MemoryRelation(
                        edge_type=str(raw_relation["edge_type"]),
                        from_id=ends[0],
                        to_id=ends[1],
                    )
            # A claim object entity_ref must resolve like any other id.
            object_entity_id: str | None = None
            if claim is not None and claim.object_entity_ref:
                resolved = _resolve_ids([claim.object_entity_ref])
                object_entity_id = resolved[0] if resolved else None
            memory = Memory(
                organization_id=source["organization_id"],
                memory_type=MemoryType(item["memory_type"]),
                content=content,
                canonical_claim=claim_legacy,
                relation=relation,
                claim=claim,
                confidence=float(item.get("confidence", 0.75)),
                authority=float(item.get("authority", 0.7)),
                scope_id=source["scope_id"],
                extraction_method="contextual_llm_v1",
                model_version=getattr(self.model, "model", "unknown"),
                source_ids=[source["id"]],
                entity_ids=entity_ids,
                # The author is the source's, never the model's to infer --
                # participants can be extracted from the text, but who
                # asserted the claim is a fact about provenance.
                author_entity_id=source.get("author_entity_id"),
                participant_ids=participant_ids
                or (
                    [source["author_entity_id"]]
                    if source.get("author_entity_id")
                    else []
                ),
                source_spans=[
                    {
                        "source_id": source["id"],
                        "start": 0,
                        "end": min(len(source.get("content") or ""), 2000),
                        "text": (source.get("content") or "")[:2000],
                    }
                ],
                is_private=bool(source.get("is_private_channel")),
                valid_from=source.get("occurred_at") or utcnow(),
                # Only ever what the content stated. A claim with no stated
                # end is open, not short-lived, and guessing a lifetime here
                # would quietly retire facts that are still true.
                valid_until=_parse_timestamp(item.get("valid_until")),
                observed_at=source.get("ingested_at") or utcnow(),
            )
            data = self.memory._consolidate(memory)
            if object_entity_id:
                data["object_entity_id"] = object_entity_id
            committed.append(data)
        if self.memory.indexing and committed:
            self.memory.indexing.index_memories(committed)
        self.audit.record(
            source["organization_id"],
            "memory.capture",
            {
                "source_id": source["id"],
                "memory_ids": [item["id"] for item in committed],
                "model": getattr(self.model, "model", ""),
            },
            principal_id=source.get("author_entity_id"),
        )
        return committed
