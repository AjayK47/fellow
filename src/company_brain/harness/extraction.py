"""Asynchronous model-assisted organizational memory extraction."""

from __future__ import annotations

import json
from typing import Any

from company_brain.domain.models import (
    CanonicalClaim,
    Memory,
    MemoryType,
    utcnow,
)


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
                    "canonical_claim": {
                        "type": ["object", "null"],
                        "properties": {
                            "subject": {"type": "string"},
                            "predicate": {"type": "string"},
                            "object": {"type": "string"},
                        },
                        "required": ["subject", "predicate", "object"],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "memory_type",
                    "content",
                    "confidence",
                    "authority",
                    "entity_ids",
                    "participant_ids",
                    "canonical_claim",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}


class ModelMemoryExtractor:
    def __init__(
        self,
        *,
        model: Any,
        memory_service: Any,
        store: Any,
        audit: Any,
    ) -> None:
        self.model = model
        self.memory = memory_service
        self.store = store
        self.audit = audit

    @property
    def live(self) -> bool:
        return getattr(self.model, "model", "").lower() not in {
            "deterministic-harness-v1",
            "",
        }

    def extract_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        entities = self.store.list_entities(source["organization_id"])
        entity_catalog = [
            {
                "id": entity["id"],
                "type": entity.get("entity_type"),
                "name": entity.get("name"),
                "aliases": entity.get("aliases") or [],
            }
            for entity in entities[:500]
        ]
        response = self.model.generate(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract only durable, evidence-backed organizational "
                        "facts, decisions, commitments, issues, relationships, "
                        "and outcomes. Do not invent entity IDs. Use an entity ID "
                        "only when it appears in the supplied catalog. Keep claims "
                        "atomic and omit greetings, speculation, and agent prose."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_id": source["id"],
                            "content": source.get("content") or "",
                            "known_entities": entity_catalog,
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
        known_ids = {entity["id"] for entity in entities}
        committed: list[dict[str, Any]] = []
        for item in payload.get("memories") or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            entity_ids = [
                entity_id
                for entity_id in item.get("entity_ids") or []
                if entity_id in known_ids
            ]
            participant_ids = [
                entity_id
                for entity_id in item.get("participant_ids") or []
                if entity_id in known_ids
            ]
            claim_data = item.get("canonical_claim")
            claim = CanonicalClaim(**claim_data) if claim_data else None
            memory = Memory(
                organization_id=source["organization_id"],
                memory_type=MemoryType(item["memory_type"]),
                content=content,
                canonical_claim=claim,
                confidence=float(item.get("confidence", 0.75)),
                authority=float(item.get("authority", 0.7)),
                scope_id=source["scope_id"],
                extraction_method="contextual_llm_v1",
                model_version=getattr(self.model, "model", "unknown"),
                source_ids=[source["id"]],
                entity_ids=entity_ids,
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
                observed_at=utcnow(),
            )
            data = self.memory._consolidate(memory)
            committed.append(data)
        if self.memory.indexing and committed:
            self.memory.indexing.index_memories(committed)
        self.audit.record(
            source["organization_id"],
            "memory.model_extracted",
            {
                "source_id": source["id"],
                "memory_ids": [item["id"] for item in committed],
                "model": getattr(self.model, "model", ""),
            },
            principal_id=source.get("author_entity_id"),
        )
        return committed
