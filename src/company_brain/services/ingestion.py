"""Evidence ingestion pipeline."""

from __future__ import annotations

import hashlib
from typing import Any

from company_brain.domain.models import SourceEvidence, new_id, utcnow
from company_brain.services.audit import AuditService
from company_brain.services.memory import MemoryService
from company_brain.services.indexing import IndexingService
from company_brain.storage.db import BrainStore


class IngestionService:
    def __init__(
        self,
        store: BrainStore,
        memory: MemoryService,
        audit: AuditService,
        indexing: IndexingService,
    ) -> None:
        self.store = store
        self.memory = memory
        self.audit = audit
        self.indexing = indexing

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
    ) -> dict[str, Any]:
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
            occurred_at=utcnow(),
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
        chunks = self.indexing.index_source(data)

        memories: list[dict[str, Any]] = []
        if learn and processing_mode == "memory":
            memories = self.memory.learn_from_source(data)
        # reference-only path stores evidence without memory extraction

        self.audit.record(
            organization_id=organization_id,
            event_type="ingestion.accepted",
            principal_id=author_entity_id,
            payload={
                "source_id": data["id"],
                "processing_mode": processing_mode,
                "memory_count": len(memories),
                "chunk_count": len(chunks),
                "content_hash": content_hash,
            },
        )
        return {"source": data, "memories": memories}

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
