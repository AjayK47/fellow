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
