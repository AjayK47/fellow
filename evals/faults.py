"""Small production-eval fault hooks applied around real configured clients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


class _ModelFault:
    def __init__(self, delegate: Any, *, once: bool) -> None:
        self.delegate = delegate
        self.once = once
        self.model = getattr(delegate, "model", "")
        self.provider = getattr(delegate, "provider", "")
        self._raised = False

    def generate(self, **kwargs: Any) -> Any:
        if not self._raised or not self.once:
            self._raised = True
            raise RuntimeError("injected provider failure")
        return self.delegate.generate(**kwargs)


@dataclass
class FaultHooks:
    before_execute: Callable[[dict[str, Any]], None] = lambda _task: None
    after_execute: Callable[[dict[str, Any]], None] = lambda _task: None
    verify: Callable[[], tuple[bool, str]] = lambda: (True, "no injected fault")


def install(brain: Any, fixture: str) -> FaultHooks:
    """Install one explicit fault without replacing the configured provider."""
    if fixture == "model_unavailable":
        brain.company_agent.model = _ModelFault(brain.company_agent.model, once=False)
    elif fixture == "model_error_once":
        brain.company_agent.model = _ModelFault(brain.company_agent.model, once=True)
    elif fixture == "tool_timeout":
        registered = brain.tool_executor.registry.get("integration_execute")
        if registered is not None:
            brain.tool_executor.registry.unregister("integration_execute")

            def timeout(_args: dict[str, Any], _runtime: dict[str, Any]) -> dict[str, Any]:
                raise TimeoutError("injected connector timeout")

            brain.tool_executor.registry.register(registered.definition, timeout)
    elif fixture == "cancel_before_execute":
        return FaultHooks(before_execute=lambda task: brain.store.cancel_task(task["id"]))
    elif fixture == "duplicate_delivery":
        return FaultHooks(after_execute=lambda task: brain.company_agent.execute_task(task["id"]))
    elif fixture == "queue_contention":
        evidence: dict[str, Any] = {}

        def contend(task: dict[str, Any]) -> None:
            session_id = str(task["session_id"])
            first = brain.store.claim_session_manager(
                session_id, owner="eval-worker-a", lease_seconds=30
            )
            second = brain.store.claim_session_manager(
                session_id, owner="eval-worker-b", lease_seconds=30
            )
            brain.store.release_session_manager(
                session_id, owner="eval-worker-a", idle=False
            )
            evidence.update(first=first, second=second)

        return FaultHooks(
            before_execute=contend,
            verify=lambda: (
                evidence == {"first": True, "second": False},
                "one session manager won the atomic lease",
            ),
        )
    elif fixture == "worker_restart":
        evidence: dict[str, Any] = {}

        def restart(task: dict[str, Any]) -> None:
            job = brain.harness_jobs.enqueue(
                organization_id=task["organization_id"],
                kind="eval.worker_restart",
                payload={"task_id": task["id"]},
                idempotency_key=f"eval.worker_restart:{task['id']}",
            )
            first = brain.harness_jobs.lease(
                worker_id="eval-worker-before-restart",
                limit=1,
                kinds=["eval.worker_restart"],
            )
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with brain.store.cursor() as cur:
                cur.execute(
                    """UPDATE harness_jobs
                    SET lease_expires_at = ?,
                        payload = json_set(payload, '$.lease_expires_at', ?)
                    WHERE id = ?""",
                    (expired, expired, job["id"]),
                )
            second = brain.harness_jobs.lease(
                worker_id="eval-worker-after-restart",
                limit=1,
                kinds=["eval.worker_restart"],
            )
            if second:
                brain.harness_jobs.complete(
                    second[0]["id"], {"recovered": True},
                    worker_id="eval-worker-after-restart",
                )
            evidence.update(
                first_owner=bool(first),
                recovered=bool(second and second[0]["id"] == job["id"]),
            )

        return FaultHooks(
            before_execute=restart,
            verify=lambda: (
                evidence == {"first_owner": True, "recovered": True},
                "an expired worker lease was reclaimed and completed",
            ),
        )
    return FaultHooks()
