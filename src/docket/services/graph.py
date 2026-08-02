"""Organization and memory graph mutations."""

from __future__ import annotations

from typing import Any

from docket.domain.models import (
    Entity,
    EntityType,
    GraphEdge,
    Memory,
    MemoryEdgeType,
    MemoryStatus,
    OrgEdgeType,
    new_id,
    utcnow,
)
from docket.storage.db import BrainStore


class GraphService:
    def __init__(self, store: BrainStore) -> None:
        self.store = store

    # ---- entities ----

    def upsert_entity(self, entity: Entity | dict[str, Any]) -> dict[str, Any]:
        data = entity.model_dump(mode="json") if isinstance(entity, Entity) else dict(entity)
        if "entity_type" in data and hasattr(data["entity_type"], "value"):
            data["entity_type"] = data["entity_type"].value
        data["updated_at"] = utcnow().isoformat()
        self.store.upsert_entity(data)
        return data

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        return self.store.get_entity(entity_id)

    def list_entities(self, organization_id: str, entity_type: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_entities(organization_id, entity_type=entity_type)

    # ---- edges ----

    def add_edge(
        self,
        organization_id: str,
        edge_type: str | OrgEdgeType | MemoryEdgeType,
        from_id: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        et = edge_type.value if hasattr(edge_type, "value") else str(edge_type)
        edge = GraphEdge(
            id=new_id("edge"),
            organization_id=organization_id,
            edge_type=et,
            from_id=from_id,
            to_id=to_id,
            properties=properties or {},
            source_ids=source_ids or [],
            valid_from=utcnow(),
        )
        data = edge.model_dump(mode="json")
        self.store.upsert_edge(data)
        return data

    def list_edges(
        self,
        organization_id: str,
        edge_type: str | None = None,
        from_id: str | None = None,
        to_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_edges(
            organization_id, edge_type=edge_type, from_id=from_id, to_id=to_id
        )

    def neighbors(
        self, organization_id: str, node_id: str, edge_types: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self.store.neighbors(organization_id, node_id, edge_types=edge_types)

    def paths_between(
        self,
        organization_id: str,
        from_id: str,
        to_id: str,
        max_depth: int = 3,
        edge_types: list[str] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """BFS multi-hop paths for relationship queries."""
        edges = self.store.list_edges(organization_id)
        adj: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            if edge_types and e["edge_type"] not in edge_types:
                continue
            adj.setdefault(e["from_id"], []).append(e)
            # undirected for relationship exploration
            adj.setdefault(e["to_id"], []).append(
                {**e, "from_id": e["to_id"], "to_id": e["from_id"], "_reversed": True}
            )

        paths: list[list[dict[str, Any]]] = []
        queue: list[tuple[str, list[dict[str, Any]], set[str]]] = [(from_id, [], {from_id})]
        while queue:
            node, path, visited = queue.pop(0)
            if len(path) > max_depth:
                continue
            if node == to_id and path:
                paths.append(path)
                continue
            for e in adj.get(node, []):
                nxt = e["to_id"]
                if nxt in visited:
                    continue
                queue.append((nxt, path + [e], visited | {nxt}))
        return paths[:20]

    # ---- memory graph ----

    def commit_memory(self, memory: Memory | dict[str, Any]) -> dict[str, Any]:
        data = memory.model_dump(mode="json") if isinstance(memory, Memory) else dict(memory)
        if "memory_type" in data and hasattr(data["memory_type"], "value"):
            data["memory_type"] = data["memory_type"].value
        if "status" in data and hasattr(data["status"], "value"):
            data["status"] = data["status"].value
        self.store.upsert_memory(data)

        # link ABOUT / INVOLVES / SCOPED_TO
        org = data["organization_id"]
        for eid in data.get("entity_ids") or []:
            self.add_edge(org, MemoryEdgeType.ABOUT, data["id"], eid, source_ids=data.get("source_ids"))
        for pid in data.get("participant_ids") or []:
            self.add_edge(org, MemoryEdgeType.INVOLVES, data["id"], pid, source_ids=data.get("source_ids"))
        if data.get("scope_id"):
            self.add_edge(org, MemoryEdgeType.SCOPED_TO, data["id"], data["scope_id"])
        for sid in data.get("source_ids") or []:
            self.add_edge(org, MemoryEdgeType.SUPPORTED_BY, data["id"], sid)

        return data

    def supersede_memory(
        self,
        old_memory_id: str,
        new_memory: Memory | dict[str, Any],
        edge_type: MemoryEdgeType = MemoryEdgeType.UPDATES,
    ) -> dict[str, Any]:
        """Create new memory that UPDATES old; keep history."""
        now = utcnow().isoformat()
        self.store.mark_superseded(old_memory_id, now)
        data = self.commit_memory(new_memory)
        org = data["organization_id"]
        self.add_edge(org, edge_type, data["id"], old_memory_id, source_ids=data.get("source_ids"))
        return data

    def current_memories(
        self,
        organization_id: str,
        entity_ids: list[str] | None = None,
        memory_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        mems = self.store.list_memories(organization_id, latest_only=True)
        mems = [m for m in mems if m.get("status") in {"active", "confirmed", MemoryStatus.ACTIVE.value, MemoryStatus.CONFIRMED.value}]
        if entity_ids:
            wanted = set(entity_ids)
            mems = [m for m in mems if wanted.intersection(m.get("entity_ids") or [])]
        if memory_types:
            types = set(memory_types)
            mems = [m for m in mems if m.get("memory_type") in types]
        return mems

    def memory_history(self, organization_id: str, memory_id: str) -> list[dict[str, Any]]:
        """Walk UPDATES edges backward/forward to reconstruct lineage."""
        chain = []
        seen = set()
        current = self.store.get_memory(memory_id)
        if not current:
            return []
        # Walk temporal replacement edges in both directions.
        stack = [current]
        while stack:
            m = stack.pop()
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            chain.append(m)
            for edge_type in (MemoryEdgeType.UPDATES.value, MemoryEdgeType.CORRECTS.value):
                for e in self.store.list_edges(organization_id, edge_type=edge_type, from_id=m["id"]):
                    older = self.store.get_memory(e["to_id"])
                    if older:
                        stack.append(older)
                for e in self.store.list_edges(organization_id, edge_type=edge_type, to_id=m["id"]):
                    newer = self.store.get_memory(e["from_id"])
                    if newer:
                        stack.append(newer)
        chain.sort(key=lambda x: x.get("valid_from") or x.get("created_at") or "")
        return chain

    def resolve_owners(
        self, organization_id: str, entity_ids: list[str]
    ) -> list[dict[str, Any]]:
        results = []
        for eid in entity_ids:
            owners = self.store.list_edges(organization_id, edge_type=OrgEdgeType.OWNS.value, to_id=eid)
            for e in owners:
                owner = self.store.get_entity(e["from_id"])
                results.append(
                    {
                        "entity_id": eid,
                        "owner_id": e["from_id"],
                        "owner": owner,
                        "edge": e,
                    }
                )
            # ACCOUNT_OWNED_BY (customer -> person)
            accounts = self.store.list_edges(
                organization_id, edge_type=OrgEdgeType.ACCOUNT_OWNED_BY.value, from_id=eid
            )
            for e in accounts:
                owner = self.store.get_entity(e["to_id"])
                results.append(
                    {
                        "entity_id": eid,
                        "owner_id": e["to_id"],
                        "owner": owner,
                        "edge": e,
                    }
                )
        return results

    def on_call_for(self, organization_id: str, service_or_team_id: str) -> list[dict[str, Any]]:
        edges = self.store.list_edges(
            organization_id, edge_type=OrgEdgeType.ON_CALL_FOR.value, to_id=service_or_team_id
        )
        out = []
        for e in edges:
            person = self.store.get_entity(e["from_id"])
            out.append({"person": person, "edge": e})
        return out

    def explorer_view(self, organization_id: str, allowed_source_ids: set[str] | None = None) -> dict[str, Any]:
        """Return stable organization graph edges, respecting evidence visibility."""
        nodes = self.store.list_entities(organization_id)
        edges = []
        memory_edge_types = {edge.value for edge in MemoryEdgeType}
        for edge in self.store.list_edges(organization_id):
            if edge.get("edge_type") in memory_edge_types:
                continue
            source_ids = set(edge.get("source_ids") or [])
            if source_ids and (allowed_source_ids is None or not source_ids.issubset(allowed_source_ids)):
                continue
            edges.append(edge)
        node_ids = {node["id"] for edge in edges for node in ()}
        node_ids.update(edge["from_id"] for edge in edges)
        node_ids.update(edge["to_id"] for edge in edges)
        return {"nodes": [node for node in nodes if node["id"] in node_ids], "edges": edges}
