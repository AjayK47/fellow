"""Model-request context accounting.

The model context window is shared by the input and the requested output.  A
retrieval budget that ignores the system prompt, history, or tool schemas can
look safe while the provider still rejects the complete request.  This module
keeps the arithmetic pure and provider-neutral so it can be tested without a
live model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


_CHARS_PER_TOKEN_FALLBACK = 4
_DEFAULT_SAFETY_MARGIN = 512


def estimate_tokens(value: Any, *, model: str | None = None) -> int:
    """Estimate tokens for a provider request.

    Exact tokenizers differ by model family and are not always available for
    an OpenRouter model.  JSON serialization plus a conservative character
    ratio is deterministic and deliberately includes keys, tool schemas, and
    message structure rather than counting message text only.
    """
    if value is None:
        return 0
    if model and isinstance(value, dict) and "messages" in value:
        # LiteLLM is optional in the local deterministic install, but when a
        # provider bundle supplies it we can use the model's actual tokenizer
        # for the complete request (including tools/response format). Any
        # unsupported OpenRouter model falls through to the stable heuristic.
        try:
            from litellm import token_counter  # type: ignore[import-not-found]

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": value.get("messages") or [],
            }
            if value.get("tools"):
                kwargs["tools"] = value["tools"]
            if value.get("response_format"):
                kwargs["response_format"] = value["response_format"]
            counted = token_counter(**kwargs)
            if isinstance(counted, int) and counted > 0:
                return counted
        except Exception:
            # Tokenizer support is an optimization, never a reason to fail a
            # request boundary calculation.
            pass
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN_FALLBACK))


@dataclass(frozen=True)
class RequestBudget:
    context_window: int
    output_reservation: int
    safety_margin: int
    effective_input_window: int
    estimated_input_tokens: int
    remaining_for_context: int

    @property
    def fits(self) -> bool:
        return self.estimated_input_tokens <= self.effective_input_window

    @property
    def over_by(self) -> int:
        return max(0, self.estimated_input_tokens - self.effective_input_window)


def request_budget(
    *,
    context_window: int,
    output_tokens: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_schema: dict[str, Any] | None = None,
    safety_margin: int = _DEFAULT_SAFETY_MARGIN,
    model: str | None = None,
    calibration_factor: float = 1.0,
) -> RequestBudget:
    """Account for the complete provider request before sending it."""
    window = max(1, int(context_window))
    # Never reserve more than half the window for output.  This keeps a
    # misconfigured large completion cap from reducing the input window to
    # zero while still honoring ordinary configured caps.
    reservation = min(max(1, int(output_tokens)), max(1, window // 2))
    margin = max(0, min(int(safety_margin), max(0, window - reservation - 1)))
    effective = max(1, window - reservation - margin)
    request: dict[str, Any] = {"messages": messages}
    if tools:
        request["tools"] = tools
    if response_schema:
        request["response_format"] = response_schema
    raw_estimated = estimate_tokens(request, model=model)
    estimated = max(0, math.ceil(raw_estimated * max(1.0, float(calibration_factor))))
    return RequestBudget(
        context_window=window,
        output_reservation=reservation,
        safety_margin=margin,
        effective_input_window=effective,
        estimated_input_tokens=estimated,
        remaining_for_context=max(0, effective - estimated),
    )


def context_budget_for_request(
    *,
    context_window: int,
    output_tokens: int,
    messages_without_company_context: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    configured_context_budget: int = 12_000,
    safety_margin: int = _DEFAULT_SAFETY_MARGIN,
    model: str | None = None,
    calibration_factor: float = 1.0,
) -> int:
    """Return the retrieval budget that fits after fixed request content."""
    budget = request_budget(
        context_window=context_window,
        output_tokens=output_tokens,
        messages=messages_without_company_context,
        tools=tools,
        safety_margin=safety_margin,
        model=model,
        calibration_factor=calibration_factor,
    )
    return max(0, min(int(configured_context_budget), budget.remaining_for_context))
