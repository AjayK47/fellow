"""Organization and memory graph mutations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from toda.domain.models import (
    Entity,
    GraphEdge,
    Memory,
    MemoryEdgeType,
    OrgEdgeType,
    new_id,
    utcnow,
)
from toda.storage.db import BrainStore


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

    def list_entities(
        self,
        organization_id: str,
        entity_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_entities(organization_id, entity_types=entity_types)

    # ---- edges ----

    def add_edge(
        self,
        organization_id: str,
        edge_type: str | OrgEdgeType | MemoryEdgeType,
        from_id: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
        source_ids: list[str] | None = None,
        replaces: str | None = None,
    ) -> dict[str, Any]:
        """Create an edge, optionally closing the edge it replaces.

        Pass `replaces=<edge_id>` when the caller knows this edge supersedes
        a specific prior one (ownership transferred, on-call rotated, a
        membership ended and a new one began) — the old edge is closed via
        `supersede_edge` rather than left as a stale, still-"current" row
        alongside its replacement.
        """
        if replaces:
            self.supersede_edge(replaces, reason=f"replaced by a new {edge_type} edge")
        et = edge_type.value if hasattr(edge_type, "value") else str(edge_type)
        existing = self.store.get_active_edge(
            organization_id, et, from_id, to_id
        )
        if existing:
            return existing
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

    def get_edge(self, edge_id: str) -> dict[str, Any] | None:
        return self.store.get_json("edges", edge_id)

    def supersede_edge(
        self,
        edge_id: str,
        *,
        valid_until: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Close an edge's validity window in place instead of deleting it.

        The row is never removed — `edges_as_of()` can still reconstruct it
        for any point in time before the close — it just stops being
        "current" for `list_edges(active_only=True)` and everything built on
        that default (`resolve_owners`, `on_call_for`, the graph explorer).
        """
        edge = self.get_edge(edge_id)
        if not edge:
            return None
        now = utcnow().isoformat()
        edge["valid_until"] = valid_until or now
        edge["system_to"] = now
        if reason:
            edge["superseded_reason"] = reason
        self.store.upsert_edge(edge)
        return edge

    def edges_as_of(
        self,
        organization_id: str,
        *,
        valid_at: str | None = None,
        edge_type: str | None = None,
        from_id: str | None = None,
        to_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Edges whose validity window covers `valid_at` (default: now).

        Answers "who owned this then?", not just "who owns this now?" —
        the same question `RealityEngine.current_facts(valid_at=...)`
        answers for facts, applied to the graph's structural relationships.
        """
        as_of = valid_at or utcnow().isoformat()
        edges = self.store.list_edges(
            organization_id,
            edge_type=edge_type,
            from_id=from_id,
            to_id=to_id,
            active_only=False,
        )
        return [
            e
            for e in edges
            if (e.get("valid_from") or "") <= as_of
            and (not e.get("valid_until") or e["valid_until"] > as_of)
        ]

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
        """Bounded-depth, undirected multi-hop paths for relationship
        queries — same semantics as the original BFS (cycle-free by node,
        undirected traversal, capped at 20 paths), but scoped to the actual
        query via a recursive SQL CTE instead of loading every edge in the
        organization into Python on every call. Only the edges that end up
        on a found path are ever hydrated into full dicts.
        """
        edge_type_clause = ""
        edge_type_params: list[Any] = []
        if edge_types:
            edge_type_clause = f"AND e.edge_type IN ({','.join('?' for _ in edge_types)})"
            edge_type_params = list(edge_types)

        sql = f"""
            WITH RECURSIVE search(node, path_ids, path_dirs, visited, depth) AS (
                SELECT ?, '', '', ',' || ? || ',', 0
                UNION ALL
                SELECT
                    CASE WHEN e.from_id = search.node THEN e.to_id ELSE e.from_id END,
                    search.path_ids || ',' || e.id,
                    search.path_dirs || (CASE WHEN e.from_id = search.node THEN 'F' ELSE 'R' END),
                    search.visited || (CASE WHEN e.from_id = search.node THEN e.to_id ELSE e.from_id END) || ',',
                    search.depth + 1
                FROM edges e
                JOIN search ON (e.from_id = search.node OR e.to_id = search.node)
                WHERE e.organization_id = ?
                  AND e.valid_until IS NULL
                  {edge_type_clause}
                  AND search.depth < ?
                  AND instr(
                        search.visited,
                        ',' || (CASE WHEN e.from_id = search.node THEN e.to_id ELSE e.from_id END) || ','
                      ) = 0
            )
            SELECT path_ids, path_dirs FROM search WHERE node = ? AND depth > 0
            ORDER BY depth LIMIT 20
        """
        params = [
            from_id, from_id,
            organization_id, *edge_type_params, max_depth,
            to_id,
        ]
        with self.store.cursor() as cur:
            cur.execute(sql, params)
            rows = [(r["path_ids"], r["path_dirs"]) for r in cur.fetchall()]

        if not rows:
            return []

        all_edge_ids = {
            edge_id
            for path_ids, _ in rows
            for edge_id in path_ids.strip(",").split(",")
        }
        edges_by_id = {
            edge_id: edge
            for edge_id in all_edge_ids
            if (edge := self.store.get_json("edges", edge_id)) is not None
        }

        paths: list[list[dict[str, Any]]] = []
        for path_ids, path_dirs in rows:
            edge_ids = path_ids.strip(",").split(",")
            path: list[dict[str, Any]] = []
            for edge_id, direction in zip(edge_ids, path_dirs):
                edge = edges_by_id.get(edge_id)
                if edge is None:
                    continue
                if direction == "R":
                    path.append(
                        {**edge, "from_id": edge["to_id"], "to_id": edge["from_id"], "_reversed": True}
                    )
                else:
                    path.append(edge)
            paths.append(path)
        return paths

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
        scope_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_current_memories(
            organization_id,
            entity_ids=entity_ids,
            memory_types=memory_types,
            scope_ids=scope_ids,
        )

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

    def explorer_view(
        self,
        organization_id: str,
        allowed_source_ids: set[str] | None = None,
        *,
        valid_at: str | None = None,
    ) -> dict[str, Any]:
        """Return the organization graph, respecting evidence visibility.

        Pass `valid_at` to view the graph as it stood at a point in time
        instead of its current state -- a closed OWNS edge still appears if
        `valid_at` falls before it was superseded.

        Three things this used to get wrong, each of which made the picture
        claim less than the store actually holds:

        * Memory edges were skipped wholesale, and MemoryEdgeType covers
          SUPPORTED_BY, INVOLVES and ABOUT -- the edges that connect what
          Toda learned to who and what it is about. Hiding them left a graph
          of Slack plumbing with the knowledge removed.
        * Visibility used ``issubset``, requiring *every* cited source to be
          visible, while AuthorizationService.can_read_edge uses ``any``.
          Two answers to one question, and the stricter one silently won
          here.
        * An edge to something that is not an entity -- an artifact, a
          skill, a scope -- was returned with no node for its endpoint, so
          the renderer dropped it. That is why a Task that had PRODUCED a
          report drew as an isolated dot.
        """
        entities = {node["id"]: node for node in self.store.list_entities(organization_id)}
        candidate_edges = (
            self.edges_as_of(organization_id, valid_at=valid_at)
            if valid_at
            else self.store.list_edges(organization_id)
        )
        edges = [
            edge
            for edge in candidate_edges
            if self._edge_is_visible(edge, allowed_source_ids)
        ]
        node_ids: set[str] = set()
        for edge in edges:
            node_ids.add(edge["from_id"])
            node_ids.add(edge["to_id"])
        nodes = [
            entities[node_id] if node_id in entities else _placeholder_node(node_id, organization_id)
            for node_id in sorted(node_ids)
        ]
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _edge_is_visible(
        edge: dict[str, Any], allowed_source_ids: set[str] | None
    ) -> bool:
        """Match AuthorizationService.can_read_edge: any cited source is enough.

        An edge with no cited source carries no evidence to be gated on --
        PRODUCED, USED_SKILL and SCOPED_TO are structural facts about Toda's
        own state -- so it is visible.
        """
        source_ids = set(edge.get("source_ids") or [])
        if not source_ids or allowed_source_ids is None:
            return True
        return bool(source_ids & allowed_source_ids)


# Prefixes used to name a graph endpoint that is not an entity row.
_NODE_KIND_BY_PREFIX = {
    "artifact": ("artifact", "Artifact"),
    "skill": ("skill", "Skill"),
    "scope": ("scope", "Scope"),
    "mem": ("memory", "Memory"),
    "task": ("task", "Task"),
    "src": ("source", "Source"),
    "person": ("person", "Person"),
    "conversation": ("conversation", "Conversation"),
}


def _placeholder_node(node_id: str, organization_id: str) -> dict[str, Any]:
    """A drawable node for an edge endpoint that has no entity row.

    Artifacts, skills and scopes are real things Toda links to but does not
    store as entities. Returning the edge without its endpoint left the
    renderer with a dangling reference, which it drops -- so the Task that
    produced a report appeared unconnected to it.
    """
    prefix = node_id.split("_", 1)[0]
    entity_type, label = _NODE_KIND_BY_PREFIX.get(prefix, ("unknown", "Node"))
    # Show the identifier whole when it is short enough to read. Cutting it
    # to a fixed eight characters produced labels like "Person slack_t0"
    # and "Skill builtin_", which are neither a name nor a usable id.
    suffix = node_id.split("_", 1)[-1]
    shown = suffix if len(suffix) <= 24 else f"{suffix[:10]}…{suffix[-6:]}"
    return {
        "id": node_id,
        "organization_id": organization_id,
        "entity_type": entity_type,
        "name": f"{label} {shown}",
        "placeholder": True,
    }
