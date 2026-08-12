"""Episode search and the unified brain_search orchestration (memory
ingestion/retrieval plan §8)."""

from __future__ import annotations


def _ambient_message(*, event_id: str, channel_id: str, text: str) -> dict:
    return {
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


def _seed_episode(brain, *, channel_id: str, text: str) -> str:
    brain.event_router.config.episode_quiet_seconds = 0
    brain.event_router.route(
        _ambient_message(event_id=f"ev_{channel_id}", channel_id=channel_id, text=text)
    )
    brain.harness_worker.drain_until_idle(kinds=brain.harness_worker.knowledge_job_kinds())
    episode = brain.store.get_episode_by_group_key("org_acme", f"slack:TACME:{channel_id}:1")
    assert episode is not None
    return episode["id"]


def test_search_episodes_finds_a_lexically_matching_episode(brain):
    episode_id = _seed_episode(brain, channel_id="Cunified1", text="Payment gateway outage resolved after rollback")

    result = brain.retrieval.search_episodes(
        organization_id="org_acme",
        query="payment gateway outage",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="search",
    )
    ids = {r["episode_id"] for r in result["results"]}
    assert episode_id in ids


def test_search_episodes_respects_scope_authorization(brain):
    private = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="unified-private-src",
        content="Confidential headcount planning discussion",
        scope_id="scope_channel_leadership_private",
        is_private_channel=True,
        channel_id="channel_leadership",
        learn=False,
        processing_mode="reference",
    )
    episode = {
        "id": "episode_unified_private",
        "organization_id": "org_acme",
        "group_key": "slack:TACME:Cprivate:1",
        "group_kind": "slack_thread",
        "source_system": "slack",
        "started_at": private["source"]["occurred_at"],
        "ended_at": private["source"]["occurred_at"],
        "last_source_id": private["source"]["id"],
        "event_count": 1,
        "status": "quiet",
        "summary": "Confidential headcount planning discussion",
        "summary_embedding": brain.embeddings.embed(["Confidential headcount planning discussion"])[0],
        "extraction_revision": 1,
        "scope_id": "scope_channel_leadership_private",
        "privacy_class": "internal",
        "error": None,
        "created_at": private["source"]["ingested_at"],
        "updated_at": private["source"]["ingested_at"],
    }
    brain.store.upsert_episode(episode)

    public_result = brain.retrieval.search_episodes(
        organization_id="org_acme",
        query="confidential headcount planning",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="search",
    )
    assert "episode_unified_private" not in {r["episode_id"] for r in public_result["results"]}

    private_result = brain.retrieval.search_episodes(
        organization_id="org_acme",
        query="confidential headcount planning",
        acting_principal_id="person_ajay",
        destination={"type": "private_channel", "id": "channel_leadership", "is_private": True},
        purpose="search",
    )
    assert "episode_unified_private" in {r["episode_id"] for r in private_result["results"]}


def test_unified_search_with_kind_filter_returns_only_that_section(brain):
    _seed_episode(brain, channel_id="Cunified2", text="Renewal call with Northstar rescheduled to Friday")

    result = brain.retrieval.unified_search(
        organization_id="org_acme",
        query="Northstar renewal",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="search",
        kind="episode",
    )
    assert set(result["results"]) == {"episode"}


def test_unified_search_without_kind_returns_all_three_sections(brain):
    result = brain.retrieval.unified_search(
        organization_id="org_acme",
        query="export worker",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="search",
    )
    assert set(result["results"]) == {"memory", "episode", "evidence"}


def test_unified_search_filters_evidence_by_source_system(brain):
    brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="composio:github",
        source_type="composio.issue",
        external_id="unified-src-github",
        content="Deploy pipeline flaky test tracked in github",
        scope_id="scope_org_public",
        learn=False,
        processing_mode="reference",
    )
    brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="unified-src-slack",
        content="Deploy pipeline flaky test mentioned in slack",
        scope_id="scope_org_public",
        learn=False,
        processing_mode="reference",
    )

    result = brain.retrieval.unified_search(
        organization_id="org_acme",
        query="deploy pipeline flaky test",
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="search",
        kind="evidence",
        source_system="composio:github",
    )
    assert all(item["source_system"] == "composio:github" for item in result["results"]["evidence"])
    assert result["results"]["evidence"]
