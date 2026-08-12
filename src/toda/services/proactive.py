"""Proactive intelligence — knowledge intervention and issue routing policy."""

from __future__ import annotations

from typing import Any

from toda.domain.models import (
    Destination,
    ProactiveCandidate,
    ProactiveDecision,
    ProactiveEvaluationResult,
    Signal,
    SignalType,
    new_id,
)
from toda.services.audit import AuditService
from toda.services.authorization import AuthorizationService
from toda.services.graph import GraphService
from toda.services.retrieval import RetrievalService
from toda.services.signals import SignalsService
from toda.storage.db import BrainStore


def decide(candidate: ProactiveCandidate) -> ProactiveDecision:
    """Policy from spec §26 — pure function, unit-testable."""
    if not candidate.permission_safe:
        return ProactiveDecision.SILENT

    if candidate.privacy_risk > 0.05:
        return ProactiveDecision.SILENT

    if candidate.already_answered or not candidate.cooldown_eligible:
        return ProactiveDecision.REMEMBER_ONLY

    if candidate.social_sensitivity > 0.30:
        return ProactiveDecision.SILENT

    value = (
        candidate.usefulness
        * candidate.confidence
        * candidate.evidence_authority
        * max(candidate.urgency, 0.30)
        * max(candidate.actionability, 0.30)
        * candidate.novelty
        - candidate.interruption_cost
    )

    if candidate.signal_type == SignalType.VERIFIED_OPERATIONAL_INCIDENT.value and value > 0.72:
        return ProactiveDecision.ESCALATE

    if candidate.signal_type == SignalType.UNACKNOWLEDGED_CUSTOMER_ISSUE.value and value > 0.68:
        return ProactiveDecision.PRIVATE_ROUTE

    if candidate.signal_type in {
        SignalType.VERIFIED_MISSING_ANSWER.value,
        SignalType.VERIFIED_CORRECTION.value,
    }:
        if (
            candidate.confidence >= 0.95
            and candidate.evidence_authority >= 0.85
            and value > 0.62
        ):
            return ProactiveDecision.REPLY

    if value > 0.42:
        return ProactiveDecision.DIGEST

    return ProactiveDecision.REMEMBER_ONLY


class ProactiveService:
    def __init__(
        self,
        store: BrainStore,
        authz: AuthorizationService,
        retrieval: RetrievalService,
        graph: GraphService,
        audit: AuditService,
        signals: SignalsService | None = None,
    ) -> None:
        self.store = store
        self.authz = authz
        self.retrieval = retrieval
        self.graph = graph
        self.audit = audit
        self.signals = signals

    def evaluate_candidate(self, candidate: ProactiveCandidate | dict[str, Any]) -> ProactiveEvaluationResult:
        if isinstance(candidate, dict):
            candidate = ProactiveCandidate(**candidate)
        decision = decide(candidate)
        value = (
            candidate.usefulness
            * candidate.confidence
            * candidate.evidence_authority
            * max(candidate.urgency, 0.30)
            * max(candidate.actionability, 0.30)
            * candidate.novelty
            - candidate.interruption_cost
        )
        reason = self._reason(candidate, decision, value)
        suggested = None
        if decision in {ProactiveDecision.REPLY, ProactiveDecision.DIGEST}:
            suggested = candidate.content or "Relevant verified company knowledge is available."
        if decision == ProactiveDecision.PRIVATE_ROUTE:
            suggested = "Route privately to the responsible owner."
        if decision == ProactiveDecision.ESCALATE:
            suggested = "Escalate operational incident to on-call / escalation path."

        result = ProactiveEvaluationResult(
            decision=decision,
            signal_type=candidate.signal_type,
            confidence=candidate.confidence,
            reason=reason,
            suggested_message=suggested,
            evidence=list(candidate.evidence),
            policy_explanation={
                "permission_safe": candidate.permission_safe,
                "privacy_risk": candidate.privacy_risk,
                "social_sensitivity": candidate.social_sensitivity,
                "already_answered": candidate.already_answered,
                "cooldown_eligible": candidate.cooldown_eligible,
                "value_score": value,
                "thresholds": {
                    "reply_confidence": 0.95,
                    "reply_authority": 0.85,
                    "reply_value": 0.62,
                    "private_route_value": 0.68,
                    "escalate_value": 0.72,
                    "digest_value": 0.42,
                },
            },
            cooldown={"eligible": candidate.cooldown_eligible},
        )
        return result

    def evaluate(
        self,
        *,
        organization_id: str,
        trigger: dict[str, Any],
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        mode: str = "dry_run",
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Full evaluate path. If `candidate` provided, use it; else derive from trigger + graph.
        """
        dest = destination or {"type": "internal_agent_session", "id": "default", "is_private": True}
        decision_auth = self.authz.resolve(
            organization_id=organization_id,
            acting_principal_id=acting_principal_id,
            destination=dest,
            purpose="proactive_evaluation",
        )

        if candidate is None:
            candidate = self._derive_candidate(organization_id, trigger, decision_auth, dest)
        else:
            # enforce permission from authz
            candidate = dict(candidate)
            candidate["permission_safe"] = candidate.get("permission_safe", True) and bool(
                decision_auth.allowed_scope_ids
            )

        # fail closed if no allowed scopes
        if not decision_auth.allowed_scope_ids:
            candidate["permission_safe"] = False

        result = self.evaluate_candidate(candidate)
        result.permission_decision_id = decision_auth.id
        payload = result.model_dump(mode="json")
        payload["mode"] = mode
        payload["trigger"] = trigger

        # persist signal when not silent
        if result.decision != ProactiveDecision.SILENT:
            if self.signals:
                sig = self.signals.create_or_merge(
                    organization_id=organization_id, signal_type=result.signal_type or SignalType.VERIFIED_MISSING_ANSWER.value,
                    confidence=result.confidence, content=result.suggested_message or result.reason,
                    entity_ids=list((candidate or {}).get("entity_ids") or []),
                    destination_id=(dest.get("id") if isinstance(dest, dict) else dest.id), evidence=result.evidence,
                )
                payload["signal_id"] = sig["id"]
            else:
                sig = Signal(id=new_id("sig"), organization_id=organization_id, signal_type=SignalType.VERIFIED_MISSING_ANSWER, confidence=result.confidence, content=result.suggested_message or result.reason)
                self.store.upsert_signal(sig.model_dump(mode="json"))
                payload["signal_id"] = sig.id

        self.audit.record(
            organization_id=organization_id,
            event_type="proactive.evaluated",
            principal_id=acting_principal_id,
            payload=payload,
        )
        return payload

    def _derive_candidate(
        self,
        organization_id: str,
        trigger: dict[str, Any],
        decision_auth: Any,
        dest: Any,
    ) -> dict[str, Any]:
        text = (
            trigger.get("text")
            or trigger.get("message")
            or trigger.get("issue_text")
            or ""
        )
        ttype = (trigger.get("type") or "").lower()

        # classify signal type
        if "incident" in ttype or "outage" in text.lower() or "deployment" in text.lower():
            signal_type = SignalType.VERIFIED_OPERATIONAL_INCIDENT.value
        elif "customer" in ttype or "export" in text.lower() or "stuck" in text.lower():
            signal_type = SignalType.UNACKNOWLEDGED_CUSTOMER_ISSUE.value
        elif "wrong" in text.lower() or "incorrect" in text.lower() or "actually" in text.lower():
            signal_type = SignalType.VERIFIED_CORRECTION.value
        else:
            signal_type = SignalType.VERIFIED_MISSING_ANSWER.value

        search = self.retrieval.search(
            organization_id=organization_id,
            query=text or " ",
            acting_principal_id=decision_auth.acting_principal_id,
            destination=dest,
            purpose="proactive_evaluation",
            limit=5,
        )
        results = search["results"]
        permission_safe = True
        # if destination is public, ensure we only used public scopes
        if isinstance(dest, dict) and not dest.get("is_private", False):
            if dest.get("type") in {"slack_channel", "public_channel", "shared_room"}:
                # private results already filtered; if nothing found that's fine
                pass

        best = results[0] if results else None
        evidence = []
        confidence = 0.5
        authority = 0.5
        content = ""
        entity_ids: list[str] = []
        if best:
            mem = best["memory"]
            confidence = float(mem.get("confidence") or 0.5)
            authority = float(mem.get("authority") or 0.5)
            content = mem.get("content") or ""
            entity_ids = list(mem.get("entity_ids") or [])
            evidence.append(
                {
                    "memory_id": mem["id"],
                    "content": content,
                    "source_ids": mem.get("source_ids"),
                    "score": best["score"],
                }
            )
        else:
            permission_safe = bool(decision_auth.allowed_scope_ids)
            confidence = 0.2
            authority = 0.2

        already_answered = "already answered" in text.lower() or "thanks" in text.lower()
        is_question = "?" in text or any(
            text.lower().startswith(w) for w in ("what", "when", "who", "did", "is ", "are ")
        )

        return {
            "signal_type": signal_type,
            "confidence": confidence if best else 0.4,
            "evidence_authority": authority,
            "novelty": 0.9 if is_question else 0.5,
            "usefulness": 0.9 if best and is_question else 0.5,
            "urgency": 0.9 if signal_type == SignalType.VERIFIED_OPERATIONAL_INCIDENT.value else 0.5,
            "actionability": 0.85,
            "interruption_cost": 0.2,
            "privacy_risk": 0.0,
            "social_sensitivity": 0.0,
            "permission_safe": permission_safe and bool(decision_auth.allowed_scope_ids),
            "already_answered": already_answered,
            "cooldown_eligible": True,
            "content": content,
            "evidence": evidence,
            "entity_ids": entity_ids,
        }

    @staticmethod
    def _reason(candidate: ProactiveCandidate, decision: ProactiveDecision, value: float) -> str:
        if not candidate.permission_safe:
            return "Permission-unsafe or fail-closed authorization; staying silent."
        if candidate.privacy_risk > 0.05:
            return "Privacy risk exceeds threshold; staying silent."
        if candidate.social_sensitivity > 0.30:
            return "Social sensitivity too high; staying silent."
        if candidate.already_answered:
            return "Already answered; remember only."
        if not candidate.cooldown_eligible:
            return "Cooldown active; remember only."
        if decision == ProactiveDecision.REPLY:
            return "Authoritative verified knowledge is missing from the room and permission-safe."
        if decision == ProactiveDecision.PRIVATE_ROUTE:
            return "Unacknowledged customer/operational issue; route to smallest valid owner."
        if decision == ProactiveDecision.ESCALATE:
            return "Verified operational incident exceeds escalation threshold."
        if decision == ProactiveDecision.DIGEST:
            return f"Material signal (value={value:.2f}) suitable for digest, not interrupt."
        if decision == ProactiveDecision.REMEMBER_ONLY:
            return f"Signal retained without intervention (value={value:.2f})."
        return "Default silence."
