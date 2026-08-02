"""Normalize external webhook envelopes into Docket events."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any


def verify_slack_signature(*, signing_secret: str, timestamp: str, raw_body: bytes, signature: str, now: int | None = None, max_age_seconds: int = 300) -> bool:
    """Verify Slack's v0 HMAC signature and five-minute replay window."""
    try:
        age = abs((now if now is not None else int(datetime.now(timezone.utc).timestamp())) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > max_age_seconds:
        return False
    expected = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def normalize_slack_event(payload: dict[str, Any], *, organization_id: str, scope_id: str) -> dict[str, Any]:
    """Convert a Slack Events API ``event_callback`` envelope to a brain event."""
    if payload.get("type") != "event_callback" or not isinstance(payload.get("event"), dict):
        raise ValueError("Expected a Slack event_callback envelope")
    inner, event_id = payload["event"], payload.get("event_id")
    if not event_id:
        raise ValueError("Slack event_callback is missing event_id")
    team_id = payload.get("team_id", "unknown-team")
    return {"id": str(event_id), "source": f"slack:{team_id}", "type": f"slack.{inner.get('type', 'event')}", "subject": str(inner.get("channel") or inner.get("user") or team_id), "organization_id": organization_id, "principal_id": inner.get("user") or inner.get("bot_id"), "scope_id": scope_id, "trace_id": payload.get("event_context"), "data": {"text": inner.get("text", ""), "channel": inner.get("channel"), "subtype": inner.get("subtype"), "team_id": team_id, "raw_event": inner}}


def normalize_composio_trigger(payload: dict[str, Any], *, organization_id: str, scope_id: str) -> dict[str, Any]:
    """Convert Composio's V3 trigger delivery into a Docket event."""
    metadata, data = payload.get("metadata") or {}, payload.get("data") or {}
    event_id = payload.get("id") or metadata.get("log_id")
    if not event_id:
        raise ValueError("Composio trigger delivery is missing id")
    trigger = metadata.get("trigger_slug") or payload.get("type", "unknown_trigger")
    content = data.get("text") or data.get("message") or data.get("title") or json.dumps(data, sort_keys=True)
    return {"id": str(event_id), "source": f"composio:{metadata.get('connected_account_id', 'connection')}", "type": f"composio.{trigger.lower()}", "subject": str(metadata.get("trigger_id") or trigger), "organization_id": organization_id, "principal_id": metadata.get("user_id"), "scope_id": scope_id, "trace_id": metadata.get("log_id"), "data": {"content": content, "trigger": trigger, "metadata": metadata, **data}}


def composio_connected_account_id(payload: dict[str, Any]) -> str | None:
    """Extract the account ID from V3 and legacy Composio envelopes."""
    metadata = payload.get("metadata") or {}
    data = payload.get("data") or {}
    return (
        metadata.get("connected_account_id")
        or data.get("connected_account_id")
        or data.get("connection_id")
        or data.get("connection_nano_id")
        or payload.get("connected_account_id")
        or payload.get("connection_id")
    )


def normalize_composio_harness_event(
    payload: dict[str, Any],
    *,
    account: dict[str, Any],
    principal_id: str,
) -> dict[str, Any]:
    """Normalize an arbitrary authorized Composio trigger for the harness."""
    metadata = payload.get("metadata") or {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload.get("payload")
    if not isinstance(data, dict):
        data = {"value": data}
    event_id = payload.get("id") or metadata.get("log_id") or payload.get("log_id")
    if not event_id:
        raise ValueError("Composio trigger delivery is missing an event id")
    trigger_slug = (
        metadata.get("trigger_slug")
        or payload.get("trigger_name")
        or payload.get("type")
        or "unknown_trigger"
    )
    trigger_id = (
        metadata.get("trigger_id")
        or payload.get("trigger_id")
        or data.get("trigger_id")
        or trigger_slug
    )
    connected_account_id = composio_connected_account_id(payload)
    if connected_account_id != account.get("connected_account_id"):
        raise ValueError("Composio event does not match the resolved account")
    toolkit = str(account.get("toolkit") or "unknown").lower()
    preferred = [
        data.get("title"),
        data.get("subject"),
        data.get("message"),
        data.get("text"),
        data.get("body"),
        data.get("summary"),
        data.get("action"),
        data.get("status"),
        data.get("author"),
        data.get("url"),
    ]
    readable = "\n".join(str(value) for value in preferred if value)
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    content = readable
    if not content:
        content = serialized
    elif serialized not in content:
        content += "\nEvent data: " + serialized
    visibility = account.get("visibility") or "organization_public"
    scope_id = account.get("scope_id") or "scope_org_public"
    conversation_key = (
        f"composio:{connected_account_id}:{trigger_id}"
    )
    return {
        "id": str(event_id),
        "organization_id": account["organization_id"],
        "source": f"composio:{toolkit}",
        "type": f"composio.{str(trigger_slug).lower()}",
        "occurred_at": payload.get("timestamp")
        or datetime.now(timezone.utc).isoformat(),
        "actor": {
            "type": "connector",
            "id": principal_id,
            "source_id": metadata.get("user_id") or data.get("user_id"),
        },
        "scope": {
            "workspace_id": connected_account_id,
            "channel_id": account.get("destination_id") or toolkit,
            "thread_id": str(trigger_id),
            "visibility": visibility,
            "scope_id": scope_id,
        },
        "payload": {
            "text": content,
            "conversation_key": conversation_key,
            "trigger_slug": trigger_slug,
            "trigger_id": trigger_id,
            "connected_account_id": connected_account_id,
            "toolkit": toolkit,
            "observe_enabled": account.get("observe_enabled", True),
            "proactivity_enabled": account.get("proactivity_enabled", True),
            "learn": account.get("learn", True),
            "raw_data": data,
            "destination": account.get("destination"),
        },
        "dedupe_key": f"composio:{event_id}",
        "raw_event": payload,
    }
