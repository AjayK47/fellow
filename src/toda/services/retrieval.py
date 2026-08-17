"""Hybrid retrieval with permission filtering before content return."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from toda.domain.models import AuthorizationDecision, Destination, SearchResult, utcnow
from toda.services.authorization import AuthorizationService
from toda.services.graph import GraphService
from toda.services.embeddings import EmbeddingProvider
from toda.storage.db import BrainStore


# A year, in days. Claims older than this get no recency credit at all
# rather than going negative -- age is a tiebreaker, not a disqualifier.
_FRESHNESS_HORIZON_DAYS = 365.0


def _freshness(memory: dict[str, Any]) -> float:
    """1.0 for a claim true right now, decaying to 0 over a year.

    Ranking had no notion of time at all: a claim from 2024 and one from
    2026 about the same subject scored identically, so a stale answer won
    on a coin flip. This is deliberately a small term -- relevance still
    dominates, because the newest memory about the wrong subject is worse
    than an older one about the right subject.

    A claim whose validity has already ended scores 0. It is still
    retrievable, and should be: "Ajay was on leave 1-10 August" is exactly
    what answers "was he around on the 3rd". It just should not outrank
    what is true now.
    """
    now = utcnow()
    valid_until = _as_datetime(memory.get("valid_until"))
    if valid_until is not None and valid_until <= now:
        return 0.0
    anchor = _as_datetime(memory.get("valid_from")) or _as_datetime(memory.get("observed_at"))
    if anchor is None:
        return 0.5
    age_days = (now - anchor).total_seconds() / 86_400.0
    if age_days <= 0:
        return 1.0
    return max(0.0, 1.0 - age_days / _FRESHNESS_HORIZON_DAYS)


def _as_datetime(value: Any) -> "datetime | None":
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class _LookupCache:
    """Bounded, expiring memo for display-name lookups.

    Three properties, each fixing a way the plain dict this replaces was
    wrong on a service that lives for the whole process:

    * a MISS is never stored. An entity looked up before its Slack profile
      resolved was memoized as nameless and stayed that way for the life of
      the gateway -- reintroducing precisely the "renders as a raw
      U08ABC123 forever" failure `_entity_name`'s docstring says it exists
      to avoid.
    * entries EXPIRE, so a rename propagates within a few minutes instead
      of never.
    * entries are BOUNDED, so a long-running gateway that has seen a lot of
      entities cannot grow this without limit.

    Within one retrieval -- the case the memo actually exists for, where
    the same author is resolved for result after result -- all three are
    invisible.
    """

    __slots__ = ("_entries", "_ttl", "_max_entries")

    def __init__(self, *, ttl_seconds: float = 300.0, max_entries: int = 2_048) -> None:
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._ttl = ttl_seconds
        self._max_entries = max_entries

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if monotonic() - stored_at > self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        if not value:
            # Never memoize "we don't know yet" -- see the class docstring.
            return
        self._entries[key] = (monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class RetrievalService:
    def __init__(
        self,
        store: BrainStore,
        authz: AuthorizationService,
        graph: GraphService,
        embeddings: EmbeddingProvider,
        reality: Any | None = None,
    ) -> None:
        self.store = store
        self.authz = authz
        self.graph = graph
        self.embeddings = embeddings
        self.reality = reality
        self._entity_name_cache: _LookupCache = _LookupCache()
        self._scope_label_cache: _LookupCache = _LookupCache()

    def _scope_label(self, scope_id: str | None) -> str | None:
        """Human-readable name for the room a claim came from.

        A raw `C0BP95K48V6` is not something anyone can act on, and it is all
        the model had to work with. The scope already carries a label, so this
        is a lookup rather than new state -- and it covers non-Slack scopes
        (projects, DMs) for free.
        """
        if not scope_id:
            return None
        cached = self._scope_label_cache.get(scope_id)
        if cached is not None:
            return cached
        label = (self.store.get_scope(scope_id) or {}).get("label") or ""
        self._scope_label_cache.put(scope_id, label)
        return label or None

    def _entity_name(self, entity_id: str | None) -> str | None:
        """Resolve a person/entity id to a display name at read time.

        Deliberately lazy rather than denormalized onto every row: names
        change, and a source written before a display name was known would
        otherwise keep rendering as a raw `U08ABC123` forever. Falls back to
        None (not the id) so a caller can tell "no name known" apart from a
        name that happens to look like an id.

        The memo is deliberately miss-free and expiring (see _LookupCache):
        caching a miss on this service, which lives as long as the process,
        is what reintroduced the exact staleness described above.
        """
        if not entity_id:
            return None
        cached = self._entity_name_cache.get(entity_id)
        if cached is not None:
            return cached
        name = (self.store.get_entity(entity_id) or {}).get("name") or ""
        self._entity_name_cache.put(entity_id, name)
        return name or None

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
        self.store.trace.record(
            "retrieval.search", component="retrieval", phase="started",
            input={"query": query, "limit": limit, "entity_ids": entity_ids},
            organization_id=organization_id,
        )
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
        # Semantic candidates come from the vec0 ANN index (ordered by cosine
        # distance) rather than a per-row Python cosine loop. A memory that
        # isn't among the nearest `k` simply scores 0 on the semantic term —
        # correct, since it isn't a strong semantic match either way.
        semantic_hits = self._semantic_hits(
            self.store.vec_search_memories, organization_id, query_embedding, k=max(200, limit * 10)
        )
        fts_ranks = self.store.memory_fts_ranks(query, [m["id"] for m in allowed])

        scored: list[SearchResult] = []
        q_tokens = self._tokens(query)
        for m in allowed:
            semantic_score = semantic_hits.get(m["id"], 0.0)
            score = self._score(m, q_tokens, query, semantic_score, fts_ranks.get(m["id"], 0.0))
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
                neighbor_edges = self.authz.filter_edges(decision, self.graph.neighbors(organization_id, eid))
                for e in neighbor_edges[:5]:
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
        reality_facts: list[dict[str, Any]] = []
        if self.reality:
            fact_candidates = self.reality.current_facts(
                organization_id,
                valid_at=as_of,
                known_at=None,
                scope_ids=decision.allowed_scope_ids,
            )
            query_tokens = self._tokens(query)
            for fact in fact_candidates:
                text = " ".join(
                    [
                        fact.get("subject_id") or "",
                        fact.get("predicate") or "",
                        fact.get("object_value") or "",
                    ]
                )
                overlap = len(query_tokens & self._tokens(text))
                if query_tokens and not overlap and not (
                    entity_ids
                    and fact.get("subject_id") in set(entity_ids)
                ):
                    continue
                reality_facts.append(
                    {
                        **fact,
                        "score": (
                            overlap / max(len(query_tokens), 1)
                            + float(fact.get("authority") or 0) * 0.15
                            + float(fact.get("confidence") or 0) * 0.1
                        ),
                    }
                )
            reality_facts.sort(
                key=lambda item: item["score"], reverse=True
            )
        result = {
            "results": [r.model_dump(mode="json") for r in results],
            "reality_facts": reality_facts[:limit],
            "permission_decision_id": decision.id,
            "allowed_scope_ids": decision.allowed_scope_ids,
            "count": len(results),
        }
        self.store.trace.record(
            "retrieval.search", component="retrieval", phase="completed",
            output={"count": result["count"], "permission_decision_id": decision.id},
            organization_id=organization_id,
        )
        return result

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
        """Search raw embedded source chunks with permission filtering first.

        No fixed row cutoff: candidates come from real indexes (FTS5 for
        lexical matches, vec0 ANN for semantic ones), not a Python scan
        capped at "the newest N chunks" — evidence stays searchable no
        matter how old it is or how large the corpus has grown.
        """
        decision = self.authorize(
            organization_id, acting_principal_id, destination, purpose
        )
        query_tokens = self._tokens(query)
        results: list[dict[str, Any]] = []

        if not query.strip():
            # Browse mode: no query to rank against, just the latest allowed
            # evidence. Bounded on purpose — this isn't a search.
            chunks = self.store.list_chunks(
                organization_id, scope_ids=decision.allowed_scope_ids, limit=max(limit, 200)
            )
            for chunk in chunks:
                source = self.store.get_source(chunk["source_id"])
                if not source or not self.authz.can_read_source(decision, source):
                    continue
                results.append(self._evidence_result(chunk, source, score=0.0))
            return {
                "results": results[:limit],
                "permission_decision_id": decision.id,
                "count": min(len(results), limit),
            }

        query_vector = self.embeddings.embed([query])[0]
        fts_ranks = self.store.chunk_fts_ranks(query, organization_id, decision.allowed_scope_ids)
        semantic_hits = self._semantic_hits(
            self.store.vec_search_chunks, organization_id, query_vector, k=max(200, limit * 20)
        )
        candidate_ids = set(fts_ranks) | set(semantic_hits)

        for chunk_id in candidate_ids:
            chunk = self.store.get_chunk(chunk_id)
            if (
                not chunk
                or chunk.get("organization_id") != organization_id
                or chunk.get("scope_id") not in decision.allowed_scope_ids
            ):
                continue
            source = self.store.get_source(chunk["source_id"])
            if not source or not self.authz.can_read_source(decision, source):
                continue
            content = chunk.get("content") or ""
            lexical = len(query_tokens & self._tokens(content)) / max(len(query_tokens), 1)
            semantic = semantic_hits.get(chunk_id, 0.0)
            score = lexical * 0.55 + max(semantic, 0.0) * 0.35
            score += min(max(fts_ranks.get(chunk_id, 0.0), 0.0), 1.0) * 0.10
            if score <= 0.05:
                continue
            results.append(self._evidence_result(chunk, source, score=score))

        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "results": results[:limit],
            "permission_decision_id": decision.id,
            "count": min(len(results), limit),
        }

    def _evidence_result(
        self, chunk: dict[str, Any], source: dict[str, Any], *, score: float
    ) -> dict[str, Any]:
        # author_* is what makes "who said that?" answerable. The evidence row
        # has carried author_entity_id since ingestion, but this projection
        # dropped it, so the agent could name the channel a claim came from
        # and never the person -- it reported exactly the fields it was given.
        author_entity_id = source.get("author_entity_id")
        return {
            "chunk_id": chunk["id"],
            "source_id": chunk["source_id"],
            "content": chunk.get("content") or "",
            "score": score,
            "scope_id": chunk["scope_id"],
            "source_system": source.get("source_system"),
            "source_type": source.get("source_type"),
            "occurred_at": source.get("occurred_at"),
            "author_entity_id": author_entity_id,
            "author_name": self._entity_name(author_entity_id),
            "channel_id": source.get("channel_id"),
            "channel_name": self._scope_label(chunk.get("scope_id")),
            "metadata": source.get("metadata") or {},
        }

    def _semantic_hits(
        self, vec_search: Any, organization_id: str, query_embedding: list[float], *, k: int
    ) -> dict[str, float]:
        """Run a vec0 KNN lookup and turn cosine distance into similarity
        (1 - distance). `vec_search` is one of store.vec_search_{chunks,
        memories,episodes} — a thin seam so this stays one implementation
        for all three.

        The org is passed into the KNN itself, not applied afterward: `k`
        has to mean "k within this organization", or a large tenant's rows
        fill the shortlist and a small tenant's equally-relevant rows never
        appear at all.
        """
        if not query_embedding:
            return {}
        return {
            row_id: max(0.0, 1.0 - distance)
            for row_id, distance in vec_search(organization_id, query_embedding, k)
        }

    def search_episodes(
        self,
        *,
        organization_id: str,
        query: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Episode summaries are their own result kind (memory ingestion/
        retrieval plan §8) — a coherent narrative over a bounded window of
        evidence, not a single fact. Scope/privacy authorization uses the
        episode's own first-class scope_id/privacy_class columns, same
        pattern as evidence and memory search."""
        decision = self.authorize(organization_id, acting_principal_id, destination, purpose)
        query_tokens = self._tokens(query)

        if not query.strip():
            episodes = self.store.list_episodes(organization_id, scope_ids=decision.allowed_scope_ids, limit=limit)
            results = [self._episode_result(e, score=0.0) for e in episodes]
            return {"results": results[:limit], "permission_decision_id": decision.id, "count": len(results)}

        query_vector = self.embeddings.embed([query])[0]
        fts_ranks = self.store.episode_fts_ranks(query, organization_id, decision.allowed_scope_ids)
        semantic_hits = self._semantic_hits(
            self.store.vec_search_episodes, organization_id, query_vector, k=max(50, limit * 10)
        )
        candidate_ids = set(fts_ranks) | set(semantic_hits)

        results = []
        for episode_id in candidate_ids:
            episode = self.store.get_episode(episode_id)
            if (
                not episode
                or episode.get("organization_id") != organization_id
                or episode.get("scope_id") not in decision.allowed_scope_ids
            ):
                continue
            summary = episode.get("summary") or ""
            lexical = len(query_tokens & self._tokens(summary)) / max(len(query_tokens), 1)
            semantic = semantic_hits.get(episode_id, 0.0)
            score = lexical * 0.5 + max(semantic, 0.0) * 0.4
            score += min(max(fts_ranks.get(episode_id, 0.0), 0.0), 1.0) * 0.10
            if score <= 0.05:
                continue
            results.append(self._episode_result(episode, score=score))

        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "results": results[:limit],
            "permission_decision_id": decision.id,
            "count": min(len(results), limit),
        }

    @staticmethod
    def _episode_result(episode: dict[str, Any], *, score: float) -> dict[str, Any]:
        return {
            "kind": "episode",
            "episode_id": episode["id"],
            "group_key": episode.get("group_key"),
            "group_kind": episode.get("group_kind"),
            "source_system": episode.get("source_system"),
            "summary": episode.get("summary") or "",
            "started_at": episode.get("started_at"),
            "ended_at": episode.get("ended_at"),
            "event_count": episode.get("event_count"),
            "status": episode.get("status"),
            "scope_id": episode.get("scope_id"),
            "score": score,
        }

    def unified_search(
        self,
        *,
        organization_id: str,
        query: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
        kind: str | None = None,
        entity_ids: list[str] | None = None,
        source_system: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """One orchestration entry point over memory/episode/evidence search
        (memory ingestion/retrieval plan §8) so `brain_search` and
        ContextService.prepare_context call the same permission-safe
        primitives instead of each hand-rolling a subset. `kind` narrows to
        one result type; omitted, all three run and come back labeled."""
        kinds = [kind] if kind else ["memory", "episode", "evidence"]
        results: dict[str, list[dict[str, Any]]] = {}
        decision_id = None

        if "memory" in kinds:
            memory_result = self.search(
                organization_id=organization_id, query=query, acting_principal_id=acting_principal_id,
                destination=destination, purpose=purpose, entity_ids=entity_ids, limit=limit,
            )
            decision_id = memory_result["permission_decision_id"]
            results["memory"] = memory_result["results"]

        if "episode" in kinds:
            episode_result = self.search_episodes(
                organization_id=organization_id, query=query, acting_principal_id=acting_principal_id,
                destination=destination, purpose=purpose, limit=limit,
            )
            decision_id = decision_id or episode_result["permission_decision_id"]
            results["episode"] = episode_result["results"]

        if "evidence" in kinds:
            evidence_result = self.search_evidence(
                organization_id=organization_id, query=query, acting_principal_id=acting_principal_id,
                destination=destination, purpose=purpose, limit=limit,
            )
            decision_id = decision_id or evidence_result["permission_decision_id"]
            items = evidence_result["results"]
            if source_system:
                items = [item for item in items if item.get("source_system") == source_system]
            results["evidence"] = items

        def _timestamp(section: str, item: dict[str, Any]) -> str:
            if section == "memory":
                return item.get("memory", {}).get("valid_from") or ""
            if section == "episode":
                return item.get("ended_at") or ""
            return item.get("occurred_at") or ""

        if since or until:
            for section, items in results.items():
                results[section] = [
                    item for item in items
                    if (not since or _timestamp(section, item) >= since)
                    and (not until or _timestamp(section, item) <= until)
                ]

        return {
            "results": results,
            "permission_decision_id": decision_id,
            "count": sum(len(items) for items in results.values()),
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
            author_entity_id = src.get("author_entity_id")
            out.append(
                {
                    "id": src["id"],
                    "source_system": src.get("source_system"),
                    "source_type": src.get("source_type"),
                    "content_excerpt": (src.get("content") or "")[:300],
                    "occurred_at": src.get("occurred_at"),
                    "author_entity_id": author_entity_id,
                    "author_name": self._entity_name(author_entity_id),
                    "scope_id": src.get("scope_id"),
                    "scope_label": self._scope_label(src.get("scope_id")),
                }
            )
        return out

    def _public_memory(self, m: dict[str, Any]) -> dict[str, Any]:
        # Fall back to the single-participant convention older memories were
        # written with, so attribution works on rows created before the
        # explicit author field existed and no backfill is required.
        participants = m.get("participant_ids") or []
        author_entity_id = m.get("author_entity_id") or (
            participants[0] if len(participants) == 1 else None
        )
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
            "scope_label": self._scope_label(m.get("scope_id")),
            "source_ids": m.get("source_ids"),
            "entity_ids": m.get("entity_ids"),
            "author_entity_id": author_entity_id,
            "author_name": self._entity_name(author_entity_id),
            "participant_ids": participants,
            "participant_names": [
                name
                for name in (self._entity_name(pid) for pid in participants)
                if name
            ],
            "source_spans": m.get("source_spans"),
            "canonical_claim": m.get("canonical_claim"),
        }

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}

    def _score(
        self, memory: dict[str, Any], q_tokens: set[str], query: str,
        semantic_score: float = 0.0, fts_rank: float = 0.0,
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
        fts_bonus = min(max(fts_rank, 0.0), 1.0) * 0.12
        return (
            overlap * 0.55
            + max(semantic_score, 0.0) * 0.33
            + fts_bonus
            + phrase_bonus
            + type_bonus
            + authority
            + confidence
            + latest_bonus
            + _freshness(memory) * 0.12
        )
