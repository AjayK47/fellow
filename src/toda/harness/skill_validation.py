"""Skill-driven validation for user-facing agent responses."""

from __future__ import annotations

import json
from typing import Any

from toda.harness.prompting.catalog import load


SKILL_VALIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "violations": {"type": "array", "items": {"type": "string"}},
        "corrected_response": {"type": "string"},
    },
    "required": ["valid", "violations", "corrected_response"],
    "additionalProperties": False,
}


class SkillResponseValidator:
    def __init__(self, *, model: Any) -> None:
        self.model = model

    def validate(
        self,
        *,
        response: str,
        active_skills: list[dict[str, Any]],
        destination: dict[str, Any],
    ) -> dict[str, Any]:
        validating_skills = [
            skill
            for skill in active_skills
            if (skill.get("response_validation") or {}).get("enabled")
        ]
        if not validating_skills:
            return {
                "response": response,
                "valid": True,
                "checked": False,
                "violations": [],
                "skill_refs": [],
                "usage": {},
            }
        contracts = [
            {
                "id": skill["id"],
                "version": skill["version"],
                "name": skill["name"],
                "mode": skill["activation_mode"],
                "instructions": (
                    skill.get("response_validation") or {}
                ).get("instructions", ""),
                "steps": skill.get("steps") or [],
                "warnings": skill.get("warnings") or [],
            }
            for skill in validating_skills
        ]
        result = self.model.generate(
            messages=[
                {"role": "system", "content": load("skill_response_validation")},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "destination": destination,
                            "active_skill_contracts": contracts,
                            "draft_response": response,
                        },
                        sort_keys=True,
                    ),
                },
            ],
            response_schema=SKILL_VALIDATION_SCHEMA,
            temperature=0.0,
            max_tokens=2200,
        )
        skill_refs = [
            {"id": skill["id"], "version": skill["version"]} for skill in validating_skills
        ]
        # Validation is a quality check on top of an already-produced draft,
        # not a hard requirement for the Task to succeed. A malformed or
        # unusable response from the *validator's own* model call (bad JSON,
        # or "invalid" with no correction to fall back to) must never raise
        # and fail the whole Task -- that would discard everything the Task
        # actually did (real research, real tool calls) over an auxiliary
        # side-check glitch. Fail open: pass the draft through unvalidated
        # rather than replacing real work with a hard failure.
        try:
            evaluation = json.loads(result.content)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "response": response,
                "valid": True,
                "checked": False,
                "violations": [f"skill_validation_error: invalid JSON from validator: {exc}"],
                "skill_refs": skill_refs,
                "usage": result.usage or {},
            }
        valid = bool(evaluation.get("valid"))
        corrected = str(evaluation.get("corrected_response") or "").strip()
        if not valid and not corrected:
            return {
                "response": response,
                "valid": True,
                "checked": False,
                "violations": [
                    "skill_validation_error: validator reported invalid with no correction",
                    *[str(v) for v in (evaluation.get("violations") or [])],
                ],
                "skill_refs": skill_refs,
                "usage": result.usage or {},
            }
        return {
            "response": response if valid else corrected,
            "valid": valid,
            "checked": True,
            "violations": list(evaluation.get("violations") or []),
            "skill_refs": skill_refs,
            "usage": result.usage or {},
            "model": result.model or getattr(self.model, "model", ""),
        }
