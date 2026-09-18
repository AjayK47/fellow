"""Learn personal preferences from what people say and how they decide."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from toda.domain.models import utcnow

log = logging.getLogger("toda.preferences")

_SHORTEN_TEXT_TOOLS = {"post_draft", "slack_send_message"}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class PreferenceLearner:
    """Knowledge-lane learner over per-person judgments.

    Two detectors, both small and explicit:
    - habits: 3+ edited approvals on the same tool where the edit
      consistently shortens the text -> inferred `prefers_shorter_drafts`.
    - ask-less: 5 unedited approvals, same tool + same channel, within 30
      days, no prior decline -> one DM question with Yes / No buttons.
    """

    def __init__(self, *, store: Any, reality: Any, slack_sender: Any = None) -> None:
        self.store = store
        self.reality = reality
        self.slack_sender = slack_sender

    def learn(self, organization_id: str, principal_id: str) -> dict[str, Any]:
        now = utcnow()
        judgments = self.reality.list_judgments(
            organization_id, principal_id=principal_id, judgment_type="tool_approval"
        )
        approvals = [j for j in judgments if (j.get("subject") or {}).get("tool_name")]
        habit = self._learn_habit(organization_id, principal_id, approvals, now)
        suggestion = self._maybe_suggest_ask_less(
            organization_id, principal_id, approvals, now
        )
        return {"habit": habit, "suggestion": suggestion}

    # -- habits ---------------------------------------------------------

    def _learn_habit(
        self,
        organization_id: str,
        principal_id: str,
        judgments: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        if self._has_active_preference(organization_id, principal_id, "prefers_shorter_drafts"):
            return {"learned": False, "reason": "already_known"}
        if self._has_tombstone(organization_id, principal_id, "prefers_shorter_drafts"):
            return {"learned": False, "reason": "tombstoned"}
        by_tool: dict[str, list[dict[str, Any]]] = {}
        for judgment in judgments:
            if judgment.get("decision") != "approved" or judgment.get("reason") != "arguments_edited":
                continue
            by_tool.setdefault((judgment.get("subject") or {}).get("tool_name") or "", []).append(judgment)
        for tool_name, items in by_tool.items():
            if tool_name not in _SHORTEN_TEXT_TOOLS or len(items) < 3:
                continue
            shortened = 0
            for judgment in items:
                approval = self.store.get_harness_record(
                    "approvals", (judgment.get("subject") or {}).get("approval_id") or ""
                )
                if not approval:
                    continue
                original = str(((approval.get("arguments") or {}).get("text")) or "")
                edited = str((((approval.get("edited_arguments") or {}).get("text"))) or "")
                if original and edited and len(edited) < len(original):
                    shortened += 1
            if shortened >= 3:
                self._write_preference(
                    organization_id, principal_id, "prefers_shorter_drafts", "true",
                    authority=0.4, scope_id=f"scope_person_{principal_id}",
                )
                return {"learned": True, "preference": "prefers_shorter_drafts"}
        return {"learned": False, "reason": "no_pattern"}

    # -- ask-less suggestions --------------------------------------------

    def _maybe_suggest_ask_less(
        self,
        organization_id: str,
        principal_id: str,
        judgments: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for judgment in judgments:
            if judgment.get("decision") != "approved" or judgment.get("reason") == "arguments_edited":
                continue
            subject = judgment.get("subject") or {}
            channel = self._approval_channel(judgment)
            if not channel:
                continue
            weight = max(1, int(judgment.get("reinforcement_count") or 1))
            groups.setdefault((subject.get("tool_name") or "", channel), []).extend([judgment] * weight)
        for (tool_name, channel), items in groups.items():
            recent = [
                j for j in items
                if (created := _parse_time(j.get("last_reinforced_at") or j.get("created_at"))) is not None
                and now - created <= timedelta(days=30)
            ]
            if len(recent) < 5:
                continue
            if self._has_active_preference(
                organization_id, principal_id, "declined_auto_approve",
                object_match=f"{tool_name}@{channel}",
            ):
                continue
            if self._already_suggested(organization_id, principal_id, tool_name, channel):
                continue
            if self._send_ask_less_dm(organization_id, principal_id, tool_name, channel):
                scope_id = f"scope_person_{principal_id}"
                if self.store.get_scope(scope_id) is not None:
                    self._write_preference(
                        organization_id, principal_id, "auto_approve_suggested",
                        f"{tool_name}@{channel}", authority=0.4, scope_id=scope_id,
                    )
                return {"asked": True, "tool": tool_name, "channel": channel}
            return {"asked": False, "reason": "no_dm_route"}
        return {"asked": False, "reason": "no_pattern"}

    def _approval_channel(self, judgment: dict[str, Any]) -> str | None:
        approval = self.store.get_harness_record(
            "approvals", (judgment.get("subject") or {}).get("approval_id") or ""
        )
        if not approval:
            return None
        task = self.store.get_task(approval.get("task_id") or "")
        if not task:
            return None
        return ((task.get("metadata") or {}).get("slack") or {}).get("channel_id")

    def _already_suggested(
        self, organization_id: str, principal_id: str, tool_name: str, channel: str
    ) -> bool:
        scope_id = f"scope_person_{principal_id}"
        for fact in self.reality.current_facts(organization_id, scope_ids=[scope_id]):
            if fact.get("predicate") in {"auto_approve", "declined_auto_approve", "auto_approve_suggested"} and (
                f"{tool_name}@{channel}" in str(fact.get("object_value") or "")
            ):
                return True
        return False

    def _send_ask_less_dm(
        self, organization_id: str, principal_id: str, tool_name: str, channel: str
    ) -> bool:
        if self.slack_sender is None:
            return False
        route = self._dm_route(organization_id, principal_id)
        if route is None:
            log.info("No DM route for %s; skipping ask-less suggestion", principal_id)
            return False
        workspace_id, user_id = route
        try:
            channel_id = self.slack_sender.open_dm(workspace_id, user_id).get("channel_id")
        except Exception:
            log.warning("Could not open DM for %s", principal_id, exc_info=True)
            return False
        if not channel_id:
            return False
        value = f"{principal_id}::{tool_name}::{channel}"
        try:
            self.slack_sender.send(
                channel_id=channel_id,
                text=f"You've approved {tool_name} in <#{channel}> 5 times without edits. Want me to stop asking for that one?",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"You've approved `{tool_name}` in <#{channel}> 5 times without edits. Stop asking for that one?"},
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {"type": "button", "style": "primary",
                             "text": {"type": "plain_text", "text": "Yes"},
                             "action_id": "toda_preference_yes", "value": value},
                            {"type": "button",
                             "text": {"type": "plain_text", "text": "No"},
                             "action_id": "toda_preference_no", "value": value},
                        ],
                    },
                ],
                workspace_id=workspace_id,
                organization_id=organization_id,
            )
        except Exception:
            log.warning("Could not send ask-less DM to %s", principal_id, exc_info=True)
            return False
        return True

    def _dm_route(self, organization_id: str, principal_id: str) -> tuple[str, str] | None:
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM identity_mappings WHERE organization_id = ? AND source = ?",
                (organization_id, "slack"),
            )
            mappings = [row["payload"] for row in cur.fetchall()]
        import json as _json

        for raw in mappings:
            try:
                mapping = _json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if (mapping or {}).get("principal_id") != principal_id:
                continue
            user_id = (mapping or {}).get("source_user_id") or ""
            workspace_id = (mapping or {}).get("workspace_id") or (mapping or {}).get("team_id") or ""
            if user_id and workspace_id:
                return workspace_id, user_id
        return None

    # -- fact helpers ----------------------------------------------------

    def _personal_facts(self, organization_id: str, principal_id: str) -> list[dict[str, Any]]:
        scope_id = f"scope_person_{principal_id}"
        if self.store.get_scope(scope_id) is None:
            return []
        return self.reality.current_facts(organization_id, scope_ids=[scope_id])

    def _has_active_preference(
        self, organization_id: str, principal_id: str, predicate: str,
        object_match: str | None = None,
    ) -> bool:
        for fact in self._personal_facts(organization_id, principal_id):
            if fact.get("predicate") != predicate:
                continue
            if object_match is not None and object_match not in str(fact.get("object_value") or ""):
                continue
            return True
        return False

    def _has_tombstone(self, organization_id: str, principal_id: str, predicate: str) -> bool:
        scope_id = f"scope_person_{principal_id}"
        if self.store.get_scope(scope_id) is None:
            return False
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM reality_facts WHERE organization_id = ? AND subject_id = ? AND scope_id = ?",
                (organization_id, principal_id, scope_id),
            )
            import json as _json

            for row in cur.fetchall():
                try:
                    fact = _json.loads(row["payload"])
                except Exception:
                    continue
                if fact.get("predicate") == predicate and fact.get("status") in {"deleted", "retracted"}:
                    return True
        return False

    def _write_preference(
        self, organization_id: str, principal_id: str, predicate: str,
        object_value: str, *, authority: float, scope_id: str,
    ) -> dict[str, Any]:
        return self.reality.assert_fact(
            organization_id=organization_id,
            subject_id=principal_id,
            predicate=predicate,
            object_value=object_value,
            scope_id=scope_id,
            valid_from=utcnow().isoformat(),
            source_ids=[],
            confidence=0.7,
            authority=authority,
        )
