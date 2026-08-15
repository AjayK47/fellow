"""A channel is one room, so it is one Session.

Channels used to fork a Session per addressed message
(`{context_key}:event:{message_identity}`), which tied context to the sender
instead of the conversation. Two people raising the same issue got two
isolated Task trees, an addressed "stop" landed in a brand-new empty Session
and cancelled nothing, and mentioning Toda gave LESS continuity than not
mentioning it (ambient messages attach to the channel-wide
conversation_state.active_session_id; addressed ones never consulted it).

These tests pin the room-scoped model and the one place where the acting
principal still genuinely matters: a DM destination, which unlocks the
actor's whole membership set rather than a single room's scope.
"""

from __future__ import annotations

from toda.domain.models import new_id, utcnow
from toda.harness.models import (
    AgentRunState,
    DestinationEnvelope,
    InvocationEnvelope,
)

CHANNEL = {"type": "slack_channel", "id": "CENG", "is_private": False}
GROUP_DM = {"type": "dm", "id": "GTEAM", "is_private": True}


def _slack_event(
    *, channel: str, channel_type: str, user: str, text: str, ts: str,
    thread_ts: str | None = None,
) -> dict:
    return {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": f"Ev-{ts}",
        "event": {
            "type": "message",
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "text": text,
            "ts": ts,
            "client_msg_id": f"msg-{ts}",
            **({"thread_ts": thread_ts} if thread_ts else {}),
        },
    }


def _root_task(brain, *, session_id: str, principal_id: str, destination: dict) -> dict:
    now = utcnow().isoformat()
    task_id = new_id("task")
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id=principal_id,
        application_id="slack_toda",
        conversation_id="slack:TACME:CENG",
        destination=DestinationEnvelope(**destination),
        purpose="reactive_agent",
        permission_decision_id="permdecision_test",
        allowed_scope_ids=[],
    ).model_dump(mode="json")
    task = {
        "id": task_id,
        "organization_id": "org_acme",
        "session_id": session_id,
        "parent_task_id": None,
        "root_task_id": task_id,
        "state": AgentRunState.RUNNING.value,
        "worker_type": "session_manager",
        "trigger_kind": "reactive",
        "trigger_event_id": None,
        "prompt": "check the export pipeline",
        "goal": "check the export pipeline",
        "envelope": envelope,
        "trace_id": "trace_test",
        "max_iterations": 10,
        "max_tool_calls": 16,
        "iteration_count": 0,
        "tool_call_count": 0,
        "final_answer": None,
        "result": None,
        "error": None,
        "events": [],
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    brain.store.create_task(task)
    brain.store.claim_task(task_id, owner="exec_seed", lease_seconds=1800)
    return task


def _signal_kinds(brain, task_id: str) -> list[str]:
    kinds = []
    while True:
        signal = brain.store.pop_task_signal(task_id)
        if not signal:
            return kinds
        kinds.append(signal["kind"])


# ---------------------------------------------------------------------------
# One Session per room
# ---------------------------------------------------------------------------


def test_two_addressed_channel_messages_resolve_to_one_session(brain):
    brain.slack_gateway.config.slack.bot_user_id = "UBOT"

    first = brain.slack_gateway.normalize(
        _slack_event(
            channel="CENG", channel_type="channel", user="UAJAY",
            text="<@UBOT> check the export pipeline", ts="1.0",
        )
    )
    second = brain.slack_gateway.normalize(
        _slack_event(
            channel="CENG", channel_type="channel", user="USANJAY",
            text="<@UBOT> same thing here", ts="2.0",
        )
    )

    assert first.is_direct_agent_request and second.is_direct_agent_request
    assert first.payload["conversation_key"] == "slack:TACME:CENG"
    assert second.payload["conversation_key"] == first.payload["conversation_key"]
    assert first.payload["conversation_key"] == first.payload["context_key"]
    # Distinct messages must still deduplicate independently.
    assert first.dedupe_key != second.dedupe_key


def test_threads_are_routing_metadata_inside_the_channel_session(brain):
    brain.slack_gateway.config.slack.bot_user_id = "UBOT"

    root = brain.slack_gateway.normalize(
        _slack_event(
            channel="CENG", channel_type="channel", user="UAJAY",
            text="<@UBOT> investigate", ts="10.0",
        )
    )
    first_thread = brain.slack_gateway.normalize(
        _slack_event(
            channel="CENG", channel_type="channel", user="UAJAY",
            text="<@UBOT> first follow-up", ts="11.0", thread_ts="10.0",
        )
    )
    second_thread = brain.slack_gateway.normalize(
        _slack_event(
            channel="CENG", channel_type="channel", user="USANJAY",
            text="<@UBOT> separate thread", ts="21.0", thread_ts="20.0",
        )
    )

    assert {
        root.payload["conversation_key"],
        first_thread.payload["conversation_key"],
        second_thread.payload["conversation_key"],
    } == {"slack:TACME:CENG"}
    assert root.payload["thread_ts"] is None
    assert first_thread.payload["thread_ts"] == "10.0"
    assert second_thread.payload["thread_ts"] == "20.0"

    routed = brain.event_router.route(first_thread.model_dump(mode="json"))
    assert routed["route"] == "session_wake"
    session = brain.store.find_session_by_conversation(
        "org_acme", "slack:TACME:CENG"
    )
    messages = brain.store.list_session_messages(session["id"])
    assert messages[-1]["metadata"]["thread_ts"] == "10.0"


def test_a_second_speaker_wakes_the_same_session_not_a_new_one(brain):
    first = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        may_join=True,
    )
    second = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="same thing here",
        principal_id="person_sanjay",
        may_join=True,
    )

    assert second["session_id"] == first["session_id"]
    assert second["revision"] > first["revision"]
    # Both messages are in one transcript, so the Manager can see that the
    # second person is talking about work already running.
    contents = [
        message["content"]
        for message in brain.sessions.messages(first["session_id"])
    ]
    assert "check the export pipeline" in contents
    assert "same thing here" in contents


# ---------------------------------------------------------------------------
# Steering now reaches a channel's work
# ---------------------------------------------------------------------------


def test_stop_addressed_in_a_channel_cancels_the_running_root(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        may_join=True,
    )
    root = _root_task(
        brain,
        session_id=wake["session_id"],
        principal_id="person_ajay",
        destination=CHANNEL,
    )

    stop = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="stop",
        principal_id="person_ajay",
        may_join=True,
    )

    assert stop["steer_signal"] == "abort"
    assert brain.store.get_task(root["id"])["state"] == AgentRunState.CANCELLED.value


def test_a_second_speaker_in_a_channel_steers_instead_of_pausing(brain):
    """The destination, not the actor, bounds disclosure in a shared room --
    so a second speaker must not bounce the Task through WAITING_FOR_HUMAN."""
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        may_join=True,
    )
    root = _root_task(
        brain,
        session_id=wake["session_id"],
        principal_id="person_ajay",
        destination=CHANNEL,
    )

    followup = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="same thing here, it started around 10am",
        principal_id="person_sanjay",
        may_join=True,
    )

    assert followup["steer_signal"] == "steer"
    assert _signal_kinds(brain, root["id"]) == ["steer"]
    assert brain.store.get_task(root["id"])["state"] == AgentRunState.RUNNING.value


def test_a_second_speaker_in_a_dm_still_forces_reauthorization(brain):
    """A dm/person_private/internal_agent_session destination unlocks the
    acting principal's whole membership set (AuthorizationService.
    _destination_allows_private), so in a group DM a different speaker really
    can change what is disclosable. That case must still stop."""
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:GTEAM",
        destination=GROUP_DM,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        may_join=True,
    )
    root = _root_task(
        brain,
        session_id=wake["session_id"],
        principal_id="person_ajay",
        destination=GROUP_DM,
    )

    followup = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:GTEAM",
        destination=GROUP_DM,
        role="user",
        content="what did it say?",
        principal_id="person_sanjay",
        may_join=True,
    )

    assert followup["steer_signal"] == "reauthorize"
    assert _signal_kinds(brain, root["id"]) == ["reauthorize"]


def test_the_same_speaker_never_triggers_reauthorization(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:GTEAM",
        destination=GROUP_DM,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        may_join=True,
    )
    root = _root_task(
        brain,
        session_id=wake["session_id"],
        principal_id="person_ajay",
        destination=GROUP_DM,
    )

    followup = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:GTEAM",
        destination=GROUP_DM,
        role="user",
        content="also check the retry queue",
        principal_id="person_ajay",
        may_join=True,
    )

    assert followup["steer_signal"] == "steer"
    assert _signal_kinds(brain, root["id"]) == ["steer"]


# ---------------------------------------------------------------------------
# A shared transcript needs speakers
# ---------------------------------------------------------------------------


def test_user_turns_carry_their_speaker_into_the_prompt(brain):
    """One room, one transcript, several people -- an unattributed history
    makes a second person's "same thing here" read as the first person."""
    from toda.harness.prompts import build_agent_messages

    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="check the export pipeline",
        principal_id="person_ajay",
        message_metadata={"author_name": "Ajay"},
        may_join=True,
    )
    brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
        role="user",
        content="same thing here",
        principal_id="person_sanjay",
        message_metadata={"author_name": "Sanjay"},
        may_join=True,
    )
    root = _root_task(
        brain,
        session_id=wake["session_id"],
        principal_id="person_ajay",
        destination=CHANNEL,
    )

    messages = build_agent_messages(
        run=brain.store.get_task(root["id"]),
        context={},
        history=brain.sessions.messages(wake["session_id"]),
    )
    user_turns = [m["content"] for m in messages if m["role"] == "user"]
    assert "Ajay: check the export pipeline" in user_turns
    assert "Sanjay: same thing here" in user_turns

    # The stored message is untouched, so a Task's goal never inherits a label.
    stored = [
        m["content"] for m in brain.sessions.messages(wake["session_id"])
    ]
    assert "check the export pipeline" in stored
    assert "Ajay: check the export pipeline" not in stored


def test_an_unattributed_message_renders_exactly_as_before(brain):
    from toda.harness.prompts import build_agent_messages

    session = brain.sessions.resolve(
        organization_id="org_acme",
        conversation_key="slack:TACME:CENG",
        destination=CHANNEL,
    )
    brain.sessions.append_message(
        organization_id="org_acme",
        session_id=session["id"],
        role="user",
        content="legacy message with no author",
    )
    root = _root_task(
        brain,
        session_id=session["id"],
        principal_id="person_ajay",
        destination=CHANNEL,
    )

    messages = build_agent_messages(
        run=brain.store.get_task(root["id"]),
        context={},
        history=brain.sessions.messages(session["id"]),
    )
    assert "legacy message with no author" in [
        m["content"] for m in messages if m["role"] == "user"
    ]
