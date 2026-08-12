"""Cheap, model-free heartbeat for durable Task and Session recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


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
            self.queue.enqueue(
                organization_id=organization_id,
                kind="delivery.send",
                payload={"task_id": task["id"]},
                idempotency_key=f"delivery.send:{task['id']}",
            )
            deliveries.append(task["id"])
        return {
            "organization_id": organization_id,
            "requeued_task_ids": recovered,
            "woken_parent_task_ids": woken,
            "queued_delivery_task_ids": deliveries,
        }
