"""A Slack private channel must never become an organization-public scope.

Regression for a confirmed live disclosure: a private channel's salary
discussion was retrievable by a workspace member who was never in that
channel, because `normalize()` classified the room as `organization_public`.

Two independent faults produced it:

1. The check read `inner.get("is_private")`, which Slack does not send on a
   message event -- it only exists on a conversations.info channel object --
   so the condition was permanently False.
2. `_channel_policy()` defaults `is_private` to `is_dm`, i.e. False for any
   channel, so the fallback did not catch it either.

The authoritative signal was in the payload all along: Slack sets
`channel_type="group"` for a private channel.
"""

from __future__ import annotations

from docket.harness.models import DestinationEnvelope


def _event(channel: str, channel_type: str, *, user: str = "UAJAY", ts: str = "1.0") -> dict:
    return {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": f"Ev-{channel}-{ts}",
        "event": {
            "type": "message",
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "text": "we are giving a 10% hike this year",
            "ts": ts,
            "client_msg_id": f"msg-{channel}-{ts}",
        },
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_slack_private_channel_is_not_organization_public(brain):
    """channel_type="group" is Slack's private-channel marker."""
    event = brain.slack_gateway.normalize(_event("CPRIV", "group"))

    assert event.scope.visibility == "private_channel"
    scope = brain.store.get_scope(event.scope.scope_id)
    assert scope["is_public"] is False
    assert scope["kind"] == "private_channel"


def test_slack_public_channel_stays_organization_public(brain):
    event = brain.slack_gateway.normalize(_event("CPUB", "channel"))

    assert event.scope.visibility == "organization_public"
    assert brain.store.get_scope(event.scope.scope_id)["is_public"] is True


def test_an_explicit_channel_policy_can_still_mark_a_channel_private(brain):
    """The operator override is kept -- it just is no longer the only signal."""
    brain.store.upsert_simple_harness_record(
        "channel_policies",
        {
            "id": "chpolicy_test",
            "organization_id": "org_acme",
            "source": "slack",
            "channel_id": "CPOLICY",
            "enabled": True,
            "observe": True,
            "proactive": True,
            "free_response": False,
            "is_private": True,
        },
    )
    event = brain.slack_gateway.normalize(_event("CPOLICY", "channel"))
    assert event.scope.visibility == "private_channel"


# ---------------------------------------------------------------------------
# The disclosure itself
# ---------------------------------------------------------------------------


def test_a_non_member_cannot_read_a_private_channels_evidence_from_a_dm(brain):
    """The exact reported failure: someone outside the private channel asks
    the bot in a DM and must not receive that channel's content.

    A DM destination is the widest read surface in the system -- it unlocks
    the acting principal's whole membership set -- so it is the case that
    matters most.
    """
    insider = brain.slack_gateway.normalize(_event("CSECRET", "group", user="UAJAY"))
    scope_id = insider.scope.scope_id

    brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="evt_secret_1",
        content="We are giving a 10% hike this year; market conditions are not good.",
        scope_id=scope_id,
        author_entity_id=insider.actor.id,
        channel_id="CSECRET",
        is_private_channel=True,
    )

    outsider = "person_slack_tacme_uoutsider"
    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id=outsider,
        destination=DestinationEnvelope(
            type="dm", id="DOUT", is_private=True
        ).model_dump(mode="json"),
        purpose="reactive_agent",
    )
    assert scope_id not in decision.allowed_scope_ids

    results = brain.retrieval.search(
        organization_id="org_acme",
        query="hike raise salary this year",
        acting_principal_id=outsider,
        destination={"type": "dm", "id": "DOUT", "is_private": True},
        purpose="reactive_agent",
    )
    leaked = [
        item for item in results["results"]
        if "10%" in (item["memory"].get("content") or "")
        or "hike" in (item["memory"].get("content") or "").lower()
    ]
    assert leaked == [], f"private channel content leaked to a non-member: {leaked}"


def test_a_member_of_the_private_channel_still_reads_it_from_their_dm(brain):
    """The fix must not lock the room's own participants out."""
    insider_event = brain.slack_gateway.normalize(
        _event("CTEAM", "group", user="UAJAY")
    )
    insider = insider_event.actor.id
    scope_id = insider_event.scope.scope_id

    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id=insider,
        destination=DestinationEnvelope(
            type="dm", id="DAJAY", is_private=True
        ).model_dump(mode="json"),
        purpose="reactive_agent",
    )
    assert scope_id in decision.allowed_scope_ids


# ---------------------------------------------------------------------------
# Repairing a deployment that already got it wrong
# ---------------------------------------------------------------------------


def test_a_mislabelled_public_scope_is_narrowed_on_the_next_message(brain):
    """Existing deployments already have private channels stored as public
    scopes. _ensure_scope() only ever created a scope, never corrected one,
    so those rooms would leak forever. They now self-heal."""
    scope_id = "scope_slack_TACME_CBROKEN"
    brain.store.upsert_scope(
        {
            "id": scope_id,
            "organization_id": "org_acme",
            "kind": "organization_public",
            "label": "Slack CBROKEN",
            "channel_id": "CBROKEN",
            "principal_ids": [],
            "is_public": True,
        }
    )
    brain.store.add_membership("org_acme", "person_earlier_speaker", scope_id)

    brain.slack_gateway.normalize(_event("CBROKEN", "group", user="UAJAY"))

    repaired = brain.store.get_scope(scope_id)
    assert repaired["is_public"] is False
    assert repaired["kind"] == "private_channel"
    # Everyone who already spoke there keeps access.
    assert "person_earlier_speaker" in repaired["principal_ids"]


def test_a_public_scope_is_never_widened_back(brain):
    """Narrowing is one-directional. A private scope must not be flipped to
    public by a later event, or everything already stored in it is disclosed."""
    private_event = brain.slack_gateway.normalize(_event("CFLIP", "group"))
    scope_id = private_event.scope.scope_id
    assert brain.store.get_scope(scope_id)["is_public"] is False

    # Same channel id arriving as a public "channel" type must not widen it.
    brain.slack_gateway.normalize(_event("CFLIP", "channel", ts="2.0"))

    assert brain.store.get_scope(scope_id)["is_public"] is False


# ---------------------------------------------------------------------------
# app_mention carries no channel_type -- the same hole, one step over
# ---------------------------------------------------------------------------


def _mention_event(channel: str, *, ts: str = "9.0") -> dict:
    """A real app_mention payload. Note the absence of channel_type: Slack
    only sends it on `message` events, and _ignore_event() accepts both."""
    return {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": f"Ev-mention-{channel}-{ts}",
        "event": {
            "type": "app_mention",
            "user": "UAJAY",
            "channel": channel,
            "text": "<@UBOT> what is the hike this year",
            "ts": ts,
            "event_ts": ts,
        },
    }


def test_an_unknown_channel_type_never_implies_public(brain):
    """Failing open on a missing signal is what caused the original leak."""
    event = brain.slack_gateway.normalize(_mention_event("CUNKNOWN"))

    assert event.scope.visibility == "private_channel"
    assert brain.store.get_scope(event.scope.scope_id)["is_public"] is False


def test_an_unknown_channel_type_inherits_a_known_public_scope(brain):
    """A channel already proven public stays usable -- the fail-closed default
    only applies when nothing is known about the room."""
    public = brain.slack_gateway.normalize(_event("CKNOWNPUB", "channel"))
    assert brain.store.get_scope(public.scope.scope_id)["is_public"] is True

    mention = brain.slack_gateway.normalize(_mention_event("CKNOWNPUB"))
    assert mention.scope.visibility == "organization_public"


def test_an_authoritative_public_signal_can_widen_a_guessed_private_scope(brain):
    """A public channel Docket only ever sees through mentions would otherwise
    be stuck private forever, since a guess may never widen."""
    guessed = brain.slack_gateway.normalize(_mention_event("CLATERPUB"))
    scope_id = guessed.scope.scope_id
    assert brain.store.get_scope(scope_id)["is_public"] is False

    brain.slack_gateway.normalize(_event("CLATERPUB", "channel", ts="10.0"))

    assert brain.store.get_scope(scope_id)["is_public"] is True


def test_a_guess_can_never_widen_a_private_scope(brain):
    """The dangerous direction stays shut: an app_mention with no channel_type
    must not turn a known private room public."""
    private = brain.slack_gateway.normalize(_event("CSTAYPRIV", "group"))
    scope_id = private.scope.scope_id

    brain.slack_gateway.normalize(_mention_event("CSTAYPRIV", ts="11.0"))

    assert brain.store.get_scope(scope_id)["is_public"] is False


def test_a_confirmed_private_scope_is_never_reopened_by_slack(brain):
    """Slack supports converting a private channel to public. Docket does not
    follow it: people who spoke while the room was private had a private
    expectation, and re-disclosing that backlog is not an automatic decision."""
    private = brain.slack_gateway.normalize(_event("CCONVERT", "group"))
    scope_id = private.scope.scope_id
    assert brain.store.get_scope(scope_id)["privacy_confirmed"] is True

    brain.slack_gateway.normalize(_event("CCONVERT", "channel", ts="12.0"))

    scope = brain.store.get_scope(scope_id)
    assert scope["is_public"] is False, "a confirmed private room was reopened"
    assert scope["kind"] == "private_channel"


def test_a_guessed_private_scope_is_not_marked_confirmed(brain):
    """The marker is what separates 'Slack said private' from 'we did not
    know, so we assumed private'."""
    guessed = brain.slack_gateway.normalize(_mention_event("CGUESS"))
    scope = brain.store.get_scope(guessed.scope.scope_id)

    assert scope["is_public"] is False
    assert bool(scope.get("privacy_confirmed")) is False


def test_an_operator_policy_can_declare_a_channel_public_without_slack(brain):
    """A deployment with no bot token cannot call conversations.info, so a
    stored channel policy is how an operator states a room's privacy. It is
    authoritative in both directions -- unlike the default policy, which only
    ever says "not a DM"."""
    brain.store.upsert_simple_harness_record(
        "channel_policies",
        {
            "id": "chpolicy_public",
            "organization_id": "org_acme",
            "source": "slack",
            "channel_id": "CDECLARED",
            "enabled": True,
            "observe": True,
            "proactive": True,
            "free_response": False,
            "is_private": False,
        },
    )
    event = brain.slack_gateway.normalize(_mention_event("CDECLARED"))

    assert event.scope.visibility == "organization_public"
    assert brain.store.get_scope(event.scope.scope_id)["is_public"] is True
