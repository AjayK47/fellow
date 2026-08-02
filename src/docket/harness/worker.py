"""In-process worker over the same durable SQLite job contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from docket.domain.models import new_id


class HarnessWorker:
    def __init__(
        self,
        *,
        queue: Any,
        router: Any,
        attention: Any,
        agent: Any,
        memory_capture: Any,
        delivery: Any,
        indexing: Any | None = None,
        profiles: Any | None = None,
        batch_size: int = 20,
        concurrency: int = 4,
        worker_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.router = router
        self.attention = attention
        self.agent = agent
        self.memory_capture = memory_capture
        self.delivery = delivery
        self.indexing = indexing
        self.profiles = profiles
        self.batch_size = batch_size
        self.concurrency = max(1, concurrency)
        self.worker_id = worker_id or new_id("worker")
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "event.route": lambda payload: self.router.route(payload["event"]),
            "attention.evaluate": lambda payload: self.attention.process_event_payload(
                payload["event"]
            ),
            "agent.run": self._run_agent,
            "agent.human_resume": self._human_resume,
            "memory.capture_run": lambda payload: self.memory_capture.capture_run(
                payload["run_id"]
            ),
            "memory.consolidate_episode": lambda payload: (
                self.memory_capture.consolidate_conversation(
                    payload["organization_id"],
                    payload["conversation_key"],
                    quiet_seconds=int(payload.get("quiet_seconds", 0)),
                )
            ),
            "delivery.send": lambda payload: self.delivery.deliver_run(
                payload["run_id"]
            ),
            "delivery.approval": lambda payload: self.delivery.deliver_approval(
                payload["approval_id"]
            ),
            "delivery.question": lambda payload: self.delivery.deliver_question(
                payload["run_id"], payload["question"], choices=payload.get("choices")
            ),
            "approval.resolve": self._resolve_approval,
        }
        if self.indexing:
            self.handlers["index.backfill"] = (
                lambda payload: self.indexing.backfill()
            )
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
        completed = sum("result" in item for item in results)
        failed = sum("error" in item for item in results)
        return {
            "leased": len(jobs),
            "completed": completed,
            "failed": failed,
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
            return {
                "job_id": job["id"],
                "kind": job["kind"],
                "result": result,
            }
        except Exception as exc:
            self.queue.fail(job, str(exc))
            return {
                "job_id": job["id"],
                "kind": job["kind"],
                "error": str(exc),
            }

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

    def _run_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        outcome = self.agent.run(payload["run_id"])
        data = outcome.model_dump(mode="json")
        if data["state"] == "failed" and self._retryable_agent_error(
            data.get("error") or ""
        ):
            raise RuntimeError(data["error"])
        if data["state"] == "waiting_for_approval" and data.get("approval_id"):
            self.queue.enqueue(
                organization_id=self.agent.runs.get(payload["run_id"])[
                    "organization_id"
                ],
                kind="delivery.approval",
                payload={"approval_id": data["approval_id"]},
                idempotency_key=f"delivery.approval:{data['approval_id']}",
            )
        return data

    @staticmethod
    def _retryable_agent_error(error: str) -> bool:
        lower = error.lower()
        return any(
            marker in lower
            for marker in (
                "timeout",
                "timed out",
                "connection",
                "temporarily unavailable",
                "rate limit",
                "429",
                "500 internal server",
                "502 bad gateway",
                "503 service unavailable",
                "504 gateway",
                "model returned no choices",
            )
        )

    def _human_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.agent.resume_with_human_message(
            payload["run_id"],
            message=payload["message"],
            principal_id=payload["principal_id"],
        ).model_dump(mode="json")

    def _resolve_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        outcome = self.agent.resume_after_approval(
            approval_id=payload["approval_id"],
            approved=bool(payload["approved"]),
            decided_by=payload["decided_by"],
            edited_arguments=payload.get("edited_arguments"),
        )
        data = outcome.model_dump(mode="json")
        if data["state"] == "waiting_for_approval" and data.get("approval_id"):
            run = self.agent.runs.get(data["run_id"])
            self.queue.enqueue(
                organization_id=run["organization_id"],
                kind="delivery.approval",
                payload={"approval_id": data["approval_id"]},
                idempotency_key=f"delivery.approval:{data['approval_id']}",
            )
        return data
