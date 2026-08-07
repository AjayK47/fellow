"""SkillResponseValidator must fail open: a glitch in the auxiliary
validation model call (bad JSON, or "invalid" with no correction) must
never raise and take down the whole Task -- that would discard everything
the Task actually did over a side-check glitch. Regression test for a real
production failure: "skill response validation returned invalid JSON:
Unterminated string starting at..." killed an entire request."""

from __future__ import annotations

from docket.harness.model_client import ScriptedModelClient
from docket.harness.models import ModelResponse
from docket.harness.skill_validation import SkillResponseValidator

_VALIDATING_SKILL = {
    "id": "skill_test",
    "version": 1,
    "name": "Test Skill",
    "activation_mode": "mandatory",
    "response_validation": {"enabled": True, "instructions": "be nice"},
    "steps": [],
    "warnings": [],
}


def test_malformed_validator_json_fails_open_not_raises():
    model = ScriptedModelClient(
        [ModelResponse(content='{"valid": true, "violations": [], "corr')]  # truncated
    )
    validator = SkillResponseValidator(model=model)
    result = validator.validate(
        response="the real draft answer",
        active_skills=[_VALIDATING_SKILL],
        destination={"type": "dm", "id": "D1", "is_private": True},
    )
    assert result["response"] == "the real draft answer"
    assert result["valid"] is True
    assert result["checked"] is False
    assert "invalid JSON" in result["violations"][0]


def test_invalid_with_no_correction_fails_open_not_raises():
    model = ScriptedModelClient(
        [
            ModelResponse(
                content='{"valid": false, "violations": ["too long"], "corrected_response": ""}'
            )
        ]
    )
    validator = SkillResponseValidator(model=model)
    result = validator.validate(
        response="the real draft answer",
        active_skills=[_VALIDATING_SKILL],
        destination={"type": "dm", "id": "D1", "is_private": True},
    )
    assert result["response"] == "the real draft answer"
    assert result["valid"] is True
    assert result["checked"] is False
    assert "too long" in result["violations"]


def test_valid_json_with_a_real_correction_still_applies_it():
    model = ScriptedModelClient(
        [
            ModelResponse(
                content='{"valid": false, "violations": ["formatting"], "corrected_response": "fixed answer"}'
            )
        ]
    )
    validator = SkillResponseValidator(model=model)
    result = validator.validate(
        response="the real draft answer",
        active_skills=[_VALIDATING_SKILL],
        destination={"type": "dm", "id": "D1", "is_private": True},
    )
    assert result["response"] == "fixed answer"
    assert result["valid"] is False
    assert result["checked"] is True
