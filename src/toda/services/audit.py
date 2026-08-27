"""Append-only audit ledger."""

from __future__ import annotations

from typing import Any

from toda.domain.models import AuditEvent, new_id, utcnow
from toda.storage.db import BrainStore
from toda.telemetry.context import current_otel_trace_id


class AuditService:
    def __init__(self, store: BrainStore) -> None:
        self.store = store

    def record(
        self,
        organization_id: str,
        event_type: str,
        payload: dict[str, Any],
        principal_id: str | None = None,
        application_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        # The durable trace field is reserved for the real active OTel trace.
        # Application-level continuity belongs in ``correlation_id`` and must
        # never be mistaken for a distributed trace identifier.
        event = AuditEvent(
            id=new_id("audit"),
            organization_id=organization_id,
            event_type=event_type,
            principal_id=principal_id,
            application_id=application_id,
            trace_id=current_otel_trace_id(),
            correlation_id=correlation_id,
            payload=payload,
            created_at=utcnow(),
        )
        self.store.append_audit(event.model_dump(mode="json"))
        return event

    def list_events(self, organization_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_audit(organization_id, event_type=event_type)

    def get_authorization_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self.store.get_authz(decision_id)
