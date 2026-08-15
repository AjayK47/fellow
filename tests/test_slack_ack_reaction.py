"""A Slack reply can take anywhere from a second to several minutes (a full
agent run with tool calls), and Slack's Web API has no "bot is typing"
indicator. router.py reacts to the triggering message immediately on wake
so the person knows Toda is alive; delivery.py clears it once the real
answer (or a failure notice) actually posts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from toda.harness.router import _ACK_REACTION


def _direct_message_payload(*, event_id: str, channel_id: str, message_ts: str, text: str) -> dict:
    return {
        "id": event_id,
        "organization_id": "org_acme",
        "source": "slack",
        "type": "slack.dm",
        "actor": {"id": "person_ajay", "type": "person"},
        "scope": {
            "workspace_id": "TACME",
            "channel_id": channel_id,
            "visibility": "dm",
            "scope_id": "scope_org_public",
        },
        "payload": {
            "text": text,
            "conversation_key": f"slack:TACME:{channel_id}:1",
            "message_ts": message_ts,
        },
        "dedupe_key": f"dedupe-{event_id}",
        "is_direct_agent_request": True,
    }


def test_routing_a_direct_message_reacts_to_it_immediately(brain, monkeypatch):
    react = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(brain.slack_sender, "react", react)

    routed = brain.event_router.route(
        _direct_message_payload(
            event_id="ev_ack_1", channel_id="Dack1", message_ts="1700000000.000100", text="status?"
        )
    )

    assert routed["route"] == "session_wake"
    react.assert_called_once_with(
        channel_id="Dack1",
        timestamp="1700000000.000100",
        name=_ACK_REACTION,
        workspace_id="TACME",
    )


def test_a_failing_reaction_call_does_not_break_routing(brain, monkeypatch):
    monkeypatch.setattr(brain.slack_sender, "react", MagicMock(side_effect=RuntimeError("rate limited")))

    routed = brain.event_router.route(
        _direct_message_payload(
            event_id="ev_ack_fail", channel_id="Dackfail", message_ts="1700000001.000100", text="status?"
        )
    )

    assert routed["route"] == "session_wake"


def test_ambient_messages_are_not_reacted_to(brain, monkeypatch):
    react = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(brain.slack_sender, "react", react)

    brain.event_router.route(
        {
            "id": "ev_ack_ambient",
            "organization_id": "org_acme",
            "source": "slack",
            "type": "slack.channel_message",
            "actor": {"id": "person_ajay", "type": "person"},
            "scope": {
                "workspace_id": "TACME",
                "channel_id": "Cambient",
                "visibility": "organization_public",
                "scope_id": "scope_org_public",
            },
            "payload": {
                "text": "just chatting",
                "conversation_key": "slack:TACME:Cambient:1",
                "message_ts": "1700000002.000100",
            },
            "dedupe_key": "dedupe-ev_ack_ambient",
        }
    )

    react.assert_not_called()


def test_non_slack_direct_requests_are_not_reacted_to(brain, monkeypatch):
    """The reaction endpoint is Slack-specific; a direct request from
    another surface (API invocation, another connector) must not call it."""
    react = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(brain.slack_sender, "react", react)

    payload = _direct_message_payload(
        event_id="ev_ack_other_source", channel_id="Dother", message_ts="1700000003.000100", text="status?"
    )
    payload["source"] = "teams"
    brain.event_router.route(payload)

    react.assert_not_called()


def test_delivering_the_final_answer_clears_the_ack_reaction(brain, monkeypatch):
    brain.session_manager.conversation_agent = None
    react = MagicMock(return_value={"ok": True})
    unreact = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(brain.slack_sender, "react", react)
    monkeypatch.setattr(brain.slack_sender, "unreact", unreact)

    brain.event_router.route(
        _direct_message_payload(
            event_id="ev_ack_deliver", channel_id="Ddeliver", message_ts="1700000004.000100", text="say hi"
        )
    )
    react.assert_called_once()

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0, drained
    task_id = next(
        r["result"]["task_id"] for r in drained["results"] if r["kind"] == "session.dispatch"
    )
    task = brain.store.get_task(task_id)
    assert task["state"] == "completed"

    brain.delivery.deliver_task(task_id)

    # Both the acknowledgement and the final delivery clear the reaction:
    # the acknowledgement normally gets there first, and the delivery keeps
    # a safety net for a run whose acknowledgement never posted. This
    # asserted exactly one call, which only held while
    # deliver_acknowledgement was silently raising TypeError against the
    # deterministic model (it passes reasoning=False, which that model did
    # not accept). Unreacting an absent reaction is a no-op, so the second
    # call is harmless -- the alternative is losing the safety net.
    unreact.assert_any_call(
        channel_id="Ddeliver",
        timestamp="1700000004.000100",
        name=_ACK_REACTION,
        workspace_id="TACME",
    )


def test_delivering_a_task_failure_also_clears_the_ack_reaction(brain, monkeypatch):
    brain.session_manager.conversation_agent = None
    unreact = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(brain.slack_sender, "react", MagicMock(return_value={"ok": True}))
    monkeypatch.setattr(brain.slack_sender, "unreact", unreact)

    class RaisesError:
        model = "raises-error"

        def generate(self, **_kwargs):
            raise RuntimeError("invalid model tool arguments: boom")

        def context_length(self):
            return 128_000

    brain.company_agent.model = RaisesError()

    brain.event_router.route(
        _direct_message_payload(
            event_id="ev_ack_failure", channel_id="Dfailure", message_ts="1700000005.000100", text="do the thing"
        )
    )
    drained = brain.harness_worker.drain_until_idle()
    task_id = next(
        r["result"]["task_id"] for r in drained["results"] if r["kind"] == "session.dispatch"
    )
    task = brain.store.get_task(task_id)
    assert task["state"] == "failed"

    brain.delivery.deliver_task_failure(task_id)

    unreact.assert_any_call(
        channel_id="Dfailure",
        timestamp="1700000005.000100",
        name=_ACK_REACTION,
        workspace_id="TACME",
    )


def test_react_tolerates_already_reacted_and_unreact_tolerates_no_reaction(brain, monkeypatch):
    """Both are benign, expected outcomes -- neither should raise."""
    import httpx

    def fake_post(url, **kwargs):
        error = "already_reacted" if "reactions.add" in url else "no_reaction"
        return httpx.Response(200, json={"ok": False, "error": error}, request=httpx.Request("POST", url))

    monkeypatch.setattr(brain.slack_sender, "bot_token", "xoxb-test")
    monkeypatch.setattr("toda.harness.slack.httpx.post", fake_post)

    brain.slack_sender.react(channel_id="C1", timestamp="1.1", name=_ACK_REACTION)
    brain.slack_sender.unreact(channel_id="C1", timestamp="1.1", name=_ACK_REACTION)
