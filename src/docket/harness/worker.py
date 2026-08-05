"""Durable worker for Session and Task infrastructure jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from docket.domain.models import new_id
from docket.harness.retry import is_retryable_task_error
from docket.harness.task_run_store import TaskRunStore


class HarnessWorker:
    """Process only infrastructure jobs and Task-native execution jobs.

    A Session Manager is always entered through ``session.dispatch``. General
    and Pi child Tasks use their own ``task.run_child``/``task.pi_poll`` jobs.
    There is deliberately no generic agent execution job: ownership belongs to
    the Session or individual Task claim, not to a legacy run record.
    """

    def __init__(
        self,
        *,
        queue: Any,
        router: Any,
        attention: Any,
        memory_capture: Any,
        delivery: Any,
        indexing: Any | None = None,
        profiles: Any | None = None,
        batch_size: int = 20,
        concurrency: int = 4,
        worker_id: str | None = None,
        store: Any,
        task_agent_factory: Any,
        session_manager: Any,
        sessions: Any,
    ) -> None:
        self.queue = queue
        self.router = router
        self.attention = attention
        self.memory_capture = memory_capture
        self.delivery = delivery
        self.indexing = indexing
        self.profiles = profiles
        self.batch_size = batch_size
        self.concurrency = max(1, concurrency)
        self.worker_id = worker_id or new_id("worker")
        self.store = store
        self.task_agent_factory = task_agent_factory
        self.session_manager = session_manager
        self.sessions = sessions
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "event.route": lambda payload: self.router.route(payload["event"]),
            "attention.evaluate": lambda payload: self.attention.process_event_payload(
                payload["event"]
            ),
            "session.dispatch": self._dispatch_session,
            "memory.capture_task": lambda payload: self.memory_capture.capture_task(
                payload["task_id"]
            ),
            "memory.consolidate_episode": lambda payload: (
                self.memory_capture.consolidate_conversation(
                    payload["organization_id"],
                    payload["conversation_key"],
                    quiet_seconds=int(payload.get("quiet_seconds", 0)),
                )
            ),
            "delivery.send": lambda payload: self.delivery.deliver_task(
                payload["task_id"]
            ),
            "delivery.failure": lambda payload: self.delivery.deliver_task_failure(
                payload["task_id"]
            ),
            "delivery.approval": lambda payload: self.delivery.deliver_approval(
                payload["approval_id"]
            ),
            "delivery.question": lambda payload: self.delivery.deliver_question(
                payload["task_id"],
                payload["question"],
                choices=payload.get("choices"),
            ),
            "delivery.status_update": lambda payload: self.delivery.deliver_status_update(
                payload["task_id"]
            ),
            "approval.resolve": self._resolve_approval,
        }
        if self.indexing:
            self.handlers["index.backfill"] = lambda payload: self.indexing.backfill()
        if self.profiles:
            self.handlers["profile.refresh"] = lambda payload: {
                "profile": self.profiles.refresh(
                    payload["organization_id"], payload["entity_id"]
                )
            }

    def register(
        self, kind: str, handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        self.handlers[kind] = handler

    def drain_once(self, *, kinds: list[str] | None = None) -> dict[str, Any]:
        jobs = self.queue.lease(
            worker_id=self.worker_id, limit=self.batch_size, kinds=kinds
        )
        results: list[dict[str, Any]] = []
        if jobs:
            with ThreadPoolExecutor(
                max_workers=min(self.concurrency, len(jobs)),
                thread_name_prefix="docket-job",
            ) as pool:
                futures = [pool.submit(self._process_job, job) for job in jobs]
                for future in as_completed(futures):
                    results.append(future.result())
        return {
            "leased": len(jobs),
            "completed": sum("result" in item for item in results),
            "failed": sum("error" in item for item in results),
            "results": results,
        }

    def _process_job(self, job: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(job["kind"])
        if not handler:
            error = f"no handler for job kind {job['kind']}"
            self.queue.fail(job, error)
            return {"job_id": job["id"], "kind": job["kind"], "error": error}
        try:
            result = handler(job["payload"]) or {}
            self.queue.complete(job["id"], result, worker_id=self.worker_id)
            return {"job_id": job["id"], "kind": job["kind"], "result": result}
        except Exception as exc:
            self.queue.fail(job, str(exc))
            return {"job_id": job["id"], "kind": job["kind"], "error": str(exc)}

    def drain_until_idle(
        self, *, max_cycles: int = 100, kinds: list[str] | None = None
    ) -> dict[str, Any]:
        cycles = 0
        completed = 0
        failed = 0
        all_results: list[dict[str, Any]] = []
        while cycles < max_cycles:
            batch = self.drain_once(kinds=kinds)
            cycles += 1
            completed += batch["completed"]
            failed += batch["failed"]
            all_results.extend(batch["results"])
            if batch["leased"] == 0:
                break
        return {
            "cycles": cycles,
            "completed": completed,
            "failed": failed,
            "results": all_results,
        }

    def _dispatch_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        outcome = self.session_manager.dispatch(payload["session_id"])
        if outcome is None:
            session = self.sessions.get(payload["session_id"])
            if session and session.get("manager_state") == "idle" and int(
                session.get("processed_revision", 0)
            ) >= int(session.get("manager_revision", 0)):
                # Another dispatch already processed this revision. Distinct
                # revision jobs are deliberately allowed to coalesce, so an
                # old job reaching an idle, fully-processed Session is a
                # successful no-op rather than a retry loop.
                return {"dispatched": False, "coalesced": True}
            # A live Manager owns the Session (or ownership changed between
            # dispatch() and this read). Requeue instead of acknowledging the
            # wake: once the owner releases, this job is the durable fallback
            # that prevents a message arriving mid-run from being stranded.
            raise RuntimeError("session_manager_busy")
        data = outcome.model_dump(mode="json")
        data["dispatched"] = True
        if data["state"] == "failed" and is_retryable_task_error(data.get("error") or ""):
            raise RuntimeError(data["error"])
        if data["state"] == "waiting_for_approval" and data.get("approval_id"):
            task = self.store.get_task(data["task_id"])
            self.queue.enqueue(
                organization_id=task["organization_id"],
                kind="delivery.approval",
                payload={"approval_id": data["approval_id"]},
                idempotency_key=f"delivery.approval:{data['approval_id']}",
            )
        if data["state"] == "failed":
            self._maybe_notify_failure(data["task_id"])
        return data

    def _approval_agent(self, approval_id: str) -> Any:
        approval = self.store.get_harness_record("approvals", approval_id)
        if not approval:
            raise KeyError(approval_id)
        task_id = approval.get("task_id")
        task = self.store.get_task(task_id) if task_id else None
        if not task:
            raise KeyError(task_id or approval_id)
        return self.task_agent_factory(
            TaskRunStore(self.store, owner=new_id("approval"), lease_seconds=1800)
        )

    def _resolve_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent = self._approval_agent(payload["approval_id"])
        outcome = agent.resume_after_approval(
            approval_id=payload["approval_id"],
            approved=bool(payload["approved"]),
            decided_by=payload["decided_by"],
            edited_arguments=payload.get("edited_arguments"),
        )
        self.delivery.mark_approval_decided(
            payload["approval_id"],
            approved=bool(payload["approved"]),
            decided_by=payload["decided_by"],
        )
        data = outcome.model_dump(mode="json")
        resolved_task = self.store.get_task(data["task_id"])
        if resolved_task:
            self.store.bump_task_revision(resolved_task["id"])
            parent = (
                self.store.get_task(resolved_task["parent_task_id"])
                if resolved_task.get("parent_task_id")
                else None
            )
            if (
                parent
                and parent["state"] not in {"completed", "failed", "cancelled"}
                and data["state"] in {"completed", "failed", "cancelled"}
            ):
                self.store.bump_task_revision(parent["id"])
                self.sessions.notify(
                    resolved_task["session_id"],
                    reason=f"child_task.approval_{data['state']}",
                )
        if data["state"] == "waiting_for_approval" and data.get("approval_id"):
            task = self.store.get_task(data["task_id"])
            self.queue.enqueue(
                organization_id=task["organization_id"],
                kind="delivery.approval",
                payload={"approval_id": data["approval_id"]},
                idempotency_key=f"delivery.approval:{data['approval_id']}",
            )
        if data["state"] == "failed":
            self._maybe_notify_failure(data["task_id"])
        return data

    def _maybe_notify_failure(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or (task.get("metadata") or {}).get("suppress_delivery"):
            return
        self.queue.enqueue(
            organization_id=task["organization_id"],
            kind="delivery.failure",
            payload={"task_id": task_id},
            idempotency_key=f"delivery.failure:{task_id}",
        )
