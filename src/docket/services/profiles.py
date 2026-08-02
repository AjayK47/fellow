"""Permission-safe profile projections for people, projects, and services."""

from __future__ import annotations

from typing import Any

from docket.domain.models import new_id, utcnow
from docket.services.audit import AuditService
from docket.services.graph import GraphService
from docket.services.retrieval import RetrievalService
from docket.storage.db import BrainStore


class ProfilesService:
    def __init__(self, store: BrainStore, retrieval: RetrievalService, graph: GraphService, audit: AuditService) -> None:
        self.store = store
        self.retrieval = retrieval
        self.graph = graph
        self.audit = audit

    def refresh(self, organization_id: str, entity_id: str) -> dict[str, Any] | None:
        entity = self.store.get_entity(entity_id)
        if not entity or entity.get("organization_id") != organization_id:
            return None
        profile_type = entity.get("entity_type") or "entity"
        projection = {
            "id": new_id("profile"),
            "organization_id": organization_id,
            "profile_type": profile_type,
            "entity_id": entity_id,
            "entity": {"id": entity_id, "name": entity.get("name"), "type": profile_type, "attributes": entity.get("attributes") or {}},
            "relationship_count": len(self.graph.neighbors(organization_id, entity_id)),
            "memory_count": len(self.graph.current_memories(organization_id, entity_ids=[entity_id])),
            "updated_at": utcnow().isoformat(),
        }
        self.store.upsert_profile(projection)
        self.audit.record(organization_id, "profile.refreshed", {"entity_id": entity_id, "profile_type": profile_type})
        return projection

    def get(
        self, *, organization_id: str, entity_id: str, acting_principal_id: str,
        destination: dict[str, Any] | None = None, purpose: str | None = None,
    ) -> dict[str, Any] | None:
        entity = self.store.get_entity(entity_id)
        if not entity or entity.get("organization_id") != organization_id:
            return None
        profile_type = entity.get("entity_type") or "entity"
        cached = self.store.get_profile(organization_id, profile_type, entity_id) or self.refresh(organization_id, entity_id)
        decision = self.retrieval.authorize(organization_id, acting_principal_id, destination, purpose or "profile_read")
        memories = self.retrieval.authz.filter_memories(
            decision, self.graph.current_memories(organization_id, entity_ids=[entity_id])
        )
        # Only return graph facts that are attached to this entity; all memory content is filtered above.
        relations = [
            {"edge_type": edge["edge_type"], "from_id": edge["from_id"], "to_id": edge["to_id"], "valid_from": edge.get("valid_from"), "valid_until": edge.get("valid_until")}
            for edge in self.graph.neighbors(organization_id, entity_id)[:30]
        ]
        output = {
            "profile": cached,
            "current_facts": [self._memory_view(memory) for memory in memories if memory.get("memory_type") == "fact"],
            "decisions": [self._memory_view(memory) for memory in memories if memory.get("memory_type") == "decision"],
            "open_commitments": [self._memory_view(memory) for memory in memories if memory.get("memory_type") == "commitment"],
            "issues": [self._memory_view(memory) for memory in memories if memory.get("memory_type") == "issue"],
            "relationships": relations,
            "permission_decision_id": decision.id,
        }
        self.audit.record(organization_id, "profile.read", {"entity_id": entity_id, "permission_decision_id": decision.id}, principal_id=acting_principal_id)
        return output

    @staticmethod
    def _memory_view(memory: dict[str, Any]) -> dict[str, Any]:
        return {"id": memory["id"], "content": memory["content"], "source_ids": memory.get("source_ids") or [], "valid_from": memory.get("valid_from")}
