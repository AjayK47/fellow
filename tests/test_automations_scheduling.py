"""H-10: real automation scheduling — cron, timezone, quiet hours, misfire
and overlap policy, and the automation-trigger prompt guidance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docket.harness.automations import (
    compute_next_run_at,
    next_cron_fire,
)
from docket.harness.models import InvocationEnvelope
from docket.harness.prompts import AUTOMATION_TRIGGER_GUIDANCE, build_agent_messages


def _destination() -> dict:
    return {"type": "internal_agent_session", "id": "digest", "is_private": True}


def test_next_cron_fire_every_weekday_9am():
    # "0 9 * * 1-5" — 9:00 AM UTC, Monday through Friday.
    after = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)  # a Saturday
    fire = next_cron_fire("0 9 * * 1-5", "UTC", after)
    assert fire.weekday() == 0  # Monday
    assert fire.hour == 9 and fire.minute == 0
    assert fire > after


def test_next_cron_fire_respects_timezone():
    # 9am in America/New_York is 13:00 or 14:00 UTC depending on DST.
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    fire = next_cron_fire("0 9 * * *", "America/New_York", after)
    local = fire.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert local.hour == 9


def test_next_cron_fire_rejects_malformed_expression():
    with pytest.raises(ValueError):
        next_cron_fire("not a cron expression", "UTC", datetime.now(timezone.utc))


def test_quiet_hours_defers_a_cron_fire_that_lands_inside_the_window():
    automation = {
        "schedule_kind": "cron",
        "cron_expression": "30 23 * * *",  # 23:30 UTC daily
        "timezone": "UTC",
        "quiet_hours": {"start": "22:00", "end": "07:00"},
    }
    after = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    next_run = datetime.fromisoformat(compute_next_run_at(automation, after=after))
    # 23:30 falls inside the 22:00->07:00 quiet window, so it must be
    # deferred to 07:00 the next morning, not delivered at 23:30.
    assert next_run.hour == 7 and next_run.minute == 0


def test_quiet_hours_leaves_a_fire_outside_the_window_untouched():
    automation = {
        "schedule_kind": "cron",
        "cron_expression": "0 12 * * *",  # noon UTC
        "timezone": "UTC",
        "quiet_hours": {"start": "22:00", "end": "07:00"},
    }
    after = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    next_run = datetime.fromisoformat(compute_next_run_at(automation, after=after))
    assert next_run.hour == 12


def test_interval_schedule_still_computes_next_run_at():
    automation = {"schedule_kind": "interval", "interval_seconds": 3600, "timezone": "UTC"}
    after = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    next_run = datetime.fromisoformat(compute_next_run_at(automation, after=after))
    assert next_run == after + timedelta(hours=1)


def test_create_rejects_unknown_misfire_or_overlap_policy(brain):
    with pytest.raises(ValueError):
        brain.automations.create(
            organization_id="org_acme",
            name="bad policy",
            goal="test",
            principal_id="person_ajay",
            conversation_id="automation:bad-policy",
            destination=_destination(),
            interval_seconds=3600,
            misfire_policy="whenever",
        )


def test_create_validates_cron_expression_eagerly(brain):
    with pytest.raises(ValueError):
        brain.automations.create(
            organization_id="org_acme",
            name="bad cron",
            goal="test",
            principal_id="person_ajay",
            conversation_id="automation:bad-cron",
            destination=_destination(),
            cron_expression="garbage",
        )


def test_misfire_skip_reschedules_without_firing_when_badly_overdue(brain):
    automation = brain.automations.create(
        organization_id="org_acme",
        name="Digest",
        goal="Summarize export issues",
        principal_id="person_ajay",
        conversation_id="automation:misfire-skip",
        destination=_destination(),
        interval_seconds=3600,
        misfire_policy="skip",
    )
    automation["next_run_at"] = "2000-01-01T00:00:00+00:00"
    brain.automations._upsert(automation)

    tick = brain.automations.tick("org_acme")
    assert tick["fired"] == 0
    assert any(item["automation_id"] == automation["id"] for item in tick["skipped"])

    refreshed = brain.store.get_harness_record("automations", automation["id"])
    next_run = datetime.fromisoformat(refreshed["next_run_at"])
    assert next_run > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_misfire_fire_once_still_fires_when_badly_overdue(brain):
    automation = brain.automations.create(
        organization_id="org_acme",
        name="Digest",
        goal="Summarize export issues",
        principal_id="person_ajay",
        conversation_id="automation:misfire-fire-once",
        destination=_destination(),
        interval_seconds=3600,
        misfire_policy="fire_once",
    )
    automation["next_run_at"] = "2000-01-01T00:00:00+00:00"
    brain.automations._upsert(automation)

    tick = brain.automations.tick("org_acme")
    assert tick["fired"] == 1


def test_overlap_skip_if_running_does_not_double_fire_while_previous_run_active(brain):
    automation = brain.automations.create(
        organization_id="org_acme",
        name="Digest",
        goal="Summarize export issues",
        principal_id="person_ajay",
        conversation_id="automation:overlap-skip",
        destination=_destination(),
        interval_seconds=60,
        overlap_policy="skip_if_running",
    )
    automation["next_run_at"] = "2000-01-01T00:00:00+00:00"
    # Simulate a still-running previous run rather than draining the queue,
    # so overlap detection is exercised in isolation from job draining.
    fake_run = brain.agent_runs.create_run(
        session_id=brain.agent_runs.get_or_create_session(
            organization_id="org_acme",
            conversation_key="automation:overlap-skip:fake",
            destination=_destination(),
        )["id"],
        envelope=InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id="automation:overlap-skip:fake",
            destination=_destination(),
            purpose="scheduled_automation",
            permission_decision_id="authz_fake",
            allowed_scope_ids=["scope_org_public"],
        ),
        goal="still running",
        trigger_kind="automation",
    )
    brain.agent_runs.transition(fake_run["id"], "running", force=True)
    automation["last_run_id"] = fake_run["id"]
    brain.automations._upsert(automation)

    tick = brain.automations.tick("org_acme")
    assert tick["fired"] == 0
    assert any(item["automation_id"] == automation["id"] for item in tick["skipped"])


def test_automation_trigger_guidance_only_appears_for_automation_runs():
    base_run = {
        "id": "arun_x",
        "goal": "check things",
        "trigger_kind": "reactive",
        "max_iterations": 12,
        "max_tool_calls": 24,
        "envelope": {
            "principal_id": "person_ajay",
            "destination": _destination(),
            "purpose": "reactive_agent",
            "allowed_scope_ids": ["scope_org_public"],
        },
    }
    reactive_messages = build_agent_messages(run=base_run, context={}, history=[])
    assert not any(
        AUTOMATION_TRIGGER_GUIDANCE in m["content"] for m in reactive_messages
    )

    automation_run = {**base_run, "trigger_kind": "automation"}
    automation_messages = build_agent_messages(run=automation_run, context={}, history=[])
    assert any(
        AUTOMATION_TRIGGER_GUIDANCE in m["content"] for m in automation_messages
    )
