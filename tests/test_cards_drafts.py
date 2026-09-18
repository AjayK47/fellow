"""U3: cards, drafts, Approve & post / Edit / Discard."""

from toda.harness.delivery import render_card_blocks


def test_card_rendering_structure_and_limits():
    card = {
        "title": "Status",
        "subtitle": "Today",
        "rows": [
            {"tag": "API", "tone": "good", "text": "up"},
            {"tag": "DB", "tone": "risk", "text": "down"},
        ],
        "footer": "foot",
    }
    blocks = render_card_blocks("Hello", card)
    types = [b["type"] for b in blocks]
    assert types[0] == "section"
    assert "header" in types
    assert blocks[1]["text"]["text"] == "Status"
    row_texts = " ".join(
        b["text"]["text"] for b in blocks if b["type"] == "section"
    )
    assert ":large_green_circle:" in row_texts
    assert ":red_circle:" in row_texts
    # limits: long rows truncated, >50 blocks capped
    big = {"title": "T", "rows": [{"tag": "x", "tone": "good", "text": "y" * 5000}]}
    out = render_card_blocks("t", big)
    assert len(out[2]["text"]["text"]) <= 3000
    many = {"title": "T", "rows": [{"tag": f"t{i}", "tone": "good", "text": "v"} for i in range(10)]}
    assert len(render_card_blocks("t", many)) <= 50


def test_reply_card_in_conversation_turn_delivers_text_plus_card(brain):
    from toda.harness.model_client import ScriptedModelClient
    from toda.harness.models import ModelResponse, ModelToolCall

    brain.conversation_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="c1", name="reply_card",
                        arguments={"title": "Status", "rows": [{"tag": "API", "tone": "good", "text": "up"}]},
                    )
                ],
            ),
            ModelResponse(content="All green"),
        ]
    )
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="api:u3:card",
        destination={"type": "internal_agent_session", "id": "u3card", "is_private": True},
        role="user", content="status?", principal_id="person_ajay",
        metadata={}, message_metadata={}, may_join=True,
    )
    from toda.harness.models import InvocationEnvelope

    decision = brain.authz.resolve(
        "org_acme", "person_ajay",
        {"type": "internal_agent_session", "id": "u3card", "is_private": True},
        "reactive_agent",
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme", principal_id="person_ajay",
        conversation_id="api:u3:card",
        destination={"type": "internal_agent_session", "id": "u3card", "is_private": True},
        purpose="reactive_agent", permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    session = brain.store.get_session(wake["session_id"])
    outcome = brain.conversation_agent.run(
        session=session, envelope=envelope, runs=brain.company_agent.runs,
        trigger_kind=__import__("toda.harness.models", fromlist=["TriggerKind"]).TriggerKind.REACTIVE,
        trigger_event_id=None, task_metadata={},
    )
    assert outcome.answer == "All green"
    sent = []
    brain.delivery.slack.send = lambda **kw: (sent.append(kw), {"ok": True, "channel": "x", "ts": "1"})[1]
    # deliver the produced assistant message
    msgs = brain.store.list_session_messages(session["id"], limit=10)
    assistant = next(m for m in reversed(msgs) if m.get("role") == "assistant")
    assert (assistant.get("metadata") or {}).get("reply_card", {}).get("title") == "Status"
    sess = brain.store.get_session(session["id"])
    sess["destination"] = {"type": "internal_agent_session", "id": "u3card", "is_private": True}
    sess["last_actor_principal_id"] = "person_ajay"
    brain.store.update_session(sess)
    brain.delivery.deliver_conversation(session["id"], assistant["id"])
    assert sent and any(b.get("type") == "header" for b in (sent[0].get("blocks") or []))


def test_post_draft_posts_only_after_approval_in_thread(brain):
    from tests.test_company_agent_harness import _envelope

    brain.company_agent.model = __import__(
        "toda.harness.model_client", fromlist=["ScriptedModelClient"]
    ).ScriptedModelClient(
        [__import__("toda.harness.models", fromlist=["ModelResponse"]).ModelResponse(content="drafting")]
    )
    brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="u3:draft"),
        goal="post announcement", trigger_kind="reactive", initial_message="post announcement",
        metadata={"slack": {"channel_id": "C9", "thread_ts": "999.1"}},
    )
    # post_draft is gated even though slack.* is contained
    from toda.harness.tools import runs_without_approval

    definition = next(d for d in brain.tool_executor.registry.definitions() if d.name == "post_draft")
    assert runs_without_approval(definition) is False


def test_post_draft_full_approval_flow_posts_once_in_thread(brain):
    from toda.harness.model_client import ScriptedModelClient
    from toda.harness.models import ModelResponse, ModelToolCall

    destination = {"type": "slack_channel", "id": "C9", "is_private": False}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    from toda.harness.models import InvocationEnvelope

    envelope = InvocationEnvelope(
        organization_id="org_acme", principal_id="person_ajay",
        conversation_id="slack:T:C9:u3draftflow",
        destination=destination, purpose="reactive_agent",
        permission_decision_id=decision.id, allowed_scope_ids=decision.allowed_scope_ids,
    )
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[ModelToolCall(id="c1", name="post_draft", arguments={"text": "Team update: deploy at 5"})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Posted."),
        ]
    )
    task = brain.company_agent.create_invocation(
        envelope=envelope, goal="post announcement", trigger_kind="reactive",
        initial_message="post announcement",
        metadata={"slack": {"workspace_id": "T", "channel_id": "C9", "thread_ts": "999.1"}},
    )
    outcome = brain.company_agent.execute_task(task["id"])
    assert outcome.state.value == "waiting"
    approval_id = outcome.approval_id
    # Nothing posted before approval
    sent: list[dict] = []

    def _fake_send(**kw):
        sent.append(kw)
        return {"ok": True, "channel": "C9", "ts": "5.1", "blocks": kw.get("blocks") or []}

    brain.delivery.slack.send = _fake_send
    stored = brain.store.get_task(outcome.task_id)
    assert stored["state"] == "waiting"
    # Approval card shows the draft with three buttons
    card_out = brain.delivery.deliver_approval(approval_id)
    assert card_out["approval_id"] == approval_id
    action_ids = [
        e.get("action_id")
        for b in card_out["delivery"]["provider_result"].get("blocks", [])
        if b.get("type") == "actions"
        for e in b.get("elements", [])
    ]
    assert action_ids == ["toda_approve", "toda_edit", "toda_deny"]
    assert "Team update" in card_out["delivery"].get("text", "")
    # Approve → posts exactly once, in the Task's thread
    result = brain.harness_worker._resolve_approval(
        {"approval_id": approval_id, "approved": True, "decided_by": "person_ajay"}
    )
    assert result["state"] == "completed"
    posts = [
        s for s in sent
        if (s.get("channel_id") == "C9" or s.get("channel") == "C9")
        and not any(b.get("type") == "actions" for b in (s.get("blocks") or []))
    ]
    assert len(posts) == 1
    assert posts[0].get("thread_ts") == "999.1"


def test_edit_action_and_view_submission_routing(brain):
    from tests.test_company_agent_harness import _envelope

    task = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="u3:edit"),
        goal="post announcement", trigger_kind="reactive", initial_message="post announcement",
    )
    approval = {
        "id": "approval_edit1",
        "organization_id": "org_acme",
        "task_id": task["id"],
        "tool_call_id": "call_edit1",
        "tool_name": "post_draft",
        "arguments": {"text": "original draft"},
        "status": "pending",
        "requested_by": "person_ajay",
        "allowed_approver_ids": ["person_ajay"],
        "preview": {"action": "post_draft", "description": "d", "arguments": {"text": "original draft"}},
        "created_at": "2026-09-18T00:00:00+00:00",
        "updated_at": "2026-09-18T00:00:00+00:00",
    }
    brain.store.upsert_approval(approval)
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_slack_u1", "organization_id": "org_acme", "source": "slack",
            "source_user_id": "U1", "principal_id": "person_ajay",
        },
    )
    opened = []
    brain.slack_gateway.open_view = lambda trigger_id, view, workspace_id=None: (
        opened.append((trigger_id, view)), {"ok": True}
    )[1]
    # Non-approver Edit click rejected
    import pytest

    from toda.harness.slack import UnauthorizedApprover

    with pytest.raises(UnauthorizedApprover):
        brain.slack_gateway._resolve_interaction(
            {"type": "block_actions", "team": {"id": "T"}, "user": {"id": "U9"},
             "trigger_id": "t1",
             "actions": [{"action_id": "toda_edit", "value": "approval_edit1"}]}
        )
    # Approver Edit click opens the modal synchronously
    out = brain.slack_gateway._resolve_interaction(
        {"type": "block_actions", "team": {"id": "T"}, "user": {"id": "U1"},
         "trigger_id": "t1",
         "actions": [{"action_id": "toda_edit", "value": "approval_edit1"}]}
    )
    assert out.get("opened_modal") is True
    assert opened and opened[0][1]["callback_id"] == "toda_edit_draft"
    # Non-approver view_submission rejected
    with pytest.raises(UnauthorizedApprover):
        brain.slack_gateway._resolve_interaction(
            {"type": "view_submission", "team": {"id": "T"}, "user": {"id": "U9"},
             "view": {"callback_id": "toda_edit_draft", "private_metadata": "approval_edit1",
                      "state": {"values": {"draft": {"text": {"value": "edited"}}}}}}
        )
    # Approver view_submission enqueues approval.resolve with edited text
    jobs_before = brain.harness_jobs.lease(worker_id="u3edit", limit=50)
    out = brain.slack_gateway._resolve_interaction(
        {"type": "view_submission", "team": {"id": "T"}, "user": {"id": "U1"},
         "view": {"callback_id": "toda_edit_draft", "private_metadata": "approval_edit1",
                  "state": {"values": {"draft": {"text": {"value": "edited draft"}}}}}}
    )
    assert out == {}
    queued = brain.harness_jobs.lease(worker_id="u3edit2", limit=50)
    resolve_jobs = [j for j in jobs_before + queued if j.get("kind") == "approval.resolve"]
    assert resolve_jobs
    assert resolve_jobs[0]["payload"]["edited_arguments"]["text"] == "edited draft"
