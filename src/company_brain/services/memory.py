"""Memory learning and consolidation."""

from __future__ import annotations

from typing import Any, Protocol

from company_brain.domain.models import Memory, MemoryEdgeType, MemoryStatus, MemoryType, utcnow
from company_brain.extractors.deterministic import DeterministicExtractor
from company_brain.services.audit import AuditService
from company_brain.services.graph import GraphService
from company_brain.services.indexing import IndexingService
from company_brain.storage.db import BrainStore


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
        entities = self.store.list_entities(org)
        name_map = {e["id"]: e["name"] for e in entities}

        result = self.extractor.extract(
            organization_id=org,
            content=source.get("content") or "",
            scope_id=source["scope_id"],
            source_id=source["id"],
            entity_ids=list(source.get("entity_ids") or []),
            participant_ids=[source["author_entity_id"]] if source.get("author_entity_id") else [],
            is_private=bool(source.get("is_private_channel")),
            entity_name_map=name_map,
        )

        committed: list[dict[str, Any]] = []
        for mem in result.memories:
            data = self._consolidate(mem)
            committed.append(data)
        if self.indexing and committed:
            self.indexing.index_memories(committed)

        self.audit.record(
            organization_id=org,
            event_type="memory.learned",
            principal_id=source.get("author_entity_id"),
            payload={
                "source_id": source["id"],
                "memory_ids": [m["id"] for m in committed],
                "extraction_method": getattr(self.extractor, "name", "unknown"),
            },
        )
        return committed

    def _consolidate(self, memory: Memory) -> dict[str, Any]:
        """If a current memory shares the same canonical claim subject+predicate, supersede it."""
        org = memory.organization_id
        existing = self.graph.current_memories(org)

        match = self._find_supersede_target(memory, existing)
        if match:
            # new memory updates old
            memory.valid_from = utcnow()
            return self.graph.supersede_memory(match["id"], memory, MemoryEdgeType.UPDATES)

        # check extends (same subject, different predicate detail)
        extend_target = self._find_extend_target(memory, existing)
        data = self.graph.commit_memory(memory)
        if extend_target:
            self.graph.add_edge(
                org,
                MemoryEdgeType.EXTENDS,
                data["id"],
                extend_target["id"],
                source_ids=data.get("source_ids"),
            )
        return data

    # Predicates that represent a single current value (supersede on object change).
    # Generic "is" is NOT identity — "X is using PG" and "X is owned by Y" coexist.
    _IDENTITY_PREDICATES = frozenset(
        {
            "launch_date",
            "uses_primary_store",
            "owner_of",
            "primary_owner",
            "status",
            "on_call",
            "version",
        }
    )

    def _find_supersede_target(
        self, memory: Memory, existing: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        claim = memory.canonical_claim
        if claim and self._is_identity_predicate(claim.predicate):
            for m in existing:
                cc = m.get("canonical_claim") or {}
                if (
                    cc.get("subject") == claim.subject
                    and cc.get("predicate") == claim.predicate
                    and cc.get("object") != claim.object
                    and m.get("scope_id") == memory.scope_id
                ):
                    return m

        # Content-level identity only for known single-value claim phrases
        identity_key = self._identity_content_key(memory.content)
        if not identity_key:
            return None
        for m in existing:
            if m.get("memory_type") != (
                memory.memory_type.value if hasattr(memory.memory_type, "value") else memory.memory_type
            ):
                continue
            if m.get("scope_id") != memory.scope_id:
                continue
            if not set(m.get("entity_ids") or []).intersection(set(memory.entity_ids or [])):
                continue
            other_key = self._identity_content_key(m.get("content") or "")
            if other_key == identity_key and m.get("content") != memory.content:
                return m
        return None

    def _find_extend_target(
        self, memory: Memory, existing: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        claim = memory.canonical_claim
        if not claim:
            return None
        # Only EXTENDS for non-identity predicates that share a subject
        if self._is_identity_predicate(claim.predicate):
            return None
        for m in existing:
            cc = m.get("canonical_claim") or {}
            if cc.get("subject") == claim.subject and cc.get("predicate") != claim.predicate:
                if set(m.get("entity_ids") or []).intersection(set(memory.entity_ids or [])):
                    return m
        return None

    @classmethod
    def _is_identity_predicate(cls, predicate: str | None) -> bool:
        if not predicate:
            return False
        p = predicate.strip().lower()
        if p in cls._IDENTITY_PREDICATES:
            return True
        # Never treat bare copula "is"/"are" as identity
        if p in {"is", "are", "was", "were", "has", "have"}:
            return False
        return False

    @staticmethod
    def _identity_content_key(content: str) -> str | None:
        """Return a stable key only for single-valued atomic claims; else None."""
        lower = content.lower()
        markers = (
            ("launch date", "launch_date"),
            ("release date", "launch_date"),
            ("ship date", "launch_date"),
            ("primary store", "uses_primary_store"),
            ("selected postgresql", "uses_primary_store"),
            ("selected postgres", "uses_primary_store"),
        )
        for phrase, key in markers:
            if phrase in lower:
                return key
        return None

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
