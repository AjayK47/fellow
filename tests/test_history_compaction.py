"""H-05: structured, incremental conversation compaction — modeled on
Hermes's context_compressor.py (protect the tail, summarize only the
newly-aged-out middle, redact secrets from the persisted summary, frame it
as reference-only with latest-message-wins semantics)."""

from __future__ import annotations

import json

from toda.domain.models import new_id, utcnow
from toda.harness.history_compaction import (
    COMPACTION_SCHEMA,
    SUMMARY_PREFIX,
    HistoryCompactor,
)
from toda.harness.model_client import DeterministicHarnessModel, ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse
from toda.harness.prompts import build_agent_messages


def _messages(n: int, *, role: str = "user", prefix: str = "msg") -> list[dict]:
    return [{"role": role, "content": f"{prefix} {i}"} for i in range(n)]


def test_compactor_is_not_live_for_the_deterministic_model():
    compactor = HistoryCompactor(model=DeterministicHarnessModel())
    assert compactor.live is False


def test_compactor_is_live_for_a_configured_model():
    compactor = HistoryCompactor(model=ScriptedModelClient([ModelResponse(content="{}")]))
    assert compactor.live is True


def test_no_new_messages_returns_previous_summary_unchanged():
    compactor = HistoryCompactor(model=DeterministicHarnessModel())
    result = compactor.compact(previous_summary="## Decisions\n- shipped v1", new_messages=[])
    assert result == "## Decisions\n- shipped v1"


def test_non_live_fallback_merges_previous_summary_with_new_messages():
    compactor = HistoryCompactor(model=DeterministicHarnessModel())
    result = compactor.compact(
        previous_summary="## Decisions\n- shipped v1",
        new_messages=_messages(3, prefix="follow-up"),
    )
    assert "shipped v1" in result
    assert "follow-up 0" in result
    assert "follow-up 2" in result


def test_non_live_fallback_redacts_secrets():
    compactor = HistoryCompactor(model=DeterministicHarnessModel())
    result = compactor.compact(
        previous_summary="",
        new_messages=[{"role": "user", "content": "key: AKIAABCDEFGHIJKLMNOP"}],
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "[REDACTED:aws_access_key]" in result


def test_live_compactor_produces_a_structured_summary():
    payload = {
        "decisions": ["adopted SQLite for v1"],
        "open_commitments": ["ship the migration by Friday"],
        "unresolved_questions": ["who owns the rollout"],
        "entities": ["person_ajay"],
        "source_ids": ["src_abc123"],
    }
    model = ScriptedModelClient([ModelResponse(content=json.dumps(payload))])
    compactor = HistoryCompactor(model=model)
    result = compactor.compact(previous_summary="", new_messages=_messages(2))
    assert "adopted SQLite for v1" in result
    assert "ship the migration by Friday" in result
    assert "who owns the rollout" in result
    assert "person_ajay" in result
    assert "src_abc123" in result


def test_live_compactor_redacts_secrets_from_the_rendered_summary():
    payload = {
        "decisions": ["rotate the key AKIAABCDEFGHIJKLMNOP immediately"],
        "open_commitments": [],
        "unresolved_questions": [],
        "entities": [],
        "source_ids": [],
    }
    model = ScriptedModelClient([ModelResponse(content=json.dumps(payload))])
    compactor = HistoryCompactor(model=model)
    result = compactor.compact(previous_summary="", new_messages=_messages(1))
    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "[REDACTED:aws_access_key]" in result


def test_live_compactor_falls_back_on_malformed_model_output():
    model = ScriptedModelClient([ModelResponse(content="not valid json")])
    compactor = HistoryCompactor(model=model)
    result = compactor.compact(previous_summary="", new_messages=_messages(2, prefix="turn"))
    assert "turn 0" in result


def test_compaction_schema_requires_all_five_sections():
    assert set(COMPACTION_SCHEMA["required"]) == {
        "decisions",
        "open_commitments",
        "unresolved_questions",
        "entities",
        "source_ids",
    }


def test_summary_prefix_appears_only_when_a_summary_is_present():
    envelope_kwargs = dict(
        principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "test", "is_private": True},
        purpose="reactive_agent",
        allowed_scope_ids=["scope_org_public"],
    )
    base_run = {
        "id": "arun_x",
        "goal": "check things",
        "trigger_kind": "reactive",
        "max_iterations": 12,
        "max_tool_calls": 24,
        "envelope": envelope_kwargs,
    }
    no_summary = build_agent_messages(run=base_run, context={}, history=[], conversation_summary="")
    assert not any(SUMMARY_PREFIX in m["content"] for m in no_summary)

    with_summary = build_agent_messages(
        run=base_run, context={}, history=[], conversation_summary="## Decisions\n- shipped v1"
    )
    matches = [m for m in with_summary if SUMMARY_PREFIX in m["content"]]
    assert matches
    assert "shipped v1" in matches[0]["content"]
    assert "latest authorized user message wins" in matches[0]["content"]


def test_slack_session_context_and_company_memory_are_separate_prompt_blocks():
    run = {
        "id": "arun_slack_context",
        "goal": "answer the follow-up",
        "trigger_kind": "reactive",
        "max_iterations": 12,
        "max_tool_calls": 24,
        "envelope": {
            "principal_id": "person_ajay",
            "destination": {
                "type": "slack_channel",
                "id": "CPUBLIC",
                "is_private": False,
            },
            "purpose": "reactive_agent",
            "allowed_scope_ids": ["scope_org_public"],
        },
    }
    messages = build_agent_messages(
        run=run,
        context={"current_facts": [{"content": "company memory"}]},
        history=[{"role": "user", "content": "current request"}],
        slack_conversation=[
            {"role": "user", "author": "Ajay", "text": "earlier message"},
            {"role": "assistant", "author": "toda", "text": "earlier reply"},
        ],
    )
    slack_block = next(
        message["content"]
        for message in messages
        if message["content"].startswith("<slack_conversation>")
    )
    memory_block = next(
        message["content"]
        for message in messages
        if message["content"].startswith("<company_context>")
    )
    assert "earlier reply" in slack_block
    assert "company memory" not in slack_block
    assert "company memory" in memory_block
    assert "earlier reply" not in memory_block


def _envelope(brain, *, conversation: str) -> InvocationEnvelope:
    destination = {"type": "internal_agent_session", "id": "compaction-test", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


class _SpyCompactor:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list[list[dict]] = []

    def compact(self, *, previous_summary, new_messages):
        self.calls.append(list(new_messages))
        return self.inner.compact(previous_summary=previous_summary, new_messages=new_messages)


def test_compaction_is_incremental_across_two_runs_on_the_same_conversation(brain):
    """The second compaction pass on a conversation must only summarize
    messages added since the first pass, not re-derive from everything."""
    spy = _SpyCompactor(HistoryCompactor(model=DeterministicHarnessModel()))
    brain.company_agent.history_compactor = spy
    # Compaction now triggers relative to the configured model's actual
    # context window (H-05 follow-up, ported from Hermes), not a fixed
    # message count — force a small window here so 55 short seed messages
    # actually cross the threshold, the same way a real, larger history
    # would against a real model's window.
    brain.company_agent.model.context_length = lambda: 200

    conversation = "compaction-test:thread"
    envelope = _envelope(brain, conversation=conversation)
    run_a = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="Investigate the export failure",
        trigger_kind="reactive",
        initial_message="Investigate the export failure",
    )
    session_id = run_a["session_id"]
    def append_message(task_id: str, role: str, content: str) -> None:
        brain.store.append_session_message(
            {
                "id": new_id("smsg"),
                "organization_id": "org_acme",
                "session_id": session_id,
                "task_id": task_id,
                "role": role,
                "content": content,
                "created_at": utcnow().isoformat(),
            }
        )
    # Seed 55 plain messages directly so the token threshold is already
    # crossed before the first run() call, without needing dozens of real
    # tool-call iterations.
    for i in range(55):
        append_message(
            run_a["id"],
            "user" if i % 2 == 0 else "assistant",
            f"seed message {i}",
        )
    outcome_a = brain.company_agent.execute_task(run_a["id"])
    assert outcome_a.state.value == "completed"
    assert len(spy.calls) == 1
    first_call_size = len(spy.calls[0])
    assert first_call_size > 0

    session_after_a = brain.store.get_session(session_id)
    assert session_after_a["summary"]
    summarized_through = session_after_a["summary_through_message_id"]

    # A second run on the SAME conversation reuses the same session. Add a
    # few more messages, then run again — the compactor must only see the
    # new ones, not the 55 already folded into the summary.
    run_b = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="Any update on the export failure?",
        trigger_kind="reactive",
        initial_message="Any update on the export failure?",
    )
    assert run_b["session_id"] == session_id
    for i in range(5):
        append_message(run_b["id"], "user", f"extra message {i}")
    outcome_b = brain.company_agent.execute_task(run_b["id"])
    assert outcome_b.state.value == "completed"
    assert len(spy.calls) == 2
    second_call = spy.calls[1]
    assert len(second_call) < first_call_size
    assert all(m["id"] != summarized_through for m in second_call)
    # The core incremental guarantee: nothing folded into the summary on the
    # first pass is ever handed to the compactor again on the second.
    first_call_ids = {m["id"] for m in spy.calls[0]}
    second_call_ids = {m["id"] for m in second_call}
    assert not (first_call_ids & second_call_ids)
