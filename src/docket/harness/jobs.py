"""SQLite-backed durable queue with leases, retries, and idempotency."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.storage.db import BrainStore


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
            "max_attempts": max_attempts or self.max_attempts,
            "idempotency_key": idempotency_key,
            "created_at": now,
            "updated_at": now,
        }
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
        self, job_id: str, result: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return self.store.finish_harness_job(
            job_id, status="completed", result=result or {}
        )

    def fail(self, job: dict[str, Any], error: str) -> dict[str, Any] | None:
        attempts = int(job.get("attempts", 0))
        max_attempts = int(job.get("max_attempts", self.max_attempts))
        if attempts >= max_attempts:
            return self.store.finish_harness_job(
                job["id"], status="dead_letter", error=error
            )
        retry_at = (
            utcnow() + timedelta(seconds=min(2**attempts, 300))
        ).isoformat()
        return self.store.finish_harness_job(
            job["id"],
            status="queued",
            error=error,
            retry_at=retry_at,
        )
