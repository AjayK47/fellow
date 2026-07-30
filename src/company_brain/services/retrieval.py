"""Hybrid retrieval with permission filtering before content return."""

from __future__ import annotations

import re
import math
from typing import Any

from company_brain.domain.models import AuthorizationDecision, Destination, SearchResult
from company_brain.services.authorization import AuthorizationService
from company_brain.services.graph import GraphService
from company_brain.services.embeddings import EmbeddingProvider
from company_brain.storage.db import BrainStore


class RetrievalService:
    def __init__(
        self,
        store: BrainStore,
        authz: AuthorizationService,
        graph: GraphService,
        embeddings: EmbeddingProvider,
    ) -> None:
        self.store = store
        self.authz = authz
        self.graph = graph
        self.embeddings = embeddings

    def authorize(
        self,
        organization_id: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> AuthorizationDecision:
        return self.authz.resolve(
            organization_id=organization_id,
            acting_principal_id=acting_principal_id,
            destination=destination,
            purpose=purpose,
        )

    def search(
        self,
        *,
        organization_id: str,
        query: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
        entity_ids: list[str] | None = None,
        limit: int = 20,
        include_superseded: bool = False,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        decision = self.authorize(organization_id, acting_principal_id, destination, purpose)

        if include_superseded:
            candidates = self.store.list_memories(organization_id, latest_only=False)
        else:
            candidates = self.store.list_memories(organization_id, latest_only=True)
            candidates = [
                m
                for m in candidates
                if m.get("status") in {"active", "confirmed"}
            ]

        # Permission filter FIRST — before ranking exposes content
        allowed = self.authz.filter_memories(decision, candidates)

        if as_of:
            allowed = [
                memory for memory in allowed
                if (not memory.get("valid_from") or memory["valid_from"] <= as_of)
                and (not memory.get("valid_until") or memory["valid_until"] > as_of)
            ]

        # Drop memories whose ALL sources are revoked
        allowed = [m for m in allowed if self._has_live_source(m, decision)]

        if entity_ids:
            wanted = set(entity_ids)
            allowed = [m for m in allowed if wanted.intersection(m.get("entity_ids") or [])]

        query_embedding = self.embeddings.embed([query])[0] if query.strip() and allowed else []
        embeddings = self.store.memory_embeddings([m["id"] for m in allowed])
        fts_ranks = self.store.memory_fts_ranks(query, [m["id"] for m in allowed])

        scored: list[SearchResult] = []
        q_tokens = self._tokens(query)
        for m in allowed:
            memory_embedding = embeddings.get(m["id"])
            semantic_score = self._cosine(query_embedding, memory_embedding) if query_embedding and memory_embedding else 0.0
            score = self._score(m, q_tokens, query, query_embedding, memory_embedding, fts_ranks.get(m["id"], 0.0))
            if query.strip():
                c_tokens = self._tokens(m.get("content") or "")
                # Require lexical overlap for non-empty queries (avoid authority-only noise)
                if q_tokens and not (q_tokens & c_tokens) and query.lower() not in (m.get("content") or "").lower():
                    # still allow entity-name hits via entity list
                    entity_hit = False
                    for eid in m.get("entity_ids") or []:
                        ent = self.store.get_entity(eid)
                        if ent and any(t in (ent.get("name") or "").lower() for t in q_tokens):
                            entity_hit = True
                            break
                    if not entity_hit and semantic_score < 0.20:
                        continue
            if score <= 0 and query.strip():
                continue
            sources = self._source_payloads(m, decision)
            paths = []
            for eid in (m.get("entity_ids") or [])[:3]:
                for e in self.graph.neighbors(organization_id, eid)[:5]:
                    paths.append(e)
            scored.append(
                SearchResult(
                    memory=self._public_memory(m),
                    score=score if query.strip() else 0.5,
                    graph_paths=paths[:5],
                    sources=sources,
                )
            )

        scored.sort(key=lambda r: r.score, reverse=True)
        results = scored[:limit]
        return {
            "results": [r.model_dump(mode="json") for r in results],
            "permission_decision_id": decision.id,
            "count": len(results),
        }

    def search_evidence(
        self,
        *,
        organization_id: str,
        query: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search raw embedded source chunks with permission filtering first."""
        decision = self.authorize(
            organization_id, acting_principal_id, destination, purpose
        )
        chunks = self.store.list_chunks(
            organization_id, scope_ids=decision.allowed_scope_ids, limit=1000
        )
        query_vector = self.embeddings.embed([query])[0] if query.strip() else []
        fts = self.store.chunk_fts_ranks(
            query, organization_id, decision.allowed_scope_ids
        )
        query_tokens = self._tokens(query)
        results: list[dict[str, Any]] = []
        for chunk in chunks:
            source = self.store.get_source(chunk["source_id"])
            if not source or not self.authz.can_read_source(decision, source):
                continue
            vector = chunk.get("embedding") or []
            semantic = (
                self._cosine(query_vector, vector)
                if query_vector and vector
                else 0.0
            )
            content = chunk.get("content") or ""
            lexical = len(query_tokens & self._tokens(content)) / max(
                len(query_tokens), 1
            )
            score = lexical * 0.55 + max(semantic, 0.0) * 0.35
            score += min(max(fts.get(chunk["id"], 0.0), 0.0), 1.0) * 0.10
            if query.strip() and score <= 0.05:
                continue
            results.append(
                {
                    "chunk_id": chunk["id"],
                    "source_id": chunk["source_id"],
                    "content": content,
                    "score": score,
                    "scope_id": chunk["scope_id"],
                    "source_system": source.get("source_system"),
                    "source_type": source.get("source_type"),
                    "occurred_at": source.get("occurred_at"),
                    "channel_id": source.get("channel_id"),
                    "metadata": source.get("metadata") or {},
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "results": results[:limit],
            "permission_decision_id": decision.id,
            "count": min(len(results), limit),
        }

    def _has_live_source(self, memory: dict[str, Any], decision: AuthorizationDecision) -> bool:
        source_ids = memory.get("source_ids") or []
        if not source_ids:
            return True
        live = 0
        for sid in source_ids:
            src = self.store.get_source(sid)
            if not src:
                continue
            if src.get("revoked") or src.get("deleted_at"):
                continue
            if self.authz.can_read_source(decision, src):
                live += 1
        # if all sources revoked/unreadable, exclude
        return live > 0 or not source_ids

    def _source_payloads(
        self, memory: dict[str, Any], decision: AuthorizationDecision
    ) -> list[dict[str, Any]]:
        out = []
        for sid in memory.get("source_ids") or []:
            src = self.store.get_source(sid)
            if not src or not self.authz.can_read_source(decision, src):
                continue
            out.append(
                {
                    "id": src["id"],
                    "source_system": src.get("source_system"),
                    "source_type": src.get("source_type"),
                    "content_excerpt": (src.get("content") or "")[:300],
                    "occurred_at": src.get("occurred_at"),
                    "scope_id": src.get("scope_id"),
                }
            )
        return out

    @staticmethod
    def _public_memory(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": m["id"],
            "memory_type": m.get("memory_type"),
            "content": m.get("content"),
            "status": m.get("status"),
            "confidence": m.get("confidence"),
            "authority": m.get("authority"),
            "is_latest": m.get("is_latest"),
            "valid_from": m.get("valid_from"),
            "valid_until": m.get("valid_until"),
            "scope_id": m.get("scope_id"),
            "source_ids": m.get("source_ids"),
            "entity_ids": m.get("entity_ids"),
            "participant_ids": m.get("participant_ids"),
            "source_spans": m.get("source_spans"),
            "canonical_claim": m.get("canonical_claim"),
        }

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}

    def _score(
        self, memory: dict[str, Any], q_tokens: set[str], query: str,
        query_embedding: list[float] | None = None, memory_embedding: list[float] | None = None,
        fts_rank: float = 0.0,
    ) -> float:
        content = (memory.get("content") or "").lower()
        if not q_tokens:
            return float(memory.get("confidence") or 0.5)
        c_tokens = self._tokens(content)
        if not c_tokens:
            return 0.0
        overlap = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
        phrase_bonus = 0.25 if query.lower() in content else 0.0
        type_bonus = {
            "decision": 0.08,
            "fact": 0.06,
            "issue": 0.07,
            "commitment": 0.05,
        }.get(memory.get("memory_type") or "", 0.0)
        authority = float(memory.get("authority") or 0.5) * 0.15
        confidence = float(memory.get("confidence") or 0.5) * 0.1
        latest_bonus = 0.05 if memory.get("is_latest") else -0.1
        semantic = self._cosine(query_embedding, memory_embedding) if query_embedding and memory_embedding else 0.0
        fts_bonus = min(max(fts_rank, 0.0), 1.0) * 0.12
        return overlap * 0.55 + max(semantic, 0.0) * 0.33 + fts_bonus + phrase_bonus + type_bonus + authority + confidence + latest_bonus

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0
