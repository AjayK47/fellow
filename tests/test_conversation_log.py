"""One ordered conversation log is the single source of truth for what was
said in a Slack channel.

The timeline used to be RECONSTRUCTED on every read by unioning raw
`received_events` with `deliveries` joined to `tasks`. Two write paths, two
latencies, and two real production bugs:

  * a Task could be shown an inbound message that had not been routed to it
    yet, and act on it, before the Task that owned that message existed;
  * anything Docket sent through the slack_send_message TOOL left no trace
    at all, because only delivery-path sends wrote a row the reconstruction
    could find -- so Docket could not see its own messages on later turns.

Both are closed by construction now: one append-only log, written at the two
real choke points (EventRouter.route inbound, SlackSender.send outbound).
"""

from __future__ import annotations

import json
from typing import Any

from docket.harness.slack import SlackSender


def _timeline(brain, *, workspace_id: str = "TACME", channel_id: str = "CSOCIAL"):
    return brain.store.recent_slack_messages(
        "org_acme", workspace_id=workspace_id, channel_id=channel_id, limit=20
    )


def test_inbound_and_outbound_share_one_ordered_timeline(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvOne",
        "event": {
            "type": "app_mention",
            "user": "UAJAY",
            "channel": "CSOCIAL",
            "text": "Hey <@UBOT>",
            "ts": "250.001",
            "client_msg_id": "one",
        },
    }
    assert brain.slack_gateway.accept(
        body=json.dumps(payload).encode(), headers={}, allow_unsigned=True
    )["accepted"]
    assert brain.harness_worker.drain_until_idle()["failed"] == 0

    timeline = _timeline(brain)
    assert [item["at"] for item in timeline] == sorted(item["at"] for item in timeline)
    assert any(item["role"] == "user" and item["text"] == "Hey" for item in timeline)
    assert any(item["role"] == "assistant" for item in timeline)


def test_a_tool_sent_message_is_visible_in_the_conversation(brain):
    """slack_send_message writes no delivery record, so under the old
    reconstruction this message was invisible to Docket forever."""
    brain.slack_sender.send(
        channel_id="CSOCIAL",
        text="Sent straight from the tool, not the delivery pipeline.",
        workspace_id="TACME",
        organization_id="org_acme",
    )

    texts = [item["text"] for item in _timeline(brain)]
    assert "Sent straight from the tool, not the delivery pipeline." in texts


def test_the_log_is_idempotent_on_message_id(brain):
    """A redelivered Slack event or a retried send must not double up."""
    for _ in range(3):
        brain.store.append_conversation_message(
            organization_id="org_acme",
            workspace_id="TACME",
            channel_id="CSOCIAL",
            role="user",
            text="only once",
            message_id="EvDuplicate",
        )

    assert [item["text"] for item in _timeline(brain)].count("only once") == 1


def test_blank_and_unaddressed_messages_never_break_the_log(brain):
    """Ambient chatter belongs in the log (attention needs the room's
    context), but an empty message body is not a message."""
    brain.store.append_conversation_message(
        organization_id="org_acme",
        workspace_id="TACME",
        channel_id="CSOCIAL",
        role="user",
        text="   ",
        message_id="EvBlank",
    )
    brain.store.append_conversation_message(
        organization_id="org_acme",
        workspace_id="",
        channel_id="CSOCIAL",
        role="user",
        text="no workspace",
        message_id="EvNoWorkspace",
    )

    assert _timeline(brain) == []


def test_scrubbing_an_emoji_never_reflows_the_rest_of_the_message(monkeypatch):
    """Regression test for a real regression introduced by the emoji
    blocklist: the scrubber collapsed whitespace with " ".join(split()),
    which flattened paragraph breaks, bullet lists and code blocks in EVERY
    outbound message -- including ones containing no blocked emoji at all."""
    posted: list[dict[str, Any]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"ok": True, "channel": "C", "ts": "1.0"}

    monkeypatch.setattr(
        "docket.harness.slack.httpx.post",
        lambda *a, **kw: (posted.append(kw["json"]), _Response())[1],
    )
    sender = SlackSender(bot_token="xoxb-test")

    structured = "Findings:\n\n- one\n- two\n\n```\ncode  indented\n```"
    sender.send(channel_id="C", text=structured)
    assert posted[0]["text"] == structured, "untouched message must be byte-identical"

    sender.send(channel_id="C", text="Drink up 💧 — stay hydrated.\n\nNext line.")
    scrubbed = posted[1]["text"]
    assert "💧" not in scrubbed
    assert "\n\n" in scrubbed, "paragraph break must survive emoji removal"
    assert "Drink up — stay hydrated." in scrubbed
