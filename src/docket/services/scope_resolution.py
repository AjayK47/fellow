"""One canonical answer to "what scope does this room write to, and what can
be read from it" — modeled on supermemory's write-one-scope /
read-by-room-and-membership rule.

Used at three points: post-run memory capture (the write half), and the
delivery disclosure recheck (the read half, recomputed fresh right before
sending). Deliberately independent of `AuthorizationService.resolve()`'s
`allowed_scope_ids`, which answers a related but different question — "what
can this principal see across everything they're a member of" — and can
legitimately contain scopes from more than one room. This module answers
"which single room is this, and what does *that room* unlock," which is
what a write needs and what a disclosure check needs, neither of which is
"pick one entry out of a list."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# "internal_agent_session" is a private one-on-one interaction with the
# agent, not a named channel — AuthorizationService._destination_allows_private
# already treats it the same as a DM for private-scope access, and this
# module matches that so a legitimate personal-scope membership (e.g. a
# seeded leadership-private scope) isn't newly blocked at delivery time.
_DM_DESTINATION_TYPES = {"dm", "person_private", "internal_agent_session"}


@dataclass
class ScopeResolution:
    write_scope_id: str
    read_scope_ids: set[str] = field(default_factory=set)


def resolve_room_scope_id(
    *, store: Any, organization_id: str, principal_id: str, destination: dict[str, Any]
) -> tuple[str, bool]:
    """Return (scope_id, is_public) for the room a destination represents.

    Looks up the scope record that actually corresponds to this destination
    — by the person's own scope for DMs, by channel identity for channels —
    rather than guessing from an authorization decision's allowed_scope_ids,
    which can contain scopes from multiple rooms at once.
    """
    dest_type = str(destination.get("type") or "")
    if dest_type in _DM_DESTINATION_TYPES:
        scope_id = f"scope_person_{principal_id}"
        scope = store.get_scope(scope_id)
        return scope_id, bool(scope.get("is_public")) if scope else False
    dest_id = destination.get("id")
    if dest_id:
        for scope in store.list_scopes(organization_id):
            if scope.get("channel_id") and scope["channel_id"] == dest_id:
                return scope["id"], bool(scope.get("is_public"))
    # No real room corresponds to this destination (tests, internal agent
    # sessions with no Slack channel behind them, etc.) — fail open to the
    # broadest *public* scope rather than guessing at a private one.
    return "scope_org_public", True


def resolve_scope(
    *, store: Any, organization_id: str, principal_id: str, destination: dict[str, Any]
) -> ScopeResolution:
    room_scope_id, room_is_public = resolve_room_scope_id(
        store=store,
        organization_id=organization_id,
        principal_id=principal_id,
        destination=destination,
    )
    scopes = store.list_scopes(organization_id)
    public_scope_ids = {scope["id"] for scope in scopes if scope.get("is_public")}

    dest_type = str(destination.get("type") or "")
    if dest_type in _DM_DESTINATION_TYPES:
        member_scope_ids = set(store.memberships_for(organization_id, principal_id))
        return ScopeResolution(
            write_scope_id=room_scope_id,
            read_scope_ids={room_scope_id} | public_scope_ids | member_scope_ids,
        )
    if room_is_public:
        return ScopeResolution(
            write_scope_id=room_scope_id,
            read_scope_ids=public_scope_ids | {room_scope_id},
        )
    return ScopeResolution(
        write_scope_id=room_scope_id,
        read_scope_ids={room_scope_id} | public_scope_ids,
    )
