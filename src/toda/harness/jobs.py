"""SQLite-backed durable queue with leases, retries, and idempotency."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.storage.db import BrainStore
from toda.telemetry.context import inject_carrier
from opentelemetry.trace import SpanKind

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
        link_carrier: dict[str, str] | None = None,
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
        with self.store.telemetry.span(
            "queue.publish",
            component="queue",
            kind=SpanKind.PRODUCER,
            organization_id=organization_id,
            job_id=job["id"],
            job_kind=kind,
            input={"kind": kind, "available_at": job["available_at"]},
        ) as span:
            # The carrier is transport metadata. It intentionally stays out
            # of the business payload so handlers cannot accidentally depend
            # on tracing state.
            job["telemetry"] = {} if link_carrier else inject_carrier()
            job["telemetry_correlation"] = {
                "organization_id": organization_id,
                "job_id": job["id"],
                "job_kind": kind,
                **{
                    key: payload[key]
                    for key in ("task_id", "root_task_id", "session_id", "approval_id")
                    if isinstance(payload.get(key), (str, int))
                },
            }
            if link_carrier:
                # Long waits/resumptions are a new trace segment. Keep the
                # prior context as a link rather than making a human wait part
                # of the original synchronous trace.
                job["telemetry_links"] = [link_carrier]
            created = self.store.enqueue_harness_job(job)
            span.output({"job_id": created["id"], "kind": kind})
            metrics = self.store.telemetry.metrics
            if metrics is not None:
                try:
                    metrics.counter(
                        "toda_queue_jobs_total",
                        labels={"operation": kind, "outcome": "published"},
                    )
                except Exception:
                    pass
        return created

    def observe_metrics(self) -> None:
        """Publish current queue state using only bounded kind labels."""
        metrics = getattr(self.store.telemetry, "metrics", None)
        if metrics is None:
            return
        organizations = self.store.list_organizations()
        counts: dict[str, int] = {}
        oldest: dict[str, float] = {}
        expired = 0
        for organization in organizations:
            organization_id = str(organization["id"])
            for kind, count in self.store.harness_job_counts(organization_id).items():
                counts[kind] = counts.get(kind, 0) + int(count)
            for kind, age in self.store.harness_job_queue_age_seconds(organization_id).items():
                oldest[kind] = max(oldest.get(kind, 0.0), float(age))
            expired += self.store.count_expired_harness_jobs(organization_id)
        for kind, count in counts.items():
            try:
                metrics.gauge(
                    "toda_queue_depth",
                    count,
                    labels={"kind": kind, "lane": "all"},
                )
                metrics.gauge(
                    "toda_queue_oldest_ready_job_age_seconds",
                    oldest.get(kind, 0.0),
                    labels={"kind": kind, "lane": "all"},
                )
            except Exception:
                pass
        try:
            metrics.gauge("toda_queue_expired_leases_total", expired, labels={"lane": "all"})
            metrics.gauge(
                "toda_queue_dead_letters_total",
                sum(
                    int(self.store.harness_job_counts(str(org["id"])).get("dead_letter", 0))
                    for org in organizations
                ),
                labels={"lane": "all"},
            )
        except Exception:
            pass

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
        return finished
