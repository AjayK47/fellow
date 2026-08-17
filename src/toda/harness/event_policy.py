"""Explicit event-processing policy defaults.

Replaces the old implicit `learn = event.source != "slack"` heuristic
(memory ingestion/retrieval plan §2.1) with a named defaults table (plan
§4) that decides, for every inbound event, independently of any Task or
model call:

- whether it should wake the Session Manager at all (`wake_policy`), and
- how (or whether) it should turn into durable memory (`memory_policy`,
  `group_key`, `group_kind`).

A caller that already knows better than the default (a connector adapter
with real per-object semantics) can set these fields directly on the
`NormalizedEvent` it builds; `resolve()` only fills in what's still unset.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from toda.harness.models import NormalizedEvent

# Connector object-key extractors: source-specific because "what identifies
# the related conversation/object" only makes sense per toolkit. An entry
# here is what makes a group_kind other than "custom" possible; anything
# not listed (or that doesn't match) falls back to evidence_only rather
# than guessing at a group (plan §5 Phase C: "a missing or ambiguous group
# key should fall back to evidence-only processing").
def _repo_from_url(value: Any) -> str | None:
    """"https://api.github.com/repos/acme/export-worker" -> "acme/export-worker"."""
    text = str(value or "")
    marker = "/repos/"
    if marker not in text:
        return None
    slug = text.split(marker, 1)[1].strip("/")
    parts = slug.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def _github_object(data: dict[str, Any]) -> tuple[str, str] | None:
    # Composio/toolkit payload shapes are inconsistent about whether
    # `repository` is a nested object, already an "owner/repo" string, or --
    # as Composio's own GitHub triggers actually send -- absent entirely in
    # favour of `repository_url`. Only the first two were handled, so every
    # real Composio GitHub event returned None here, fell through to
    # `evidence_only`, and was never extracted into a memory. Measured
    # live: 1,229 pull-request events produced 4,421 searchable chunks and
    # zero facts, because this function could not name the object they all
    # belonged to.
    repository = data.get("repository")
    repo = (
        repository.get("full_name")
        if isinstance(repository, dict)
        else repository
        or _repo_from_url(data.get("repository_url"))
        or _repo_from_url(data.get("html_url"))
    )
    pr = data.get("pull_request")
    pr = pr if isinstance(pr, dict) else {}
    issue = data.get("issue")
    issue = issue if isinstance(issue, dict) else {}
    number = data.get("number") or pr.get("number") or issue.get("number")
    if not repo or not number:
        return None
    # Composio flattens the object rather than nesting it, so `pull_request`
    # is absent even for a PR event -- `pr_id` and the /pull/ URL are what
    # actually distinguish one from an issue.
    is_pr = bool(
        pr
        or data.get("pr_id")
        or "/pull/" in str(data.get("html_url") or "")
    )
    kind = "github_pr" if is_pr else "github_issue"
    return f"{kind}:{repo}#{number}", kind


def _linear_object(data: dict[str, Any]) -> tuple[str, str] | None:
    raw_issue = data.get("issue")
    issue = raw_issue if isinstance(raw_issue, dict) else data
    identifier = issue.get("identifier") or issue.get("id")
    if not identifier or not isinstance(identifier, str):
        return None
    return f"linear_issue:{identifier}", "linear_issue"


_CONNECTOR_OBJECT_KEYS = {
    "github": _github_object,
    "linear": _linear_object,
}

# Events that represent a real, self-contained problem — not routine
# connector chatter — get immediate extraction and at least attention-level
# wake, matching the plan's "security, billing or failure alert" row.
_ALERT_TRIGGER_HINTS = ("alert", "failure", "failed", "incident", "security", "billing", "payment")

# How recent a connector event must be to be worth a model call.
#
# A poll trigger's first run has seen nothing, so every object in the
# account's history arrives at once as "new". Measured live: enabling one
# GitHub PR trigger produced 6,525 events in under an hour, each one routed
# to the attention classifier at ~5.8s a call, to decide 6,000 times not to
# interrupt anyone. Nothing about a pull request from 2023 is news.
#
# Older events are still persisted, chunked, embedded and searchable -- only
# the model call is skipped. Deliberately age-based rather than
# connector-based: it bounds every backfill, including ones from connectors
# that do not exist yet, while a genuinely live event still gets attention.
_CONNECTOR_ATTENTION_MAX_AGE_SECONDS = 6 * 60 * 60


class EventPolicy:
    __slots__ = ("wake_policy", "memory_policy", "group_key", "group_kind")

    def __init__(
        self,
        *,
        wake_policy: str,
        memory_policy: str,
        group_key: str | None,
        group_kind: str | None,
    ) -> None:
        self.wake_policy = wake_policy
        self.memory_policy = memory_policy
        self.group_key = group_key
        self.group_kind = group_kind


def _is_historical(event: NormalizedEvent) -> bool:
    """True when this event is old enough to be history rather than news."""
    occurred = event.occurred_at
    if occurred is None:
        return False
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - occurred).total_seconds()
    return age > _CONNECTOR_ATTENTION_MAX_AGE_SECONDS


def resolve_event_policy(event: NormalizedEvent) -> EventPolicy:
    # Respect an explicitly pre-set policy in full — a caller opting into
    # this contract already made the call, no need for a second opinion.
    if event.wake_policy and event.memory_policy:
        return EventPolicy(
            wake_policy=event.wake_policy,
            memory_policy=event.memory_policy,
            group_key=event.group_key,
            group_kind=event.group_kind,
        )

    if event.is_direct_agent_request:
        # A direct DM/mention is already headed for a Task; the completed
        # Task is what gets captured as memory (memory_capture.capture_task),
        # not the raw inbound message — this evidence row exists for
        # citation/search, not for a duplicate episode extraction pass.
        return EventPolicy(
            wake_policy="always",
            memory_policy=event.memory_policy or "evidence_only",
            group_key=event.group_key or _slack_group_key(event),
            group_kind=event.group_kind or "slack_thread",
        )

    if event.source == "slack":
        return EventPolicy(
            wake_policy="attention",
            memory_policy="episode",
            group_key=_slack_group_key(event),
            group_kind="slack_thread",
        )

    toolkit, raw_data = _composio_toolkit_and_data(event)
    if toolkit:
        extractor = _CONNECTOR_OBJECT_KEYS.get(toolkit)
        object_key = extractor(raw_data) if extractor else None
        trigger_slug = str(event.payload.get("trigger_slug") or "").lower()
        is_alert = event.urgency == "urgent" or any(hint in trigger_slug for hint in _ALERT_TRIGGER_HINTS)
        if is_alert:
            return EventPolicy(
                wake_policy="always" if event.urgency == "urgent" else "attention",
                memory_policy="immediate",
                group_key=object_key[0] if object_key else f"alert:{toolkit}:{event.id}",
                group_kind=object_key[1] if object_key else "custom",
            )
        if object_key:
            group_key, group_kind = object_key
            return EventPolicy(
                wake_policy="never" if _is_historical(event) else "attention",
                memory_policy="episode",
                group_key=group_key,
                group_kind=group_kind,
            )
        # No stable per-object identifier for this toolkit/payload shape —
        # stay evidence-only rather than batching unrelated events together.
        return EventPolicy(
            wake_policy="never" if _is_historical(event) else "attention",
            memory_policy="evidence_only",
            group_key=None,
            group_kind=None,
        )

    # Unknown/background connector noise: don't wake, don't extract, but the
    # canonical evidence row is still written and searchable.
    return EventPolicy(wake_policy="never", memory_policy="evidence_only", group_key=None, group_kind=None)


def _slack_group_key(event: NormalizedEvent) -> str:
    return (
        event.payload.get("context_key")
        or event.payload.get("conversation_key")
        or f"slack:{event.scope.channel_id or event.scope.workspace_id or event.organization_id}"
    )


def _composio_toolkit_and_data(event: NormalizedEvent) -> tuple[str | None, dict[str, Any]]:
    if not event.source.startswith("composio:"):
        return None, {}
    toolkit = str(event.payload.get("toolkit") or event.source.split(":", 1)[1] or "").lower()
    raw_data = event.payload.get("raw_data")
    return toolkit, raw_data if isinstance(raw_data, dict) else {}
