"""Contention is not failure: a job deferred because another owner holds
the resource must retry on a short fixed interval, not the exponential
backoff meant for genuine errors.

Regression coverage for a user-visible stall. `session.dispatch` raises
when a live Session Manager already owns the Session -- the normal case
whenever someone sends a follow-up mid-run -- and that retrying job is the
ONLY thing that picks the pending revision back up once the Manager
releases. Under exponential backoff the retries land at 1s, 2s, 4s, 8s ...
capped at 300s, so a follow-up sent during a 60-second run could sit
unanswered for minutes after Docket was already free, with nothing wrong
in any log.
"""

from __future__ import annotations

from datetime import datetime

from docket.harness.jobs import JobContention


def _retry_delay_seconds(brain, job_id: str, *, failed_at: datetime) -> float:
    job = brain.store.get_harness_job(job_id)
    return (
        datetime.fromisoformat(job["available_at"]) - failed_at
    ).total_seconds()


def test_contention_retries_fast_while_real_errors_back_off(brain):
    from docket.domain.models import utcnow

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
    from docket.domain.models import utcnow

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
