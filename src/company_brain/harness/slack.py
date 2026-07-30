"""Slack Events API gateway, normalizer, activation policy, and sender."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from company_brain.config import Settings
from company_brain.domain.models import new_id, utcnow
from company_brain.harness.models import EventActor, EventScope, NormalizedEvent
from company_brain.storage.db import BrainStore


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    tolerance_seconds: int = 300,
) -> bool:
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - request_time) > tolerance_seconds:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode(), base, digestmod=hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature or "")


class SlackGateway:
    def __init__(
        self,
        *,
        store: BrainStore,
        queue: Any,
        graph: Any,
        config: Settings,
        audit: Any,
    ) -> None:
        self.store = store
        self.queue = queue
        self.graph = graph
        self.config = config
        self.audit = audit

    def accept(
        self,
        *,
        body: bytes,
        headers: dict[str, str],
        allow_unsigned: bool = False,
    ) -> dict[str, Any]:
        if not allow_unsigned:
            if not self.config.slack_signing_secret:
                raise RuntimeError("Slack signing secret is not configured")
            if not verify_slack_signature(
                signing_secret=self.config.slack_signing_secret,
                timestamp=headers.get("x-slack-request-timestamp", ""),
                signature=headers.get("x-slack-signature", ""),
                body=body,
                tolerance_seconds=self.config.slack_signature_tolerance_seconds,
            ):
                raise PermissionError("Invalid Slack request signature")
        payload = json.loads(body.decode("utf-8"))
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if payload.get("type") != "event_callback":
            return {"accepted": False, "reason": "unsupported_slack_payload"}
        inner = payload.get("event") or {}
        if self._ignore_event(inner, str(payload.get("team_id") or "")):
            return {"accepted": False, "reason": "ignored_event"}
        event = self.normalize(payload)
        if not event.payload.get("channel_enabled", True):
            return {
                "accepted": False,
                "reason": "channel_disabled",
                "event_id": event.id,
            }
        received = {
            "id": event.id,
            "organization_id": event.organization_id,
            "type": event.type,
            "source": "slack",
            "received_at": utcnow().isoformat(),
            "data": event.model_dump(mode="json"),
        }
        if not self.store.upsert_received_event(received):
            return {"accepted": False, "duplicate": True, "event_id": event.id}
        job = self.queue.enqueue(
            organization_id=event.organization_id,
            kind="event.route",
            payload={"event": event.model_dump(mode="json")},
            idempotency_key=f"event.route:{event.dedupe_key}",
        )
        self.audit.record(
            event.organization_id,
            "slack.event.accepted",
            {
                "event_id": event.id,
                "job_id": job["id"],
                "type": event.type,
                "channel_id": event.scope.channel_id,
                "direct": event.is_direct_agent_request,
            },
            principal_id=event.actor.id,
        )
        return {
            "accepted": True,
            "event_id": event.id,
            "job_id": job["id"],
        }

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        inner = payload.get("event") or {}
        workspace_id = str(payload.get("team_id") or inner.get("team") or "")
        organization_id = self._organization_for_workspace(workspace_id)
        channel_id = str(inner.get("channel") or "")
        channel_type = str(inner.get("channel_type") or "")
        is_dm = channel_type == "im" or channel_id.startswith("D")
        is_group_dm = channel_type == "mpim" or channel_id.startswith("G")
        ts = str(inner.get("ts") or inner.get("event_ts") or "")
        thread_id = str(inner.get("thread_ts") or ts)
        source_user_id = str(inner.get("user") or "")
        principal_id = self._principal_for_user(
            organization_id, workspace_id, source_user_id
        )
        text = str(inner.get("text") or "")
        bot_user_id = self._bot_user_id(workspace_id)
        bot_mentioned = inner.get("type") == "app_mention" or bool(
            bot_user_id and f"<@{bot_user_id}>" in text
        )
        policy = self._channel_policy(organization_id, channel_id, is_dm)
        conversation_key = f"slack:{workspace_id}:{channel_id}:{thread_id}"
        existing_session = self.store.session_by_conversation(
            organization_id, conversation_key
        )
        addressed_thread = bool(existing_session)
        free_response = channel_id in self._csv_set(
            self.config.slack_free_response_channels
        ) or bool(policy.get("free_response"))
        direct = bool(
            is_dm
            or bot_mentioned
            or free_response
            or (
                addressed_thread
                and not self.config.slack_thread_require_mention
                and not policy.get("thread_require_mention", False)
            )
        )
        if is_group_dm and not bot_mentioned and not free_response:
            direct = addressed_thread and not self.config.slack_thread_require_mention
        visibility = (
            "person_private"
            if is_dm
            else "private_channel"
            if bool(inner.get("is_private")) or policy.get("is_private")
            else "organization_public"
        )
        scope_id = str(
            policy.get("scope_id")
            or (
                f"scope_person_{principal_id}"
                if is_dm
                else f"scope_slack_{workspace_id}_{channel_id}"
            )
        )
        self._ensure_scope(
            organization_id=organization_id,
            scope_id=scope_id,
            channel_id=channel_id,
            principal_id=principal_id,
            visibility=visibility,
        )
        event_id = str(payload.get("event_id") or f"slack_{workspace_id}_{ts}")
        event_type = (
            "slack.dm"
            if is_dm
            else "slack.app_mention"
            if bot_mentioned
            else "slack.channel_message"
        )
        return NormalizedEvent(
            id=event_id,
            organization_id=organization_id,
            source="slack",
            type=event_type,
            occurred_at=utcnow(),
            actor=EventActor(
                id=principal_id, source_id=source_user_id, type="person"
            ),
            scope=EventScope(
                workspace_id=workspace_id,
                channel_id=channel_id,
                thread_id=thread_id,
                visibility=visibility,
                scope_id=scope_id,
            ),
            payload={
                "text": self._strip_bot_mention(text, bot_user_id),
                "raw_text": text,
                "message_ts": ts,
                "thread_ts": inner.get("thread_ts"),
                "channel_type": channel_type,
                "conversation_key": conversation_key,
                "channel_enabled": bool(policy.get("enabled", True)),
                "observe_enabled": bool(policy.get("observe", True)),
                "proactivity_enabled": self._proactivity_enabled(
                    channel_id, policy
                ),
            },
            dedupe_key=f"slack:{workspace_id}:{event_id}:{ts}",
            is_direct_agent_request=direct,
            belongs_to_waiting_run=addressed_thread and direct,
            bot_mentioned=bot_mentioned,
            raw_event=payload,
        )

    def _organization_for_workspace(self, workspace_id: str) -> str:
        installation = self.store.slack_installation_by_workspace(workspace_id)
        if installation:
            return installation["organization_id"]
        return self.config.default_org_id

    def _principal_for_user(
        self, organization_id: str, workspace_id: str, source_user_id: str
    ) -> str:
        mapping = self.store.identity_mapping(
            organization_id, "slack", source_user_id
        )
        if mapping:
            return mapping["principal_id"]
        principal_id = f"person_slack_{workspace_id}_{source_user_id}".lower()
        if source_user_id and not self.store.get_entity(principal_id):
            self.graph.upsert_entity(
                {
                    "id": principal_id,
                    "organization_id": organization_id,
                    "entity_type": "person",
                    "name": source_user_id,
                    "aliases": [],
                    "attributes": {
                        "slack_user_id": source_user_id,
                        "workspace_id": workspace_id,
                    },
                }
            )
        mapping = {
            "id": new_id("identity"),
            "organization_id": organization_id,
            "source": "slack",
            "source_user_id": source_user_id,
            "principal_id": principal_id,
        }
        self.store.upsert_simple_harness_record("identity_mappings", mapping)
        return principal_id

    def _channel_policy(
        self, organization_id: str, channel_id: str, is_dm: bool
    ) -> dict[str, Any]:
        policy = self.store.channel_policy(organization_id, "slack", channel_id)
        if policy:
            return policy
        allowed = self._csv_set(self.config.slack_allowed_channels)
        return {
            "enabled": is_dm or not allowed or channel_id in allowed,
            "observe": True,
            "proactive": channel_id
            in self._csv_set(self.config.slack_proactive_channels),
            "free_response": False,
            "is_private": is_dm,
        }

    def _proactivity_enabled(
        self, channel_id: str, policy: dict[str, Any]
    ) -> bool:
        return bool(
            policy.get("proactive")
            or channel_id in self._csv_set(self.config.slack_proactive_channels)
        )

    def _ensure_scope(
        self,
        *,
        organization_id: str,
        scope_id: str,
        channel_id: str,
        principal_id: str,
        visibility: str,
    ) -> None:
        if not self.store.get_scope(scope_id):
            is_public = visibility == "organization_public"
            self.store.upsert_scope(
                {
                    "id": scope_id,
                    "organization_id": organization_id,
                    "kind": visibility,
                    "label": f"Slack {channel_id}",
                    "channel_id": channel_id,
                    "principal_ids": [] if is_public else [principal_id],
                    "is_public": is_public,
                }
            )
        self.store.add_membership(organization_id, principal_id, scope_id)

    def _ignore_event(
        self, event: dict[str, Any], workspace_id: str = ""
    ) -> bool:
        if event.get("type") not in {"message", "app_mention"}:
            return True
        if event.get("subtype") in {
            "message_deleted",
            "message_changed",
            "channel_join",
            "channel_leave",
        }:
            return True
        if event.get("bot_id"):
            return True
        bot_user_id = self._bot_user_id(workspace_id)
        if bot_user_id and event.get("user") == bot_user_id:
            return True
        return False

    def _strip_bot_mention(self, text: str, bot_user_id: str | None) -> str:
        if bot_user_id:
            return text.replace(f"<@{bot_user_id}>", "").strip()
        return text.strip()

    def _bot_user_id(self, workspace_id: str) -> str | None:
        installation = self.store.slack_installation_by_workspace(workspace_id)
        return (
            (installation or {}).get("bot_user_id")
            or self.config.slack_bot_user_id
        )

    @staticmethod
    def _csv_set(raw: str) -> set[str]:
        return {value.strip() for value in (raw or "").split(",") if value.strip()}


class SlackSender:
    def __init__(self, *, bot_token: str | None) -> None:
        self.bot_token = bot_token

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def send(
        self,
        *,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.bot_token:
            return {
                "ok": False,
                "dry_run": True,
                "channel": channel_id,
                "thread_ts": thread_id,
                "text": text,
                "blocks": blocks or [],
            }
        body: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_id:
            body["thread_ts"] = thread_id
        if blocks:
            body["blocks"] = blocks
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {self.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack delivery failed: {payload.get('error')}")
        return payload
