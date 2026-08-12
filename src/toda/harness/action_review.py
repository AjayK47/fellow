"""Auxiliary LLM risk review -- the single front door every gated action
passes through before ever reaching a human, ported from Hermes-agent's
_smart_approve() (harnesses/hermes-agent/tools/approval.py:2736).

Mirrors ToolOutputSummarizer's live/fallback pattern (tool_output_summary.py):
falls back to a safe default whenever no live model is configured or the
call fails, so tests and offline deployments never depend on a network call.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from toda.harness.prompt_library import load

REVIEW_SYSTEM_PROMPT = load("action_review")

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["APPROVE", "DENY", "ESCALATE"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}

Verdict = Literal["approve", "deny", "escalate"]


class ActionReviewer:
    def __init__(self, *, model: Any) -> None:
        self.model = model

    @property
    def live(self) -> bool:
        return getattr(self.model, "model", "").lower() not in {
            "deterministic-harness-v1",
            "",
        }

    def review(
        self, *, tool_name: str, description: str, arguments: dict[str, Any]
    ) -> Verdict:
        """"escalate" is the safe fallback -- the existing human-approval
        path -- on any failure or when no live model is configured. Never
        silently approve or deny without a real model actually assessing
        risk."""
        if not self.live:
            return "escalate"
        try:
            response = self.model.generate(
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "action": tool_name,
                                "description": description,
                                "arguments": arguments,
                            },
                            default=str,
                        ),
                    },
                ],
                response_schema=REVIEW_SCHEMA,
                temperature=0.0,
                max_tokens=16,
            )
            verdict = json.loads(response.content).get("verdict", "").upper()
        except Exception:
            return "escalate"
        if verdict == "APPROVE":
            return "approve"
        if verdict == "DENY":
            return "deny"
        return "escalate"
