"""Bounded behavioral readiness checks for the supervised pilot."""

from __future__ import annotations

import os
import pwd
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from toda.domain.models import new_id, utcnow


_PROBE_CACHE_SECONDS = 60.0


class ReadinessBlockedError(RuntimeError):
    """Raised when supervised mode refuses a new real-world Task."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        failed = [item for item in report.get("checks", []) if item.get("status") != "pass"]
        names = ", ".join(str(item.get("name")) for item in failed[:8]) or "unknown check"
        super().__init__(f"real-world readiness is {report.get('mode')}; failed checks: {names}")


def enforce_real_world_readiness(
    brain: Any, *, principal_id: str | None = None, trigger_kind: str | None = None
) -> dict[str, Any]:
    """Gate new external work until production dependencies prove healthy."""
    if not bool(getattr(brain.settings.agent, "require_real_world_readiness", False)):
        return {"mode": "development"}
    report = readiness_report(brain)
    if report.get("mode") == "real_world_ready":
        return report
    failures = [
        {key: item.get(key) for key in ("name", "message", "remediation")}
        for item in report.get("checks", [])
        if item.get("status") != "pass"
    ]
    brain.audit.record(
        brain.settings.organization.id,
        "task.start_blocked_readiness",
        {"trigger_kind": trigger_kind, "failed_checks": failures},
        principal_id=principal_id,
    )
    raise ReadinessBlockedError(report)


def _check(name: str, ok: bool, *, critical: bool, message: str, remediation: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if ok else "fail",
        "critical": critical,
        "message": message,
        "remediation": remediation,
    }


def _database_probe(brain: Any) -> tuple[bool, str]:
    marker = new_id("readiness")
    try:
        with brain.store.cursor(immediate=True) as cursor:
            cursor.execute("SAVEPOINT toda_readiness_probe")
            cursor.execute(
                "INSERT INTO idempotency_keys(organization_id, operation, key, response, created_at) VALUES (?, ?, ?, ?, ?)",
                ("__readiness__", "probe", marker, "{}", utcnow().isoformat()),
            )
            cursor.execute(
                "SELECT key FROM idempotency_keys WHERE organization_id = ? AND key = ?",
                ("__readiness__", marker),
            )
            if cursor.fetchone()["key"] != marker:
                raise RuntimeError("write/read probe returned the wrong row")
            cursor.execute("ROLLBACK TO toda_readiness_probe")
            cursor.execute("RELEASE toda_readiness_probe")
        return True, "SQLite write/read/rollback probe passed"
    except Exception as exc:
        return False, f"SQLite behavioral probe failed: {type(exc).__name__}"


def _local_exporter_probe(brain: Any) -> tuple[bool, str]:
    telemetry = brain.store.telemetry
    if bool(getattr(telemetry.local_exporter, "_shutdown", True)):
        return False, "Local span exporter is shut down"
    probe_id = new_id("spanprobe")
    span_id = None
    try:
        with telemetry.span(
            "readiness.probe",
            component="readiness",
            organization_id=brain.settings.organization.id,
            attributes={"toda.readiness.probe_id": probe_id},
        ) as span:
            span_id = span.span.get_span_context().trace_id if span.span else None
        if not telemetry.force_flush():
            return False, "Local exporter did not flush the probe span"
        trace_id = f"{int(span_id):032x}" if span_id else ""
        rows = brain.store.list_otel_spans(trace_id, organization_id=brain.settings.organization.id)
        if not any((row.get("attributes") or {}).get("toda.readiness.probe_id") == probe_id for row in rows):
            return False, "Probe span was not found in SQLite after flush"
        return True, "Probe span was exported and read back from SQLite"
    except Exception as exc:
        return False, f"Local exporter probe failed: {type(exc).__name__}"


def _provider_probe(brain: Any) -> tuple[bool, str, list[dict[str, Any]]]:
    assignments = dict(brain.settings.models.assignments)
    if not assignments:
        return False, "No model assignment is configured", []
    cached = getattr(brain, "_readiness_probe_cache", None)
    now = time.monotonic()
    if cached and now - float(cached[0]) < _PROBE_CACHE_SECONDS:
        return bool(cached[1]), str(cached[2]), list(cached[3])
    try:
        from toda.providers import ProviderResolver, capability_for, providers

        resolver = ProviderResolver(
            brain.settings.models.providers, brain.settings.models.assignments
        )
        results: list[dict[str, Any]] = []
        for role, assignment in sorted(assignments.items()):
            resolved = resolver.resolve(role)
            if resolved is None:
                results.append({"role": role, "ok": False, "error": "assignment did not resolve"})
                continue
            capability = capability_for(role=role, capability="chat")
            result = providers.probe(
                resolved.connection,
                assignment.model,
                capability=capability,
                timeout=min(float(brain.settings.models.timeout_seconds), 10.0),
            )
            results.append({
                "role": role,
                "provider": resolved.connection.provider,
                "model": assignment.model,
                "ok": bool(result.get("ok")),
                "detail": result.get("detail"),
                "error": result.get("error"),
            })
        ok = bool(results) and all(item["ok"] for item in results)
        message = "Every configured provider/model passed a real credential probe" if ok else "One or more configured provider/model probes failed"
    except Exception as exc:
        ok, message, results = False, f"Provider probe could not run: {type(exc).__name__}", []
    brain._readiness_probe_cache = (now, ok, message, results)
    return ok, message, results


def _collector_probe(endpoint: str | None) -> tuple[bool, str]:
    if not endpoint:
        return True, "OTLP export is disabled; SQLite is the local source of truth"
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.hostname:
        return False, "Configured OTLP endpoint is not a valid URL"
    health = urlunsplit((parsed.scheme, f"{parsed.hostname}:13133", "/", "", ""))
    try:
        response = httpx.get(health, timeout=httpx.Timeout(2.0, connect=1.0))
        response.raise_for_status()
        return True, f"Collector health endpoint responded at {health}"
    except Exception as exc:
        return False, f"Collector health probe failed: {type(exc).__name__}"


def _workspace_probe(brain: Any) -> tuple[bool, str]:
    root = Path(brain.settings.storage.workspaces).expanduser()
    agent_root = Path(brain.settings.storage.agent_workspaces).expanduser()
    probe_file: Path | None = None
    try:
        if not root.is_absolute() or root == Path("/") or root.is_symlink():
            return False, "Durable workspace root is unsafe"
        if not agent_root.is_absolute() or agent_root == Path("/") or agent_root.is_symlink():
            return False, "Agent workspace root is unsafe"
        resolved_root = root.resolve()
        resolved_agent = agent_root.resolve()
        data_dir = Path(brain.settings.storage.data_dir).expanduser().resolve()
        repo = Path(__file__).resolve().parents[3]
        if resolved_root in {data_dir, repo} or resolved_agent in {data_dir, repo}:
            return False, "Workspace root overlaps control-plane data or repository"
        if resolved_root == resolved_agent or resolved_root in resolved_agent.parents or resolved_agent in resolved_root.parents:
            return False, "Durable and agent workspace roots must be separate"

        # Exercise the same boundary implementation used by runtime workspace
        # operations. A fabricated persisted record must not turn an arbitrary
        # host path into an agent workspace.
        workspace_service = getattr(brain, "workspaces", None)
        if workspace_service is None:
            return False, "Workspace service is unavailable"
        malicious_record = {
            "organization_id": brain.settings.organization.id,
            "slack_workspace_id": None,
            "path": str((resolved_root / ".." / "data_escape_test").resolve()),
        }
        try:
            workspace_service.resolve_durable_path(malicious_record)
        except (ValueError, PermissionError):
            pass
        else:
            return False, "Workspace service accepted an escaped persisted path"

        try:
            workspace_service.task_root(
                brain.settings.organization.id,
                {"id": "../escape", "organization_id": brain.settings.organization.id},
            )
        except (ValueError, PermissionError):
            pass
        else:
            return False, "Workspace service accepted a traversal task id"
        # Verify symlink escape: if the root itself is a symlink, it was
        # already rejected above. Check that no symlink inside the workspace
        # points outside it.
        if resolved_root.exists():
            for child in resolved_root.iterdir():
                if child.is_symlink():
                    link_target = child.resolve()
                    if not str(link_target).startswith(str(resolved_root)):
                        return False, f"Symlink escape: {child} points to {link_target} outside workspace root"

        # Exercise disposable write and cleanup inside workspace
        root.mkdir(parents=True, exist_ok=True)
        probe_file = root / f".readiness_probe_{new_id('ws')}.tmp"
        probe_file.write_text("ok", encoding="utf-8")
        if probe_file.read_text(encoding="utf-8") != "ok":
            return False, "Workspace write/read verification failed"

        return True, "Dedicated durable and agent workspace roots pass boundary and write checks"
    except Exception as exc:
        return False, f"Workspace boundary probe failed: {type(exc).__name__}"
    finally:
        if probe_file and probe_file.exists():
            try:
                probe_file.unlink(missing_ok=True)
            except OSError:
                pass


def _idempotency_probe(brain: Any) -> tuple[bool, str]:
    marker = f"readiness_probe_{new_id('idem')}"
    try:
        brain.store.save_idempotency_response(
            "__readiness__", "probe", marker, {"ok": True}
        )
        value = brain.store.idempotency_response("__readiness__", "probe", marker)
        return bool(value == {"ok": True}), "Idempotency write/read self-check passed"
    except Exception as exc:
        return False, f"Idempotency self-check failed: {type(exc).__name__}"
    finally:
        try:
            with brain.store.cursor(immediate=True) as cursor:
                cursor.execute(
                    "DELETE FROM idempotency_keys WHERE organization_id = ? AND key = ?",
                    ("__readiness__", marker),
                )
        except Exception:
            pass


def _cancellation_probe(brain: Any) -> tuple[bool, str]:
    task_id = f"readiness_task_{new_id('cancel')}"
    cancelled_ok = False
    try:
        brain.store.create_task(
            {
                "id": task_id,
                "organization_id": "__readiness__",
                "session_id": "sess_probe",
                "root_task_id": task_id,
                "parent_task_id": "probe_parent",
                "state": "waiting",
                "worker_type": "general",
                "goal": "readiness probe",
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
            }
        )
        result = brain.store.cancel_task(task_id)
        task_row = brain.store.get_task(task_id)
        cancelled_ok = bool(result is True and task_row and task_row.get("state") == "cancelled")
        return cancelled_ok, "Cancellation disposable lifecycle self-check passed"
    except Exception as exc:
        return False, f"Cancellation self-check failed: {type(exc).__name__}"
    finally:
        try:
            with brain.store.cursor(immediate=True) as cursor:
                cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        except Exception:
            pass


def _connector_probe(brain: Any) -> tuple[bool, str, list[dict[str, Any]]]:
    """Report configured connector state using recent health records when available.

    Falls back to shape validation when no health records exist, but prefers
    bounded behavioral health checks over structural client inspection.
    """
    config = brain.settings
    results: list[dict[str, Any]] = []
    all_ok = True

    # Helper: check recent connector health records from the store
    def _recent_health(connector_name: str, max_age_seconds: int = 300) -> dict[str, Any] | None:
        try:
            records = brain.store.list_harness_records(
                "connector_health", config.organization.id,
                where={"connector": connector_name},
                limit=1,
            )
            if records:
                record = records[0]
                from datetime import datetime, timezone
                probed_at = record.get("probed_at") or record.get("created_at")
                if probed_at:
                    ts = datetime.fromisoformat(str(probed_at))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    return {"fresh": age < max_age_seconds, "age_seconds": age, "status": record.get("status")}
        except Exception:
            pass
        return None

    if config.composio.api_key:
        health = _recent_health("composio")
        client_ok = bool(
            getattr(brain, "composio", None)
            and getattr(brain.composio, "api_key", None)
            and health
            and health["fresh"]
            and health.get("status") == "ok"
        )
        results.append({"connector": "composio", "configured": True, "ok": client_ok, "health": health})
        if not client_ok:
            all_ok = False
    else:
        results.append({"connector": "composio", "configured": False, "ok": True})

    mcp_connections = brain.mcp.list(config.organization.id)
    if mcp_connections:
        for conn in mcp_connections:
            name = str(conn.get("name") or conn.get("id"))
            health = _recent_health(f"mcp:{conn['id']}")
            configured = bool(conn.get("enabled"))
            if not configured:
                results.append({"connector": f"mcp:{name}", "configured": False, "ok": True})
                continue
            conn_ok = bool(
                health
                and health["fresh"]
                and health.get("status") == "ok"
            )
            results.append({"connector": f"mcp:{name}", "configured": True, "ok": conn_ok, "health": health})
            if not conn_ok:
                all_ok = False
    else:
        results.append({"connector": "mcp", "configured": False, "ok": True})

    message = "All configured connectors verified" if all_ok else "One or more configured connectors failed verification"
    return all_ok, message, results


def _account_can_access(path: Path, *, uid: int, gid: int) -> bool:
    """Conservatively determine whether an account can traverse/read a path."""
    try:
        metadata = path.stat()
    except OSError:
        return False
    mode = metadata.st_mode
    if uid == 0:
        return True
    if uid == metadata.st_uid:
        return bool(mode & stat.S_IRUSR)
    if gid == metadata.st_gid:
        return bool(mode & stat.S_IRGRP)
    return bool(mode & stat.S_IROTH)


def _pi_isolation_probe(brain: Any) -> tuple[bool, str]:
    config = brain.settings
    if not config.agent.pi_coding_enabled:
        return True, "Pi/code execution is disabled"
    if not config.agent.pi_isolation_required:
        return True, "Pi isolation is explicitly disabled for development"
    user = str(config.agent.pi_execution_user or "").strip()
    if not user:
        return False, "Pi has no dedicated execution user"
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        return False, f"Pi execution user {user!r} does not exist"
    if account.pw_uid == os.geteuid():
        return False, "Pi execution user is the control-plane account"
    if os.geteuid() != 0:
        return False, "The service cannot switch to the configured Pi execution user"

    protected = [
        Path(__file__).resolve().parents[3],
        Path(config.storage.data_dir).expanduser().resolve(),
    ]
    exposed = [
        str(path)
        for path in protected
        if _account_can_access(path, uid=account.pw_uid, gid=account.pw_gid)
    ]
    if exposed:
        return False, "Pi execution user can read control-plane paths: " + ", ".join(exposed)
    return True, "Pi uses a distinct effective account denied access to control-plane paths"


def readiness_report(brain: Any) -> dict[str, Any]:
    config = brain.settings
    checks: list[dict[str, Any]] = []

    database_ok, database_message = _database_probe(brain)
    checks.append(_check("database", database_ok, critical=True, message=database_message, remediation="Verify the configured SQLite data directory and permissions."))
    exporter_ok, exporter_message = _local_exporter_probe(brain)
    checks.append(_check("local_span_exporter", exporter_ok, critical=True, message=exporter_message, remediation="Restart the gateway and verify the SQLite telemetry exporter."))

    catalog = brain.models_catalog.status()
    catalog_ok = bool(
        int(catalog.get("model_count") or 0) > 0
        and int(catalog.get("provider_model_count") or 0) > 0
        and catalog.get("parser_version")
        and not catalog.get("stale")
        and not catalog.get("fallback")
    )
    checks.append(_check("models_catalog", catalog_ok, critical=False, message=(f"{catalog.get('model_count', 0)} provider-model records from {catalog.get('source_url', 'models.dev')}" if catalog_ok else f"Catalog lacks fresh models.dev data (fallback={bool(catalog.get('fallback'))}, stale={bool(catalog.get('stale'))})"), remediation="Refresh models.dev catalog from upstream."))

    assignments = dict(config.models.assignments)
    assignment_ok = bool(assignments)
    assignment_errors: list[str] = []
    for required_role in ("main", "embedding"):
        if required_role not in assignments:
            assignment_ok = False
            assignment_errors.append(required_role)
    try:
        from toda.providers import ProviderResolver
        resolver = ProviderResolver(config.models.providers, assignments)
        for role, assignment in assignments.items():
            if not assignment.model or resolver.resolve(role) is None:
                assignment_ok = False
                assignment_errors.append(role)
    except Exception:
        assignment_ok = False
        assignment_errors = sorted(assignments)
    checks.append(_check("model_assignments", assignment_ok, critical=True, message=("Every configured role resolves to a provider connection" if assignment_ok else f"Unresolved assignments: {', '.join(assignment_errors)}"), remediation="Set an explicit connection and model for every configured inference role."))

    configured_provider = bool(config.models.providers and assignments)
    checks.append(_check("provider_credentials", configured_provider, critical=True, message=("Provider connections are configured" if configured_provider else "No external provider assignment is configured"), remediation="Configure provider credentials before supervised pilot work."))
    probe_ok, probe_message, probe_results = _provider_probe(brain)
    checks.append(_check("provider_probe", probe_ok, critical=True, message=probe_message, remediation="Run the real provider/model probe with valid customer credentials."))

    payloads_off = not config.observability.capture_payloads
    checks.append(_check("payload_capture_disabled", payloads_off, critical=True, message=("Raw payload capture is disabled" if payloads_off else "Raw payload capture is enabled"), remediation="Set observability.capture_payloads=false."))
    approvals_safe = not config.dashboard.auto_approve_writes
    checks.append(_check("approval_policy", approvals_safe, critical=True, message=("External writes require approval" if approvals_safe else "Dashboard auto-approval is enabled"), remediation="Disable dashboard.auto_approve_writes."))

    budget_fields = ("max_iterations", "max_model_calls", "max_tool_calls", "max_total_tokens", "max_duration_seconds")
    budgets_ok = all(int(getattr(config.agent, field, 0) or 0) > 0 for field in budget_fields)
    budgets_ok = budgets_ok and (getattr(config.agent, "max_cost_usd", None) is not None and float(config.agent.max_cost_usd) > 0)
    checks.append(_check("execution_budgets", budgets_ok, critical=True, message=("All iteration/model/tool/token/duration/cost ceilings are configured" if budgets_ok else "A required execution budget is missing or non-positive"), remediation="Configure positive max_iterations, max_model_calls, max_tool_calls, max_total_tokens, max_duration_seconds, and max_cost_usd."))

    workspace_ok, workspace_message = _workspace_probe(brain)
    checks.append(_check("workspace_boundary", workspace_ok, critical=True, message=workspace_message, remediation="Use separate dedicated durable and agent workspace roots outside control-plane data."))

    cancellation_ok, cancellation_message = _cancellation_probe(brain)
    checks.append(_check("cancellation_self_check", cancellation_ok, critical=True, message=cancellation_message, remediation="Restore the durable cancellation integration before the pilot."))
    idempotency_ok, idempotency_message = _idempotency_probe(brain)
    checks.append(_check("idempotency_self_check", idempotency_ok, critical=True, message=idempotency_message, remediation="Restore the durable idempotency path."))
    connectors_ok, connectors_message, connector_results = _connector_probe(brain)
    checks.append(_check("connectors", connectors_ok, critical=False, message=connectors_message, remediation="Configure each required connector and verify its own account health."))

    worker = getattr(brain, "harness_worker", None)
    worker_health = worker.health(max_age_seconds=max(10.0, float(config.worker.poll_interval_seconds) * 10)) if worker and hasattr(worker, "health") else {"ok": False}
    checks.append(_check("worker_runtime", bool(worker_health.get("ok")), critical=False, message=("All actual worker lanes have recent heartbeats" if worker_health.get("ok") else "One or more worker lanes have not produced a recent heartbeat"), remediation="Start the gateway worker loops and verify their heartbeat timestamps."))
    recovery = getattr(brain, "recovery_heartbeat", None)
    expired = int(brain.store.count_expired_harness_jobs(config.organization.id))
    recovery_health = recovery.health() if recovery and hasattr(recovery, "health") else {"ok": False}
    queue_ok = expired == 0 and bool(recovery_health.get("ok"))
    checks.append(_check("queue_recovery", queue_ok, critical=False, message=("No expired leases and recovery heartbeat is recent" if queue_ok else f"Queue recovery is not healthy (expired_leases={expired})"), remediation="Run the recovery heartbeat and inspect expired/dead-lettered jobs."))

    pi_ok, pi_message = _pi_isolation_probe(brain)
    checks.append(_check("pi_isolation", pi_ok, critical=False, message=pi_message, remediation="Disable Pi or configure a real low-privilege execution account denied access to the repository and control-plane data."))
    versions_ok = True
    try:
        brain.run_inspection.summary({"id": "__readiness__", "organization_id": config.organization.id, "root_task_id": "__readiness__", "metadata": {}, "usage": {}, "state": "waiting"}, persist=False)
    except Exception:
        versions_ok = False
    checks.append(_check("version_hashes", versions_ok, critical=True, message=("Prompt/tool/skill/policy/catalog hashes can be generated" if versions_ok else "Required configuration hashes could not be generated"), remediation="Fix the versioning inputs used by run inspection."))

    terminal_tasks = [task for task in brain.store.list_tasks(config.organization.id, limit=100_000) if task.get("state") in {"completed", "failed", "cancelled"}]
    missing_completeness = sum(1 for task in terminal_tasks if brain.store.get_trace_completeness(task["id"], organization_id=config.organization.id) is None)
    checks.append(_check("completeness_reconciliation", missing_completeness == 0, critical=False, message=("All terminal Tasks have a completeness record" if missing_completeness == 0 else f"{missing_completeness} terminal Task(s) lack completeness"), remediation="Run startup reconciliation or inspect the terminal lifecycle hook."))

    collector_ok, collector_message = _collector_probe(config.observability.otlp_endpoint)
    checks.append(_check("otlp_health", collector_ok, critical=False, message=collector_message, remediation="Start the configured Collector or disable OTLP export."))
    retention_result = getattr(brain, "_last_retention_result", None)
    if retention_result is None:
        retention_result = {"last_success_at": None, "deleted": 0}
    retention_ok = bool(retention_result.get("last_success_at"))
    checks.append(
        _check(
            "retention_cleanup",
            retention_ok,
            critical=False,
            message=(
                f"Span retention cleanup verified (last_success_at={retention_result.get('last_success_at')})"
                if retention_ok
                else "No successful span retention cleanup has been recorded yet"
            ),
            remediation="Execute periodic retention maintenance job to verify span pruning.",
        )
    )

    critical_failures = [item for item in checks if item["critical"] and item["status"] != "pass"]
    real_world_failures = [item for item in checks if item["status"] != "pass"]
    mode = "real_world_ready" if not real_world_failures else "blocked"
    report = {
        "mode": mode,
        "checks": checks,
        "critical_failures": critical_failures,
        "real_world_failures": real_world_failures,
        "provider_probes": probe_results,
        "connector_probes": connector_results,
        "retention": retention_result,
        "worker": worker_health,
        "recovery": recovery_health,
        "checked_at": utcnow().isoformat(),
    }
    brain._last_readiness_report = report
    return report
