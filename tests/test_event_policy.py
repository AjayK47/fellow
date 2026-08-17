"""Event policy defaults (memory ingestion/retrieval plan §4)."""

from __future__ import annotations

from toda.harness.event_policy import resolve_event_policy
from toda.harness.models import EventActor, EventScope, NormalizedEvent


def _event(**overrides: object) -> NormalizedEvent:
    defaults = dict(
        organization_id="org_acme",
        source="slack",
        type="message",
        actor=EventActor(id="user_1"),
        scope=EventScope(channel_id="C1"),
        payload={},
        dedupe_key="dedupe_1",
    )
    defaults.update(overrides)
    return NormalizedEvent(**defaults)


def test_direct_request_always_wakes_and_is_evidence_only() -> None:
    event = _event(is_direct_agent_request=True, payload={"conversation_key": "conv_1"})
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "always"
    assert policy.memory_policy == "evidence_only"
    assert policy.group_kind == "slack_thread"
    assert policy.group_key == "conv_1"


def test_ambient_slack_message_uses_attention_and_episode_grouping() -> None:
    event = _event(payload={"context_key": "chan:C1"})
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "attention"
    assert policy.memory_policy == "episode"
    assert policy.group_kind == "slack_thread"
    assert policy.group_key == "chan:C1"


def test_ambient_slack_falls_back_to_channel_derived_group_key() -> None:
    event = _event(payload={})
    policy = resolve_event_policy(event)
    assert policy.group_key == "slack:C1"


def test_github_issue_event_groups_by_repo_and_number() -> None:
    event = _event(
        source="composio:github",
        payload={
            "toolkit": "github",
            "trigger_slug": "github_issue_updated",
            "raw_data": {"repository": {"full_name": "acme/widgets"}, "issue": {"number": 42}},
        },
    )
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "attention"
    assert policy.memory_policy == "episode"
    assert policy.group_kind == "github_issue"
    assert policy.group_key == "github_issue:acme/widgets#42"


def test_github_pull_request_groups_distinctly_from_issues() -> None:
    event = _event(
        source="composio:github",
        payload={
            "toolkit": "github",
            "trigger_slug": "github_pr_review",
            "raw_data": {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": 7}},
        },
    )
    policy = resolve_event_policy(event)
    assert policy.group_kind == "github_pr"
    assert policy.group_key == "github_pr:acme/widgets#7"


def test_linear_issue_event_groups_by_identifier() -> None:
    event = _event(
        source="composio:linear",
        payload={"toolkit": "linear", "raw_data": {"issue": {"identifier": "ENG-123"}}},
    )
    policy = resolve_event_policy(event)
    assert policy.group_kind == "linear_issue"
    assert policy.group_key == "linear_issue:ENG-123"


def test_ambiguous_connector_payload_falls_back_to_evidence_only() -> None:
    event = _event(
        source="composio:github",
        payload={"toolkit": "github", "raw_data": {"comment": "no repo/number here"}},
    )
    policy = resolve_event_policy(event)
    assert policy.memory_policy == "evidence_only"
    assert policy.group_key is None


def test_alert_trigger_gets_immediate_memory_and_wakes() -> None:
    event = _event(
        source="composio:datadog",
        urgency="urgent",
        payload={"toolkit": "datadog", "trigger_slug": "datadog_alert_triggered", "raw_data": {}},
    )
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "always"
    assert policy.memory_policy == "immediate"


def test_unknown_connector_defaults_to_never_wake_evidence_only() -> None:
    event = _event(source="composio:mystery_toolkit", payload={"toolkit": "mystery_toolkit", "raw_data": {}})
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "attention"
    assert policy.memory_policy == "evidence_only"


def test_non_composio_non_slack_source_defaults_to_never_wake() -> None:
    event = _event(source="document", payload={})
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "never"
    assert policy.memory_policy == "evidence_only"
    assert policy.group_key is None


def test_explicit_policy_on_event_is_respected_as_is() -> None:
    event = _event(
        wake_policy="never",
        memory_policy="immediate",
        group_key="custom:1",
        group_kind="custom",
    )
    policy = resolve_event_policy(event)
    assert policy.wake_policy == "never"
    assert policy.memory_policy == "immediate"
    assert policy.group_key == "custom:1"
    assert policy.group_kind == "custom"


def _connector_event(*, occurred_at, raw=None, slug="GITHUB_PULL_REQUEST_CREATED"):
    from toda.harness.models import EventActor, EventScope, NormalizedEvent

    return NormalizedEvent(
        id="ev_test",
        organization_id="org_acme",
        source="composio:github",
        type="composio.github_pull_request_created",
        occurred_at=occurred_at,
        actor=EventActor(id="connector:x", source_id="x", type="service"),
        scope=EventScope(workspace_id="", channel_id="", visibility="organization_public",
                         scope_id="scope_org_public"),
        payload={"toolkit": "github", "trigger_slug": slug, "raw_data": raw or {}},
        dedupe_key="ev_test",
    )


_COMPOSIO_PR = {
    # The shape Composio actually sends: flat, repository_url not repository.
    "number": 681,
    "pr_id": 2938471,
    "repository_url": "https://api.github.com/repos/acme/export-worker",
    "html_url": "https://github.com/acme/export-worker/pull/681",
    "title": "Fix retry storm",
    "author_login": "ajay",
}


def test_composio_github_payloads_produce_a_group_key():
    """`_github_object` looked for `repository`; Composio sends
    `repository_url`. Every real GitHub event therefore returned None, fell
    through to evidence_only, and was never extracted into a memory --
    measured live as 1,229 events producing 4,421 chunks and zero facts."""
    from toda.harness.event_policy import _github_object

    assert _github_object(_COMPOSIO_PR) == (
        "github_pr:acme/export-worker#681",
        "github_pr",
    )
    # The older nested webhook shape still works.
    assert _github_object(
        {"repository": {"full_name": "acme/x"}, "number": 4, "pull_request": {"number": 4}}
    ) == ("github_pr:acme/x#4", "github_pr")
    # An issue is distinguished by the absence of PR markers.
    assert _github_object(
        {"repository_url": "https://api.github.com/repos/acme/x", "number": 9,
         "html_url": "https://github.com/acme/x/issues/9"}
    ) == ("github_issue:acme/x#9", "github_issue")


def test_historical_connector_events_never_reach_the_classifier():
    """A poll trigger's first run emits the account's whole history as
    "new". One GitHub trigger produced 6,525 events in under an hour, each
    routed to a ~5.8s model call to decide not to interrupt anyone."""
    from datetime import datetime, timedelta, timezone

    from toda.harness.event_policy import resolve_event_policy

    now = datetime.now(timezone.utc)
    old = resolve_event_policy(
        _connector_event(occurred_at=now - timedelta(days=400), raw=_COMPOSIO_PR)
    )
    assert old.wake_policy == "never"
    # Still remembered and searchable -- only the model call is skipped.
    assert old.memory_policy == "episode"
    assert old.group_key == "github_pr:acme/export-worker#681"

    fresh = resolve_event_policy(
        _connector_event(occurred_at=now - timedelta(minutes=5), raw=_COMPOSIO_PR)
    )
    assert fresh.wake_policy == "attention"


def test_slack_is_unaffected_by_the_connector_age_cutoff():
    """The cutoff is about connector backfills. Slack has no equivalent
    flood and must keep its ambient attention behaviour."""
    from datetime import datetime, timedelta, timezone

    from toda.harness.event_policy import resolve_event_policy
    from toda.harness.models import EventActor, EventScope, NormalizedEvent

    event = NormalizedEvent(
        id="ev_slack_old",
        organization_id="org_acme",
        source="slack",
        type="slack.channel_message",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=30),
        actor=EventActor(id="person_ajay", source_id="U1", type="person"),
        scope=EventScope(workspace_id="T1", channel_id="C1",
                         visibility="organization_public", scope_id="scope_org_public"),
        payload={"text": "hi", "conversation_key": "slack:T1:C1"},
        dedupe_key="ev_slack_old",
    )
    assert resolve_event_policy(event).wake_policy == "attention"
