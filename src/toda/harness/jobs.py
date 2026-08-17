"""SQLite-backed durable queue with leases, retries, and idempotency."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.observability import current_trace_id
from toda.storage.db import BrainStore

# Session and child Task execution can drive CompanyAgent._loop() to completion -- potentially
# several iterations, each with a model round-trip plus tool calls -- and can
# legitimately run past the default 60s lease. Without a longer lease, a
# concurrent lane (or a process restarted while a run was in flight) could
# see the lease as expired mid-run and re-lease + re-execute the same run.
# A stale lease only matters for actual crash recovery, so a generous static
# override is enough here -- true lease renewal isn't needed for jobs this
# infrequent.
_LONG_RUNNING_LEASE_OVERRIDES = {
    "session.dispatch": 3600,
    "task.run_child": 3600,
    "task.pi_poll": 1800,
    "approval.resolve": 1800,
}

_MAX_ATTEMPT_OVERRIDES = {
    # A dispatch that races the currently active Manager is deferred rather
    # than discarded. Contention retries are cheap and constant-interval
    # (see JobContention), so a high ceiling covers multi-hour work.
    "session.dispatch": 100,
}

# How soon to re-try a job that was deferred purely because something else
# already owned the resource. This is NOT a failure and must not use the
# exponential error backoff: a Session Manager being busy is the normal
# case whenever a user sends a follow-up mid-run, and the retrying dispatch
# job is the ONLY thing that picks the pending revision back up once the
# Manager releases. Under exponential backoff those retries land at 1s, 2s,
# 4s ... 300s, so a follow-up that arrived during a 60-second run could sit
# unanswered for minutes AFTER the Manager was free -- a user-visible
# "Toda ignored me" stall with nothing wrong in any log.
_CONTENTION_RETRY_SECONDS = 1.0

# Ceiling on how many jobs of one kind may be waiting at once.
#
# Nothing bounded this, so a single connector trigger's first poll enqueued
# 6,200 jobs -- ~5,300 `event.route` and ~900 `attention.evaluate`, the
# latter a model call each -- and the only signal was the bill. Evidence is
# still persisted when the ceiling is hit; what gets shed is the queued
# *model* work, which is the expensive part and the part that was producing
# nothing.
#
# Deliberately per-kind rather than global: a burst of one kind must not be
# able to starve dispatch or delivery, and shedding the right one requires
# knowing which is flooding.
_MAX_PENDING_PER_KIND = {
    "attention.evaluate": 500,
    "event.route": 2_000,
    "knowledge.index_evidence": 5_000,
    "knowledge.consolidate_episode": 2_000,
}


class QueueSaturated(RuntimeError):
    """Raised when a job kind already has more work queued than its ceiling.

    Not an error in the caller -- it means something upstream is producing
    faster than the lane can consume, and continuing to enqueue would only
    deepen a backlog nobody asked for.
    """


class JobContention(RuntimeError):
    """Raised by a handler when a job cannot run right now because another
    owner holds the resource. Retried on a short fixed interval instead of
    the exponential backoff used for genuine failures."""


class JobQueue:
    def __init__(
        self,
        store: BrainStore,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 5,
    ) -> None:
        self.store = store
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def enqueue(
        self,
        *,
        organization_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: str | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        ceiling = _MAX_PENDING_PER_KIND.get(kind)
        if ceiling is not None:
            pending = self.store.count_pending_jobs(organization_id, kind)
            if pending >= ceiling:
                raise QueueSaturated(
                    f"{kind} already has {pending} jobs pending (ceiling {ceiling}); "
                    "shedding rather than deepening the backlog"
                )
        now = utcnow().isoformat()
        # Stamp the trace in force onto the payload, so the job carries it
        # across the queue to whichever thread picks it up. Done here rather
        # than at the ~30 call sites because "remember to pass trace_id" is
        # a rule that gets followed for a while and then does not: the
        # previous attempt read `payload.get("trace_id")` on the way out
        # while nothing ever put one in, so the lookup was always None.
        ambient_trace_id = current_trace_id()
        if ambient_trace_id and "trace_id" not in payload:
            payload = {**payload, "trace_id": ambient_trace_id}
        job = {
            "id": new_id("hjob"),
            "organization_id": organization_id,
            "kind": kind,
            "status": "queued",
            "payload": payload,
            "available_at": available_at or now,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempts": 0,
            "max_attempts": max_attempts
            or _MAX_ATTEMPT_OVERRIDES.get(kind)
            or self.max_attempts,
            "idempotency_key": idempotency_key,
            "created_at": now,
            "updated_at": now,
        }
        lease_override = _LONG_RUNNING_LEASE_OVERRIDES.get(kind)
        if lease_override:
            job["lease_seconds"] = lease_override
        created = self.store.enqueue_harness_job(job)
        self.store.trace.record(
            "job.enqueued", component="queue", phase="completed", input=payload,
            output={"id": created["id"], "kind": kind, "available_at": created["available_at"]},
            trace_id=payload.get("trace_id"), job_id=created["id"],
            organization_id=organization_id,
        )
        return created

    def lease(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.lease_harness_jobs(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
            kinds=kinds,
        )

    def complete(
        self,
        job_id: str,
        result: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        finished = self.store.finish_harness_job(
            job_id,
            status="completed",
            result=result or {},
            lease_owner=worker_id,
        )
        self.store.trace.record(
            "job.completed", component="queue", phase="completed", output=result or {},
            trace_id=(result or {}).get("trace_id"), job_id=job_id,
        )
        return finished

    def fail(
        self,
        job: dict[str, Any],
        error: str,
        *,
        retry_in_seconds: float | None = None,
        permanent: bool = False,
    ) -> dict[str, Any] | None:
        """`permanent=True` dead-letters immediately, whatever the attempt
        ceiling says. An invalid prompt, a rejected schema or a malformed
        request does not become valid by being sent again, and retrying it
        costs a full model round trip per lap."""
        attempts = int(job.get("attempts", 0))
        max_attempts = int(job.get("max_attempts", self.max_attempts))
        if permanent or attempts >= max_attempts:
            finished = self.store.finish_harness_job(
                job["id"],
                status="dead_letter",
                error=error,
                lease_owner=job.get("lease_owner"),
            )
            self.store.trace.record(
                "job.dead_lettered", component="queue", phase="failed", error=error,
                trace_id=(job.get("payload") or {}).get("trace_id"), job_id=job["id"],
                attempt_number=attempts,
            )
            return finished
        delay = (
            retry_in_seconds
            if retry_in_seconds is not None
            else min(2**attempts, 300)
        )
        retry_at = (utcnow() + timedelta(seconds=delay)).isoformat()
        finished = self.store.finish_harness_job(
            job["id"],
            status="queued",
            error=error,
            retry_at=retry_at,
            lease_owner=job.get("lease_owner"),
        )
        self.store.trace.record(
            "job.retry.scheduled", component="queue", phase="event", error=error,
            output={"retry_at": retry_at, "delay_seconds": delay},
            trace_id=(job.get("payload") or {}).get("trace_id"), job_id=job["id"],
            attempt_number=attempts,
        )
        return finished
