"""Regression coverage for final Slack delivery retry idempotency."""

from __future__ import annotations

from typing import Any

from docket.harness.model_client import ScriptedModelClient
from docket.harness.models import InvocationEnvelope, ModelResponse
from docket.harness.slack import SlackSender


def _slack_envelope(brain) -> InvocationEnvelope:
    destination = {"type": "slack_channel", "id": "C_TEST", "is_private": False}
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="slack_docket",
        conversation_id="slack:C_TEST:delivery-idempotency",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def test_completed_delivery_is_not_sent_again(brain):
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="One durable final answer.")]
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain),
        goal="Answer once",
        trigger_kind="reactive",
        initial_message="Answer once",
    )
    outcome = brain.company_agent.execute_task(task["id"])
    assert outcome.state.value == "completed"

    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs),
        {"ok": True, "channel": kwargs["channel_id"], "ts": "1.000"},
    )[1]

    first = brain.delivery.deliver_task(task["id"])
    second = brain.delivery.deliver_task(task["id"])

    assert first["delivered"] is True
    assert second["delivered"] is False
    assert second["reason"] == "already_delivered"
    assert len(sent) == 1
    assert sent[0]["idempotency_key"] == f"slack.final:{task['id']}"


def test_slack_retries_use_the_same_client_message_id(monkeypatch):
    posted: list[dict[str, Any]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"ok": True, "channel": "C_TEST", "ts": "1.000"}

    def fake_post(*args, **kwargs):
        posted.append(kwargs["json"])
        return _Response()

    monkeypatch.setattr("docket.harness.slack.httpx.post", fake_post)
    sender = SlackSender(bot_token="xoxb-test")
    for _ in range(2):
        sender.send(
            channel_id="C_TEST",
            text="One durable final answer.",
            idempotency_key="slack.final:task_one",
        )

    assert posted[0]["client_msg_id"] == posted[1]["client_msg_id"]
