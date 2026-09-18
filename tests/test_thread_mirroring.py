"""U2: threads mirror the asker."""

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse


def _task_with_thread(brain, thread_ts, *, final_answer="done"):
    from tests.test_company_agent_harness import _envelope

    brain.company_agent.model = ScriptedModelClient([ModelResponse(content=final_answer)])
    task = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="u2:test"),
        goal="task",
        trigger_kind="reactive",
        initial_message="task",
        metadata={"slack": {"channel_id": "C1", "thread_ts": thread_ts}},
    )
    outcome = brain.company_agent.execute_task(task["id"])
    assert outcome.state.value == "completed"
    return brain.store.get_task(task["id"])


def test_threaded_task_deliveries_carry_thread_ts(brain):
    sent = []
    brain.delivery.slack.send = lambda **kw: (sent.append(kw), {"ok": True, "channel": "C1", "ts": "1.1"})[1]
    task = _task_with_thread(brain, "111.1")
    out = brain.delivery.deliver_task(task["id"])
    assert out["delivered"] is True
    assert sent and sent[0].get("thread_ts") == "111.1"
    sent.clear()
    brain.delivery.deliver_acknowledgement(task["id"])
    # ack may skip without model; if sent, it must carry the thread
    if sent:
        assert sent[0].get("thread_ts") == "111.1"


def test_top_level_task_has_no_thread_ts(brain):
    sent = []
    brain.delivery.slack.send = lambda **kw: (sent.append(kw), {"ok": True, "channel": "C1", "ts": "1.1"})[1]
    task = _task_with_thread(brain, None)
    brain.delivery.deliver_task(task["id"])
    assert sent and sent[0].get("thread_ts") is None


def test_conversation_reply_uses_triggering_message_thread(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T:C1:u2conv",
        destination={"type": "slack_channel", "id": "C1", "is_private": False},
        role="user",
        content="threaded question",
        principal_id="person_ajay",
        metadata={"slack": {"channel_id": "C1"}},
        message_metadata={"thread_ts": "222.2"},
        may_join=True,
    )
    sid = wake["session_id"]
    brain.company_agent.runs.add_message(
        organization_id="org_acme", session_id=sid, task_id=None, role="user",
        content="threaded question", metadata={"thread_ts": "222.2"},
    )
    msg = brain.company_agent.runs.add_message(
        organization_id="org_acme", session_id=sid, task_id=None, role="assistant",
        content="answer here", metadata={},
    )
    # authorize destination for delivery
    sess = brain.store.get_session(sid)
    sess["destination"] = {"type": "slack_channel", "id": "C1", "is_private": False}
    sess["last_actor_principal_id"] = "person_ajay"
    brain.store.update_session(sess)
    sent = []
    brain.delivery.slack.send = lambda **kw: (sent.append(kw), {"ok": True, "channel": "C1", "ts": "2.2"})[1]
    brain.delivery.deliver_conversation(sid, msg["id"])
    assert sent and sent[0].get("thread_ts") == "222.2"
