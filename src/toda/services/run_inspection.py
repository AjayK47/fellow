"""Task-centric logical-run assembly and completeness reconciliation."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
from typing import Any

from toda.harness.versioning import run_configuration_versions
from toda.telemetry.validation import validate_trace

class RunInspectionService:
    """Shared read model for the API, readiness and evaluation paths.

    This service deliberately reads only redacted span metadata and bounded
    lifecycle projections. It is safe to run after every terminal transition;
    persistence is idempotent on ``task_id``.
    """

    def __init__(self, *, store: Any, settings: Any, catalog: Any) -> None:
        self.store = store
        self.settings = settings
        self.catalog = catalog

    def task_spans(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        organization_id = str(task["organization_id"])
        root_task_id = str(task.get("root_task_id") or task["id"])
        related_tasks = self.store.list_tasks(
            organization_id, root_task_id=root_task_id, limit=10_000
        )
        if not related_tasks:
            related_tasks = [task]
        spans: dict[str, dict[str, Any]] = {}
        trace_ids: set[str] = set()
        for related in related_tasks:
            for item in self.store.list_otel_spans(
                None,
                task_id=str(related["id"]),
                organization_id=organization_id,
                limit=10_000,
            ):
                spans[str(item["span_id"])] = item
                if item.get("trace_id"):
                    trace_ids.add(str(item["trace_id"]))
        # A resumed approval or delegated child may be discoverable only by a
        # standard OTel link. Follow those linked trace IDs recursively with cycle protection.
        visited_traces: set[str] = set(trace_ids)
        traces_to_scan = list(trace_ids)
        while traces_to_scan:
            tid = traces_to_scan.pop(0)
            for item in self.store.list_otel_spans(
                tid, organization_id=organization_id, limit=10_000
            ):
                spans[str(item["span_id"])] = item
                for link in item.get("links") or []:
                    link_trace_id = link.get("trace_id") if isinstance(link, dict) else None
                    if link_trace_id:
                        lt_str = str(link_trace_id)
                        if lt_str not in visited_traces:
                            visited_traces.add(lt_str)
                            traces_to_scan.append(lt_str)
                            trace_ids.add(lt_str)
        return sorted(spans.values(), key=lambda item: item.get("start_time_ns") or 0)

    @staticmethod
    def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
        """Expose lifecycle/configuration facts, not business payloads."""
        allowed = {
            "id", "organization_id", "session_id", "root_task_id",
            "parent_task_id", "state", "worker_type", "trigger_kind",
            "correlation_id",
            "trigger_event_id", "iteration_count", "tool_call_count",
            "max_iterations", "max_tool_calls", "max_model_calls",
            "max_total_tokens", "max_cost_usd", "max_duration_seconds",
            "created_at", "updated_at", "completed_at", "started_at",
            "waiting_for", "pending_approval_id", "pending_child_task_ids",
        }
        safe = {key: task.get(key) for key in allowed if key in task}
        goal = str(task.get("goal") or "")
        if goal:
            safe["goal_chars"] = len(goal)
            safe["goal_sha256"] = sha256(goal.encode("utf-8")).hexdigest()
        return safe

    @staticmethod
    def _safe_errors(task: dict[str, Any], spans: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        def add(source: str, operation: str, category: Any) -> None:
            value = str(category or "execution_error")[:120]
            key = (source, operation, value)
            if key not in seen:
                seen.add(key)
                errors.append({"source": source, "operation": operation, "category": value})

        if task.get("error"):
            add("task", "agent.invoke", str(task["error"]).split(":", 1)[0])
        for span in spans:
            attrs = span.get("attributes") or {}
            if span.get("status_code") == "ERROR" or attrs.get("toda.operation.outcome") == "failed":
                add("span", str(span.get("name") or "unknown"), attrs.get("error.type") or span.get("status_description"))
        for record in records:
            if record.get("status") in {"failed", "denied"}:
                add("tool", str(record.get("tool_name") or "unknown"), record.get("status"))
        return errors

    def _queue_jobs(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        ids = {str(task["id"])}
        ids.update(
            str(item["id"])
            for item in self.store.list_tasks(
                task["organization_id"],
                root_task_id=str(task.get("root_task_id") or task["id"]),
                limit=10_000,
            )
        )
        jobs: dict[str, dict[str, Any]] = {}
        for task_id in ids:
            for job in self.store.list_harness_jobs_for_task(task["organization_id"], task_id):
                jobs[str(job["id"])] = job
        return list(jobs.values())

    def _delivery_required(self, task: dict[str, Any]) -> bool:
        metadata = task.get("metadata") or {}
        destination = (task.get("envelope") or {}).get("destination") or {}
        return bool(metadata.get("delivery_required") or (destination and not metadata.get("suppress_delivery")))

    def summary(self, task: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        spans = self.task_spans(task)
        organization_id = str(task["organization_id"])
        root_task_id = str(task.get("root_task_id") or task["id"])
        related_tasks = self.store.list_tasks(
            organization_id, root_task_id=root_task_id, limit=10_000
        )
        all_task_ids = {str(task["id"])} | {str(t["id"]) for t in related_tasks}

        # Reconcile tool records, approvals, attempts, and feedback across all tasks in the logical tree
        records: list[dict[str, Any]] = []
        all_approvals: list[dict[str, Any]] = []
        all_attempts: list[dict[str, Any]] = []
        all_feedback: list[dict[str, Any]] = []

        for tid in all_task_ids:
            records.extend(
                self.store.list_harness_records(
                    "harness_tool_calls", organization_id, where={"task_id": tid}, limit=10_000
                )
            )
            all_approvals.extend(
                self.store.list_harness_records(
                    "approvals", organization_id, where={"task_id": tid}, limit=500
                )
            )
            all_attempts.extend(self.store.list_task_attempts(tid))
            all_feedback.extend(
                self.store.list_harness_records(
                    "agent_feedback", organization_id, where={"task_id": tid}, limit=500
                )
            )

        model_spans = [
            item
            for item in spans
            if item.get("name") == "model.generate"
            and str((item.get("attributes") or {}).get("toda.model.role") or "main")
            in {"main", "conversation"}
        ]
        model_usage: dict[str, int] = {}
        cache_tokens = 0
        reasoning_tokens = 0
        cost_total = 0.0
        cost_states: set[str] = set()
        for item in model_spans:
            attrs = item.get("attributes") or {}
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = attrs.get(f"gen_ai.usage.{key}")
                if isinstance(value, (int, float)):
                    model_usage[key] = model_usage.get(key, 0) + int(value)
            for key, target in (("cached_input_tokens", "cached_tokens"), ("reasoning_tokens", "reasoning_tokens")):
                value = attrs.get(f"gen_ai.usage.{key}")
                if isinstance(value, (int, float)):
                    model_usage[key] = model_usage.get(key, 0) + int(value)
                    if target == "cached_tokens":
                        cache_tokens += int(value)
                    else:
                        reasoning_tokens += int(value)
            state = str(attrs.get("toda.cost.state") or "unavailable")
            cost_states.add(state)
            if isinstance(attrs.get("toda.cost.usd"), (int, float)):
                cost_total += float(attrs["toda.cost.usd"])
        if not model_spans or "unavailable" in cost_states:
            cost_state: str = "unavailable"
            reported_cost: float | None = None
        elif len(cost_states) == 1:
            cost_state = next(iter(cost_states))
            reported_cost = cost_total
        else:
            cost_state = "actual" if "actual" in cost_states else "estimated"
            reported_cost = cost_total
        queue_jobs = self._queue_jobs(task)
        logical_tasks = {
            str(item["id"]): item for item in [task, *related_tasks]
        }
        delivery_required = any(
            self._delivery_required(item) for item in logical_tasks.values()
        )
        completeness = validate_trace(
            spans,
            delivery_required=delivery_required,
            task=task,
            tool_records=records,
            queue_jobs=queue_jobs,
        )
        if task.get("state") == "waiting" and completeness.get("status") == "incomplete":
            continuation_checks: list[bool] = []
            missing_reasons: list[str] = []

            if task.get("pending_approval_id"):
                app_id = str(task["pending_approval_id"])
                app_records = [a for a in all_approvals if str(a.get("id")) == app_id]
                if app_records and app_records[0].get("status") == "pending":
                    continuation_checks.append(True)
                else:
                    continuation_checks.append(False)
                    missing_reasons.append("active_pending_approval")
            if task.get("pending_child_task_ids"):
                child_ids = set(str(cid) for cid in (task.get("pending_child_task_ids") or []))
                active_children = [
                    t for t in related_tasks
                    if str(t.get("id")) in child_ids and t.get("state") not in {"completed", "failed", "cancelled"}
                ]
                if active_children:
                    continuation_checks.append(True)
                else:
                    continuation_checks.append(False)
                    missing_reasons.append("active_child_task")
            waiting_for = task.get("waiting_for")
            if isinstance(waiting_for, dict):
                waiting_type = str(waiting_for.get("type") or "")
                if waiting_type == "user" and task.get("session_id"):
                    continuation_checks.append(True)
                elif waiting_type == "approval":
                    referenced = str(waiting_for.get("id") or "")
                    matched = bool(
                        referenced
                        and any(
                            str(item.get("id")) == referenced
                            and item.get("status") == "pending"
                            for item in all_approvals
                        )
                    )
                    continuation_checks.append(matched)
                    if not matched:
                        missing_reasons.append("waiting_approval_record")
                elif waiting_type == "child_task":
                    referenced_ids = {
                        str(value) for value in waiting_for.get("ids") or []
                    }
                    matched = bool(
                        referenced_ids
                        and all(
                            child_id in logical_tasks
                            and logical_tasks[child_id].get("state")
                            not in {"completed", "failed", "cancelled"}
                            for child_id in referenced_ids
                        )
                    )
                    continuation_checks.append(matched)
                    if not matched:
                        missing_reasons.append("waiting_child_task_record")
                elif waiting_type:
                    continuation_checks.append(False)
                    missing_reasons.append("recognized_waiting_type")
            if not continuation_checks:
                missing_reasons.append("pending_continuation")

            if continuation_checks and all(continuation_checks):
                completeness["status"] = "pending"
            else:
                completeness["status"] = "incomplete"
                completeness["missing"] = list(set(completeness.get("missing", []) + (missing_reasons or ["pending_continuation"])))
        trace_ids = sorted({str(item["trace_id"]) for item in spans if item.get("trace_id")})
        starts = [item.get("start_time_ns") for item in spans if item.get("start_time_ns") is not None]
        ends = [item.get("end_time_ns") for item in spans if item.get("end_time_ns") is not None]
        wall_duration_ms = (max(ends) - min(starts)) / 1_000_000 if starts and ends else None
        active_spans = [item for item in spans if item.get("name") == "agent.invoke"]
        safe_attempts = [
            {key: item.get(key) for key in ("id", "attempt_number", "status", "worker_id", "progress", "last_heartbeat_at", "created_at", "updated_at")}
            for item in all_attempts
        ]
        versions = (task.get("metadata") or {}).get("versions") or run_configuration_versions(
            models={role: assignment.model for role, assignment in self.settings.models.assignments.items()},
            catalog=self.catalog.status(),
            build="0.1.0",
        )
        task_id = str(task["id"])
        if persist:
            self.store.save_trace_completeness(
                task_id=task_id,
                organization_id=organization_id,
                trace_id=trace_ids[0] if trace_ids else None,
                result={**completeness, "validated_at": datetime.now().astimezone().isoformat()},
            )
            metrics = getattr(getattr(self.store, "telemetry", None), "metrics", None)
            if metrics is not None:
                try:
                    metrics.counter(
                        "toda_completeness_total",
                        labels={"outcome": completeness.get("status") or "unknown"},
                    )
                    if task.get("state") == "completed":
                        if wall_duration_ms is not None:
                            metrics.histogram(
                                "toda_run_active_duration_ms",
                                sum(float(item.get("duration_ms") or 0) for item in active_spans or spans),
                                labels={"outcome": "completed"},
                            )
                            metrics.histogram(
                                "toda_run_wall_duration_ms",
                                wall_duration_ms,
                                labels={"outcome": "completed"},
                            )
                        if reported_cost is not None and cost_state != "unavailable":
                            metrics.histogram(
                                "toda_task_cost_per_success_usd",
                                reported_cost,
                                labels={"outcome": "completed"},
                            )
                except Exception:
                    # Metrics are optional and cannot make reconciliation
                    # change a Task's durable outcome.
                    pass
        return {
            "task": self._safe_task(task),
            "outcome": task.get("state"),
            "trace_ids": trace_ids,
            "spans": spans,
            "metrics": {
                "span_count": len(spans),
                "model_calls": len(model_spans),
                "iterations": task.get("iteration_count"),
                "tool_calls": task.get("tool_call_count"),
                "active_duration_ms": sum(float(item.get("duration_ms") or 0) for item in (active_spans or spans)),
                "wall_duration_ms": wall_duration_ms,
                "tokens": model_usage,
                "cache_hit_ratio": (cache_tokens / model_usage["input_tokens"] if model_usage.get("input_tokens") else None),
                "cost": {"amount": reported_cost, "currency": "USD", "source": cost_state},
                "queue_delay_ms": sum(float((item.get("attributes") or {}).get("toda.queue.delay_ms") or 0) for item in spans),
            },
            "models": [
                {
                    "role": (item.get("attributes") or {}).get("toda.model.role"),
                    "provider": (item.get("attributes") or {}).get("toda.provider.id"),
                    "model": (item.get("attributes") or {}).get("toda.provider_model.id"),
                    "cost_state": (item.get("attributes") or {}).get("toda.cost.state"),
                }
                for item in model_spans
            ],
            "children": [self._safe_task(item) for item in self.store.list_tasks(organization_id, parent_task_id=task_id, limit=500)],
            "tools": {"count": len(records), "by_outcome": dict(Counter(str(item.get("status") or "unknown") for item in records)), "items": [{key: item.get(key) for key in ("id", "tool_name", "status", "risk", "approval_id", "idempotency_key", "created_at", "updated_at")} for item in records]},
            "approvals": [{key: item.get(key) for key in ("id", "task_id", "tool_name", "status", "created_at", "updated_at")} for item in all_approvals],
            "retries": {
                "count": sum(
                    max(0, count - 1)
                    for count in Counter(
                        str(item.get("task_id") or task_id) for item in all_attempts
                    ).values()
                ),
                "attempts": safe_attempts,
            },
            "queue": [{key: item.get(key) for key in ("id", "kind", "status", "attempts", "max_attempts", "created_at", "updated_at")} for item in queue_jobs],
            "errors": self._safe_errors(task, spans, records),
            "budgets": {"configured": {key: task.get(key) for key in ("max_iterations", "max_tool_calls", "max_model_calls", "max_total_tokens", "max_cost_usd", "max_duration_seconds")}, "consumed": {"iterations": task.get("iteration_count"), "tool_calls": task.get("tool_call_count"), "model_calls": (task.get("usage") or {}).get("model_calls"), "total_tokens": (task.get("usage") or {}).get("total_tokens"), "cost_usd": (task.get("usage") or {}).get("cost_usd"), "cost_state": (task.get("usage") or {}).get("cost_state"), "duration_ms": wall_duration_ms}, "enforced": True},
            "delivery": {"required": delivery_required, "spans": [item for item in spans if item.get("name") == "delivery.send"]},
            "versions": versions,
            "completeness": completeness,
            "catalog": self.catalog.status(),
            "privacy": {"payload_capture_enabled": bool(self.settings.observability.capture_payloads)},
            "feedback": all_feedback,
        }

    def reconcile_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if not task:
            return None
        return self.summary(task)

    def reconcile_terminal_tasks(self, organization_id: str | None = None) -> int:
        """Startup repair for terminal Tasks created before auto-reconcile."""
        organization = organization_id or self.settings.organization.id
        count = 0
        for task in self.store.list_tasks(organization, limit=100_000):
            if task.get("state") not in {"completed", "failed", "cancelled"}:
                continue
            if self.store.get_trace_completeness(task["id"], organization_id=organization) is None:
                self.summary(task)
                count += 1
        return count

    def list_runs(
        self,
        organization_id: str,
        *,
        state: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        tool: str | None = None,
        approval_id: str | None = None,
        errors_only: bool = False,
        incomplete_only: bool = False,
        high_cost_usd: float | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return a bounded logical-run index for operator surfaces."""
        rows: list[dict[str, Any]] = []
        for task in self.store.list_tasks(organization_id, state=state, limit=10_000):
            try:
                created_at = datetime.fromisoformat(str(task.get("created_at")).replace("Z", "+00:00"))
                updated_at = datetime.fromisoformat(str(task.get("updated_at") or task.get("created_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if since and updated_at < since or until and created_at > until:
                continue
            summary = self.summary(task, persist=False)
            models = summary.get("models") or []
            tools = (summary.get("tools") or {}).get("items") or []
            approvals = summary.get("approvals") or []
            metrics = summary.get("metrics") or {}
            errors = summary.get("errors") or []
            if provider and not any(item.get("provider") == provider for item in models):
                continue
            if model and not any(item.get("model") == model for item in models):
                continue
            if tool and not any(item.get("tool_name") == tool for item in tools):
                continue
            if approval_id and not any(item.get("id") == approval_id for item in approvals):
                continue
            if errors_only and not (task.get("error") or errors):
                continue
            completeness = summary.get("completeness") or {}
            if incomplete_only and completeness.get("status") == "complete":
                continue
            cost = (metrics.get("cost") or {}).get("amount")
            if high_cost_usd is not None and not isinstance(cost, (int, float)):
                continue
            if high_cost_usd is not None and float(cost) < float(high_cost_usd):
                continue
            rows.append(
                {
                    "task_id": task["id"],
                    "state": task.get("state"),
                    "goal": task.get("goal"),
                    "trace_ids": summary.get("trace_ids") or [],
                    "created_at": task.get("created_at"),
                    "updated_at": task.get("updated_at") or task.get("created_at"),
                    "providers_models": [
                        {"provider": item.get("provider"), "model": item.get("model"), "role": item.get("role")}
                        for item in models
                    ],
                    "metrics": {
                        "tokens": metrics.get("tokens") or {},
                        "cost": metrics.get("cost") or {},
                        "wall_duration_ms": metrics.get("wall_duration_ms"),
                        "active_duration_ms": metrics.get("active_duration_ms"),
                    },
                    "completeness": completeness,
                    "tool_count": len(tools),
                    "approval_count": len(approvals),
                    "has_errors": bool(task.get("error") or errors),
                }
            )
            if len(rows) >= min(max(1, int(limit)), 200):
                break
        rows.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return rows
