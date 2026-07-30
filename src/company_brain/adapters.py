"""Normalize external webhook envelopes into Company Brain events."""

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
    return {"id": str(event_id), "source": f"slack:{team_id}", "type": f"slack.{inner.get('type', 'event')}", "subject": str(inner.get("channel") or inner.get("user") or team_id), "organization_id": organization_id, "principal_id": inner.get("user") or inner.get("bot_id"), "scope_id": scope_id, "trace_id": payload.get("event_context"), "data": {"text": inner.get("text", ""), "channel": inner.get("channel"), "thread_ts": inner.get("thread_ts") or inner.get("ts"), "subtype": inner.get("subtype"), "team_id": team_id, "raw_event": inner}}


def normalize_composio_trigger(payload: dict[str, Any], *, organization_id: str, scope_id: str) -> dict[str, Any]:
    """Convert Composio's V3 trigger delivery into a Company Brain event."""
    metadata, data = payload.get("metadata") or {}, payload.get("data") or {}
    event_id = payload.get("id") or metadata.get("log_id")
    if not event_id:
        raise ValueError("Composio trigger delivery is missing id")
    trigger = metadata.get("trigger_slug") or payload.get("type", "unknown_trigger")
    content = data.get("text") or data.get("message") or data.get("title") or json.dumps(data, sort_keys=True)
    return {"id": str(event_id), "source": f"composio:{metadata.get('connected_account_id', 'connection')}", "type": f"composio.{trigger.lower()}", "subject": str(metadata.get("trigger_id") or trigger), "organization_id": organization_id, "principal_id": metadata.get("user_id"), "scope_id": scope_id, "trace_id": metadata.get("log_id"), "data": {"content": content, "trigger": trigger, "metadata": metadata, **data}}
