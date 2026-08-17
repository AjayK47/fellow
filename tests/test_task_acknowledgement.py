"""A Task says one personalized line the moment it is created.

Before this, a request sat in silence for the whole first reasoning turn --
retrieval, a full main-model call, then tool calls -- which on a real
request is upwards of ten seconds with nothing to show it was heard.
"""

from __future__ import annotations

from typing import Any

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import InvocationEnvelope, ModelResponse


def _envelope(brain) -> InvocationEnvelope:
    destination = {"type": "slack_channel", "id": "C_ACK", "is_private": False}
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        application_id="slack_toda",
        conversation_id="slack:C_ACK:ack",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _task(brain, goal: str = "make an economic report of HSR effect on Japan"):
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="done")])
    return brain.company_agent.create_invocation(
        envelope=_envelope(brain), goal=goal,
        trigger_kind="reactive", initial_message=goal,
    )


def _capture(brain) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs), {"ok": True, "channel": "C_ACK", "ts": f"{len(sent)}.0"}
    )[1]
    return sent


def test_the_acknowledgement_names_the_actual_request(brain):
    """A canned "working on it" reads like an autoresponder and says
    nothing about whether Toda understood. The model writes this line."""
    task = _task(brain)
    sent = _capture(brain)
    brain.delivery.model = ScriptedModelClient(
        [ModelResponse(content="Let me pull the Japanese rail and GDP figures.")]
    )

    result = brain.delivery.deliver_acknowledgement(task["id"])

    assert result["delivered"] is True
    assert len(sent) == 1
    assert sent[0]["text"] == "Let me pull the Japanese rail and GDP figures."
    assert sent[0]["channel_id"] == "C_ACK"


def test_a_model_failure_never_breaks_the_task(brain):
    """The acknowledgement is a courtesy. One that breaks the work it
    introduces is worse than no acknowledgement at all."""
    task = _task(brain)
    sent = _capture(brain)

    class Broken:
        def generate(self, **_kwargs):
            raise RuntimeError("provider down")

    brain.delivery.model = Broken()
    result = brain.delivery.deliver_acknowledgement(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "no_text"
    assert sent == []


def test_a_rambling_model_is_collapsed_to_one_line(brain):
    task = _task(brain)
    sent = _capture(brain)
    brain.delivery.model = ScriptedModelClient(
        [ModelResponse(content='  "Sure!\n\nLet me look\tinto that.  "  ')]
    )

    brain.delivery.deliver_acknowledgement(task["id"])

    assert sent[0]["text"] == "Sure! Let me look into that."
    assert "\n" not in sent[0]["text"]


def test_it_is_skipped_when_the_model_writes_directly(brain):
    """suppress_delivery means the run posts its own message; an
    acknowledgement would be a second, unexpected one."""
    task = _task(brain)
    task["metadata"] = {**(task.get("metadata") or {}), "suppress_delivery": True}
    brain.store.update_task(task)
    sent = _capture(brain)
    brain.delivery.model = ScriptedModelClient([ModelResponse(content="hello")])

    result = brain.delivery.deliver_acknowledgement(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "suppressed"
    assert sent == []


def test_child_tasks_stay_silent(brain):
    """Children are private specialist evidence -- only the root
    conversation ever speaks."""
    task = _task(brain)
    task["parent_task_id"] = "task_parent"
    brain.store.update_task(task)
    sent = _capture(brain)
    brain.delivery.model = ScriptedModelClient([ModelResponse(content="hello")])

    result = brain.delivery.deliver_acknowledgement(task["id"])

    assert result["delivered"] is False
    assert result["reason"] == "child_task"
    assert sent == []


def test_the_delivery_lane_handles_the_job_kind(brain):
    """It must run on the infrastructure lane: the agent lane is inside
    execute_task, so an acknowledgement queued there would land after the
    work it is meant to precede."""
    kinds = brain.harness_worker.infrastructure_job_kinds()
    assert "delivery.acknowledge" in kinds
    assert "delivery.acknowledge" not in brain.harness_worker.agent_job_kinds()


def test_the_acknowledgement_asks_for_no_deliberation(brain):
    """A reasoning model given a one-line budget spends all of it thinking
    and returns nothing -- three of four live probes came back empty before
    this. The acknowledgement must land fast, so it asks for no reasoning."""
    task = _task(brain)
    _capture(brain)
    captured: dict[str, object] = {}

    class Recording:
        def generate(self, **kwargs):
            captured.update(kwargs)
            return ModelResponse(content="On it.")

    brain.delivery.model = Recording()
    brain.delivery.deliver_acknowledgement(task["id"])

    assert captured["reasoning"] is False


def test_the_hourglass_clears_when_the_acknowledgement_posts(brain):
    """The reaction covers the gap between "your message arrived" and
    "Toda has spoken". Once the acknowledgement posts it is redundant --
    leaving it up through a multi-minute run reads as stuck."""
    task = _task(brain)
    task["metadata"] = {
        **(task.get("metadata") or {}),
        "slack": {"channel_id": "C_ACK", "message_ts": "111.222", "workspace_id": "TACME"},
    }
    brain.store.update_task(task)
    _capture(brain)
    cleared: list[dict[str, Any]] = []
    brain.delivery.slack.unreact = lambda **kwargs: cleared.append(kwargs)
    brain.delivery.model = ScriptedModelClient([ModelResponse(content="On it.")])

    brain.delivery.deliver_acknowledgement(task["id"])

    assert len(cleared) == 1
    assert cleared[0]["timestamp"] == "111.222"
    assert cleared[0]["channel_id"] == "C_ACK"


def test_the_hourglass_survives_until_toda_speaks(brain):
    """A failed acknowledgement must leave the reaction in place: it is
    the only remaining signal that anything is happening."""
    task = _task(brain)
    task["metadata"] = {
        **(task.get("metadata") or {}),
        "slack": {"channel_id": "C_ACK", "message_ts": "111.222", "workspace_id": "TACME"},
    }
    brain.store.update_task(task)
    _capture(brain)
    cleared: list[dict[str, Any]] = []
    brain.delivery.slack.unreact = lambda **kwargs: cleared.append(kwargs)

    class Broken:
        def generate(self, **_kwargs):
            raise RuntimeError("provider down")

    brain.delivery.model = Broken()
    result = brain.delivery.deliver_acknowledgement(task["id"])

    assert result["delivered"] is False
    assert cleared == []
