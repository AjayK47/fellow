"""Durable signal lifecycle, deduplication, acknowledgement, and private routing."""

from __future__ import annotations

from typing import Any

from company_brain.domain.models import IssueState, Signal, SignalType, new_id
from company_brain.services.audit import AuditService
from company_brain.services.graph import GraphService
from company_brain.storage.db import BrainStore


class SignalsService:
    def __init__(self, store: BrainStore, graph: GraphService, audit: AuditService) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit

    def create_or_merge(
        self, *, organization_id: str, signal_type: str, confidence: float, content: str,
        entity_ids: list[str], destination_id: str | None, evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for signal in self.store.list_signals(organization_id):
            if (signal.get("signal_type") == signal_type and signal.get("destination_id") == destination_id
                    and set(signal.get("entity_ids") or []) == set(entity_ids)
                    and not signal.get("acknowledged")
                    and signal.get("state") not in {IssueState.DISMISSED.value, IssueState.RESOLVED.value}):
                signal["confidence"] = max(float(signal.get("confidence") or 0), confidence)
                signal["evidence"] = list(signal.get("evidence") or []) + [e for e in evidence if e not in signal.get("evidence", [])]
                self.store.upsert_signal(signal)
                return signal
        signal = Signal(
            id=new_id("sig"), organization_id=organization_id,
            signal_type=SignalType(signal_type) if signal_type in SignalType._value2member_map_ else SignalType.VERIFIED_MISSING_ANSWER,
            confidence=confidence, content=content, entity_ids=entity_ids,
            destination_id=destination_id, evidence=evidence,
        ).model_dump(mode="json")
        self.store.upsert_signal(signal)
        return signal

    def list(self, organization_id: str, state: str | None = None) -> list[dict[str, Any]]:
        signals = self.store.list_signals(organization_id)
        return [signal for signal in signals if not state or signal.get("state") == state]

    def acknowledge(self, signal_id: str, principal_id: str | None = None) -> dict[str, Any] | None:
        signal = self.store.get_signal(signal_id)
        if not signal:
            return None
        signal["state"] = IssueState.ACKNOWLEDGED.value
        signal["acknowledged"] = True
        signal["acknowledged_by"] = principal_id
        self.store.upsert_signal(signal)
        self.audit.record(signal["organization_id"], "signal.acknowledged", {"signal_id": signal_id}, principal_id=principal_id)
        return signal

    def dismiss(self, signal_id: str, principal_id: str | None = None) -> dict[str, Any] | None:
        signal = self.store.get_signal(signal_id)
        if not signal:
            return None
        signal["state"] = IssueState.DISMISSED.value
        signal["acknowledged"] = True
        self.store.upsert_signal(signal)
        self.audit.record(signal["organization_id"], "signal.dismissed", {"signal_id": signal_id}, principal_id=principal_id)
        return signal

    def route(self, signal_id: str, recipient_id: str | None = None) -> dict[str, Any] | None:
        signal = self.store.get_signal(signal_id)
        if not signal:
            return None
        if not recipient_id:
            owners = self.graph.resolve_owners(signal["organization_id"], signal.get("entity_ids") or [])
            if owners:
                recipient_id = owners[0].get("owner_id")
            if not recipient_id:
                for entity_id in signal.get("entity_ids") or []:
                    on_call = self.graph.on_call_for(signal["organization_id"], entity_id)
                    if on_call and on_call[0].get("person"):
                        recipient_id = on_call[0]["person"].get("id")
                        break
        if not recipient_id:
            return None
        signal["recipient_id"] = recipient_id
        signal["state"] = IssueState.ROUTED.value
        self.store.upsert_signal(signal)
        self.audit.record(signal["organization_id"], "signal.routed", {"signal_id": signal_id, "recipient_id": recipient_id})
        return signal
