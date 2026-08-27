"""Evidence ingestion pipeline."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from toda.domain.models import SourceEvidence, new_id, utcnow
from toda.services.audit import AuditService
from toda.services.memory import MemoryService
from toda.services.indexing import IndexingService
from toda.services.redaction import redact
from toda.storage.db import BrainStore


class IngestionService:
    def __init__(
        self,
        store: BrainStore,
        memory: MemoryService,
        audit: AuditService,
        indexing: IndexingService,
        reality: Any | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.audit = audit
        self.indexing = indexing
        self.reality = reality

    def persist(
        self,
        *,
        organization_id: str,
        source_system: str,
        source_type: str,
        external_id: str,
        content: str,
        scope_id: str,
        author_entity_id: str | None = None,
        channel_id: str | None = None,
        is_private_channel: bool = False,
        entity_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Fast, local-only path: redact, hash, version, write the Source
        row. No embedding call, no model extraction, no reality/graph
        expansion — those are `process_knowledge`'s job. This is what the
        event-routing control path calls directly so a slow embedding
        provider can never delay wake (memory ingestion/retrieval plan §2
        Phase A)."""
        self.store.telemetry.record(
            "knowledge.ingestion.persist", component="ingestion", phase="started",
            input={"source_system": source_system, "source_type": source_type,
                   "external_id": external_id, "content": content, "scope_id": scope_id},
            organization_id=organization_id,
        )
        # Secrets/credentials never get past this point: everything downstream
        # (the stored Source, indexing, deterministic/model memory extraction,
        # reality-engine ingestion) reads from `content` after this redaction,
        # so gating here covers every caller — raw event ingestion, post-run
        # memory capture, and episode consolidation — through one place.
        redaction = redact(content)
        if redaction.redacted:
            content = redaction.text
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        # Bump version for same (source_system, external_id) so prior evidence rows remain
        version = self.store.next_source_version(organization_id, source_system, external_id)
        source = SourceEvidence(
            id=new_id("src"),
            organization_id=organization_id,
            source_system=source_system,
            source_type=source_type,
            external_id=external_id,
            author_entity_id=author_entity_id,
            occurred_at=occurred_at or utcnow(),
            ingested_at=utcnow(),
            content=content,
            content_uri=f"local://{organization_id}/sources/{external_id}/v{version}",
            content_hash=content_hash,
            version=version,
            acl_snapshot_id=f"acl_{scope_id}",
            scope_id=scope_id,
            channel_id=channel_id,
            is_private_channel=is_private_channel,
            metadata={**(metadata or {}), "external_key": f"{source_system}:{external_id}"},
            entity_ids=entity_ids or [],
        )
        data = source.model_dump(mode="json")
        self.store.upsert_source(data)
        if redaction.redacted:
            self.audit.record(
                organization_id=organization_id,
                event_type="ingestion.secret_redacted",
                principal_id=author_entity_id,
                payload={
                    "source_id": data["id"],
                    "categories": redaction.categories,
                },
            )
        self.audit.record(
            organization_id=organization_id,
            event_type="ingestion.persisted",
            principal_id=author_entity_id,
            payload={"source_id": data["id"], "content_hash": content_hash},
        )
        self.store.telemetry.record(
            "knowledge.ingestion.persist", component="ingestion", phase="completed",
            output={"source_id": data["id"], "content_hash": content_hash, "version": version},
            organization_id=organization_id,
        )
        return data

    def process_knowledge(
        self,
        source: dict[str, Any],
        *,
        learn: bool = True,
        processing_mode: str = "memory",
    ) -> dict[str, Any]:
        """The slow/remote continuation of ingest(): chunk + embed (always,
        so the evidence stays searchable regardless of memory_policy), then
        extract memories + update the reality graph only when `learn` is
        set. Runs in the knowledge lane (harness/worker.py), never inline
        on the wake path."""
        self.store.telemetry.record(
            "knowledge.ingestion.process", component="ingestion", phase="started",
            input={"source_id": source.get("id"), "learn": learn, "processing_mode": processing_mode},
            organization_id=source.get("organization_id"),
        )
        chunks = self.indexing.index_source(source)

        memories: list[dict[str, Any]] = []
        if learn and processing_mode == "memory":
            memories = self.memory.learn_from_source(source)
        # reference-only path stores evidence without memory extraction
        reality_result = (
            self.reality.ingest_source(source, memories)
            if self.reality
            else None
        )

        self.audit.record(
            organization_id=source["organization_id"],
            event_type="ingestion.accepted",
            principal_id=source.get("author_entity_id"),
            payload={
                "source_id": source["id"],
                "processing_mode": processing_mode,
                "memory_count": len(memories),
                "chunk_count": len(chunks),
                "content_hash": source.get("content_hash"),
            },
        )
        result = {
            "source": source,
            "memories": memories,
            "reality": reality_result,
        }
        self.store.telemetry.record(
            "knowledge.ingestion.process", component="ingestion", phase="completed",
            output={"source_id": source.get("id"), "chunk_count": len(chunks), "memory_count": len(memories)},
            organization_id=source.get("organization_id"),
        )
        return result

    def ingest(
        self,
        *,
        organization_id: str,
        source_system: str,
        source_type: str,
        external_id: str,
        content: str,
        scope_id: str,
        author_entity_id: str | None = None,
        channel_id: str | None = None,
        is_private_channel: bool = False,
        entity_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        learn: bool = True,
        processing_mode: str = "memory",
        occurred_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Synchronous persist + process_knowledge, for callers that want
        the old atomic behavior and don't sit on the interactive wake path
        (document upload, memory_capture's own background jobs, tests)."""
        data = self.persist(
            organization_id=organization_id,
            source_system=source_system,
            source_type=source_type,
            external_id=external_id,
            content=content,
            scope_id=scope_id,
            author_entity_id=author_entity_id,
            channel_id=channel_id,
            is_private_channel=is_private_channel,
            entity_ids=entity_ids,
            metadata=metadata,
            occurred_at=occurred_at,
        )
        return self.process_knowledge(data, learn=learn, processing_mode=processing_mode)

    def revoke_source(self, source_id: str) -> dict[str, Any] | None:
        src = self.store.revoke_source(source_id)
        if src:
            self.audit.record(
                organization_id=src["organization_id"],
                event_type="source.revoked",
                payload={"source_id": source_id},
            )
            # Mark memories that ONLY had this source as retracted from search by tagging
            # Retrieval checks source revocation live — no need to delete memories.
        return src

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        return self.store.get_source(source_id)
