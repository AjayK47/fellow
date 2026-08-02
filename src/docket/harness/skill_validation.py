"""Skill-driven validation for user-facing agent responses."""

from __future__ import annotations

import json
from typing import Any


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
                {
                    "role": "system",
                    "content": (
                        "Validate a draft agent response only against the supplied "
                        "active-skill contracts. Do not add, remove, reinterpret, or "
                        "fact-check claims. If invalid, correct only presentation and "
                        "contract violations while preserving meaning, facts, links, "
                        "citations, and uncertainty exactly. Return the draft unchanged "
                        "when valid."
                    ),
                },
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
        try:
            evaluation = json.loads(result.content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"skill response validation returned invalid JSON: {exc}") from exc
        valid = bool(evaluation.get("valid"))
        corrected = str(evaluation.get("corrected_response") or "").strip()
        if not valid and not corrected:
            raise RuntimeError("skill response validation rejected the response without a correction")
        return {
            "response": response if valid else corrected,
            "valid": valid,
            "checked": True,
            "violations": list(evaluation.get("violations") or []),
            "skill_refs": [
                {"id": skill["id"], "version": skill["version"]}
                for skill in validating_skills
            ],
            "usage": result.usage or {},
            "model": result.model or getattr(self.model, "model", ""),
        }
