"""Memory learning and consolidation."""

from __future__ import annotations

from typing import Any, Protocol

from toda.domain.models import (
    Memory,
    MemoryEdgeType,
    MemoryStatus,
    utcnow,
)
from toda.extractors.deterministic import DeterministicExtractor
from toda.services.audit import AuditService
from toda.services.graph import GraphService
from toda.services.indexing import IndexingService
from toda.storage.db import BrainStore


class Extractor(Protocol):
    name: str
    model_version: str

    def extract(self, **kwargs: Any) -> Any: ...


class MemoryService:
    def __init__(
        self,
        store: BrainStore,
        graph: GraphService,
        audit: AuditService,
        extractor: Extractor | None = None,
        indexing: IndexingService | None = None,
    ) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit
        self.extractor = extractor or DeterministicExtractor()
        self.indexing = indexing

    def learn_from_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract and consolidate memories from a single source evidence item."""
        org = source["organization_id"]
        self.store.telemetry.record(
            "memory.capture", component="memory", phase="started",
            input={"source_id": source.get("id"), "content": source.get("content")},
            organization_id=org,
        )
        entities = self.store.list_entities(org)
        name_map = {e["id"]: e["name"] for e in entities}

        result = self.extractor.extract(
            organization_id=org,
            content=source.get("content") or "",
            scope_id=source["scope_id"],
            source_id=source["id"],
            entity_ids=list(source.get("entity_ids") or []),
            participant_ids=[source["author_entity_id"]] if source.get("author_entity_id") else [],
            author_entity_id=source.get("author_entity_id"),
            is_private=bool(source.get("is_private_channel")),
            entity_name_map=name_map,
            occurred_at=source.get("occurred_at"),
            observed_at=source.get("ingested_at"),
        )

        committed: list[dict[str, Any]] = []
        for mem in result.memories:
            data = self._consolidate(mem)
            committed.append(data)
        if self.indexing and committed:
            self.indexing.index_memories(committed)

        self.audit.record(
            organization_id=org,
            event_type="memory.capture",
            principal_id=source.get("author_entity_id"),
            payload={
                "source_id": source["id"],
                "memory_ids": [m["id"] for m in committed],
                "extraction_method": getattr(self.extractor, "name", "unknown"),
            },
        )
        self.store.telemetry.record(
            "memory.capture", component="memory", phase="completed",
            output={"source_id": source["id"], "memory_ids": [m["id"] for m in committed]},
            organization_id=org,
        )
        return committed

    def _consolidate(self, memory: Memory) -> dict[str, Any]:
        """Commit a memory as an observation. Memories are never overwritten
        by other memories; facts carry the current truth instead."""
        return self.graph.commit_memory(memory)

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        return self.store.get_memory(memory_id)

    def list_current(
        self,
        organization_id: str,
        entity_ids: list[str] | None = None,
        memory_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.graph.current_memories(organization_id, entity_ids=entity_ids, memory_types=memory_types)

    def retract(self, memory_id: str) -> dict[str, Any] | None:
        mem = self.store.get_memory(memory_id)
        if not mem:
            return None
        mem["status"] = MemoryStatus.RETRACTED.value
        mem["is_latest"] = False
        self.store.upsert_memory(mem)
        self.audit.record(mem["organization_id"], "memory.retracted", {"memory_id": memory_id})
        return mem

    def confirm(self, memory_id: str) -> dict[str, Any] | None:
        mem = self.store.get_memory(memory_id)
        if not mem:
            return None
        mem["status"] = MemoryStatus.CONFIRMED.value
        self.store.upsert_memory(mem)
        self.audit.record(mem["organization_id"], "memory.confirmed", {"memory_id": memory_id})
        return mem

    def forget(self, memory_id: str) -> dict[str, Any] | None:
        """Hide a memory from all retrieval while retaining an auditable tombstone."""
        mem = self.store.get_memory(memory_id)
        if not mem:
            return None
        mem["status"] = MemoryStatus.DELETED.value
        mem["is_latest"] = False
        mem["content"] = "[forgotten]"
        mem["source_spans"] = []
        self.store.upsert_memory(mem)
        self.audit.record(mem["organization_id"], "memory.forgotten", {"memory_id": memory_id})
        return mem

    def correct(self, memory_id: str, *, content: str, source_id: str) -> dict[str, Any] | None:
        """Create an evidence-backed replacement; corrections never mutate history in place."""
        old = self.store.get_memory(memory_id)
        source = self.store.get_source(source_id)
        if not old or not source or source.get("organization_id") != old.get("organization_id"):
            return None
        corrected = Memory(
            organization_id=old["organization_id"], memory_type=old["memory_type"], content=content,
            canonical_claim=old.get("canonical_claim"), scope_id=old["scope_id"],
            confidence=old.get("confidence", 0.8), authority=old.get("authority", 0.8),
            source_ids=[source_id], entity_ids=list(old.get("entity_ids") or []),
            participant_ids=list(old.get("participant_ids") or []), is_private=bool(old.get("is_private")),
            source_spans=[{"source_id": source_id, "start": 0, "end": min(len(content), 500), "text": content[:500]}],
        )
        now = utcnow().isoformat()
        self.store.mark_superseded(memory_id, now)
        data = self.graph.commit_memory(corrected)
        self.graph.add_edge(data["organization_id"], MemoryEdgeType.CORRECTS, data["id"], memory_id, source_ids=[source_id])
        if self.indexing:
            self.indexing.index_memory(data)
        self.audit.record(data["organization_id"], "memory.corrected", {"memory_id": memory_id, "corrected_memory_id": data["id"], "source_id": source_id})
        return data
