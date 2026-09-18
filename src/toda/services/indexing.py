"""SQLite FTS and vector indexing for sources and atomic memories."""

from __future__ import annotations

import hashlib
from typing import Any

from toda.services.embeddings import EmbeddingProvider
from toda.storage.db import BrainStore


class IndexingService:
    def __init__(self, store: BrainStore, embeddings: EmbeddingProvider) -> None:
        self.store = store
        self.embeddings = embeddings

    def index_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        chunks = self._chunks(source.get("content") or "")
        rows = []
        vectors = self.embeddings.embed(chunks) if chunks else []
        for position, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            row = {
                "id": f"chunk_{source['id']}_{position}_{digest}",
                "organization_id": source["organization_id"],
                "source_id": source["id"],
                "scope_id": source["scope_id"],
                "position": position,
                "content": content,
                "model": self.embeddings.model,
                "embedding": vector,
            }
            self.store.upsert_chunk(row)
            rows.append(row)
        return rows

    def index_memory(self, memory: dict[str, Any]) -> None:
        self.index_memories([memory])

    def index_memories(
        self, memories: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Embed a memory batch in one provider request."""
        pending = [
            memory for memory in memories if (memory.get("content") or "").strip()
        ]
        if not pending:
            return {"indexed_memories": 0}
        contents = [memory["content"] for memory in pending]
        vectors = self.embeddings.embed(contents)
        for memory, content, vector in zip(
            pending, contents, vectors, strict=True
        ):
            self.store.upsert_memory_embedding(
                memory_id=memory["id"],
                organization_id=memory["organization_id"],
                scope_id=memory["scope_id"],
                model=self.embeddings.model,
                embedding=vector,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
        return {"indexed_memories": len(pending)}

    def backfill(self) -> dict[str, int]:
        """Index pre-existing records after a schema upgrade without duplicating work.

        Also re-embeds rows whose stored vector width does not match the
        current model — otherwise a switch from a remote 8B embedder to a
        local 384-d model leaves the ANN index empty (reindex skips the
        old-width JSON columns).
        """
        probe = self.embeddings.embed(["\u200b"])[0]
        width = len(probe)
        indexed_sources = 0
        indexed_memories = 0
        for org in self.store.list_organizations():
            org_id = org["id"]
            for source in self.store.list_sources(org_id, include_revoked=True):
                existing = self.store.list_chunks(
                    org_id, source_ids=[source["id"]], limit=1
                )
                stored = (existing[0].get("embedding") if existing else None) or []
                if not existing or len(stored) != width:
                    self.index_source(source)
                    indexed_sources += 1
            memories = self.store.list_memories(org_id, latest_only=False)
            stored_vectors = self.store.memory_embeddings(
                [memory["id"] for memory in memories]
            )
            missing_memories = [
                memory
                for memory in memories
                if (not self.store.has_memory_embedding(
                    memory["id"], memory.get("content") or ""
                ))
                or len(stored_vectors.get(memory["id"]) or []) != width
            ]
            if missing_memories:
                indexed_memories += self.index_memories(missing_memories)[
                    "indexed_memories"
                ]
        return {
            "indexed_sources": indexed_sources,
            "indexed_memories": indexed_memories,
        }

    @staticmethod
    def _chunks(content: str, max_chars: int = 900) -> list[str]:
        text = content.strip()
        if not text:
            return []
        parts = [part.strip() for part in text.split("\n\n") if part.strip()]
        chunks: list[str] = []
        current = ""
        for part in parts or [text]:
            if current and len(current) + len(part) + 2 > max_chars:
                chunks.append(current)
                current = part
            else:
                current = f"{current}\n\n{part}".strip()
        if current:
            chunks.append(current)
        return chunks
