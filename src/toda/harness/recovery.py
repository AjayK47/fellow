"""Cheap, model-free heartbeat for durable Task and Session recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import time

# How many times the sweep will try to rescue one undelivered answer before
# leaving it for a human. Bounded because the sweep runs every 30s forever:
# an answer that genuinely cannot be delivered (deleted channel, revoked
# token) would otherwise be retried until the process stops.
_MAX_DELIVERY_RECOVERIES = 3


class RecoveryHeartbeat:
    def __init__(
        self,
        *,
        store: Any,
        sessions: Any,
        queue: Any,
        workspaces: Any | None = None,
        workspace_retention_hours: int = 24,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.queue = queue
        self.workspaces = workspaces
        self.workspace_retention_hours = max(1, int(workspace_retention_hours))
        self._last_tick_monotonic: float | None = None
        self._last_tick_result: dict[str, Any] | None = None

    def tick(self, organization_id: str) -> dict[str, Any]:
        self._last_tick_monotonic = time.monotonic()
        self.store.telemetry.record(
            "recovery.heartbeat", component="recovery", phase="started",
            input={"organization_id": organization_id}, organization_id=organization_id,
        )
        now = datetime.now(timezone.utc).isoformat()
        recovered: list[str] = []
        woken: list[str] = []
        deliveries: list[str] = []
        cleaned_workspaces: list[str] = []
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
        if self.workspaces is not None:
            active_task_ids = set(self.store.list_active_task_ids(organization_id))
            cleaned_workspaces = self.workspaces.cleanup_expired_tasks(
                organization_id,
                older_than_hours=self.workspace_retention_hours,
                active_task_ids=active_task_ids,
            )
        result = {
            "organization_id": organization_id,
            "requeued_task_ids": recovered,
            "woken_parent_task_ids": woken,
            "queued_delivery_task_ids": deliveries,
            "cleaned_workspace_paths": cleaned_workspaces,
        }
        self.store.telemetry.record(
            "recovery.heartbeat", component="recovery", phase="completed",
            output=result, organization_id=organization_id,
        )
        self._last_tick_result = result
        return result

    def health(self, *, max_age_seconds: float = 90.0) -> dict[str, Any]:
        age = (
            None
            if self._last_tick_monotonic is None
            else max(0.0, time.monotonic() - self._last_tick_monotonic)
        )
        return {
            "ok": age is not None and age <= max_age_seconds,
            "last_tick_age_seconds": age,
            "last_result": self._last_tick_result,
        }

    def _delivery_recovery_attempts(self, organization_id: str, task_id: str) -> int:
        return self.store.count_jobs_with_key_prefix(
            organization_id, f"delivery.recover:{task_id}:"
        )
