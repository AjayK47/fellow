"""SQLite-backed durable queue with leases, retries, and idempotency."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.storage.db import BrainStore

# Session and child Task execution can drive CompanyAgent._loop() to completion -- potentially
# several iterations, each with a model round-trip plus tool calls -- and can
# legitimately run past the default 60s lease. Without a longer lease, a
# second worker process (the documented DOCKET_WORKER_ENABLED=false +
# separate docket-worker topology) could see the lease as expired mid-run and
# re-lease + re-execute the same run concurrently. A stale lease only matters
# for actual crash recovery, so a generous static override is enough here --
# true lease renewal isn't needed for jobs this infrequent.
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
# "Docket ignored me" stall with nothing wrong in any log.
_CONTENTION_RETRY_SECONDS = 1.0


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
        return self.store.enqueue_harness_job(job)

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
        return self.store.finish_harness_job(
            job_id,
            status="completed",
            result=result or {},
            lease_owner=worker_id,
        )

    def fail(
        self,
        job: dict[str, Any],
        error: str,
        *,
        retry_in_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        attempts = int(job.get("attempts", 0))
        max_attempts = int(job.get("max_attempts", self.max_attempts))
        if attempts >= max_attempts:
            return self.store.finish_harness_job(
                job["id"],
                status="dead_letter",
                error=error,
                lease_owner=job.get("lease_owner"),
            )
        delay = (
            retry_in_seconds
            if retry_in_seconds is not None
            else min(2**attempts, 300)
        )
        retry_at = (utcnow() + timedelta(seconds=delay)).isoformat()
        return self.store.finish_harness_job(
            job["id"],
            status="queued",
            error=error,
            retry_at=retry_at,
            lease_owner=job.get("lease_owner"),
        )
