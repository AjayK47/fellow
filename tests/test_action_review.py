"""ActionReviewer -- the single auxiliary-LLM front door every gated action
passes through before a human is ever asked (ported from Hermes-agent's
_smart_approve()). Falls back to "escalate" (the existing human-approval
path) whenever no live model is configured or the call fails, so it never
silently approves or denies without a real risk assessment."""

from __future__ import annotations

import json

from docket.harness.action_review import ActionReviewer
from docket.harness.model_client import DeterministicHarnessModel, ScriptedModelClient
from docket.harness.models import ModelResponse


def test_reviewer_escalates_for_the_deterministic_model():
    reviewer = ActionReviewer(model=DeterministicHarnessModel())
    assert reviewer.live is False
    verdict = reviewer.review(tool_name="run_code", description="run code", arguments={})
    assert verdict == "escalate"


def test_reviewer_is_live_for_a_configured_model():
    reviewer = ActionReviewer(model=ScriptedModelClient([ModelResponse(content="{}")]))
    assert reviewer.live is True


def test_reviewer_approves_when_the_model_says_approve():
    model = ScriptedModelClient(
        [ModelResponse(content=json.dumps({"verdict": "APPROVE"}))]
    )
    reviewer = ActionReviewer(model=model)
    verdict = reviewer.review(
        tool_name="automation_create", description="create automation", arguments={}
    )
    assert verdict == "approve"


def test_reviewer_denies_when_the_model_says_deny():
    model = ScriptedModelClient(
        [ModelResponse(content=json.dumps({"verdict": "DENY"}))]
    )
    reviewer = ActionReviewer(model=model)
    verdict = reviewer.review(
        tool_name="run_code", description="run code", arguments={"code": "rm -rf /"}
    )
    assert verdict == "deny"


def test_reviewer_escalates_on_malformed_model_output():
    model = ScriptedModelClient([ModelResponse(content="not valid json")])
    reviewer = ActionReviewer(model=model)
    verdict = reviewer.review(tool_name="run_code", description="run code", arguments={})
    assert verdict == "escalate"


def test_reviewer_escalates_on_an_unrecognized_verdict_string():
    model = ScriptedModelClient(
        [ModelResponse(content=json.dumps({"verdict": "MAYBE"}))]
    )
    reviewer = ActionReviewer(model=model)
    verdict = reviewer.review(tool_name="run_code", description="run code", arguments={})
    assert verdict == "escalate"
