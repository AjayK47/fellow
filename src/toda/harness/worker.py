"""Durable worker for Session and Task infrastructure jobs."""

from __future__ import annotations

import threading
import time
import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from toda.domain.models import new_id
from toda.harness.jobs import _CONTENTION_RETRY_SECONDS, JobContention
from toda.harness.retry import is_permanent_task_error, is_retryable_task_error
from toda.harness.task_run_store import TaskRunStore
from opentelemetry.trace import SpanKind

from toda.telemetry.context import link_from_carrier, use_carrier


log = logging.getLogger("toda.harness.worker")

# Job kinds that drive a full agent reasoning loop and can legitimately run
# for many minutes. Everything else -- Slack event routing, attention,
# delivery, memory capture -- finishes in well under a second.
#
# These are separated because drain_once() waits for its whole leased batch
# to finish before returning: one long agent job in a batch blocks every
# fast job behind it (head-of-line blocking). That was a real, measured
# production problem, not a theoretical one -- inbound Slack messages sat
# unrouted for up to 66 seconds while a single 57-second agent run held the
# batch, which in turn widened the window for a Task to observe (and act
# on) a message that had not yet been routed to it. Running the two classes
# in separate lanes keeps event routing responsive no matter how long an
# agent run takes.
AGENT_JOB_KINDS = (
    "session.dispatch",
    "task.run_child",
    "task.pi_poll",
    "approval.resolve",
)

# Knowledge work (chunking, embedding, model extraction, episode
# consolidation, reality/graph updates) is slow/remote like the agent lane,
# but it isn't a conversational turn -- nothing is waiting on it the way a
# Slack reply is. Giving it a third, smaller pool means a burst of ingest
# traffic can never starve the fast control-path lane (event routing,
# attention, delivery) the way sharing one pool would (memory
# ingestion/retrieval plan §9 -- "control jobs must remain available ...
# while knowledge jobs are slow").
KNOWLEDGE_JOB_KINDS = (
    "knowledge.index_evidence",
    "knowledge.consolidate_episode",
    "memory.capture_task",
    "skill.curate_task",
)


def _agent_share(concurrency: int) -> int:
    """How much of the main budget the agent lane gets by default.

    Two thirds. Agent jobs are the slow ones and there are usually more of
    them wanting to run at once, while the infrastructure lane only needs
    enough headroom to stay responsive -- its jobs finish in well under a
    second, so a handful of slots absorbs a large arrival rate.
    """
    return max(1, (concurrency * 2) // 3)


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
        ingestion: Any | None = None,
        indexing: Any | None = None,
        profiles: Any | None = None,
        batch_size: int = 20,
        concurrency: int = 4,
        agent_concurrency: int | None = None,
        knowledge_concurrency: int = 4,
        worker_id: str | None = None,
        store: Any,
        task_agent_factory: Any,
        session_manager: Any,
        sessions: Any,
        run_inspection: Any | None = None,
    ) -> None:
        self.queue = queue
        self.router = router
        self.attention = attention
        self.memory_capture = memory_capture
        self.delivery = delivery
        self.ingestion = ingestion
        self.indexing = indexing
        self.profiles = profiles
        self.batch_size = batch_size
        self.concurrency = max(1, concurrency)
        self.knowledge_concurrency = max(1, knowledge_concurrency)
        self.worker_id = worker_id or new_id("worker")
        self.store = store
        # Three lanes, three pools. `pump()` (below) keeps each topped up
        # without ever waiting on the slowest job in a batch.
        #
        # The agent lane used to share the infrastructure pool: separating
        # the *leases* bounded head-of-line blocking within a batch, but a
        # burst of long agent runs could still occupy every slot in the
        # shared pool and leave event routing and delivery with nowhere to
        # run -- the same starvation the split was introduced to prevent,
        # one level down. Capacity has to be partitioned, not just leases.
        self._agent_concurrency = max(1, agent_concurrency or _agent_share(self.concurrency))
        self._infra_concurrency = max(1, self.concurrency - self._agent_concurrency)
        self._pool = ThreadPoolExecutor(
            max_workers=self._infra_concurrency, thread_name_prefix="toda-job"
        )
        self._agent_pool = ThreadPoolExecutor(
            max_workers=self._agent_concurrency, thread_name_prefix="toda-agent"
        )
        self._knowledge_pool = ThreadPoolExecutor(
            max_workers=self.knowledge_concurrency, thread_name_prefix="toda-knowledge"
        )
        self._inflight: set[Future[dict[str, Any]]] = set()
        self._inflight_lock = threading.Lock()
        self._agent_inflight: set[Future[dict[str, Any]]] = set()
        self._agent_inflight_lock = threading.Lock()
        self._knowledge_inflight: set[Future[dict[str, Any]]] = set()
        self._knowledge_inflight_lock = threading.Lock()
        self._lane_heartbeat: dict[str, float | None] = {
            "infrastructure": None,
            "agent": None,
            "knowledge": None,
        }
        self.task_agent_factory = task_agent_factory
        self.session_manager = session_manager
        self.sessions = sessions
        self.run_inspection = run_inspection
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "event.route": lambda payload: self.router.route(payload["event"]),
            "attention.evaluate": lambda payload: self.attention.process_event_payload(
                payload["event"]
            ),
            "session.dispatch": self._dispatch_session,
            "memory.capture_task": lambda payload: self.memory_capture.capture_task(
                payload["task_id"]
            ),
            "delivery.send": lambda payload: self.delivery.deliver_task(
                payload["task_id"]
            ),
            "delivery.conversation": lambda payload: self.delivery.deliver_conversation(
                payload["session_id"], payload["message_id"]
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
            "delivery.acknowledge": lambda payload: self.delivery.deliver_acknowledgement(
                payload["task_id"]
            ),
            "approval.resolve": self._resolve_approval,
        }
        if self.ingestion:
            self.handlers["knowledge.index_evidence"] = self._index_evidence
        if self.memory_capture:
            self.handlers["knowledge.consolidate_episode"] = self._consolidate_episode
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

    def agent_job_kinds(self) -> list[str]:
        """The long-running lane: only kinds this worker actually handles."""
        return [kind for kind in AGENT_JOB_KINDS if kind in self.handlers]

    def knowledge_job_kinds(self) -> list[str]:
        """The knowledge lane: chunking/embedding/extraction/episode work,
        isolated so a burst of ingest traffic can't starve control jobs."""
        return [kind for kind in KNOWLEDGE_JOB_KINDS if kind in self.handlers]

    def infrastructure_job_kinds(self) -> list[str]:
        """The fast lane: everything that is neither a full agent reasoning
        loop nor knowledge processing."""
        excluded = set(AGENT_JOB_KINDS) | set(KNOWLEDGE_JOB_KINDS)
        return [kind for kind in self.handlers if kind not in excluded]

    def _is_knowledge_lane(self, kinds: list[str] | None) -> bool:
        return bool(kinds) and all(kind in KNOWLEDGE_JOB_KINDS for kind in kinds)

    def _is_agent_lane(self, kinds: list[str] | None) -> bool:
        return bool(kinds) and all(kind in AGENT_JOB_KINDS for kind in kinds)

    def _lane(
        self, kinds: list[str] | None
    ) -> tuple[ThreadPoolExecutor, set[Future[dict[str, Any]]], threading.Lock, int]:
        """The pool, inflight set, lock and capacity serving `kinds`."""
        if self._is_knowledge_lane(kinds):
            return (
                self._knowledge_pool,
                self._knowledge_inflight,
                self._knowledge_inflight_lock,
                self.knowledge_concurrency,
            )
        if self._is_agent_lane(kinds):
            return (
                self._agent_pool,
                self._agent_inflight,
                self._agent_inflight_lock,
                self._agent_concurrency,
            )
        return self._pool, self._inflight, self._inflight_lock, self._infra_concurrency

    def _pool_for(self, kinds: list[str] | None) -> ThreadPoolExecutor:
        return self._lane(kinds)[0]

    def available_capacity(self, *, kinds: list[str] | None = None) -> int:
        """Free slots in the pool that would serve `kinds` right now."""
        _pool, inflight, lock, capacity = self._lane(kinds)
        with lock:
            live = {future for future in inflight if not future.done()}
            inflight.clear()
            inflight.update(live)
            return max(0, capacity - len(live))

    def pump(self, *, kinds: list[str] | None = None) -> dict[str, Any]:
        """Lease up to the currently free capacity and start those jobs
        WITHOUT waiting for them to finish. This is what the live worker
        loops call.

        drain_once() below blocks until every job in its batch completes,
        which means one slow job stalls everything leased beside it -- the
        exact head-of-line blocking that had inbound Slack messages waiting
        on a 57-second agent run. Splitting jobs into lanes bounded that
        blast radius; this removes it, because a lane keeps leasing and
        starting new work while its slow jobs are still running.

        Leasing is capped at free capacity rather than batch_size so a job
        is never leased -- and its lease clock never started -- while it
        sits waiting for a thread. `kinds` also selects which pool (main vs
        knowledge) the batch runs on, so a knowledge-lane call can never
        consume the main lane's capacity or vice versa.
        """
        lane_name = (
            "knowledge" if self._is_knowledge_lane(kinds)
            else "agent" if self._is_agent_lane(kinds)
            else "infrastructure"
        )
        self._lane_heartbeat[lane_name] = time.monotonic()
        try:
            self.queue.observe_metrics()
        except Exception:
            log.debug("queue.metrics_failed", exc_info=True)
        capacity = min(self.available_capacity(kinds=kinds), self.batch_size)
        if capacity <= 0:
            return {"leased": 0, "saturated": True}
        jobs = self.queue.lease(
            worker_id=self.worker_id, limit=capacity, kinds=kinds
        )
        if jobs:
            pool, inflight, lock, _capacity = self._lane(kinds)
            with lock:
                for job in jobs:
                    inflight.add(pool.submit(self._process_job, job))
        return {"leased": len(jobs), "saturated": False}

    def health(self, *, max_age_seconds: float = 10.0) -> dict[str, Any]:
        now = time.monotonic()
        lanes = {
            name: {
                "last_heartbeat_age_seconds": (
                    None if stamp is None else max(0.0, now - stamp)
                ),
                "running": stamp is not None and now - stamp <= max_age_seconds,
            }
            for name, stamp in self._lane_heartbeat.items()
        }
        return {
            "ok": bool(lanes) and all(item["running"] for item in lanes.values()),
            "lanes": lanes,
        }

    def drain_once(self, *, kinds: list[str] | None = None) -> dict[str, Any]:
        """Lease a batch and wait for all of it. Used by tests and by
        drain_until_idle(); the live loops use pump() instead."""
        jobs = self.queue.lease(
            worker_id=self.worker_id, limit=self.batch_size, kinds=kinds
        )
        pool = self._pool_for(kinds)
        results: list[dict[str, Any]] = []
        if jobs:
            futures = [pool.submit(self._process_job, job) for job in jobs]
            for future in as_completed(futures):
                results.append(future.result())
        return {
            "leased": len(jobs),
            "completed": sum("result" in item for item in results),
            "failed": sum("error" in item for item in results),
            "results": results,
        }

    def close(self) -> None:
        """Stop all three pools. In-flight jobs are allowed to finish so a
        shutdown never abandons a leased job mid-execution."""
        self._pool.shutdown(wait=True)
        self._agent_pool.shutdown(wait=True)
        self._knowledge_pool.shutdown(wait=True)

    def _process_job(self, job: dict[str, Any]) -> dict[str, Any]:
        # Pool threads start with empty context. Reattach the W3C carrier
        # persisted by JobQueue, then make the dequeue/handler interval a
        # consumer span so queue delay and execution are queryable together.
        with use_carrier(job.get("telemetry")):
            started = time.perf_counter()
            delay_ms = 0.0
            with self.store.telemetry.span(
                "queue.process",
                component="queue",
                kind=SpanKind.CONSUMER,
                organization_id=job.get("organization_id"),
                job_id=job["id"],
                job_kind=job["kind"],
                attempt_number=int(job.get("attempts") or 0),
                links=[
                    link
                    for link in (
                        link_from_carrier(carrier)
                        for carrier in job.get("telemetry_links") or []
                    )
                    if link is not None
                ],
            ) as span:
                created_at = job.get("created_at")
                if created_at:
                    try:
                        created = datetime.fromisoformat(str(created_at))
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        delay_ms = (
                            datetime.now(timezone.utc) - created
                        ).total_seconds() * 1000
                        span.set_attribute("toda.queue.delay_ms", round(max(0.0, delay_ms), 3))
                    except (TypeError, ValueError):
                        pass
                result = self._process_job_traced(job)
                outcome = "failed" if result.get("error") else "completed"
                span.set_attribute("toda.queue.outcome", outcome)
                if result.get("error"):
                    span.error(result["error"])
                span.output({"kind": job["kind"], "outcome": outcome})
                metrics = self.store.telemetry.metrics
                if metrics is not None:
                    try:
                        metrics.counter(
                            "toda_queue_jobs_total",
                            labels={"operation": job["kind"], "outcome": outcome},
                        )
                        metrics.histogram(
                            "toda_queue_process_duration_ms",
                            (time.perf_counter() - started) * 1000,
                            labels={"operation": job["kind"]},
                        )
                        if created_at:
                            metrics.histogram(
                                "toda_queue_wait_duration_ms",
                                max(0.0, delay_ms),
                                labels={"operation": job["kind"]},
                            )
                    except Exception:
                        pass
                pass
            if self.run_inspection:
                task_id = (job.get("payload") or {}).get("task_id")
                if not task_id and (job.get("payload") or {}).get("approval_id"):
                    approval = self.store.get_harness_record(
                        "approvals", str(job["payload"]["approval_id"])
                    )
                    task_id = approval.get("task_id") if approval else None
                if task_id:
                    try:
                        self.run_inspection.reconcile_task(str(task_id))
                    except Exception:
                        # Inspection is a projection; it cannot make a
                        # successfully completed delivery fail.
                        log.exception("run_inspection.reconcile_failed", extra={"task_id": task_id})
            return result

    def _process_job_traced(self, job: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(job["kind"])
        if not handler:
            error = f"no handler for job kind {job['kind']}"
            self.queue.fail(job, error)
            return {"job_id": job["id"], "kind": job["kind"], "error": error}
        try:
            result = handler(job["payload"]) or {}
            self.queue.complete(job["id"], result, worker_id=self.worker_id)
            return {"job_id": job["id"], "kind": job["kind"], "result": result}
        except JobContention as exc:
            # Normal contention, not a failure: retry on a short fixed
            # interval rather than the exponential error backoff. See
            # jobs.py's _CONTENTION_RETRY_SECONDS for why this distinction
            # is user-visible.
            self.queue.fail(job, str(exc), retry_in_seconds=_CONTENTION_RETRY_SECONDS)
            return {"job_id": job["id"], "kind": job["kind"], "error": str(exc)}
        except Exception as exc:
            # A permanent failure must not be retried. `session.dispatch`
            # allows 100 attempts because contention is normal, but that
            # ceiling applied to a provider rejecting the request outright
            # meant one poisoned transcript re-ran the whole discovery
            # sequence a hundred times -- observed live as a Task that kept
            # searching HubSpot, describing the same action and 400ing, for
            # minutes, at full model cost per lap.
            #
            # Nothing about an invalid prompt resolves by sending it again.
            if is_permanent_task_error(str(exc)):
                self.queue.fail(job, str(exc), permanent=True)
                return {
                    "job_id": job["id"], "kind": job["kind"],
                    "error": str(exc), "permanent": True,
                }
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

    def _index_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Knowledge-lane continuation of a persisted evidence row: chunk +
        embed always, extract only when memory_policy calls for it inline
        ("immediate" — episode/evidence_only defer or skip extraction, see
        harness/event_policy.py)."""
        source = self.store.get_source(payload["source_id"])
        if not source:
            return {"skipped": "source_not_found"}
        memory_policy = payload.get("memory_policy", "evidence_only")
        learn = memory_policy == "immediate"
        result = self.ingestion.process_knowledge(
            source, learn=learn, processing_mode="memory" if learn else "reference"
        )
        return {
            "source_id": source["id"],
            "memory_policy": memory_policy,
            "memory_count": len(result.get("memories") or []),
        }

    def _consolidate_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.memory_capture.consolidate_episode(
            payload["organization_id"],
            group_key=payload["group_key"],
            group_kind=payload.get("group_kind") or "custom",
            source_system=payload.get("source_system") or "unknown",
            scope_id=payload.get("scope_id") or "scope_org_public",
            quiet_seconds=int(payload.get("quiet_seconds", 0)),
        )

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
            # JobContention rather than a bare RuntimeError: a Manager
            # being busy is the normal case for any follow-up sent mid-run,
            # and this job is the ONLY thing that picks the pending
            # revision back up once the Manager releases -- exponential
            # error backoff here meant a follow-up could sit unanswered for
            # minutes after Toda was already free.
            raise JobContention("session_manager_busy")
        data = outcome.model_dump(mode="json")
        data["dispatched"] = True
        if data["state"] == "failed" and is_retryable_task_error(data.get("error") or ""):
            raise RuntimeError(data["error"])
        if data["state"] == "waiting" and data.get("approval_id"):
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
        # Collapse the Approve/Deny buttons before resuming the actual Task,
        # not after. resume_after_approval() below can run the resumed model
        # loop for a long time (more tool calls, another turn, sometimes a
        # whole additional response) -- sequencing the button-collapse edit
        # after it meant a person saw their own click do nothing for as long
        # as that resumed work took, and would click Approve again thinking
        # it hadn't registered. The decision itself (approved/decided_by) is
        # already final at this point regardless of how the resumed run goes.
        self.delivery.mark_approval_decided(
            payload["approval_id"],
            approved=bool(payload["approved"]),
            decided_by=payload["decided_by"],
        )
        outcome = agent.resume_after_approval(
            approval_id=payload["approval_id"],
            approved=bool(payload["approved"]),
            decided_by=payload["decided_by"],
            edited_arguments=payload.get("edited_arguments"),
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
        if data["state"] == "waiting" and data.get("approval_id"):
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
