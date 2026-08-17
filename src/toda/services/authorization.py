"""Permission graph evaluation — fail closed, destination-based."""

from __future__ import annotations

from typing import Any

from toda.domain.models import (
    AuthorizationDecision,
    Destination,
    ScopeKind,
    new_id,
    utcnow,
)
from toda.services.audit import AuditService
from toda.storage.db import BrainStore


class AuthorizationService:
    """
    Computes allowed scopes for (principal, destination, purpose).

    Rules (V1):
    - Always include public/org scopes the principal belongs to.
    - Private channel scopes only if principal is a member AND destination is safe.
    - Destination public/shared → intersection with destination audience visibility.
    - Fail closed: if scope cannot be verified, exclude it.
    - Revoked sources are never readable (enforced later in retrieval).
    """

    def __init__(self, store: BrainStore, audit: AuditService) -> None:
        self.store = store
        self.audit = audit

    def resolve(
        self,
        organization_id: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> AuthorizationDecision:
        dest = self._normalize_destination(destination)
        scopes = self.store.list_scopes(organization_id)
        memberships = set(self.store.memberships_for(organization_id, acting_principal_id))

        allowed: list[str] = []
        denied: list[str] = []

        for scope in scopes:
            scope_id = scope["id"]
            kind = scope.get("kind")
            is_public = bool(scope.get("is_public")) or kind == ScopeKind.ORGANIZATION_PUBLIC.value

            if is_public:
                # A private-channel response may use only knowledge from that
                # exact private channel. Public organization memory is not
                # silently mixed into a private room's context.
                if dest and dest.type == "private_channel":
                    denied.append(f"{scope_id}:outside_private_channel")
                    continue
                if scope_id in memberships or kind == ScopeKind.ORGANIZATION_PUBLIC.value:
                    allowed.append(scope_id)
                else:
                    # org public scopes are always readable within tenant for members of org
                    if self._is_org_member(organization_id, acting_principal_id):
                        allowed.append(scope_id)
                    else:
                        denied.append(f"{scope_id}:not_org_member")
                continue

            # private scopes require membership
            if scope_id not in memberships:
                denied.append(f"{scope_id}:not_member")
                continue

            # destination-based: a specific room only unlocks its own private
            # scope, not every private scope the principal happens to belong
            # to elsewhere. This check must run for private destinations too
            # (a DM or a named private channel) — it previously only ran for
            # public destinations, which is why membership in private
            # channel B could leak into a conversation happening in channel A.
            if dest and not self._destination_allows_private(dest, scope):
                denied.append(f"{scope_id}:destination_not_private")
                continue

            allowed.append(scope_id)

        # de-dupe preserve order
        seen: set[str] = set()
        uniq_allowed: list[str] = []
        for s in allowed:
            if s not in seen:
                seen.add(s)
                uniq_allowed.append(s)

        decision = AuthorizationDecision(
            id=new_id("authz"),
            organization_id=organization_id,
            acting_principal_id=acting_principal_id,
            destination_type=dest.type if dest else None,
            destination_id=dest.id if dest else None,
            purpose=purpose,
            allowed_scope_ids=uniq_allowed,
            denied_reasons=denied,
            fail_closed=True,
            created_at=utcnow(),
        )
        self.store.save_authz(decision.model_dump(mode="json"))
        self.audit.record(
            organization_id=organization_id,
            event_type="authorization.decision",
            principal_id=acting_principal_id,
            payload=decision.model_dump(mode="json"),
        )
        return decision

    def can_read_memory(self, decision: AuthorizationDecision, memory: dict[str, Any]) -> bool:
        if memory.get("organization_id") != decision.organization_id:
            return False
        if memory.get("scope_id") not in decision.allowed_scope_ids:
            return False
        # private memory needs private destination unless scope is public
        if memory.get("is_private") and decision.destination_type in {
            "slack_channel",
            "public_channel",
            "shared_room",
        }:
            # if destination is public-style, private memories blocked even if scope mistakenly allowed
            scope = self.store.get_scope(memory.get("scope_id", ""))
            if scope and not scope.get("is_public"):
                return False
        return True

    def can_read_source(self, decision: AuthorizationDecision, source: dict[str, Any]) -> bool:
        if source.get("organization_id") != decision.organization_id:
            return False
        if source.get("revoked") or source.get("deleted_at"):
            return False
        if source.get("scope_id") not in decision.allowed_scope_ids:
            return False
        if source.get("is_private_channel"):
            if decision.destination_type in {"slack_channel", "public_channel", "shared_room"}:
                # public destination cannot see private-channel sources
                # check if destination id matches the private channel itself
                if decision.destination_id and decision.destination_id == source.get("channel_id"):
                    # only if destination is that private channel and principal is member (already in scopes)
                    return source.get("scope_id") in decision.allowed_scope_ids
                return False
        return True

    def filter_memories(
        self, decision: AuthorizationDecision, memories: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [m for m in memories if self.can_read_memory(decision, m)]

    def filter_sources(
        self, decision: AuthorizationDecision, sources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [s for s in sources if self.can_read_source(decision, s)]

    def can_read_edge(self, decision: AuthorizationDecision, edge: dict[str, Any]) -> bool:
        """Graph edges don't carry their own scope_id — they inherit
        visibility from the evidence that put them there. A structural edge
        with no source_ids (org chart, ownership, on-call — the kind
        GraphService.explorer_view already always shows) has no evidentiary
        privacy to enforce and stays visible; an edge backed by evidence
        (e.g. a memory's SUPPORTED_BY/ABOUT/INVOLVES edges) is only visible
        if at least one of its sources is. This is the same rule
        explorer_view has always applied — GraphService.neighbors() and
        paths_between() just never applied it (memory ingestion/retrieval
        plan §5: graph expansion must not bypass scope authorization)."""
        if edge.get("organization_id") != decision.organization_id:
            return False
        source_ids = edge.get("source_ids") or []
        if not source_ids:
            return True
        return any(
            (source := self.store.get_source(sid)) is not None and self.can_read_source(decision, source)
            for sid in source_ids
        )

    def filter_edges(
        self, decision: AuthorizationDecision, edges: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [e for e in edges if self.can_read_edge(decision, e)]

    def _normalize_destination(self, destination: Destination | dict[str, Any] | None) -> Destination | None:
        if destination is None:
            return None
        if isinstance(destination, Destination):
            return destination
        return Destination(
            type=destination.get("type", "internal_agent_session"),
            id=destination.get("id", "default"),
            is_private=bool(destination.get("is_private", False)),
            audience_principal_ids=list(destination.get("audience_principal_ids") or []),
        )

    def _destination_allows_private(self, dest: Destination, scope: dict[str, Any]) -> bool:
        if dest.type in {"dm", "person_private", "internal_agent_session"}:
            # A personal session has no single "room" to narrow to — it
            # naturally reaches everything the principal is a member of.
            return True
        if dest.is_private:
            # A specific private room (a named private channel) only
            # unlocks its OWN scope, never another private scope the
            # principal happens to belong to elsewhere. A destination
            # naming a channel is not itself proof the scope IS that
            # channel — only a channel_id match is.
            return bool(scope.get("channel_id")) and scope.get("channel_id") == dest.id
        # public destinations only if scope is public
        return bool(scope.get("is_public"))

    def _is_org_member(self, organization_id: str, principal_id: str) -> bool:
        # any membership or known person entity counts as org member for V1
        memberships = self.store.memberships_for(organization_id, principal_id)
        if memberships:
            return True
        ent = self.store.get_entity(principal_id)
        return bool(ent and ent.get("organization_id") == organization_id)
