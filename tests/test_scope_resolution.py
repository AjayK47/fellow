"""H-12/H-13: resolve_scope() — one write scope per room, a correct read
set, and the delivery disclosure check that recomputes it fresh."""

from __future__ import annotations

import pytest

from docket.harness.models import InvocationEnvelope
from docket.services.scope_resolution import resolve_scope


def _make_channel_scope(brain, *, scope_id: str, channel_id: str, is_public: bool) -> None:
    brain.store.upsert_scope(
        {
            "id": scope_id,
            "organization_id": "org_acme",
            "kind": "organization_public" if is_public else "private_channel",
            "label": scope_id,
            "channel_id": channel_id,
            "principal_ids": [],
            "is_public": is_public,
        }
    )


def test_public_channel_sees_every_public_scope_not_just_its_own(brain):
    _make_channel_scope(brain, scope_id="scope_pub_a", channel_id="Cpuba", is_public=True)
    _make_channel_scope(brain, scope_id="scope_pub_b", channel_id="Cpubb", is_public=True)

    resolution = resolve_scope(
        store=brain.store,
        organization_id="org_acme",
        principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "Cpuba", "is_private": False},
    )
    assert resolution.write_scope_id == "scope_pub_a"
    assert "scope_pub_a" in resolution.read_scope_ids
    assert "scope_pub_b" in resolution.read_scope_ids


def test_private_channel_never_sees_another_private_channel_even_for_a_shared_member(brain):
    _make_channel_scope(brain, scope_id="scope_priv_a", channel_id="Cpriva", is_public=False)
    _make_channel_scope(brain, scope_id="scope_priv_b", channel_id="Cprivb", is_public=False)
    brain.store.add_membership("org_acme", "person_shared", "scope_priv_a")
    brain.store.add_membership("org_acme", "person_shared", "scope_priv_b")

    resolution = resolve_scope(
        store=brain.store,
        organization_id="org_acme",
        principal_id="person_shared",
        destination={"type": "private_channel", "id": "Cpriva", "is_private": True},
    )
    assert resolution.write_scope_id == "scope_priv_a"
    assert resolution.read_scope_ids == {"scope_priv_a"}
    assert "scope_priv_b" not in resolution.read_scope_ids


def test_dm_expands_to_every_private_channel_the_person_belongs_to(brain):
    _make_channel_scope(brain, scope_id="scope_priv_c", channel_id="Cprivc", is_public=False)
    _make_channel_scope(brain, scope_id="scope_priv_d", channel_id="Cprivd", is_public=False)
    brain.store.add_membership("org_acme", "person_multi", "scope_priv_c")
    brain.store.add_membership("org_acme", "person_multi", "scope_priv_d")

    resolution = resolve_scope(
        store=brain.store,
        organization_id="org_acme",
        principal_id="person_multi",
        destination={"type": "dm", "id": "D_multi", "is_private": True},
    )
    assert resolution.read_scope_ids >= {"scope_priv_c", "scope_priv_d"}


def test_removing_membership_immediately_removes_dm_based_access(brain):
    _make_channel_scope(brain, scope_id="scope_priv_e", channel_id="Cprive", is_public=False)
    brain.store.add_membership("org_acme", "person_leaver", "scope_priv_e")

    before = resolve_scope(
        store=brain.store,
        organization_id="org_acme",
        principal_id="person_leaver",
        destination={"type": "dm", "id": "D_leaver", "is_private": True},
    )
    assert "scope_priv_e" in before.read_scope_ids

    with brain.store.cursor() as cur:
        cur.execute(
            "DELETE FROM principal_memberships WHERE principal_id = ? AND scope_id = ?",
            ("person_leaver", "scope_priv_e"),
        )

    after = resolve_scope(
        store=brain.store,
        organization_id="org_acme",
        principal_id="person_leaver",
        destination={"type": "dm", "id": "D_leaver", "is_private": True},
    )
    assert "scope_priv_e" not in after.read_scope_ids


def test_capture_run_writes_to_the_conversations_own_scope(brain):
    """H-12: the memory captured from a run must be scoped to the room the
    run actually happened in, not an arbitrary entry from allowed_scope_ids."""
    _make_channel_scope(brain, scope_id="scope_priv_f", channel_id="Cprivf", is_public=False)
    brain.store.add_membership("org_acme", "person_ajay", "scope_priv_f")

    destination = {"type": "private_channel", "id": "Cprivf", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    # A private channel reads only its exact private scope.
    assert decision.allowed_scope_ids == ["scope_priv_f"]

    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="slack:T:Cprivf:1",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    run = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="Summarize the private rollout plan",
        trigger_kind="reactive",
        initial_message="Summarize the private rollout plan",
    )
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "completed"

    captured = brain.harness_memory.capture_run(run["id"])
    assert captured["captured"]
    source = brain.store.get_source(captured["source_id"])
    assert source["scope_id"] == "scope_priv_f"


def _run_dict(*, organization_id: str, principal_id: str, destination: dict, final_answer: str) -> dict:
    return {
        "id": "arun_test_disclosure",
        "organization_id": organization_id,
        "state": "completed",
        "final_answer": final_answer,
        "envelope": {
            "organization_id": organization_id,
            "principal_id": principal_id,
            "destination": destination,
        },
        "trigger_kind": "reactive",
        "trace_id": "trace_test_disclosure",
        "metadata": {},
    }


def test_delivery_blocks_an_answer_citing_evidence_outside_the_destination_scope(brain):
    _make_channel_scope(brain, scope_id="scope_priv_g", channel_id="Cprivg", is_public=False)
    _make_channel_scope(brain, scope_id="scope_pub_g", channel_id="Cpubg", is_public=True)
    # A source that only lives in the private channel's scope.
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="ev_private_g",
        content="the acquisition talks are confidential",
        scope_id="scope_priv_g",
        author_entity_id="person_outsider",
        learn=False,
        processing_mode="reference",
    )
    private_source_id = result["source"]["id"]

    # Deliver to the *public* channel while citing the private-channel source.
    run = _run_dict(
        organization_id="org_acme",
        principal_id="person_outsider",
        destination={"type": "slack_channel", "id": "Cpubg", "is_private": False},
        final_answer=f"The plan is confirmed.\n\nEvidence: {private_source_id}",
    )
    with pytest.raises(PermissionError, match="outside the current destination"):
        brain.delivery._check_disclosure(run)


def test_delivery_allows_an_answer_citing_evidence_inside_the_destination_scope(brain):
    _make_channel_scope(brain, scope_id="scope_pub_h", channel_id="Cpubh", is_public=True)
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="ev_public_h",
        content="the export job finished on schedule",
        scope_id="scope_pub_h",
        author_entity_id="person_ajay",
        learn=False,
        processing_mode="reference",
    )
    public_source_id = result["source"]["id"]

    run = _run_dict(
        organization_id="org_acme",
        principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "Cpubh", "is_private": False},
        final_answer=f"All good.\n\nEvidence: {public_source_id}",
    )
    # Must not raise.
    brain.delivery._check_disclosure(run)
