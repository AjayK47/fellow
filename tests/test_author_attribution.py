"""A retrieved claim must carry who made it, not just what was said.

Regression for a confirmed live failure: asked "who said that?" about a
salary-increment claim it had just surfaced, Toda answered that "the source
record doesn't carry the author's name" and could only name the raw channel
ID. The author was never missing -- `router.py` writes `author_entity_id` onto
every source row at ingest -- but every retrieval projection dropped the field
on the way to the model, so the agent reported exactly the fields it was given.

The second half is identity: an author id is only useful if it resolves to a
person, and that person has to be the same human across Slack, email and
connectors. Email is the join key every system agrees on, so it is registered
as the highest-confidence *identifier*. The entity id stays the primary key --
addresses change and people hold several at once, while source rows, memory
participants and graph edges all point at the entity.
"""

from __future__ import annotations

from toda.fixtures.seed import ORG
from toda.harness.slack import resolve_slack_principal, slack_identity_mapping


def _profile(name: str = "Ajay", email: str | None = None) -> dict:
    profile = {"display_name": name, "real_name": name}
    if email:
        profile["email"] = email
    return profile


# ---------------------------------------------------------------------------
# Identity: email as the resolution key
# ---------------------------------------------------------------------------


def test_slack_principal_registers_slack_and_email_identifiers(brain):
    principal = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_AJAY",
        user_profile=_profile(email="ajay@acme.com"),
        reality=brain.reality,
    )

    slack_link = brain.reality.lookup_identifier(ORG, "slack", "TACME:U_AJAY")
    email_link = brain.reality.lookup_identifier(ORG, "email", "ajay@acme.com")
    assert slack_link["entity_id"] == principal
    assert email_link["entity_id"] == principal
    # The address has to survive onto the entity too -- it used to be filtered
    # out of the profile attributes even when Slack had already supplied it.
    assert brain.store.get_entity(principal)["attributes"]["email"] == "ajay@acme.com"


def test_slack_user_reuses_the_person_a_connector_created_by_email(brain):
    """The whole point of the join key: one human, not one per system."""
    brain.graph.upsert_entity(
        {
            "id": "person_from_connector",
            "organization_id": ORG,
            "entity_type": "person",
            "name": "Ajay Kommineni",
            "aliases": [],
            "attributes": {},
        }
    )
    brain.reality.link_identifier(
        organization_id=ORG,
        entity_id="person_from_connector",
        namespace="email",
        value="ajay@acme.com",
        confidence=1.0,
        verified=True,
    )

    principal = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_AJAY",
        user_profile=_profile(email="ajay@acme.com"),
        reality=brain.reality,
    )

    assert principal == "person_from_connector"
    # ...and the connector-built entity picks up its Slack identity, which it
    # had no way to know before this point.
    assert brain.store.get_entity(principal)["attributes"]["slack_user_id"] == "U_AJAY"


def test_a_contested_address_files_a_candidate_instead_of_raising(brain):
    """`link_identifier` raises on conflict, which is right for an operator
    action and fatal for ingestion -- two systems disagreeing about who owns an
    address is ordinary, and must not cost us the message."""
    principal = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_ARUN",
        user_profile=_profile("Arun"),
        reality=brain.reality,
    )
    brain.graph.upsert_entity(
        {
            "id": "person_someone_else",
            "organization_id": ORG,
            "entity_type": "person",
            "name": "Someone Else",
            "aliases": [],
            "attributes": {},
        }
    )
    brain.reality.link_identifier(
        organization_id=ORG,
        entity_id="person_someone_else",
        namespace="email",
        value="shared@acme.com",
        confidence=1.0,
        verified=True,
    )

    again = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_ARUN",
        user_profile=_profile("Arun", email="shared@acme.com"),
        reality=brain.reality,
    )

    assert again == principal  # never silently remapped onto the other person
    assert (
        brain.reality.lookup_identifier(ORG, "email", "shared@acme.com")["entity_id"]
        == "person_someone_else"
    )
    pending = brain.reality.list_resolution_candidates(ORG, status="pending")
    assert any(
        candidate["identifier"]["value"] == "shared@acme.com" for candidate in pending
    )


def test_identity_linking_never_breaks_ingestion(brain):
    """Enrichment is best-effort; message ingestion is not allowed to fail."""

    class BrokenReality:
        def lookup_identifier(self, *args, **kwargs):
            raise RuntimeError("identifier store unavailable")

        def link_identifier_or_propose(self, **kwargs):
            raise RuntimeError("identifier store unavailable")

    principal = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_MEERA",
        user_profile=_profile("Meera", email="meera@acme.com"),
        reality=BrokenReality(),
    )

    assert principal == "person_slack_tacme_u_meera"


def test_slack_identifiers_are_namespaced_by_workspace(brain):
    principal = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_SHARED",
        user_profile=_profile("First"),
        reality=brain.reality,
    )

    assert (
        brain.reality.lookup_identifier(ORG, "slack", "TACME:U_SHARED")["entity_id"]
        == principal
    )
    # The bare id must not resolve on its own -- that is what would let a
    # second workspace's U_SHARED claim this person.
    assert brain.reality.lookup_identifier(ORG, "slack", "U_SHARED") is None


def test_two_workspaces_do_not_share_one_person(brain):
    """Slack assigns user IDs per workspace. The identity mapping keyed on the
    bare id merged them onto whichever principal was seen first -- and the
    same lookup decides who is allowed to approve an action."""
    first = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TACME",
        "U_SHARED",
        user_profile=_profile("First"),
        reality=brain.reality,
    )
    second = resolve_slack_principal(
        brain.store,
        brain.graph,
        ORG,
        "TOTHER",
        "U_SHARED",
        user_profile=_profile("Second"),
        reality=brain.reality,
    )

    assert first != second
    assert (
        slack_identity_mapping(brain.store, ORG, "TACME", "U_SHARED")["principal_id"]
        == first
    )
    assert (
        slack_identity_mapping(brain.store, ORG, "TOTHER", "U_SHARED")["principal_id"]
        == second
    )


def test_a_mapping_written_before_workspaces_still_resolves(brain):
    """Rows predating the qualified key must keep working -- this lookup backs
    approval authorization, so losing it would fail closed on live approvals."""
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_legacy",
            "organization_id": ORG,
            "source": "slack",
            "source_user_id": "U_LEGACY",
            "principal_id": "person_ajay",
        },
    )

    mapping = slack_identity_mapping(brain.store, ORG, "TACME", "U_LEGACY")

    assert mapping["principal_id"] == "person_ajay"
    # ...and it is upgraded in place, so the ambiguity resolves itself rather
    # than needing a migration.
    upgraded = brain.store.identity_mapping(ORG, "slack", "TACME:U_LEGACY")
    assert upgraded["principal_id"] == "person_ajay"
    assert upgraded["workspace_id"] == "TACME"


def test_a_legacy_mapping_is_not_reused_by_a_different_workspace(brain):
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_owned",
            "organization_id": ORG,
            "source": "slack",
            "source_user_id": "U_OWNED",
            "principal_id": "person_ajay",
            "workspace_id": "TACME",
        },
    )

    assert slack_identity_mapping(brain.store, ORG, "TOTHER", "U_OWNED") is None


# ---------------------------------------------------------------------------
# Projection: the author has to reach the model
# ---------------------------------------------------------------------------


def test_evidence_results_carry_the_author(brain):
    result = brain.retrieval._evidence_result(
        {
            "id": "chunk_1",
            "source_id": "src_1",
            "content": "we will be giving 5% increment i guess this year",
            "scope_id": "scope_channel_eng",
        },
        {
            "id": "src_1",
            "source_system": "slack",
            "source_type": "message",
            "author_entity_id": "person_ajay",
            "channel_id": "channel_eng",
        },
        score=0.5,
    )

    assert result["author_entity_id"] == "person_ajay"
    assert result["author_name"] == "Ajay"


def test_unattributed_evidence_reports_no_name_rather_than_an_id(brain):
    result = brain.retrieval._evidence_result(
        {"id": "chunk_2", "source_id": "src_2", "content": "x", "scope_id": "scope_channel_eng"},
        {"id": "src_2", "source_system": "api", "source_type": "document"},
        score=0.1,
    )

    assert result["author_entity_id"] is None
    assert result["author_name"] is None


def test_memory_projection_exposes_author_separately_from_participants(brain):
    projected = brain.retrieval._public_memory(
        {
            "id": "mem_1",
            "content": "5% increment planned",
            "author_entity_id": "person_ajay",
            "participant_ids": ["person_ajay", "person_ravi"],
        }
    )

    assert projected["author_entity_id"] == "person_ajay"
    assert projected["author_name"] == "Ajay"
    assert projected["participant_names"] == ["Ajay", "Ravi"]


def test_memories_written_before_the_author_field_still_attribute(brain):
    """No backfill required: a lone participant was the old way of recording
    the author, so it is a safe read-time fallback."""
    projected = brain.retrieval._public_memory(
        {"id": "mem_legacy", "content": "x", "participant_ids": ["person_ravi"]}
    )

    assert projected["author_entity_id"] == "person_ravi"
    assert projected["author_name"] == "Ravi"


def test_an_ambiguous_legacy_memory_claims_no_author(brain):
    """Two participants carry no information about which one spoke -- guessing
    would be worse than the gap it replaces."""
    projected = brain.retrieval._public_memory(
        {
            "id": "mem_legacy_2",
            "content": "x",
            "participant_ids": ["person_ajay", "person_ravi"],
        }
    )

    assert projected["author_entity_id"] is None
    assert projected["author_name"] is None


# ---------------------------------------------------------------------------
# Rooms: a channel ID is an internal handle, not an answer
# ---------------------------------------------------------------------------


def _event(channel: str, *, channel_type: str = "channel", ts: str = "1.0") -> dict:
    return {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": f"Ev-{channel}-{ts}",
        "event": {
            "type": "message",
            "user": "UAJAY",
            "channel": channel,
            "channel_type": channel_type,
            "text": "we will be giving 5% increment i guess this year",
            "ts": ts,
            "client_msg_id": f"msg-{channel}-{ts}",
        },
    }


def test_a_channel_scope_is_labelled_with_its_name(brain, monkeypatch):
    monkeypatch.setattr(
        brain.slack_gateway,
        "_fetch_channel_info",
        lambda workspace_id, channel_id: {"name": "compensation", "is_private": False},
    )

    event = brain.slack_gateway.normalize(_event("C0BP95K48V6"))

    assert brain.store.get_scope(event.scope.scope_id)["label"] == "#compensation"


def test_an_unreachable_slack_leaves_the_placeholder_and_heals_later(brain, monkeypatch):
    """A name is cosmetic; failing to fetch it must not block ingestion."""
    monkeypatch.setattr(
        brain.slack_gateway, "_fetch_channel_info", lambda *args, **kwargs: None
    )
    event = brain.slack_gateway.normalize(_event("C0BP95K48V6"))
    assert brain.store.get_scope(event.scope.scope_id)["label"] == "Slack C0BP95K48V6"

    monkeypatch.setattr(
        brain.slack_gateway,
        "_fetch_channel_info",
        lambda workspace_id, channel_id: {"name": "compensation", "is_private": False},
    )
    brain.slack_gateway.normalize(_event("C0BP95K48V6", ts="2.0"))

    assert brain.store.get_scope(event.scope.scope_id)["label"] == "#compensation"


def test_naming_a_room_never_costs_a_privacy_correction(brain, monkeypatch):
    """The label repair sits outside the privacy branches on purpose: a scope
    wrongly on record as public must still be narrowed on the same turn."""
    monkeypatch.setattr(
        brain.slack_gateway, "_fetch_channel_info", lambda *args, **kwargs: None
    )
    public = brain.slack_gateway.normalize(_event("CROOM", channel_type="channel"))
    assert brain.store.get_scope(public.scope.scope_id)["is_public"] is True

    monkeypatch.setattr(
        brain.slack_gateway,
        "_fetch_channel_info",
        lambda workspace_id, channel_id: {"name": "leadership", "is_private": True},
    )
    private = brain.slack_gateway.normalize(
        _event("CROOM", channel_type="group", ts="2.0")
    )

    scope = brain.store.get_scope(private.scope.scope_id)
    assert scope["label"] == "#leadership"
    assert scope["is_public"] is False


def test_evidence_carries_the_room_name_not_just_its_id(brain):
    result = brain.retrieval._evidence_result(
        {
            "id": "chunk_3",
            "source_id": "src_3",
            "content": "x",
            "scope_id": "scope_channel_eng",
        },
        {"id": "src_3", "source_system": "slack", "channel_id": "channel_eng"},
        score=0.4,
    )

    assert result["channel_id"] == "channel_eng"
    assert result["channel_name"] == "Engineering (shared)"


def test_memory_extraction_records_the_source_author(brain):
    source = brain.ingestion.persist(
        organization_id=ORG,
        source_system="slack",
        source_type="message",
        external_id="ev_increment",
        content="We decided to give a 5% increment this year.",
        scope_id="scope_channel_eng",
        author_entity_id="person_ajay",
        channel_id="channel_eng",
    )

    memories = brain.memory.learn_from_source(source)

    assert memories
    assert all(item["author_entity_id"] == "person_ajay" for item in memories)
