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
        """Index pre-existing records after a schema upgrade without duplicating work."""
        indexed_sources = 0
        indexed_memories = 0
        for org in self.store.list_organizations():
            org_id = org["id"]
            for source in self.store.list_sources(org_id, include_revoked=True):
                if not self.store.has_chunks_for_source(source["id"]):
                    self.index_source(source)
                    indexed_sources += 1
            missing_memories = [
                memory
                for memory in self.store.list_memories(
                    org_id, latest_only=False
                )
                if not self.store.has_memory_embedding(
                    memory["id"], memory.get("content") or ""
                )
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
