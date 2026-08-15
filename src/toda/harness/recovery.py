"""Cheap, model-free heartbeat for durable Task and Session recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# How many times the sweep will try to rescue one undelivered answer before
# leaving it for a human. Bounded because the sweep runs every 30s forever:
# an answer that genuinely cannot be delivered (deleted channel, revoked
# token) would otherwise be retried until the process stops.
_MAX_DELIVERY_RECOVERIES = 3


class RecoveryHeartbeat:
    def __init__(self, *, store: Any, sessions: Any, queue: Any) -> None:
        self.store = store
        self.sessions = sessions
        self.queue = queue

    def tick(self, organization_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        recovered: list[str] = []
        woken: list[str] = []
        deliveries: list[str] = []
        for task in self.store.list_expired_running_tasks(
            organization_id, now=now, limit=100
        ):
            if self.store.requeue_expired_task(task["id"], now=now):
                recovered.append(task["id"])
                if task.get("parent_task_id"):
                    self.sessions.notify(
                        task["session_id"], reason="child_task.stale_requeued"
                    )

        for task in self.store.list_parents_waiting_for_children(
            organization_id, limit=100
        ):
            waiting = task.get("waiting_for") or {}
            children = [
                self.store.get_task(child_id) for child_id in waiting.get("ids") or []
            ]
            if children and all(
                child and child.get("state") in {"completed", "failed", "cancelled"}
                for child in children
            ):
                self.sessions.notify(
                    task["session_id"], reason="recovery.children_terminal"
                )
                woken.append(task["id"])

        for task in self.store.list_completed_tasks_missing_delivery(
            organization_id, limit=100
        ):
            # Deliberately NOT `delivery.send:{task_id}` -- that is the key
            # the Task's own completion already used, and
            # enqueue_harness_job() dedupes on
            # UNIQUE(organization_id, idempotency_key) by returning the
            # EXISTING row. That row is already completed or dead-lettered,
            # so re-enqueueing under it queued nothing and this entire
            # safety net could never actually re-deliver anything.
            #
            # An attempt-numbered key gets a real job each sweep while
            # keeping the original attempt's history intact and the recovery
            # attempts visible and countable in the jobs table.
            attempt = self._delivery_recovery_attempts(organization_id, task["id"])
            if attempt >= _MAX_DELIVERY_RECOVERIES:
                continue
            self.queue.enqueue(
                organization_id=organization_id,
                kind="delivery.send",
                payload={"task_id": task["id"]},
                idempotency_key=f"delivery.recover:{task['id']}:{attempt + 1}",
            )
            deliveries.append(task["id"])
        return {
            "organization_id": organization_id,
            "requeued_task_ids": recovered,
            "woken_parent_task_ids": woken,
            "queued_delivery_task_ids": deliveries,
        }

    def _delivery_recovery_attempts(self, organization_id: str, task_id: str) -> int:
        return self.store.count_jobs_with_key_prefix(
            organization_id, f"delivery.recover:{task_id}:"
        )
