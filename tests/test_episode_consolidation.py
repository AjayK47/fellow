"""Incremental, generic episode consolidation (memory ingestion/retrieval
plan §5 Phase C / acceptance criteria in §12)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_quiet_window(brain):
    # Router-enqueued knowledge.consolidate_episode jobs are delayed by
    # config.episode_quiet_seconds (available_at = occurred_at + quiet);
    # tests that want the router's own enqueue (not a direct
    # consolidate_episode() call) need that delay at 0 to drain promptly.
    brain.event_router.config.episode_quiet_seconds = 0


def _ambient_message(*, event_id: str, channel_id: str, text: str, occurred_at=None) -> dict:
    payload = {
        "id": event_id,
        "organization_id": "org_acme",
        "source": "slack",
        "type": "slack.channel_message",
        "actor": {"id": "person_ajay", "type": "person"},
        "scope": {
            "workspace_id": "TACME",
            "channel_id": channel_id,
            "visibility": "organization_public",
            "scope_id": "scope_org_public",
        },
        "payload": {"text": text, "conversation_key": f"slack:TACME:{channel_id}:1"},
        "dedupe_key": f"dedupe-{event_id}",
    }
    if occurred_at:
        payload["occurred_at"] = occurred_at
    return payload


def _drain_knowledge(brain):
    return brain.harness_worker.drain_until_idle(kinds=brain.harness_worker.knowledge_job_kinds())


def test_ambient_messages_in_the_same_channel_join_one_episode(brain):
    brain.event_router.route(_ambient_message(event_id="ev_ep_1", channel_id="Cepisode", text="Deploy started"))
    brain.event_router.route(_ambient_message(event_id="ev_ep_2", channel_id="Cepisode", text="Deploy finished OK"))
    drained = _drain_knowledge(brain)
    assert drained["failed"] == 0, drained

    episode = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cepisode:1")
    assert episode is not None
    assert episode["event_count"] == 2
    members = brain.store.list_episode_sources(episode["id"])
    assert len(members) == 2
    assert episode["summary"] and "Deploy" in episode["summary"]


def test_second_consolidation_only_processes_sources_after_the_watermark(brain):
    brain.event_router.route(_ambient_message(event_id="ev_wm_1", channel_id="Cwatermark", text="first message"))
    first_drain = _drain_knowledge(brain)
    assert first_drain["failed"] == 0

    episode = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cwatermark:1")
    assert episode["event_count"] == 1
    first_ended_at = episode["ended_at"]

    brain.event_router.route(
        _ambient_message(event_id="ev_wm_2", channel_id="Cwatermark", text="second message")
    )
    second_drain = _drain_knowledge(brain)
    assert second_drain["failed"] == 0

    episode = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cwatermark:1")
    assert episode["event_count"] == 2  # not reprocessed from scratch
    assert episode["ended_at"] > first_ended_at
    members = brain.store.list_episode_sources(episode["id"])
    assert [m["ordinal"] for m in members] == [0, 1]


def test_different_groups_never_share_an_episode(brain):
    brain.event_router.route(_ambient_message(event_id="ev_grp_a", channel_id="Cgroupa", text="channel a message"))
    brain.event_router.route(_ambient_message(event_id="ev_grp_b", channel_id="Cgroupb", text="channel b message"))
    drained = _drain_knowledge(brain)
    assert drained["failed"] == 0

    episode_a = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cgroupa:1")
    episode_b = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cgroupb:1")
    assert episode_a["id"] != episode_b["id"]
    assert episode_a["event_count"] == 1
    assert episode_b["event_count"] == 1


def test_retrying_consolidation_is_idempotent(brain):
    brain.event_router.route(_ambient_message(event_id="ev_idem_1", channel_id="Cidem", text="only message"))
    _drain_knowledge(brain)
    episode = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cidem:1")

    # Re-running consolidation for the same group with nothing new must be
    # a safe no-op, not a duplicate pass over the same source.
    result = brain.harness_memory.consolidate_episode(
        "org_acme",
        group_key="slack:TACME:Cidem:1",
        group_kind="slack_thread",
        source_system="slack",
        scope_id="scope_org_public",
    )
    assert result["consolidated"] is False
    assert result["reason"] == "no_new_sources"
    unchanged = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cidem:1")
    assert unchanged["event_count"] == episode["event_count"]
    assert len(brain.store.list_episode_sources(episode["id"])) == 1


def test_quiet_window_defers_consolidation(brain):
    routed = brain.event_router.route(
        _ambient_message(event_id="ev_quiet_1", channel_id="Cquiet", text="just happened")
    )
    source_id = routed["source_id"]
    result = brain.harness_memory.consolidate_episode(
        "org_acme",
        group_key="slack:TACME:Cquiet:1",
        group_kind="slack_thread",
        source_system="slack",
        scope_id="scope_org_public",
        quiet_seconds=3600,
    )
    assert result["consolidated"] is False
    assert result["reason"] == "group_not_quiet"
    assert brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cquiet:1") is None
    assert brain.store.has_chunks_for_source(source_id) is False  # nothing extracted yet either


def test_episode_closes_at_the_max_event_boundary_and_a_new_one_opens(brain, monkeypatch):
    from toda.harness import memory_capture as memory_capture_module

    monkeypatch.setattr(memory_capture_module, "_EPISODE_MAX_EVENTS", 2)

    brain.event_router.route(_ambient_message(event_id="ev_max_1", channel_id="Cmax", text="one"))
    brain.event_router.route(_ambient_message(event_id="ev_max_2", channel_id="Cmax", text="two"))
    _drain_knowledge(brain)

    closed = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cmax:1")
    assert closed["status"] == "complete"
    assert closed["event_count"] == 2

    brain.event_router.route(
        _ambient_message(event_id="ev_max_3", channel_id="Cmax", text="three")
    )
    _drain_knowledge(brain)

    reopened = brain.store.get_episode_by_group_key("org_acme", "slack:TACME:Cmax:1")
    assert reopened["id"] != closed["id"]
    assert reopened["event_count"] == 1
