"""CloudEvents-compatible intake and document processing status."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from toda.domain.models import new_id
from toda.services.audit import AuditService
from toda.services.ingestion import IngestionService
from toda.storage.db import BrainStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventService:
    def __init__(self, store: BrainStore, ingestion: IngestionService, audit: AuditService) -> None:
        self.store = store
        self.ingestion = ingestion
        self.audit = audit

    def ingest_document(self, body: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        org = body["organization_id"]
        if idempotency_key:
            previous = self.store.idempotency_response(org, "document.ingest", idempotency_key)
            if previous:
                return {**previous, "idempotent_replay": True}
        result = self.ingestion.ingest(
            organization_id=org,
            source_system=body.get("source_system") or "document",
            source_type=body.get("source_type") or "document",
            external_id=body["external_id"],
            content=body["content"],
            scope_id=body["scope_id"],
            author_entity_id=body.get("author_entity_id"),
            entity_ids=body.get("entity_ids") or [],
            metadata=body.get("metadata") or {},
            learn=body.get("learn", True),
            processing_mode=body.get("processing_mode", "memory"),
            occurred_at=body.get("occurred_at"),
        )
        now = _now()
        job = {
            "id": new_id("job"), "organization_id": org, "kind": "document.ingest",
            "status": "completed", "source_id": result["source"]["id"],
            "created_at": now, "updated_at": now,
            "stages": {"stored": "completed", "chunked": "completed", "indexed": "completed", "extracted": "completed" if body.get("learn", True) else "skipped"},
        }
        self.store.upsert_job(job)
        payload = {"source": result["source"], "memories": result["memories"], "job": job}
        if idempotency_key:
            self.store.save_idempotency_response(org, "document.ingest", idempotency_key, payload)
        return payload

    def document_status(self, source_id: str) -> dict[str, Any] | None:
        source = self.store.get_source(source_id)
        if not source:
            return None
        jobs = [job for job in self.store.list_jobs(source["organization_id"]) if job.get("source_id") == source_id]
        return {"source": source, "jobs": jobs}

    def receive_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        org = event["organization_id"]
        event.setdefault("received_at", _now())
        accepted = self.store.upsert_received_event(event)
        if not accepted:
            return {"event_id": event["id"], "status": "duplicate", "accepted": False}
        data = event.get("data") or {}
        content = data.get("content") or data.get("text") or self._event_summary(event)
        source = self.ingestion.ingest(
            organization_id=org,
            source_system=event["source"],
            source_type=event["type"],
            external_id=event["id"],
            content=content,
            scope_id=event.get("scope_id") or "scope_org_public",
            author_entity_id=event.get("principal_id"),
            entity_ids=list(data.get("entity_ids") or self._event_entities(data)),
            metadata={"cloudevent": event, "subject": event.get("subject"), "trace_id": event.get("trace_id")},
            learn=event.get("processing_mode", "memory") == "memory",
            processing_mode=event.get("processing_mode", "memory"),
            occurred_at=event.get("time") or event.get("occurred_at"),
        )
        self.audit.record(org, "event.accepted", {"event_id": event["id"], "source_id": source["source"]["id"], "type": event["type"]}, principal_id=event.get("principal_id"), trace_id=event.get("trace_id"))
        return {"event_id": event["id"], "status": "accepted", "accepted": True, "source": source["source"], "memory_count": len(source["memories"])}

    @staticmethod
    def _event_entities(data: dict[str, Any]) -> list[str]:
        return [value for key, value in data.items() if key.endswith("_entity_id") and isinstance(value, str)]

    @staticmethod
    def _event_summary(event: dict[str, Any]) -> str:
        data = event.get("data") or {}
        fields = [event.get("type", "event"), data.get("action", ""), data.get("result", "")]
        return " ".join(str(value) for value in fields if value) or json.dumps(data, sort_keys=True)
