"""What an ordinary signed-in member may see and do.

Every other HTTP test signs in as an owner, which bypasses both the Session
participation boundary and the admin-mutation check -- so without this file
neither is exercised at all, which is exactly how they came to be missing.
"""

from __future__ import annotations

import pytest

from tests.conftest import sign_in


def _invoke_as(client, principal_id: str, prompt: str, conversation_id: str) -> dict:
    sign_in(client, principal_id=principal_id, role="member")
    response = client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "conversation_id": conversation_id,
            "destination": {
                "type": "internal_agent_session",
                "id": conversation_id,
                "is_private": True,
            },
            "prompt": prompt,
            "run_now": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- identity ----


def test_invoking_a_task_cannot_name_another_principal(api_client):
    """`principal_id` was a body field passed straight into sessions.wake(),
    so it became the Session's acting principal and therefore the identity
    its authorization envelope was resolved against on every wake."""
    invoked = _invoke_as(api_client, "person_ravi", "Who am I?", "invoke-identity")
    session = invoked["session"]
    assert session["last_actor_principal_id"] == "person_ravi"

    # An extra field is inert rather than authoritative.
    response = api_client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "conversation_id": "invoke-identity-2",
            "destination": {
                "type": "internal_agent_session",
                "id": "invoke-identity-2",
                "is_private": True,
            },
            "prompt": "Act as someone else.",
        },
    )
    assert response.status_code == 200
    assert response.json()["session"]["last_actor_principal_id"] == "person_ravi"


def test_a_member_cannot_decide_an_approval_as_someone_else(api_client):
    """`decided_by` was a body field checked against allowed_approver_ids but
    never against the session -- and GET /v1/approvals published that exact
    list to every member, so the check told the caller what to claim."""
    brain = api_client.app.state.brain
    approval = {
        "id": "approval_forge_test",
        "organization_id": "org_acme",
        "task_id": "task_forge_test",
        "tool_call_id": "call_forge_test",
        "status": "pending",
        "tool_name": "COMPOSIO_EXECUTE",
        "arguments": {},
        "preview": "Do something consequential",
        "requested_by": "person_ajay",
        "allowed_approver_ids": ["person_ajay"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    brain.store.upsert_approval(approval)

    sign_in(api_client, principal_id="person_ravi", role="member")
    denied = api_client.post(
        "/v1/approvals/approval_forge_test:decide",
        json={"approved": True, "decided_by": "person_ajay"},
    )
    assert denied.status_code == 403
    assert (
        brain.store.get_harness_record("approvals", "approval_forge_test")["status"]
        == "pending"
    )
    assert any(
        event["event_type"] == "approval.decision_rejected"
        for event in brain.store.list_audit("org_acme", "approval.decision_rejected")
    )


def test_a_member_only_lists_approvals_they_may_decide(api_client):
    brain = api_client.app.state.brain
    brain.store.upsert_approval(
        {
            "id": "approval_not_mine",
            "organization_id": "org_acme",
            "task_id": "task_x",
            "tool_call_id": "call_x",
            "status": "pending",
            "tool_name": "COMPOSIO_EXECUTE",
            "arguments": {},
            "preview": "Someone else's decision",
            "requested_by": "person_ajay",
            "allowed_approver_ids": ["person_ajay"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    sign_in(api_client, principal_id="person_ravi", role="member")
    listed = api_client.get("/v1/approvals", params={"organization_id": "org_acme"})
    assert listed.status_code == 200
    assert "approval_not_mine" not in {
        item["id"] for item in listed.json()["approvals"]
    }

    sign_in(api_client, principal_id="person_ajay", role="member")
    mine = api_client.get("/v1/approvals", params={"organization_id": "org_acme"})
    assert "approval_not_mine" in {item["id"] for item in mine.json()["approvals"]}


# ---- session participation ----


def test_a_member_cannot_read_a_conversation_they_were_not_in(api_client):
    invoked = _invoke_as(
        api_client, "person_ajay", "Something private.", "participation-test"
    )
    session_id = invoked["session"]["id"]
    task_id = invoked["tasks"][-1]["id"]

    sign_in(api_client, principal_id="person_ravi", role="member")
    assert api_client.get(f"/v1/sessions/{session_id}").status_code == 404
    assert api_client.get(
        f"/v1/tasks/{task_id}", params={"organization_id": "org_acme"}
    ).status_code == 404
    assert api_client.get(
        f"/v1/tasks/{task_id}/attempts", params={"organization_id": "org_acme"}
    ).status_code == 404
    assert api_client.get(
        f"/v1/sessions/{session_id}/tasks", params={"organization_id": "org_acme"}
    ).status_code == 404

    listed_sessions = api_client.get(
        "/v1/sessions", params={"organization_id": "org_acme"}
    ).json()["sessions"]
    assert session_id not in {item["session"]["id"] for item in listed_sessions}
    listed_tasks = api_client.get(
        "/v1/tasks", params={"organization_id": "org_acme"}
    ).json()["tasks"]
    assert task_id not in {item["id"] for item in listed_tasks}


def test_a_member_can_read_their_own_conversation(api_client):
    invoked = _invoke_as(api_client, "person_ravi", "My own request.", "own-session")
    session_id = invoked["session"]["id"]
    task_id = invoked["tasks"][-1]["id"]

    assert api_client.get(f"/v1/sessions/{session_id}").status_code == 200
    detail = api_client.get(
        f"/v1/tasks/{task_id}", params={"organization_id": "org_acme"}
    )
    assert detail.status_code == 200
    assert detail.json()["task"]["id"] == task_id


def test_an_admin_reading_someone_elses_conversation_is_audited(api_client):
    invoked = _invoke_as(api_client, "person_ravi", "Ravi's request.", "admin-read")
    session_id = invoked["session"]["id"]

    brain = api_client.app.state.brain
    before = len(brain.store.list_audit("org_acme", "session.admin_read"))
    sign_in(api_client, principal_id="person_ajay", role="owner")
    assert api_client.get(f"/v1/sessions/{session_id}").status_code == 200
    after = brain.store.list_audit("org_acme", "session.admin_read")
    assert len(after) > before
    assert any(
        event["payload"]["session_id"] == session_id
        and event["payload"]["resource"] == "session_detail"
        for event in after
    )


# ---- artifacts ----


def test_artifact_download_is_authorized_like_its_metadata(api_client):
    created = api_client.post(
        "/v1/artifacts",
        json={
            "organization_id": "org_acme",
            "artifact_type": "report",
            "title": "Leadership only",
            "content": {"summary": "Confidential"},
            "scope_id": "scope_channel_leadership_private",
            "created_by": "person_ajay",
        },
    )
    assert created.status_code == 200
    artifact_id = created.json()["id"]

    sign_in(api_client, principal_id="person_ravi", role="member")
    assert api_client.get(f"/v1/artifacts/{artifact_id}").status_code == 404
    # The bytes route used to skip the check entirely, and task detail hands
    # out artifact IDs, so knowing the ID was enough.
    assert api_client.get(f"/v1/artifacts/{artifact_id}/download").status_code == 404

    sign_in(api_client, principal_id="person_ajay", role="member")
    assert api_client.get(f"/v1/artifacts/{artifact_id}").status_code == 200
    assert api_client.get(f"/v1/artifacts/{artifact_id}/download").status_code == 200


# ---- role-based mutation ----


def test_mutations_require_an_admin_unless_the_route_says_otherwise(api_client):
    """Admin-required used to be an allowlist of path prefixes, so every
    route not listed was member-writable by omission rather than decision."""
    sign_in(api_client, principal_id="person_ravi", role="member")

    # Not declared member-writable -> refused, even though these paths were
    # never in the old _ADMIN_WRITE_PREFIXES tuple.
    assert api_client.post(
        "/v1/harness/worker:drain", json={"organization_id": "org_acme"}
    ).status_code == 403
    assert api_client.post(
        "/v1/automations",
        json={
            "organization_id": "org_acme",
            "name": "member automation",
            "goal": "run something",
            "conversation_id": "c",
            "destination": {"type": "internal_agent_session", "id": "c"},
        },
    ).status_code == 403

    # Declared member-writable -> allowed.
    assert api_client.post(
        "/v1/search",
        json={"organization_id": "org_acme", "query": "launch", "limit": 5},
    ).status_code == 200


def test_an_admin_may_still_perform_admin_mutations(api_client):
    sign_in(api_client, principal_id="person_ajay", role="owner")
    assert api_client.post(
        "/v1/harness/worker:drain", json={"organization_id": "org_acme"}
    ).status_code == 200


# ---- self-granted participation ----


def test_api_invocation_cannot_join_a_platform_conversation(api_client):
    """Participation decides who may read a transcript, and waking a
    conversation records you as a participant -- so the write path was a
    grant of the read permission. `conversation_key` for Slack is
    `slack:{workspace_id}:{channel_id}`, constructible by anyone who knows a
    channel id, and naming it in an invoke used to add you to that private
    channel's Session, append your message to its transcript, and wake its
    Manager."""
    channel_key = "slack:T_ACME:C_LEADERSHIP"
    sign_in(api_client, principal_id="person_ajay", role="member")
    victim = api_client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "conversation_id": channel_key,
            "destination": {
                "type": "private_channel",
                "id": "C_LEADERSHIP",
                "is_private": True,
            },
            "prompt": "Confidential: we are acquiring Initech.",
            "run_now": True,
        },
    ).json()
    victim_session = victim["session"]["id"]

    sign_in(api_client, principal_id="person_ravi", role="member")
    assert api_client.get(f"/v1/sessions/{victim_session}").status_code == 404

    attack = api_client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "conversation_id": channel_key,
            "destination": {"type": "dm", "id": "anything", "is_private": True},
            "prompt": "hello",
        },
    )
    # Namespacing means the attacker lands in their own conversation
    # entirely, rather than being refused entry to someone else's.
    assert attack.status_code == 200
    assert attack.json()["session"]["id"] != victim_session
    assert attack.json()["session"]["participants"] == ["person_ravi"]

    # The victim's transcript is untouched and still unreadable.
    assert api_client.get(f"/v1/sessions/{victim_session}").status_code == 404
    sign_in(api_client, principal_id="person_ajay", role="member")
    messages = api_client.get(f"/v1/sessions/{victim_session}").json()["messages"]
    assert "hello" not in [m.get("content") for m in messages]


def test_waking_an_existing_conversation_requires_a_trustworthy_claim(brain):
    """The second, independent barrier: even inside one namespace, only a
    caller the platform vouched for may enter a conversation it is not
    already part of."""
    from toda.harness.sessions import SessionJoinDenied

    destination = {"type": "slack_channel", "id": "C_X", "is_private": False}
    brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T_ACME:C_X",
        destination=destination,
        role="user",
        content="first",
        principal_id="person_ajay",
        may_join=True,
    )

    # A caller with no platform-verified claim cannot enter it.
    with pytest.raises(SessionJoinDenied):
        brain.sessions.wake(
            organization_id="org_acme",
            conversation_key="slack:T_ACME:C_X",
            destination=destination,
            role="user",
            content="let me in",
            principal_id="person_ravi",
        )

    # The Slack gateway may, because Slack enforced channel access first.
    joined = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T_ACME:C_X",
        destination=destination,
        role="user",
        content="I actually posted here",
        principal_id="person_ravi",
        may_join=True,
    )
    session = brain.sessions.get(joined["session_id"])
    assert set(session["participants"]) == {"person_ajay", "person_ravi"}

    # And an existing participant needs no such vouching to come back.
    again = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T_ACME:C_X",
        destination=destination,
        role="user",
        content="following up",
        principal_id="person_ajay",
    )
    assert again["session_id"] == joined["session_id"]


def test_an_automation_cannot_target_someone_elses_conversation(brain):
    """An automation stores a model-supplied conversation_id and fires with
    its owner as a participant, so scheduling one against a conversation you
    are not in would grant you access to it on a timer."""
    brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T_ACME:C_PRIVATE",
        destination={"type": "private_channel", "id": "C_PRIVATE", "is_private": True},
        role="user",
        content="private planning",
        principal_id="person_ajay",
        may_join=True,
    )

    with pytest.raises(ValueError, match="not part of"):
        brain.automations.create(
            organization_id="org_acme",
            name="sneak",
            goal="read the room",
            principal_id="person_ravi",
            conversation_id="slack:T_ACME:C_PRIVATE",
            destination={"type": "dm", "id": "d", "is_private": True},
            interval_seconds=3600,
        )

    # The owner of that conversation may schedule against it.
    allowed = brain.automations.create(
        organization_id="org_acme",
        name="daily",
        goal="summarize",
        principal_id="person_ajay",
        conversation_id="slack:T_ACME:C_PRIVATE",
        destination={"type": "private_channel", "id": "C_PRIVATE", "is_private": True},
        interval_seconds=3600,
    )
    assert allowed["principal_id"] == "person_ajay"
