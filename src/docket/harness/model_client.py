"""Provider-neutral chat and structured-output model clients."""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Protocol

import httpx

from docket.config import Settings
from docket.domain.models import new_id
from docket.harness.models import ModelResponse, ModelToolCall


_DEFAULT_CONTEXT_LENGTH = 128_000


class ModelClient(Protocol):
    model: str

    def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> ModelResponse: ...

    def context_length(self) -> int: ...


class OpenRouterModelClient:
    """Direct OpenRouter Chat Completions client; no agent framework involved."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        # See config.py's main_model_reasoning_effort docstring -- gives the
        # model genuine internal deliberation space before it decides on a
        # response or tool call, the same native-reasoning feature Hermes
        # relies on, ported here via OpenRouter's provider-neutral
        # `reasoning` field rather than any custom prompt-engineered
        # "think step".
        self.reasoning_effort = reasoning_effort
        self._context_length: int | None = None

    def context_length(self) -> int:
        """Resolve and cache this model's real context window from
        OpenRouter's model catalog (ported from Hermes's
        get_model_context_length — compaction should trigger relative to
        what the model actually supports, not a number picked independent
        of which model is configured). Falls back to a conservative default
        on any lookup failure so a transient network issue never breaks the
        run loop itself."""
        if self._context_length is not None:
            return self._context_length
        try:
            response = httpx.get(
                f"{self.base_url}/models", timeout=self.timeout_seconds
            )
            response.raise_for_status()
            for entry in response.json().get("data") or []:
                if entry.get("id") == self.model:
                    length = entry.get("context_length")
                    if isinstance(length, int) and length > 0:
                        self._context_length = length
                        return length
                    break
        except Exception:
            pass
        self._context_length = _DEFAULT_CONTEXT_LENGTH
        return self._context_length

    def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            # H-08: parallel tool calls are allowed at the model level now —
            # CompanyAgent._loop() partitions the returned calls by safety
            # (independent reads run concurrently, everything else stays
            # serialized), so there's no longer a reason to suppress this
            # here and force every call into one iteration each.
        if response_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "docket_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Bare raise_for_status() only gives "400 Bad Request" with no
            # indication of *why* — the response body is where OpenRouter
            # actually says what was wrong (e.g. context length, invalid
            # tool schema), and that's exactly the detail that ends up in
            # the Task's own error field and this codebase's own logs.
            raise RuntimeError(
                f"OpenRouter {response.status_code} for model {self.model}: "
                f"{response.text[:2000]}"
            ) from exc
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("model returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        calls: list[ModelToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as exc:
                # A JSONDecodeError position alone doesn't say which tool or
                # how long the argument string was -- both matter for
                # telling "the model wrote a malformed call" apart from
                # "the completion got cut off mid-argument because
                # max_tokens was too small for what this turn needed"
                # (e.g. finish_reason == "length", or a raw_args length
                # suspiciously close to the configured max_tokens).
                raise RuntimeError(
                    f"invalid model tool arguments for tool "
                    f"{function.get('name') or '<unknown>'!r} "
                    f"(finish_reason={choice.get('finish_reason')!r}, "
                    f"arguments_length={len(raw_args) if isinstance(raw_args, str) else 'n/a'}): "
                    f"{exc}"
                ) from exc
            calls.append(
                ModelToolCall(
                    id=call.get("id") or new_id("call"),
                    name=function.get("name") or "",
                    arguments=arguments or {},
                )
            )
        return ModelResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "stop",
            model=payload.get("model") or self.model,
            usage=payload.get("usage") or {},
            raw=payload,
        )


class ScriptedModelClient:
    """Deterministic model for exhaustive runtime and approval tests."""

    model = "scripted-test-model"

    def __init__(self, responses: list[ModelResponse | dict[str, Any]]) -> None:
        self.responses = deque(
            item if isinstance(item, ModelResponse) else ModelResponse(**item)
            for item in responses
        )
        self.requests: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> ModelResponse:
        self.requests.append(kwargs)
        if not self.responses:
            raise RuntimeError("scripted model exhausted")
        return self.responses.popleft()

    def context_length(self) -> int:
        return _DEFAULT_CONTEXT_LENGTH


class DeterministicHarnessModel:
    """Useful zero-cost local fallback, not a substitute for the live model.

    It answers from the permission-filtered context embedded in the prompt and
    emits a valid attention classification. This keeps the full event/run
    machinery runnable without silently making external calls.
    """

    model = "deterministic-harness-v1"

    def context_length(self) -> int:
        return _DEFAULT_CONTEXT_LENGTH

    def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> ModelResponse:
        del temperature, max_tokens
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if response_schema:
            if "corrected_response" in (response_schema.get("properties") or {}):
                try:
                    validation_input = json.loads(
                        str(messages[-1].get("content") or "{}")
                    )
                except json.JSONDecodeError:
                    validation_input = {}
                draft = str(validation_input.get("draft_response") or "")
                return ModelResponse(
                    content=json.dumps(
                        {
                            "valid": True,
                            "violations": [],
                            "corrected_response": draft,
                        }
                    ),
                    finish_reason="stop",
                    model=self.model,
                )
            # Classify the compact user payload only. The classifier's system
            # prompt necessarily contains words such as "acknowledged" and
            # must not become evidence about the actual Slack thread.
            lower = str(messages[-1].get("content") or "").lower()
            incident = any(
                word in lower
                for word in ("failing", "failure", "outage", "down", "incident", "stuck")
            )
            customer = any(word in lower for word in ("customer", "acme", "client"))
            acknowledged = any(
                word in lower for word in ("acknowledged", "investigating", "already active")
            )
            decision = (
                "ATTACH_TO_EXISTING_TASK"
                if "active_task_id" in lower and not re.search(r"active_task_id[\"']?\s*:\s*null", lower)
                else "OBSERVE"
                if acknowledged
                else "WAKE_MAIN_AGENT"
                if incident
                else "UPDATE_WORKING_STATE"
            )
            data = {
                "signal_type": "possible_customer_issue"
                if customer and incident
                else "possible_operational_issue"
                if incident
                else "conversation_update",
                "novelty": "new_recurrence" if "again" in lower else "new",
                "urgency": "high" if incident else "low",
                "decision": decision,
                "confidence": 0.90 if incident else 0.64,
                "reason": "Deterministic local evaluator found an actionable failure signal."
                if incident
                else "No high-value intervention signal was found.",
                "suggested_goal": "Investigate the reported failure and route it to the owner."
                if incident
                else None,
                "related_entity_ids": [],
                "existing_task_id": None,
            }
            return ModelResponse(
                content=json.dumps(data),
                finish_reason="stop",
                model=self.model,
            )

        context_match = re.search(
            r"<company_context>\s*(.*?)\s*</company_context>", joined, re.DOTALL
        )
        if context_match:
            try:
                context = json.loads(context_match.group(1))
            except json.JSONDecodeError:
                context = {}
            facts = (
                context.get("current_facts", [])
                + context.get("issues", [])
                + context.get("decisions", [])
                + context.get("open_commitments", [])
            )
            if facts:
                statements = [str(item.get("content") or "") for item in facts[:4]]
                source_ids = sorted(
                    {
                        source_id
                        for item in facts[:4]
                        for source_id in item.get("source_ids") or []
                    }
                )
                citation = f"\n\nEvidence: {', '.join(source_ids)}" if source_ids else ""
                return ModelResponse(
                    content="Based on the permitted company context: "
                    + " ".join(statements)
                    + citation,
                    finish_reason="stop",
                    model=self.model,
                )
        return ModelResponse(
            content=(
                "I do not have enough permission-safe company evidence to answer "
                "that confidently."
            ),
            finish_reason="stop",
            model=self.model,
        )


def build_model_clients(
    config: Settings | None = None,
) -> tuple[ModelClient, ModelClient, ModelClient, ModelClient]:
    config = config or Settings()
    if config.model_provider == "openrouter":
        if not config.model_api_key:
            raise RuntimeError(
                "DOCKET_MODEL_PROVIDER=openrouter requires DOCKET_MODEL_API_KEY"
            )
        main = OpenRouterModelClient(
            api_key=config.model_api_key,
            model=config.chat_model,
            base_url=config.model_base_url,
            timeout_seconds=config.model_timeout_seconds,
            # Only the main reasoning loop gets extended thinking -- the
            # small auxiliary classifiers below don't benefit from it.
            reasoning_effort=config.main_model_reasoning_effort,
        )
        attention = OpenRouterModelClient(
            api_key=config.model_api_key,
            model=config.attention_model,
            base_url=config.model_base_url,
            timeout_seconds=config.model_timeout_seconds,
        )
        extraction = OpenRouterModelClient(
            api_key=config.model_api_key,
            model=config.extraction_model or config.chat_model,
            base_url=config.model_base_url,
            timeout_seconds=config.model_timeout_seconds,
        )
        tool_output_summary = OpenRouterModelClient(
            api_key=config.model_api_key,
            model=config.tool_output_summary_model or config.chat_model,
            base_url=config.model_base_url,
            timeout_seconds=config.model_timeout_seconds,
        )
        return main, attention, extraction, tool_output_summary
    deterministic = DeterministicHarnessModel()
    return deterministic, deterministic, deterministic, deterministic
