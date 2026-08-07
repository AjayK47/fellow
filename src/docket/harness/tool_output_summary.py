"""H-14: compress oversized tool output with an actual summarization pass
instead of chopping the serialized JSON at a fixed character count.

Mirrors the existing `ModelMemoryExtractor.live` pattern: falls back to a
deterministic, zero-cost truncation (today's existing behavior, still always
valid JSON) whenever no live model is configured, so tests and offline
deployments never depend on a network call.
"""

from __future__ import annotations

import json
from typing import Any

from docket.harness.prompt_library import load

TOOL_OUTPUT_SUMMARY_SYSTEM_PROMPT = load("tool_output_summary")

TOOL_OUTPUT_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "item_count": {"type": ["integer", "null"]},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "item_count", "key_points"],
    "additionalProperties": False,
}

# Bound what we ever send to the summarizer model itself — an oversized tool
# result shouldn't turn into an oversized summarization request.
_SUMMARIZER_INPUT_CHARS = 60_000


class ToolOutputSummarizer:
    def __init__(self, *, model: Any) -> None:
        self.model = model

    @property
    def live(self) -> bool:
        return getattr(self.model, "model", "").lower() not in {
            "deterministic-harness-v1",
            "",
        }

    def summarize(self, *, tool_name: str, serialized: str) -> dict[str, Any]:
        if self.live:
            try:
                return self._model_summary(tool_name, serialized)
            except Exception:
                pass
        return self._truncated_fallback(serialized)

    def _model_summary(self, tool_name: str, serialized: str) -> dict[str, Any]:
        response = self.model.generate(
            messages=[
                {"role": "system", "content": TOOL_OUTPUT_SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tool_name": tool_name,
                            "output": serialized[:_SUMMARIZER_INPUT_CHARS],
                        },
                        default=str,
                    ),
                },
            ],
            response_schema=TOOL_OUTPUT_SUMMARY_SCHEMA,
            temperature=0.0,
            max_tokens=800,
        )
        parsed = json.loads(response.content)
        return {
            "summarized": True,
            "summary": parsed.get("summary", ""),
            "item_count": parsed.get("item_count"),
            "key_points": parsed.get("key_points") or [],
            "original_characters": len(serialized),
        }

    @staticmethod
    def _truncated_fallback(serialized: str) -> dict[str, Any]:
        return {
            "truncated": True,
            "content": serialized[:20_000],
            "original_characters": len(serialized),
        }
