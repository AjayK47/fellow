"""Contention is not failure: a job deferred because another owner holds
the resource must retry on a short fixed interval, not the exponential
backoff meant for genuine errors.

Regression coverage for a user-visible stall. `session.dispatch` raises
when a live Session Manager already owns the Session -- the normal case
whenever someone sends a follow-up mid-run -- and that retrying job is the
ONLY thing that picks the pending revision back up once the Manager
releases. Under exponential backoff the retries land at 1s, 2s, 4s, 8s ...
capped at 300s, so a follow-up sent during a 60-second run could sit
unanswered for minutes after Toda was already free, with nothing wrong
in any log.
"""

from __future__ import annotations

from datetime import datetime

from toda.harness.jobs import JobContention


def _retry_delay_seconds(brain, job_id: str, *, failed_at: datetime) -> float:
    job = brain.store.get_harness_job(job_id)
    return (
        datetime.fromisoformat(job["available_at"]) - failed_at
    ).total_seconds()


def test_contention_retries_fast_while_real_errors_back_off(brain):
    from toda.domain.models import utcnow

    contended = brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="session.dispatch",
        payload={"session_id": "session_x"},
        idempotency_key="contention-test:busy",
    )
    broken = brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="session.dispatch",
        payload={"session_id": "session_y"},
        idempotency_key="contention-test:broken",
    )
    # Both have already burned several attempts -- the point at which
    # exponential backoff has grown large enough to actually hurt.
    for job in (contended, broken):
        job["attempts"] = 8

    now = utcnow()
    brain.harness_jobs.fail(contended, "session_manager_busy", retry_in_seconds=1.0)
    brain.harness_jobs.fail(broken, "OpenRouter 500 for model x")

    assert _retry_delay_seconds(brain, contended["id"], failed_at=now) <= 2.0
    # 2**8 == 256s: the same attempt count costs minutes for a real error.
    assert _retry_delay_seconds(brain, broken["id"], failed_at=now) > 200.0


def test_worker_routes_a_contention_exception_to_the_fast_retry(brain):
    """The classification must hold end to end through _process_job, not
    just when fail() is called directly."""
    from toda.domain.models import utcnow

    brain.harness_worker.register(
        "test.contended", lambda _payload: (_ for _ in ()).throw(JobContention("busy"))
    )
    brain.harness_worker.register(
        "test.broken", lambda _payload: (_ for _ in ()).throw(RuntimeError("real bug"))
    )
    for kind in ("test.contended", "test.broken"):
        brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind=kind,
            payload={},
            idempotency_key=f"contention-worker:{kind}",
            # Well above the attempts forced below, so both jobs actually
            # take the retry path instead of hitting the dead-letter
            # ceiling -- the retry DELAY is what's under test here.
            max_attempts=100,
        )

    now = utcnow()
    leased = brain.harness_jobs.lease(worker_id="w1", limit=10)
    for job in leased:
        job["attempts"] = 8
        brain.harness_worker._process_job(job)

    by_kind = {job["kind"]: job["id"] for job in leased}
    assert _retry_delay_seconds(brain, by_kind["test.contended"], failed_at=now) <= 2.0
    assert _retry_delay_seconds(brain, by_kind["test.broken"], failed_at=now) > 200.0


def test_a_flooding_job_kind_is_shed_rather_than_queued_without_limit(brain):
    """Nothing bounded the queue, so one connector trigger's first poll
    enqueued 6,200 jobs -- ~900 of them a model call each -- and the only
    signal was the bill."""
    import pytest

    from toda.harness.jobs import QueueSaturated, _MAX_PENDING_PER_KIND

    ceiling = _MAX_PENDING_PER_KIND["attention.evaluate"]
    for index in range(ceiling):
        brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind="attention.evaluate",
            payload={"event": {"id": f"e{index}"}},
            idempotency_key=f"attention:flood:{index}",
        )
    with pytest.raises(QueueSaturated) as excinfo:
        brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind="attention.evaluate",
            payload={"event": {"id": "one_too_many"}},
            idempotency_key="attention:flood:overflow",
        )
    assert "attention.evaluate" in str(excinfo.value)

    # A different kind is unaffected: the ceiling is per-kind precisely so a
    # burst of one cannot starve dispatch or delivery.
    assert brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="delivery.send",
        payload={"task_id": "t1"},
        idempotency_key="delivery.send:t1",
    )


def test_routing_sheds_a_saturated_lane_instead_of_failing_the_event(brain):
    """Evidence is persisted before the enqueue, so shedding loses only the
    queued model work -- the event stays searchable either way."""
    from toda.harness.jobs import QueueSaturated

    def _saturated(**_kwargs):
        raise QueueSaturated("attention.evaluate already has 500 jobs pending")

    brain.event_router.queue = type(
        "Q", (), {"enqueue": staticmethod(_saturated)}
    )()
    routed = brain.event_router.route(
        {
            "id": "ev_shed", "organization_id": "org_acme", "source": "slack",
            "type": "slack.channel_message",
            "actor": {"id": "person_ajay", "type": "person"},
            "scope": {"workspace_id": "T1", "channel_id": "C1",
                      "visibility": "organization_public", "scope_id": "scope_org_public"},
            "payload": {"text": "ambient chatter", "conversation_key": "slack:T1:C1"},
            "dedupe_key": "ev_shed",
        }
    )
    assert routed["shed"] is True
    assert routed["source_id"], "the evidence row must still exist"
    assert any(
        e["event_type"] == "queue.saturated"
        for e in brain.store.list_audit("org_acme", "queue.saturated")
    )


def test_a_permanent_provider_rejection_is_not_retried(brain):
    """`session.dispatch` allows 100 attempts because contention is normal.
    Applied to a request the provider rejects outright, that meant one
    poisoned transcript re-ran the whole discovery sequence a hundred times
    -- observed live as a Task that kept searching HubSpot, describing the
    same action and 400ing, for minutes, at full model cost per lap."""
    worker = brain.harness_worker

    def _invalid_prompt(_payload):
        raise RuntimeError(
            "concentrate 400 for model gemini-3.7-flash: "
            '{"error":{"code":"invalid_prompt","message":"rejected the request"}}'
        )

    worker.register("test.permanent", _invalid_prompt)
    job = brain.harness_jobs.enqueue(
        organization_id="org_acme", kind="test.permanent",
        payload={}, idempotency_key="permanent:1",
    )
    result = worker._process_job({**job, "attempts": 0})
    assert result.get("permanent") is True
    assert brain.store.get_harness_job(job["id"])["status"] == "dead_letter"


def test_a_transient_failure_still_retries(brain):
    """The distinction has to hold in both directions -- dead-lettering a
    rate limit would lose real work."""
    worker = brain.harness_worker

    def _rate_limited(_payload):
        raise RuntimeError("429 rate limit exceeded, retry shortly")

    worker.register("test.transient", _rate_limited)
    job = brain.harness_jobs.enqueue(
        organization_id="org_acme", kind="test.transient",
        payload={}, idempotency_key="transient:1",
    )
    result = worker._process_job({**job, "attempts": 0})
    assert not result.get("permanent")
    assert brain.store.get_harness_job(job["id"])["status"] == "queued"
