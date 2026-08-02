"""Durable scheduled automations that activate the same CompanyAgent runtime.

Supports fixed intervals (unchanged, backward compatible) and real cron
schedules with timezone awareness, quiet hours, and explicit misfire/overlap
policy — rather than only "every N seconds" with undefined behavior when the
process was down or the previous run hasn't finished.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from docket.domain.models import new_id, utcnow
from docket.harness.models import InvocationEnvelope, TriggerKind

_TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}
_DEFAULT_MISFIRE_GRACE_SECONDS = 300


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            start, end = min_val, max_val
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def next_cron_fire(expression: str, tz_name: str, after: datetime) -> datetime:
    """Return the next UTC datetime strictly after *after* matching *expression*.

    Standard 5-field cron syntax (minute hour day-of-month month day-of-week),
    supporting `*`, `*/N`, `A-B`, `A,B`, and `A-B/N`. Day-of-week 0 and 7 both
    mean Sunday. When both day-of-month and day-of-week are restricted
    (neither is `*`), a match on either field fires — standard cron semantics.
    Brute-force minute-by-minute search bounded to one year, which is fast in
    practice (real schedules fire within days) and avoids a third-party
    dependency for a five-field grammar this small.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expression!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    minutes = _parse_cron_field(minute_f, 0, 59)
    hours = _parse_cron_field(hour_f, 0, 23)
    doms = _parse_cron_field(dom_f, 1, 31)
    months = _parse_cron_field(month_f, 1, 12)
    dows = _parse_cron_field(dow_f, 0, 7)
    dom_wild = dom_f == "*"
    dow_wild = dow_f == "*"

    tz = ZoneInfo(tz_name)
    local = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = local + timedelta(days=366)
    while local <= limit:
        cron_dow = local.isoweekday() % 7  # Python Sunday=6 -> cron Sunday=0
        dom_ok = local.day in doms
        dow_ok = cron_dow in dows or (7 in dows and cron_dow == 0)
        if dom_wild and dow_wild:
            day_match = True
        elif dom_wild:
            day_match = dow_ok
        elif dow_wild:
            day_match = dom_ok
        else:
            day_match = dom_ok or dow_ok
        if (
            local.minute in minutes
            and local.hour in hours
            and local.month in months
            and day_match
        ):
            return local.astimezone(timezone.utc)
        local += timedelta(minutes=1)
    raise ValueError(f"no fire time found for cron expression within a year: {expression!r}")


def _apply_quiet_hours(
    candidate: datetime, quiet_hours: dict[str, str] | None, tz_name: str
) -> datetime:
    """Defer *candidate* to the end of a quiet window if it falls inside one."""
    if not quiet_hours:
        return candidate
    tz = ZoneInfo(tz_name)
    local = candidate.astimezone(tz)
    start_h, start_m = (int(part) for part in quiet_hours["start"].split(":"))
    end_h, end_m = (int(part) for part in quiet_hours["end"].split(":"))
    start = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        if start <= local < end:
            return end.astimezone(timezone.utc)
        return candidate
    # Window wraps midnight (e.g. 22:00 -> 07:00).
    if local >= start:
        return (end + timedelta(days=1)).astimezone(timezone.utc)
    if local < end:
        return end.astimezone(timezone.utc)
    return candidate


def compute_next_run_at(automation: dict[str, Any], *, after: datetime) -> str:
    tz_name = automation.get("timezone") or "UTC"
    if automation.get("schedule_kind") == "cron":
        candidate = next_cron_fire(automation["cron_expression"], tz_name, after)
    else:
        candidate = after + timedelta(seconds=int(automation["interval_seconds"]))
    candidate = _apply_quiet_hours(candidate, automation.get("quiet_hours"), tz_name)
    return candidate.isoformat()


class AutomationService:
    def __init__(
        self,
        *,
        store: Any,
        authz: Any,
        agent: Any,
        queue: Any,
        audit: Any,
    ) -> None:
        self.store = store
        self.authz = authz
        self.agent = agent
        self.queue = queue
        self.audit = audit

    def create(
        self,
        *,
        organization_id: str,
        name: str,
        goal: str,
        principal_id: str,
        conversation_id: str,
        destination: dict[str, Any],
        interval_seconds: int | None = 3600,
        cron_expression: str | None = None,
        timezone: str = "UTC",
        quiet_hours: dict[str, str] | None = None,
        misfire_policy: str = "fire_once",
        overlap_policy: str = "skip_if_running",
        enabled: bool = True,
    ) -> dict[str, Any]:
        if misfire_policy not in {"skip", "fire_once"}:
            raise ValueError("misfire_policy must be 'skip' or 'fire_once'")
        if overlap_policy not in {"skip_if_running", "queue"}:
            raise ValueError("overlap_policy must be 'skip_if_running' or 'queue'")
        if cron_expression:
            schedule_kind = "cron"
            # Validate eagerly so a bad expression fails at creation, not at
            # the first scheduler tick.
            next_cron_fire(cron_expression, timezone, utcnow())
        else:
            schedule_kind = "interval"
            if not interval_seconds or interval_seconds < 60:
                raise ValueError("automation interval must be at least 60 seconds")
        now = utcnow()
        automation = {
            "id": new_id("automation"),
            "organization_id": organization_id,
            "name": name,
            "goal": goal,
            "principal_id": principal_id,
            "conversation_id": conversation_id,
            "destination": destination,
            "schedule_kind": schedule_kind,
            "interval_seconds": interval_seconds,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "quiet_hours": quiet_hours,
            "misfire_policy": misfire_policy,
            "overlap_policy": overlap_policy,
            "status": "enabled" if enabled else "disabled",
            "next_run_at": None,
            "last_run_id": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        if enabled:
            automation["next_run_at"] = compute_next_run_at(automation, after=now)
        self._upsert(automation)
        return automation

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_harness_records(
            "automations", organization_id, order_by="updated_at"
        )

    def tick(self, organization_id: str | None = None) -> dict[str, Any]:
        now = utcnow()
        now_iso = now.isoformat()
        with self.store.cursor() as cur:
            if organization_id:
                cur.execute(
                    """SELECT payload FROM automations
                    WHERE organization_id = ? AND status = 'enabled'
                    AND next_run_at <= ? ORDER BY next_run_at""",
                    (organization_id, now_iso),
                )
            else:
                cur.execute(
                    """SELECT payload FROM automations
                    WHERE status = 'enabled' AND next_run_at <= ?
                    ORDER BY next_run_at""",
                    (now_iso,),
                )
            due = [json.loads(row["payload"]) for row in cur.fetchall()]
        runs: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for automation in due:
            due_at = datetime.fromisoformat(automation["next_run_at"])
            overdue_seconds = (now - due_at).total_seconds()
            if (
                automation.get("misfire_policy", "fire_once") == "skip"
                and overdue_seconds > _DEFAULT_MISFIRE_GRACE_SECONDS
            ):
                automation["next_run_at"] = compute_next_run_at(automation, after=now)
                automation["updated_at"] = now.isoformat()
                self._upsert(automation)
                skipped.append({"automation_id": automation["id"], "reason": "misfire_skipped"})
                self.audit.record(
                    automation["organization_id"],
                    "automation.misfire_skipped",
                    {"automation_id": automation["id"], "overdue_seconds": overdue_seconds},
                    principal_id=automation["principal_id"],
                )
                continue

            if automation.get("overlap_policy", "skip_if_running") == "skip_if_running":
                last_run = (
                    self.store.get_harness_record("agent_runs", automation["last_run_id"])
                    if automation.get("last_run_id")
                    else None
                )
                if last_run and last_run.get("state") not in _TERMINAL_RUN_STATES:
                    automation["next_run_at"] = compute_next_run_at(automation, after=now)
                    automation["updated_at"] = now.isoformat()
                    self._upsert(automation)
                    skipped.append({"automation_id": automation["id"], "reason": "overlap_skipped"})
                    self.audit.record(
                        automation["organization_id"],
                        "automation.overlap_skipped",
                        {"automation_id": automation["id"], "running_run_id": last_run["id"]},
                        principal_id=automation["principal_id"],
                    )
                    continue

            decision = self.authz.resolve(
                organization_id=automation["organization_id"],
                acting_principal_id=automation["principal_id"],
                destination=automation["destination"],
                purpose="scheduled_automation",
            )
            envelope = InvocationEnvelope(
                organization_id=automation["organization_id"],
                principal_id=automation["principal_id"],
                application_id="docket_scheduler",
                conversation_id=automation["conversation_id"],
                destination=automation["destination"],
                purpose="scheduled_automation",
                permission_decision_id=decision.id,
                allowed_scope_ids=decision.allowed_scope_ids,
            )
            run = self.agent.create_invocation(
                envelope=envelope,
                goal=automation["goal"],
                trigger_kind=TriggerKind.AUTOMATION,
                initial_message=(
                    f"Scheduled automation '{automation['name']}': "
                    f"{automation['goal']}"
                ),
                metadata={"automation_id": automation["id"]},
            )
            self.queue.enqueue(
                organization_id=automation["organization_id"],
                kind="agent.run",
                payload={"run_id": run["id"]},
                idempotency_key=f"automation.run:{automation['id']}:{automation['next_run_at']}",
            )
            automation["last_run_id"] = run["id"]
            automation["next_run_at"] = compute_next_run_at(automation, after=now)
            automation["updated_at"] = now.isoformat()
            self._upsert(automation)
            runs.append(run)
            self.audit.record(
                automation["organization_id"],
                "automation.fired",
                {"automation_id": automation["id"], "run_id": run["id"]},
                principal_id=automation["principal_id"],
                trace_id=run["trace_id"],
            )
        return {
            "fired": len(runs),
            "run_ids": [run["id"] for run in runs],
            "skipped": skipped,
        }

    def set_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        automation = self.store.get_harness_record("automations", automation_id)
        if not automation:
            raise KeyError(automation_id)
        automation["status"] = "enabled" if enabled else "disabled"
        automation["next_run_at"] = (
            compute_next_run_at(automation, after=utcnow()) if enabled else None
        )
        automation["updated_at"] = utcnow().isoformat()
        self._upsert(automation)
        return automation

    def delete(self, automation_id: str) -> dict[str, Any]:
        automation = self.store.get_harness_record("automations", automation_id)
        if not automation:
            raise KeyError(automation_id)
        with self.store.cursor() as cur:
            cur.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
        self.audit.record(
            automation["organization_id"],
            "automation.deleted",
            {"automation_id": automation_id},
            principal_id=automation.get("principal_id"),
        )
        return {"id": automation_id, "deleted": True}

    def _upsert(self, automation: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO automations
                (id, organization_id, status, next_run_at, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    automation["id"],
                    automation["organization_id"],
                    automation["status"],
                    automation.get("next_run_at"),
                    json.dumps(automation),
                    automation["created_at"],
                    automation["updated_at"],
                ),
            )
