"""Append-only audit ledger."""

from __future__ import annotations

from typing import Any

from toda.domain.models import AuditEvent, new_id, utcnow
from toda.storage.db import BrainStore


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
        trace_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=new_id("audit"),
            organization_id=organization_id,
            event_type=event_type,
            principal_id=principal_id,
            application_id=application_id,
            trace_id=trace_id,
            payload=payload,
            created_at=utcnow(),
        )
        self.store.append_audit(event.model_dump(mode="json"))
        return event

    def list_events(self, organization_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_audit(organization_id, event_type=event_type)

    def get_authorization_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self.store.get_authz(decision_id)
