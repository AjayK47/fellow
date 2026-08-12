"""H-10: real automation scheduling — cron, timezone, quiet hours, misfire
and overlap policy, and the automation-trigger prompt guidance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from toda.harness.automations import (
    compute_next_run_at,
    next_cron_fire,
)
from toda.harness.prompts import AUTOMATION_TRIGGER_GUIDANCE, build_agent_messages


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


def test_update_changes_schedule_and_run_now_only_fires_selected_automation(brain):
    first = brain.automations.create(
        organization_id="org_acme", name="First", goal="first",
        principal_id="person_ajay", conversation_id="automation:first",
        destination=_destination(), interval_seconds=3600,
    )
    second = brain.automations.create(
        organization_id="org_acme", name="Second", goal="second",
        principal_id="person_ajay", conversation_id="automation:second",
        destination=_destination(), interval_seconds=3600,
    )
    second["next_run_at"] = "2000-01-01T00:00:00+00:00"
    brain.automations._upsert(second)

    updated = brain.automations.update(
        first["id"], organization_id="org_acme", name="Renamed",
        cron_expression="0 9 * * 1-5", timezone="UTC",
    )
    assert updated["name"] == "Renamed"
    assert updated["schedule_kind"] == "cron"
    assert updated["interval_seconds"] is None

    result = brain.automations.run_now(first["id"], "org_acme")
    assert result["fired"] == 1
    refreshed_second = brain.store.get_harness_record("automations", second["id"])
    assert refreshed_second["last_session_id"] is None


def test_automation_admin_api_edit_run_and_delete(api_client):
    created_response = api_client.post("/v1/automations", json={
        "organization_id": "org_acme",
        "name": "Daily check",
        "goal": "Check status",
        "principal_id": "person_ajay",
        "conversation_id": "automation:api-test",
        "destination": _destination(),
        "interval_seconds": 3600,
    })
    assert created_response.status_code == 200
    created = created_response.json()

    updated = api_client.put(f"/v1/automations/{created['id']}", json={
        "organization_id": "org_acme",
        "name": "Weekday check",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "Weekday check"
    assert updated.json()["schedule_kind"] == "cron"

    fired = api_client.post(
        f"/v1/automations/{created['id']}:run",
        json={"organization_id": "org_acme"},
    )
    assert fired.status_code == 200
    assert fired.json()["fired"] == 1

    deleted = api_client.delete(
        f"/v1/automations/{created['id']}",
        params={"organization_id": "org_acme"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


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
    # A queued Session dispatch is already active work even before its root
    # Task is created, so overlap detection must suppress another fire.
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="automation:overlap-skip",
        destination=_destination(),
        role="user",
        content="Previous scheduled activation",
        principal_id="person_ajay",
        trigger_kind="automation",
        purpose="scheduled_automation",
    )
    automation["last_session_id"] = wake["session_id"]
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


def test_a_pure_notification_automation_firing_writes_no_durable_memory(brain):
    """A scheduled firing that made no tool calls observed nothing -- its
    whole content is text Toda generated for itself, so there is no new
    company fact to remember. Capturing it anyway is how a 3-minute
    reminder automation silently wrote ~480 worthless memories a day
    ("Task task_x has final answer: 'Time for a hydration check...'"),
    permanently degrading retrieval. Delivery must still be enqueued --
    the reminder itself is the entire point of the run."""
    enqueued: list[str] = []
    brain.company_agent.enqueue_job = lambda **kwargs: enqueued.append(kwargs["kind"])

    brain.company_agent._enqueue_post_run_jobs(
        {
            "id": "task_reminder",
            "organization_id": "org_acme",
            "trigger_kind": "automation",
            "tool_call_count": 0,
        }
    )
    assert enqueued == ["delivery.send"]

    # An automation that actually went and looked at something DID observe
    # real state, so it still gets captured.
    enqueued.clear()
    brain.company_agent._enqueue_post_run_jobs(
        {
            "id": "task_metrics_check",
            "organization_id": "org_acme",
            "trigger_kind": "automation",
            "tool_call_count": 3,
        }
    )
    assert enqueued == ["memory.capture_task", "delivery.send", "skill.curate_task"]

    # And an ordinary user-triggered reply is untouched by this rule.
    enqueued.clear()
    brain.company_agent._enqueue_post_run_jobs(
        {
            "id": "task_reactive",
            "organization_id": "org_acme",
            "trigger_kind": "reactive",
            "tool_call_count": 0,
        }
    )
    assert enqueued == ["memory.capture_task", "delivery.send"]


def test_session_manager_root_task_is_never_framed_as_a_specialist_worker():
    """Regression test for a real production bug: every root Task's own
    metadata sets worker_type="session_manager" (session_manager.py,
    child_tasks.py) -- a bare truthiness check on that field wrongly treated
    the root Task itself as a child specialist assignment, injecting
    must_not_address_user/external_writes_forbidden/<specialist_assignment>
    into the ONE Task that's actually supposed to talk to Slack directly and
    own the conversation. Confirmed live: this produced replies like
    "...should be handled by the parent Session Manager" and "No action
    taken in this worker: outbound Slack delivery is restricted here" from
    a root session_manager Task reacting to a scheduled automation firing.
    Only "general"/"pi" child worker_types (the actual _SUPPORTED_WORKER_TYPES
    in child_tasks.py) should ever get that framing."""
    base_run = {
        "id": "arun_sm",
        "goal": "hey",
        "trigger_kind": "reactive",
        "max_iterations": 12,
        "max_tool_calls": 24,
        "envelope": {
            "principal_id": "person_ajay",
            "destination": _destination(),
            "purpose": "reactive_agent",
            "allowed_scope_ids": ["scope_org_public"],
        },
        "metadata": {"worker_type": "session_manager"},
    }
    messages = build_agent_messages(run=base_run, context={}, history=[])
    assert not any("specialist_assignment" in m["content"] for m in messages)
    invocation = json.loads(
        next(
            m["content"]
            for m in messages
            if m["content"].startswith("<trusted_invocation>")
        )
        .removeprefix("<trusted_invocation>\n")
        .removesuffix("\n</trusted_invocation>")
    )
    assert "worker" not in invocation

    child_run = {**base_run, "metadata": {"worker_type": "general"}}
    child_messages = build_agent_messages(run=child_run, context={}, history=[])
    assert any("specialist_assignment" in m["content"] for m in child_messages)
