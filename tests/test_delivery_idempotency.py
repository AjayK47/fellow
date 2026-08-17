"""Regression coverage for final Slack delivery retry idempotency."""

from __future__ import annotations

from typing import Any

import pytest

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse
from toda.harness.slack import SlackSender


def _slack_envelope(brain) -> InvocationEnvelope:
    destination = {"type": "slack_channel", "id": "C_TEST", "is_private": False}
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="slack_toda",
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


def test_suppress_delivery_skips_the_redundant_final_answer_send(brain):
    """Regression test for a real production bug: typed_tools.py's
    slack_send_message sets metadata["suppress_delivery"] = True precisely
    so the automatic final-answer delivery doesn't ALSO fire once the run
    completes -- but deliver_task() never checked that flag (unlike
    _maybe_notify_failure/deliver_status_update, which already did). A
    live automation firing that called slack_send_message directly then
    still got its own final_answer delivered as a second, redundant Slack
    message."""
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="This should never reach Slack.")]
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain),
        goal="Answer once",
        trigger_kind="reactive",
        initial_message="Answer once",
    )
    outcome = brain.company_agent.execute_task(task["id"])
    assert outcome.state.value == "completed"

    stored_task = brain.store.get_task(task["id"])
    metadata = dict(stored_task.get("metadata") or {})
    metadata["suppress_delivery"] = True
    stored_task["metadata"] = metadata
    brain.store.update_task(stored_task)

    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs),
        {"ok": True, "channel": kwargs["channel_id"], "ts": "1.000"},
    )[1]

    result = brain.delivery.deliver_task(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "suppressed"
    assert sent == []


def test_a_send_already_reserved_by_another_attempt_is_never_sent_again(brain):
    """Regression test for a real production bug: a read-then-send check
    (look up an existing delivery, send if none found) left a race window
    where two invocations of the same logical delivery -- e.g. a job
    retried after a transient failure between a successful Slack post and
    recording it -- could both pass the check and both actually post,
    since Slack's chat.postMessage does not deduplicate by client_msg_id
    despite the code once assuming it did. Confirmed live: a recurring
    automation delivered its reminder text twice in the channel with only
    one delivery row on record. This simulates the race directly: reserve
    the idempotency key first (as a concurrent/crashed attempt would),
    then confirm deliver_task backs off instead of sending."""
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

    # Simulate a concurrent attempt that claimed this exact idempotency key
    # moments ago and is still mid-send (a fresh timestamp -- an old one is
    # treated as abandoned and deliberately reclaimable, covered below).
    from toda.domain.models import utcnow

    now = utcnow().isoformat()
    idempotency_key = f"slack.final:{task['id']}"
    brain.store.reserve_delivery(
        {
            "id": "delivery_inflight",
            "organization_id": "org_acme",
            "task_id": task["id"],
            "kind": "final_answer",
            "status": "sending",
            "idempotency_key": idempotency_key,
            "created_at": now,
            "updated_at": now,
        }
    )

    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs),
        {"ok": True, "channel": kwargs["channel_id"], "ts": "1.000"},
    )[1]

    result = brain.delivery.deliver_task(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "send_in_progress_elsewhere"
    assert sent == []


def test_a_failed_send_releases_its_reservation_so_the_retry_can_deliver(brain):
    """The reservation guards against double-sending, but it must never
    turn one transient Slack error into a permanently lost message: if the
    send raises, the claim has to be handed back before the exception
    propagates, or the job's own retry is locked out by its own previous
    attempt."""
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="One durable final answer.")]
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain),
        goal="Answer once",
        trigger_kind="reactive",
        initial_message="Answer once",
    )
    assert brain.company_agent.execute_task(task["id"]).state.value == "completed"

    attempts: list[dict[str, Any]] = []

    def flaky_send(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("Slack delivery failed: internal_error")
        return {"ok": True, "channel": kwargs["channel_id"], "ts": "1.000"}

    brain.delivery.slack.send = flaky_send

    with pytest.raises(RuntimeError):
        brain.delivery.deliver_task(task["id"])

    # The retry must genuinely reach Slack, not be blocked by the abandoned
    # reservation from the failed attempt.
    result = brain.delivery.deliver_task(task["id"])
    assert result["delivered"] is True
    assert len(attempts) == 2

    # And once it has really been delivered, it still never sends again.
    assert brain.delivery.deliver_task(task["id"])["reason"] == "already_delivered"
    assert len(attempts) == 2


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

    monkeypatch.setattr("toda.harness.slack.httpx.post", fake_post)
    sender = SlackSender(bot_token="xoxb-test")
    for _ in range(2):
        sender.send(
            channel_id="C_TEST",
            text="One durable final answer.",
            idempotency_key="slack.final:task_one",
        )

    assert posted[0]["client_msg_id"] == posted[1]["client_msg_id"]


def test_blocked_emoji_are_scrubbed_before_the_real_slack_call(monkeypatch):
    """Regression test for a real production bug: the prompt-level rule
    against these specific emoji (STABLE_SYSTEM_PROMPT) was already live
    when a water-reminder automation still emitted :droplet: -- prompt
    adherence alone isn't reliable enough for a hard rule, so this is
    enforced deterministically in SlackSender itself as a backstop,
    regardless of what the model actually wrote."""
    posted: list[dict[str, Any]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"ok": True, "channel": "C_TEST", "ts": "1.000"}

    def fake_post(*args, **kwargs):
        posted.append(kwargs["json"])
        return _Response()

    monkeypatch.setattr("toda.harness.slack.httpx.post", fake_post)
    sender = SlackSender(bot_token="xoxb-test")
    sender.send(
        channel_id="C_TEST",
        text="Time for a water break, ajay 💧 — grab a glass and take a sip.",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Stay hydrated 💦 today!"},
            }
        ],
    )

    assert "💧" not in posted[0]["text"]
    assert "💦" not in posted[0]["blocks"][0]["text"]["text"]
    # The rest of the message survives untouched, just the emoji is gone.
    assert "Time for a water break, ajay" in posted[0]["text"]
    assert "grab a glass and take a sip." in posted[0]["text"]


def test_conversation_reply_reserves_before_sending(brain):
    """`deliver_conversation` used to look up an existing delivery and then
    send -- exactly the read-then-send gap `reserve_delivery` exists to
    close, on the most frequent send in the system. Two concurrent
    deliveries of one reply must produce one Slack message."""
    session = brain.sessions.resolve(
        organization_id="org_acme",
        conversation_key="slack:C_TEST:conversation-idempotency",
        destination={"type": "slack_channel", "id": "C_TEST", "is_private": False},
        metadata={"slack": {"workspace_id": "T_TEST", "channel_id": "C_TEST"}},
    )
    message = brain.sessions.append_message(
        organization_id="org_acme",
        session_id=session["id"],
        role="assistant",
        content="A quick reply.",
    )

    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs) or {"ok": True, "ts": "1.0"}
    )

    first = brain.delivery.deliver_conversation(session["id"], message["id"])
    second = brain.delivery.deliver_conversation(session["id"], message["id"])

    assert first["delivered"] is True
    assert second["delivered"] is False
    assert second["reason"] == "already_delivered"
    assert len(sent) == 1


def test_a_failed_conversation_send_releases_its_claim(brain):
    """A transient Slack error must not lock the reply out forever."""
    session = brain.sessions.resolve(
        organization_id="org_acme",
        conversation_key="slack:C_TEST:conversation-release",
        destination={"type": "slack_channel", "id": "C_TEST", "is_private": False},
        metadata={"slack": {"workspace_id": "T_TEST", "channel_id": "C_TEST"}},
    )
    message = brain.sessions.append_message(
        organization_id="org_acme",
        session_id=session["id"],
        role="assistant",
        content="A reply that fails once.",
    )

    attempts: list[dict[str, Any]] = []

    def _flaky(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("slack 503")
        return {"ok": True, "ts": "1.0"}

    brain.delivery.slack.send = _flaky
    with pytest.raises(RuntimeError):
        brain.delivery.deliver_conversation(session["id"], message["id"])

    retried = brain.delivery.deliver_conversation(session["id"], message["id"])
    assert retried["delivered"] is True
    assert len(attempts) == 2


def test_a_suppressed_task_records_the_skip(brain):
    """The model posting its own message wrote no delivery row at all, so
    the Task matched list_completed_tasks_missing_delivery() forever -- and
    that sweep is ORDER BY completed_at LIMIT 100, so accumulated
    suppressed Tasks permanently filled its window."""
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="The model already said this itself.")]
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain),
        goal="Say it directly",
        trigger_kind="reactive",
        initial_message="Say it directly",
    )
    brain.company_agent.execute_task(task["id"])
    record = brain.store.get_task(task["id"])
    record["metadata"] = {**(record.get("metadata") or {}), "suppress_delivery": True}
    brain.store.update_task(record)

    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs) or {"ok": True, "ts": "1.0"}
    )
    result = brain.delivery.deliver_task(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "suppressed"
    assert sent == []
    delivery = brain.store.delivery_by_idempotency_key(
        "org_acme", f"slack.final:{task['id']}"
    )
    assert delivery is not None
    assert delivery["status"] == "suppressed"
    assert task["id"] not in {
        item["id"]
        for item in brain.store.list_completed_tasks_missing_delivery("org_acme")
    }


def test_recovery_sweep_can_actually_requeue_a_lost_delivery(brain):
    """The sweep re-enqueued under `delivery.send:{task_id}` -- the key the
    Task's own completion already used -- and enqueue_harness_job() dedupes
    by returning the EXISTING terminal row, so this entire safety net
    queued nothing and could never re-deliver anything."""
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="An answer that never reached Slack.")]
    )
    task = brain.company_agent.create_invocation(
        envelope=_slack_envelope(brain),
        goal="Answer once",
        trigger_kind="reactive",
        initial_message="Answer once",
    )
    brain.company_agent.execute_task(task["id"])

    # The Task's own completion already enqueued and finished delivery.send.
    original = brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="delivery.send",
        payload={"task_id": task["id"]},
        idempotency_key=f"delivery.send:{task['id']}",
    )
    brain.harness_jobs.complete(original["id"], {})

    swept = brain.recovery_heartbeat.tick("org_acme")
    assert task["id"] in swept["queued_delivery_task_ids"]
    leasable = brain.harness_jobs.lease(
        worker_id="recovery-test", kinds=["delivery.send"]
    )
    assert [
        job
        for job in leasable
        if (job.get("payload") or {}).get("task_id") == task["id"]
    ], "recovery must produce a job that will actually run"
