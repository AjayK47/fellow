"""Slack Events API gateway, normalizer, activation policy, and sender."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx

from docket.config import Settings
from docket.domain.models import new_id, utcnow
from docket.harness.models import EventActor, EventScope, NormalizedEvent
from docket.storage.db import BrainStore

# Backstop for the prompt-level rule in prompts.py (STABLE_SYSTEM_PROMPT):
# a live test showed the model still emitting :droplet: on a water-reminder
# automation despite that instruction being in its system prompt at the
# time -- prompt adherence alone isn't reliable enough for a hard rule, so
# this scrubs the literal characters/shortcodes out of every outbound Slack
# text deterministically, regardless of what the model actually wrote. Not
# exhaustive by design: these are the specific emoji already named as a
# hard block; anything broader stays a model-judgment call per the prompt.
_BLOCKED_EMOJI_CHARS = ("💧", "💦", "🍆", "🍑", "👅", "🌭", "🍌")
_BLOCKED_EMOJI_SHORTCODES = (
    ":droplet:",
    ":sweat_drops:",
    ":eggplant:",
    ":peach:",
    ":tongue:",
    ":hotdog:",
    ":hot_dog:",
    ":banana:",
)


def _scrub_blocked_emoji(text: str) -> str:
    if not text:
        return text
    scrubbed = text
    for token in (*_BLOCKED_EMOJI_CHARS, *_BLOCKED_EMOJI_SHORTCODES):
        scrubbed = scrubbed.replace(token, "")
    if scrubbed == text:
        # Nothing was removed: return the message byte-for-byte untouched.
        return text
    # Removing an inline emoji leaves a doubled space right where it sat
    # (e.g. "break, ajay  — grab"), so close that gap -- but ONLY runs of
    # spaces/tabs. An earlier version collapsed with " ".join(text.split()),
    # which also destroyed every newline in every outbound message:
    # paragraph breaks, bullet lists and code blocks in ordinary replies
    # were being flattened into one long line, whether or not the message
    # contained an emoji at all.
    scrubbed = re.sub(r"[ \t]{2,}", " ", scrubbed)
    scrubbed = re.sub(r"[ \t]+\n", "\n", scrubbed)
    return scrubbed.strip()


def _scrub_blocks(blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Recursively scrub every string value in a Block Kit payload, not
    just the top-level `text` argument -- section/field text and button
    labels can carry the same emoji."""
    if blocks is None:
        return None

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return _scrub_blocked_emoji(node)
        if isinstance(node, dict):
            return {key: _walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(blocks)


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


class UnauthorizedApprover(Exception):
    """Raised when a Slack user who is not an allowed approver tries to decide an approval."""


def _profile_attributes(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        key: profile[key]
        for key in ("tz", "tz_label", "title")
        if profile.get(key)
    }


def _update_user_entity(graph: Any, principal_id: str, profile: dict[str, Any]) -> None:
    if not profile:
        return
    entity = graph.get_entity(principal_id)
    if not entity:
        return
    name = profile.get("display_name") or profile.get("real_name") or profile.get("name")
    if name:
        entity["name"] = name
    entity["aliases"] = list(dict.fromkeys([*(entity.get("aliases") or []), profile.get("name") or ""]))
    entity["aliases"] = [alias for alias in entity["aliases"] if alias]
    entity["attributes"] = {
        **(entity.get("attributes") or {}),
        **_profile_attributes(profile),
    }
    graph.upsert_entity(entity)


def resolve_slack_principal(
    store: BrainStore,
    graph: Any,
    organization_id: str,
    workspace_id: str,
    source_user_id: str,
    *,
    user_profile: dict[str, Any] | None = None,
) -> str:
    """Map a Slack user in a workspace to a stable principal_id, creating the
    identity mapping and graph entity on first sight.

    Shared by SlackGateway's message-ingestion path and the console's "Sign
    in with Slack" login route (api/routes.py) — the latter has no
    SlackGateway instance of its own, only brain.store/brain.graph, so this
    lives as a plain function rather than an instance method to avoid the
    two call sites drifting apart.
    """
    mapping = store.identity_mapping(organization_id, "slack", source_user_id)
    if mapping:
        _update_user_entity(graph, mapping["principal_id"], user_profile or {})
        return mapping["principal_id"]
    principal_id = f"person_slack_{workspace_id}_{source_user_id}".lower()
    if source_user_id and not store.get_entity(principal_id):
        graph.upsert_entity(
            {
                "id": principal_id,
                "organization_id": organization_id,
                "entity_type": "person",
                "name": (user_profile or {}).get("display_name")
                or (user_profile or {}).get("real_name")
                or (user_profile or {}).get("name")
                or source_user_id,
                "aliases": [source_user_id],
                "attributes": {
                    "slack_user_id": source_user_id,
                    "workspace_id": workspace_id,
                    **_profile_attributes(user_profile or {}),
                },
            }
        )
    else:
        _update_user_entity(graph, principal_id, user_profile or {})
    mapping = {
        "id": new_id("identity"),
        "organization_id": organization_id,
        "source": "slack",
        "source_user_id": source_user_id,
        "principal_id": principal_id,
    }
    store.upsert_simple_harness_record("identity_mappings", mapping)
    return principal_id


class SlackGateway:
    def __init__(
        self,
        *,
        store: BrainStore,
        queue: Any,
        graph: Any,
        config: Settings,
        audit: Any,
        sessions: Any,
    ) -> None:
        self.store = store
        self.queue = queue
        self.graph = graph
        self.config = config
        self.audit = audit
        self.sessions = sessions
        self._user_profile_cache: dict[str, dict[str, Any]] = {}
        self._channel_privacy_cache: dict[str, bool] = {}

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
            # Slack may emit both `message` and `app_mention` callbacks for
            # one mentioned message. Their event_ids differ, so dedupe on the
            # normalized underlying message identity instead.
            "id": event.dedupe_key,
            "organization_id": event.organization_id,
            "type": event.type,
            "source": "slack",
            "received_at": utcnow().isoformat(),
            "provider_event_id": event.id,
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

    def accept_interaction(
        self,
        *,
        body: bytes,
        headers: dict[str, str] | None = None,
        allow_unsigned: bool = False,
    ) -> dict[str, Any]:
        """Handle a Slack interactive payload (approval button click).

        Shared by both transports: the HTTP `/v1/slack/interactions` route
        passes the raw form-encoded body with signature headers; Socket Mode
        passes the interactive envelope's JSON body with `allow_unsigned=True`
        since the socket connection itself is already authenticated.
        """
        headers = headers or {}
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
            parsed = parse_qs(body.decode("utf-8"))
            payload = json.loads((parsed.get("payload") or ["{}"])[0])
        else:
            # Socket Mode delivers the interaction payload as JSON directly —
            # no HTTP form-encoding involved.
            payload = json.loads(body.decode("utf-8"))
        return self._resolve_interaction(payload)

    def _resolve_interaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        actions = payload.get("actions") or []
        if not actions:
            raise ValueError("Interaction has no action")
        action = actions[0]
        action_id = action.get("action_id")
        if action_id in {"docket_approve", "docket_deny"}:
            return self._resolve_approval_interaction(payload, action)
        if action_id in {"docket_choice", "docket_choice_select"}:
            return self._resolve_choice_interaction(payload, action)
        raise ValueError("Unsupported interaction")

    def _resolve_approval_interaction(
        self, payload: dict[str, Any], action: dict[str, Any]
    ) -> dict[str, Any]:
        approval_id = action.get("value")
        approval = self.store.get_harness_record("approvals", approval_id)
        if not approval:
            raise KeyError("approval not found")
        user_id = str((payload.get("user") or {}).get("id") or "unknown")
        mapping = self.store.identity_mapping(
            approval["organization_id"], "slack", user_id
        )
        decided_by = mapping["principal_id"] if mapping else f"slack:{user_id}"
        if decided_by not in set(
            approval.get("allowed_approver_ids") or [approval.get("requested_by")]
        ):
            raise UnauthorizedApprover("Slack user cannot decide this approval")
        job = self.queue.enqueue(
            organization_id=approval["organization_id"],
            kind="approval.resolve",
            payload={
                "approval_id": approval_id,
                "approved": action["action_id"] == "docket_approve",
                "decided_by": decided_by,
            },
            idempotency_key=f"approval.slack:{approval_id}:{action['action_id']}",
        )
        return {"accepted": True, "job_id": job["id"]}

    def _resolve_choice_interaction(
        self, payload: dict[str, Any], action: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve a request_human_input(choices=...) button/select click.

        The interactive element's value only ever carries `task_id::index` —
        Slack caps option/button values at 75 chars, far shorter than a
        choice's actual text could be — so the real choice text is looked up
        from the Task's own pending_question_choices, set when the question
        was asked (harness/runtime.py), rather than round-tripped through
        Slack.
        """
        if action.get("type") == "static_select":
            value = (action.get("selected_option") or {}).get("value")
        else:
            value = action.get("value")
        if not value or "::" not in value:
            raise ValueError("Malformed choice interaction")
        task_id, _, index_str = value.partition("::")
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if task.get("state") != "waiting_for_human":
            return {"accepted": False, "reason": "Task is no longer waiting for a response"}
        choices = task.get("pending_question_choices") or []
        try:
            choice_text = choices[int(index_str)]
        except (ValueError, IndexError):
            raise ValueError("Choice index out of range")
        user_id = str((payload.get("user") or {}).get("id") or "unknown")
        mapping = self.store.identity_mapping(task["organization_id"], "slack", user_id)
        principal_id = mapping["principal_id"] if mapping else f"slack:{user_id}"
        self.store.append_session_message(
            {
                "id": new_id("smsg"),
                "organization_id": task["organization_id"],
                "session_id": task["session_id"],
                "task_id": task_id,
                "role": "user",
                "content": choice_text,
                "tool_call_id": None,
                "metadata": {"principal_id": principal_id, "continuation": True},
                "created_at": utcnow().isoformat(),
            }
        )
        # Appending the continuation is also a Session wake. Without the
        # revision bump below, the durable dispatch job can claim an idle
        # Session but the Manager has no new revision to process, leaving the
        # Task stuck in waiting_for_human.
        # notify() both advances the durable Session revision and enqueues its
        # one dispatch job. Enqueuing a second key here let two queue workers
        # race the same Manager; the loser correctly reported it busy, but
        # made an otherwise successful choice interaction look failed.
        self.sessions.notify(task["session_id"], reason="slack_choice")
        return {"accepted": True}

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        inner = payload.get("event") or {}
        workspace_id = str(payload.get("team_id") or inner.get("team") or "")
        organization_id = self._organization_for_workspace(workspace_id)
        channel_id = str(inner.get("channel") or "")
        channel_type = str(inner.get("channel_type") or "")
        is_dm = (
            channel_type in {"im", "mpim"}
            or channel_id.startswith("D")
            or channel_id.startswith("G")
        )
        ts = str(inner.get("ts") or inner.get("event_ts") or "")
        source_user_id = str(inner.get("user") or "")
        user_profile = self._fetch_user_profile(workspace_id, source_user_id)
        principal_id = self._principal_for_user(
            organization_id, workspace_id, source_user_id, user_profile=user_profile
        )
        text = str(inner.get("text") or "")
        bot_user_id = self._bot_user_id(workspace_id)
        bot_mentioned = inner.get("type") == "app_mention" or bool(
            bot_user_id and f"<@{bot_user_id}>" in text
        )
        policy = self._channel_policy(organization_id, channel_id, is_dm)
        # One Session per room, channels included. Context belongs to the
        # conversation, not to whoever happened to send a message: two people
        # raising the same issue in a channel must reach the same Session so
        # the Manager can fold the second message into the work already
        # running, rather than silently starting a duplicate investigation.
        #
        # Channels previously forked a Session per addressed message
        # (`{context_key}:event:{message_identity}`), which isolated the
        # transcript, the running summary, and -- most visibly -- steering:
        # SessionService._steer_active_tasks() resolves the root Task by
        # Session id, so an addressed "stop" landed in a brand-new empty
        # Session and cancelled nothing. It also meant mentioning Docket gave
        # LESS continuity than not mentioning it, since ambient messages
        # attach to conversation_state.active_session_id (channel-wide) while
        # addressed ones never consulted it.
        #
        # Arbitration between unrelated concurrent requests belongs to the
        # Session Manager, which has start_parallel_task for exactly that.
        context_key = f"slack:{workspace_id}:{channel_id}"
        conversation_key = context_key
        existing_session = self.store.find_session_by_conversation(
            organization_id, conversation_key
        )
        free_response = channel_id in self._csv_set(
            self.config.slack_free_response_channels
        ) or bool(policy.get("free_response"))
        direct = bool(
            is_dm
            or bot_mentioned
            or free_response
        )
        # CRITICAL -- this decides whether a channel's evidence lands in a
        # private scope or an organization-public one, and getting it wrong
        # discloses a private channel to the whole workspace.
        #
        # `inner.get("is_private")` is NOT a field Slack sends on a message
        # event. It only exists on a conversations.info channel object, so it
        # was always None here and the check was dead. With no channel_policy
        # row either (_channel_policy defaults `is_private` to `is_dm`, i.e.
        # False for a channel), every private channel fell through to
        # "organization_public". Confirmed live: a private channel's salary
        # discussion was readable by a workspace member who was never in it.
        #
        # The authoritative signal is already in the event: Slack sets
        # channel_type="group" for a private channel ("channel" for a public
        # one, "im"/"mpim" for DMs). The other two inputs are kept as
        # overrides for an explicit operator policy.
        #
        # But channel_type is NOT universal: `message` events carry it,
        # `app_mention` events do not, and _ignore_event() accepts both. Since
        # Slack emits both for one mentioned message and they deduplicate
        # against each other, an app_mention can legitimately be the copy that
        # gets normalized. Treating "no channel_type" as "not a group" would
        # reopen exactly the hole above, so an unknown channel type is never
        # allowed to imply public: it inherits whatever this channel's scope
        # already records, and failing that, defaults closed to private.
        # A stored channel policy is an operator's explicit declaration and is
        # authoritative in BOTH directions -- it is how a deployment that
        # cannot reach the Slack API (no bot token) still states which rooms
        # are private, instead of depending on the fallback guess below.
        stored_policy = self.store.channel_policy(
            organization_id, "slack", channel_id
        )
        policy_declares_privacy = (
            stored_policy is not None and stored_policy.get("is_private") is not None
        )
        explicitly_private = bool(inner.get("is_private")) or bool(
            policy.get("is_private")
        )
        channel_type_known = channel_type in {"channel", "group"}
        resolved_private: bool | None = None
        if (
            not is_dm
            and not explicitly_private
            and not channel_type_known
            and not policy_declares_privacy
        ):
            # No channel_type on this event (an app_mention). Ask Slack rather
            # than guess -- see _fetch_channel_is_private for why guessing is
            # unsafe in both directions.
            resolved_private = self._fetch_channel_is_private(
                workspace_id, channel_id
            )

        if is_dm:
            visibility = "person_private"
        elif explicitly_private or channel_type == "group" or resolved_private:
            visibility = "private_channel"
        elif (
            channel_type == "channel"
            or resolved_private is False
            or policy_declares_privacy
        ):
            visibility = "organization_public"
        else:
            # Slack could not be reached and the event carries no type. Keep
            # whatever this channel's scope already records; with nothing on
            # record, stay private -- a room that never delivers is a visible
            # failure, a room that leaks is a silent one.
            known_scope = self.store.get_scope(
                f"scope_slack_{workspace_id}_{channel_id}"
            )
            visibility = (
                "organization_public"
                if known_scope and bool(known_scope.get("is_public"))
                else "private_channel"
            )
        # Only a real signal -- Slack's event, Slack's API, or an operator
        # policy -- may widen a scope. An inherited or defaulted guess never
        # can.
        privacy_authoritative = (
            channel_type_known
            or explicitly_private
            or policy_declares_privacy
            or resolved_private is not None
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
            authoritative=privacy_authoritative,
        )
        event_id = str(payload.get("event_id") or f"slack_{workspace_id}_{ts}")
        event_type = (
            "slack.dm"
            if is_dm
            else "slack.app_mention"
            if bot_mentioned
            else "slack.channel_message"
        )
        # Still needed for dedupe_key: Slack emits both `message` and
        # `app_mention` callbacks for one mentioned message, with different
        # event ids, so deduplication keys off the underlying message.
        client_message_id = str(inner.get("client_msg_id") or "")
        message_identity = client_message_id or f"{channel_id}:{ts}:{source_user_id}"
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
                visibility=visibility,
                scope_id=scope_id,
            ),
            payload={
                "text": self._strip_bot_mention(text, bot_user_id),
                "raw_text": text,
                "actor_profile": {
                    "principal_id": principal_id,
                    "source_user_id": source_user_id,
                    "display_name": user_profile.get("display_name")
                    or user_profile.get("real_name")
                    or user_profile.get("name")
                    or source_user_id,
                    "real_name": user_profile.get("real_name"),
                    "timezone": user_profile.get("tz"),
                    "timezone_label": user_profile.get("tz_label"),
                },
                "message_ts": ts,
                "channel_type": channel_type,
                "conversation_key": conversation_key,
                "context_key": context_key,
                "channel_enabled": bool(policy.get("enabled", True)),
                "observe_enabled": bool(policy.get("observe", True)),
                "proactivity_enabled": self._proactivity_enabled(
                    channel_id, policy
                ),
            },
            dedupe_key=f"slack:{workspace_id}:message:{message_identity}",
            is_direct_agent_request=direct,
            belongs_to_waiting_run=bool(existing_session) and direct,
            bot_mentioned=bot_mentioned,
            raw_event=payload,
        )

    def _organization_for_workspace(self, workspace_id: str) -> str:
        installation = self.store.slack_installation_by_workspace(workspace_id)
        if installation:
            return installation["organization_id"]
        return self.config.default_org_id

    def _principal_for_user(
        self,
        organization_id: str,
        workspace_id: str,
        source_user_id: str,
        *,
        user_profile: dict[str, Any] | None = None,
    ) -> str:
        return resolve_slack_principal(
            self.store,
            self.graph,
            organization_id,
            workspace_id,
            source_user_id,
            user_profile=user_profile,
        )

    def _fetch_user_profile(self, workspace_id: str, source_user_id: str) -> dict[str, Any]:
        """Resolve a Slack display profile once per event when configured.

        Slack events carry only the user ID. Failure to enrich must never block
        ingestion, so the ID remains the safe fallback.
        """
        installation = self.store.slack_installation_by_workspace(workspace_id)
        token = (installation or {}).get("bot_token") or self.config.slack_bot_token
        if not source_user_id or not token:
            return {}
        cache_key = f"{workspace_id}:{source_user_id}"
        if cache_key in self._user_profile_cache:
            return self._user_profile_cache[cache_key]
        try:
            response = httpx.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {token}"},
                params={"user": source_user_id},
                timeout=5.0,
            )
            response.raise_for_status()
            payload = response.json()
            profile = dict(payload.get("user") or {}) if payload.get("ok") else {}
            self._user_profile_cache[cache_key] = profile
            return profile
        except Exception:
            self._user_profile_cache[cache_key] = {}
            return {}

    def _fetch_channel_is_private(
        self, workspace_id: str, channel_id: str
    ) -> bool | None:
        """Ask Slack whether a channel is private. None means "could not tell".

        Needed because `channel_type` is only on `message` events, not on
        `app_mention` -- and both are accepted, deduplicate against each
        other, and can arrive in either order. Guessing from the absence of
        the field is not safe in either direction: assume public and a
        private room leaks; assume private and an ordinary public channel
        stops delivering. conversations.info answers it definitively.

        Cached per channel for the process lifetime, so this costs one call
        per channel rather than one per event, and never blocks ingestion.
        """
        installation = self.store.slack_installation_by_workspace(workspace_id)
        token = (installation or {}).get("bot_token") or self.config.slack_bot_token
        if not channel_id or not token:
            return None
        cache_key = f"{workspace_id}:{channel_id}"
        if cache_key in self._channel_privacy_cache:
            return self._channel_privacy_cache[cache_key]
        try:
            response = httpx.get(
                "https://slack.com/api/conversations.info",
                headers={"Authorization": f"Bearer {token}"},
                params={"channel": channel_id},
                timeout=5.0,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                return None
            channel = dict(payload.get("channel") or {})
            is_private = bool(channel.get("is_private")) or bool(
                channel.get("is_group")
            )
            self._channel_privacy_cache[cache_key] = is_private
            return is_private
        except Exception:
            # Never cache a failure: a transient network error must not pin a
            # channel's privacy for the rest of the process.
            return None

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
            # A company agent should be attentive wherever it is installed.
            # Persisted channel policy is the explicit opt-out mechanism.
            "proactive": True,
            "free_response": False,
            "is_private": is_dm,
        }

    def _proactivity_enabled(
        self, channel_id: str, policy: dict[str, Any]
    ) -> bool:
        return bool(policy.get("proactive", True))

    def _ensure_scope(
        self,
        *,
        organization_id: str,
        scope_id: str,
        channel_id: str,
        principal_id: str,
        visibility: str,
        authoritative: bool = True,
    ) -> None:
        """`authoritative` means Slack actually told us this channel's type
        (or an operator set a policy), rather than the value being an
        inherited or fail-closed guess. Only an authoritative signal may widen
        a scope; a guess can narrow but never disclose."""
        is_public = visibility == "organization_public"
        # A scope that is private because Slack said so (or an operator set a
        # policy) is *confirmed* private, and nothing later re-opens it. A
        # scope that is private only because the channel type was unknown is a
        # guess, and a real signal is allowed to correct it.
        confirmed_private = authoritative and not is_public
        existing = self.store.get_scope(scope_id)
        if not existing:
            self.store.upsert_scope(
                {
                    "id": scope_id,
                    "organization_id": organization_id,
                    "kind": visibility,
                    "label": f"Slack {channel_id}",
                    "channel_id": channel_id,
                    "principal_ids": [] if is_public else [principal_id],
                    "is_public": is_public,
                    "privacy_confirmed": confirmed_private,
                }
            )
        elif not is_public and bool(existing.get("is_public")):
            # Self-healing, one direction only: a scope already on record as
            # public but now known to be private is corrected in place, so a
            # deployment that mislabelled private channels before the
            # channel_type="group" fix repairs itself on the channel's next
            # message instead of leaking until someone runs a migration.
            #
            # Narrowing needs no proof: it can only ever remove access.
            existing["kind"] = visibility
            existing["is_public"] = False
            existing["privacy_confirmed"] = bool(
                existing.get("privacy_confirmed")
            ) or confirmed_private
            existing["principal_ids"] = sorted(
                set(existing.get("principal_ids") or [])
                | set(self.store.scope_member_ids(organization_id, scope_id))
                | {principal_id}
            )
            self.store.upsert_scope(existing)
        elif (
            is_public
            and not bool(existing.get("is_public"))
            and authoritative
            and not bool(existing.get("privacy_confirmed"))
        ):
            # Widening only ever undoes a *guess*. A channel first seen
            # through an app_mention carries no channel_type, so it is stored
            # private until a real message event proves otherwise -- that
            # correction is this branch, and without it a genuinely public
            # channel Docket only ever sees mentions in would be stuck
            # private forever.
            #
            # A scope confirmed private is never reopened, even if Slack
            # later reports the room as public. Slack does support converting
            # a private channel to public, but people who spoke there had a
            # private expectation at the time; silently re-disclosing that
            # backlog is not a decision this code should make on its own.
            existing["kind"] = visibility
            existing["is_public"] = True
            existing["principal_ids"] = []
            self.store.upsert_scope(existing)
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
    def __init__(self, *, bot_token: str | None, store: Any | None = None) -> None:
        # bot_token is the fallback for this operator's own single primary
        # workspace (configured via .env). store, when given, is checked
        # first for a per-workspace token — every OAuth-installed workspace
        # has its own bot token, and using the wrong one isn't just wrong,
        # it's a different tenant's credential.
        self.bot_token = bot_token
        self.store = store

    @property
    def configured(self) -> bool:
        return bool(self.bot_token) or bool(self.store)

    def _token_for(self, workspace_id: str | None) -> str | None:
        if workspace_id and self.store:
            installation = self.store.slack_installation_by_workspace(workspace_id)
            token = (installation or {}).get("bot_token")
            if token:
                return token
        return self.bot_token

    def send(
        self,
        *,
        channel_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        text = _scrub_blocked_emoji(text)
        blocks = _scrub_blocks(blocks)
        token = self._token_for(workspace_id)
        if not token:
            self._record_outbound(
                organization_id=organization_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                text=text,
                message_id=idempotency_key,
            )
            return {
                "ok": False,
                "dry_run": True,
                "channel": channel_id,
                "text": text,
                "blocks": blocks or [],
            }
        body: dict[str, Any] = {"channel": channel_id, "text": text}
        if idempotency_key:
            # NOTE: despite what this once said, Slack's chat.postMessage
            # does NOT deduplicate by client_msg_id for bot-token calls --
            # that field is for a client's own optimistic-UI reconciliation,
            # not a server-enforced idempotency guarantee. The real
            # idempotency guard lives in DeliveryService._reserve(), which
            # atomically claims the idempotency key before this method is
            # ever called. client_msg_id is still attached (harmless,
            # stable across retries) but must not be relied on alone.
            body["client_msg_id"] = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"docket:{idempotency_key}")
            )
        if blocks:
            body["blocks"] = blocks
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack delivery failed: {payload.get('error')}")
        self._record_outbound(
            organization_id=organization_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            text=text,
            # Slack's own message ts is the natural per-channel identity and
            # is stable across a retry that Slack itself deduplicated.
            message_id=str(payload.get("ts") or "") or idempotency_key,
        )
        return payload

    def _record_outbound(
        self,
        *,
        organization_id: str | None,
        workspace_id: str | None,
        channel_id: str,
        text: str,
        message_id: str | None,
    ) -> None:
        """Append what Docket just said to the conversation log.

        This is the single outbound choke point: EVERY Slack message
        Docket sends goes through send(), whether it came from the
        delivery pipeline or straight from the slack_send_message tool.
        Recording here is what fixed a real blind spot -- the timeline
        used to be reconstructed from `deliveries` rows, so anything sent
        by the tool left no trace and Docket could not see its own
        messages in later turns.
        """
        if not self.store or not organization_id or not workspace_id:
            return
        self.store.append_conversation_message(
            organization_id=organization_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            role="assistant",
            text=text,
            author="docket",
            message_id=message_id,
        )

    def update_message(
        self,
        *,
        channel_id: str,
        timestamp: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit an already-sent message in place — used to replace an
        approval's Approve/Deny buttons with the actual decision once one
        is made, instead of leaving live buttons on a message whose
        decision has already been acted on."""
        text = _scrub_blocked_emoji(text)
        blocks = _scrub_blocks(blocks)
        token = self._token_for(workspace_id)
        if not token:
            return {"ok": False, "dry_run": True, "channel": channel_id, "ts": timestamp}
        body: dict[str, Any] = {"channel": channel_id, "ts": timestamp, "text": text}
        if blocks is not None:
            body["blocks"] = blocks
        response = httpx.post(
            "https://slack.com/api/chat.update",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack message update failed: {payload.get('error')}")
        return payload

    def react(
        self,
        *,
        channel_id: str,
        timestamp: str,
        name: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        token = self._token_for(workspace_id)
        if not token:
            return {"ok": False, "dry_run": True, "channel": channel_id, "timestamp": timestamp, "name": name}
        response = httpx.post(
            "https://slack.com/api/reactions.add",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel_id, "timestamp": timestamp, "name": name},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack reaction failed: {payload.get('error')}")
        return payload

    def upload_file(
        self,
        *,
        channel_id: str,
        filename: str,
        content: bytes,
        title: str | None = None,
        initial_comment: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Share a file using Slack's current 3-step external upload flow
        (files.upload is deprecated): get an upload URL, PUT the bytes, then
        complete the upload into the destination channel."""
        token = self._token_for(workspace_id)
        if not token:
            return {
                "ok": False,
                "dry_run": True,
                "channel": channel_id,
                "filename": filename,
                "size": len(content),
            }
        headers = {"Authorization": f"Bearer {token}"}
        get_url = httpx.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            data={"filename": filename, "length": str(len(content))},
            timeout=30.0,
        )
        get_url.raise_for_status()
        url_payload = get_url.json()
        if not url_payload.get("ok"):
            raise RuntimeError(f"Slack upload URL request failed: {url_payload.get('error')}")

        put_response = httpx.post(
            url_payload["upload_url"], content=content, timeout=60.0
        )
        put_response.raise_for_status()

        complete_body: dict[str, Any] = {
            "files": [
                {"id": url_payload["file_id"], "title": title or filename}
            ],
            "channel_id": channel_id,
        }
        if initial_comment:
            complete_body["initial_comment"] = initial_comment
        complete = httpx.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json=complete_body,
            timeout=30.0,
        )
        complete.raise_for_status()
        complete_payload = complete.json()
        if not complete_payload.get("ok"):
            raise RuntimeError(f"Slack upload completion failed: {complete_payload.get('error')}")
        return complete_payload
