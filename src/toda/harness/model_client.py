"""Provider-neutral chat and structured-output model clients."""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any, Protocol

import httpx

from toda.config import Settings, settings as _loaded_settings
from toda.domain.models import new_id
from toda.harness.message_sanitization import (
    needs_reasoning_echo,
    sanitize_api_messages,
)
from toda.harness.models import ModelResponse, ModelToolCall
from toda.providers import ProviderResolver, ResolvedModel


log = logging.getLogger("toda.model")

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
        reasoning: bool | None = None,
    ) -> ModelResponse: ...

    def context_length(self) -> int: ...


class OpenAICompatibleModelClient:
    """Chat Completions transport for any OpenAI-compatible provider."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str | None = None,
        headers: dict[str, str] | None = None,
        provider_name: str = "openai-compatible",
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
        self.headers = headers or {}
        self.provider_name = provider_name
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
        reasoning: bool | None = None,
    ) -> ModelResponse:
        """``reasoning=False`` suppresses deliberation for this call.

        Some work wants an answer, not thought: a one-line acknowledgement
        has to land while the main model is still starting, and a reasoning
        model given a small output budget spends the whole thing thinking
        and returns nothing at all.
        """
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
                    "name": "toda_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        if reasoning is False:
            body["reasoning"] = {"enabled": False}
        elif self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return self._generate_with_recovery(body)

    # Continuations replay the model's own thinking so it can finish the
    # turn; resends are the fallback when there is nothing to continue from.
    # Both are per `generate()` call.
    _MAX_THINKING_CONTINUATIONS = 2
    _MAX_EMPTY_RESENDS = 2

    def _generate_with_recovery(self, body: dict[str, Any]) -> ModelResponse:
        """Recover the turns where a reasoning model produces no answer.

        A reasoning model can end its turn having produced only deliberation:
        no content, no tool call, everything in the separate reasoning field.
        Treating that as a failure loses a Task over a model quirk, and it is
        intermittent -- the identical request usually answers.

        Ported from Hermes's conversation loop, which handles the two cases
        differently rather than blanket-retrying:

        * **thinking-only** -- reasoning came back but no text. Replay the
          assistant turn carrying that reasoning so the model continues from
          its own thought, keeping the quality reasoning was enabled for.
        * **truly empty** -- nothing came back. Nothing to continue from, so
          re-send the identical request.

        The continuation is gated on the target actually supporting
        reasoning echo-back (see message_sanitization.reasoning_echo_family).
        This gate is the whole ballgame: replaying a trailing assistant turn
        is an assistant *prefill*, so the model resumes mid-stream rather
        than starting fresh. Given reasoning it resumes its own thought;
        given a bare empty turn it resumes whatever it likes -- in
        production, the chat template itself, which put 4,600 tokens of
        Toda's system prompt into a Slack channel. On a strict provider the
        reasoning field is stripped before the wire, which would leave
        exactly that bare empty turn, so those targets resend instead.

        The continuation lives inside this call. Hermes must flag its
        equivalents ephemeral so an append-only flush cannot commit them to
        the session store; here the caller writes the transcript from the
        returned response, so scaffolding cannot reach it by construction.
        """
        response = self._send(body)
        if self._is_usable(response, body):
            return response

        can_continue = needs_reasoning_echo(
            self.provider_name, self.model, self.base_url
        )
        attempt = dict(body)
        continuations = 0
        resends = 0
        while not self._is_budget_exhaustion(response) and (
            continuations < self._MAX_THINKING_CONTINUATIONS
            or resends < self._MAX_EMPTY_RESENDS
        ):
            reasoning_text = response.reasoning.strip()
            if can_continue and reasoning_text and continuations < self._MAX_THINKING_CONTINUATIONS:
                continuations += 1
                # reasoning_content, not reasoning: the former is what these
                # thinking modes accept on the way in, the latter is only
                # what aggregators hand back on the way out. Sending the
                # response field as a request field is how this turned into
                # a bare empty prefill the first time.
                attempt = {
                    **body,
                    "messages": [
                        *body["messages"],
                        {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": reasoning_text,
                        },
                    ],
                }
                log.info(
                    "Thinking-only turn from %s — continuing from its own "
                    "reasoning (%d/%d)",
                    self.model, continuations, self._MAX_THINKING_CONTINUATIONS,
                )
            elif resends < self._MAX_EMPTY_RESENDS:
                resends += 1
                attempt = dict(body)
                log.warning(
                    "Unusable turn from %s — resending identical request (%d/%d)",
                    self.model, resends, self._MAX_EMPTY_RESENDS,
                )
            else:
                break
            response = self._send(attempt)
            if self._is_usable(response, body):
                return response

        if self.reasoning_effort:
            log.warning(
                "%s still produced nothing usable after %d resend(s) — "
                "retrying once with reasoning disabled",
                self.model, resends,
            )
            final = dict(body)
            final["reasoning"] = {"enabled": False}
            return self._send(final)
        return response

    def _is_usable(self, response: ModelResponse, body: dict[str, Any]) -> bool:
        """A turn the agent loop can actually act on.

        Non-empty is necessary but not sufficient. A model that resumes the
        chat template rather than answering emits the prompt back, and a
        length check alone happily calls 4,600 tokens of system prompt a
        usable answer -- which is exactly how Toda once posted its own
        instructions into a Slack channel. Treat a prompt echo as unusable
        so the recovery path re-sends instead of the harness delivering it.
        """
        if response.tool_calls:
            return True
        content = response.content.strip()
        if not content:
            return False
        return not self._echoes_prompt(content, body)

    # Chat-template control tokens. These are how a model marks turn
    # boundaries internally; seeing one in *output* means it is reproducing
    # the template rather than answering within it.
    _TEMPLATE_TOKENS = ("begin▁of▁sentence", "end▁of▁sentence", "<|im_start|>",
                        "<|begin_of_text|>", "<|start_header_id|>")
    # Enough verbatim prompt to be unmistakable, short enough that a model
    # legitimately quoting one instruction back does not trip it.
    _ECHO_PROBE_CHARS = 160

    @classmethod
    def _echoes_prompt(cls, content: str, body: dict[str, Any]) -> bool:
        if any(token in content for token in cls._TEMPLATE_TOKENS):
            log.warning("Model output contained a chat-template control token")
            return True
        for message in body.get("messages") or []:
            if message.get("role") != "system":
                continue
            system = " ".join(str(message.get("content") or "").split())
            probe = system[: cls._ECHO_PROBE_CHARS]
            if len(probe) < cls._ECHO_PROBE_CHARS:
                continue
            if probe in " ".join(content.split()):
                log.warning("Model output echoed the system prompt verbatim")
                return True
        return False

    @staticmethod
    def _is_budget_exhaustion(response: ModelResponse) -> bool:
        """The model spent its whole output budget thinking.

        Worth separating from an ordinary empty turn because the recovery is
        different *and* the cost is real: re-sending an identical request
        that just consumed tens of thousands of reasoning tokens buys another
        bill and, most likely, the same outcome. There is also nothing to
        continue from -- the deliberation was spent, not returned. Skip
        straight to the reasoning-disabled attempt.
        """
        if response.finish_reason != "length":
            return False
        reasoning_tokens = (
            (response.usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or 0
        )
        return bool(reasoning_tokens)

    def _send(self, body: dict[str, Any]) -> ModelResponse:
        # The single pre-send chokepoint. Every request -- first attempt,
        # resend, prefill continuation, reasoning-disabled fallback -- passes
        # through here, so message shape is fixed in exactly one place
        # rather than at each call site (the fork Hermes explicitly rejected).
        body = {
            **body,
            "messages": sanitize_api_messages(
                body.get("messages") or [],
                provider=self.provider_name,
                model=self.model,
                base_url=self.base_url,
            ),
        }
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **self.headers,
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
                f"{self.provider_name} {response.status_code} for model {self.model}: "
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
        details = message.get("reasoning_details")
        return ModelResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "stop",
            model=payload.get("model") or self.model,
            usage=payload.get("usage") or {},
            raw=payload,
            # Providers disagree on the name; OpenRouter forwards whichever
            # the upstream used.
            reasoning=str(
                message.get("reasoning") or message.get("reasoning_content") or ""
            ),
            reasoning_details=list(details) if isinstance(details, list) else [],
        )


# Input items that count as the model having spoken last.
_MODEL_TURNS = frozenset({"assistant", "function_call"})

# Appended when the conversation would otherwise end on a model turn.
# Deliberately worded as "respond to", not "continue": "Continue with the
# request above" reads as resume-mid-sentence, and the model opened its
# reply with "Sorry about that cutoff!" for a truncation that never
# happened. This asks for the next turn without implying the last one was
# interrupted, and adds no instruction the user did not give.
_CONTINUE_TURN = "Respond to the most recent request above."


class ConcentrateResponsesModelClient:
    """Transport for Concentrate's unified Responses API.

    Not an OpenAI-compatible chat endpoint. The differences are structural
    rather than cosmetic, and each one is a translation this class owns:

      * the conversation is ``input`` (a flat list of items), not
        ``messages``, and the system prompt is a separate ``instructions``
        field rather than a message with role "system";
      * a tool is declared flat -- ``{type, name, description, parameters}``
        -- not nested under a ``function`` key;
      * a tool call comes back as an output item
        ``{type: "function_call", call_id, name, arguments}``, and its result
        goes back as ``{type: "function_call_output", call_id, output}``
        rather than a message with role "tool";
      * the reply is an ``output`` array of typed items (message, reasoning,
        function_call), not ``choices[0].message``;
      * usage is ``input_tokens``/``output_tokens``, and completion is
        reported as ``status`` rather than ``finish_reason``.

    Everything above the transport keeps speaking Toda's chat-shaped
    contract, so the agent loop, budgeting and recovery paths are unchanged.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str | None = None,
        headers: dict[str, str] | None = None,
        provider_name: str = "concentrate",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.headers = headers or {}
        self.provider_name = provider_name
        self._context_length: int | None = None

    def context_length(self) -> int:
        """Resolve the input window from Concentrate's catalog.

        Its field is ``max_input_tokens`` -- the input budget specifically --
        where the OpenAI-compatible catalog reports a combined
        ``context_length``. Compaction cares about the input side, so this
        is the more accurate of the two.
        """
        if self._context_length is not None:
            return self._context_length
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                headers=self._request_headers(),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            for entry in response.json().get("data") or []:
                if entry.get("id") == self.model:
                    window = entry.get("max_input_tokens")
                    if isinstance(window, (int, float)) and window > 0:
                        self._context_length = int(window)
                        return self._context_length
                    break
        except Exception:
            pass
        self._context_length = _DEFAULT_CONTEXT_LENGTH
        return self._context_length

    def _request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.headers,
        }

    @staticmethod
    def _to_input_items(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        """Chat-shaped messages -> (input items, instructions).

        System messages are joined into ``instructions``; an assistant turn
        carrying tool calls expands into one ``function_call`` item per call,
        because the Responses model has no notion of a message that also
        holds calls.
        """
        items: list[dict[str, Any]] = []
        instructions: list[str] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            if role == "system":
                if text.strip():
                    instructions.append(text)
                continue
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id") or "",
                        "output": text,
                    }
                )
                continue
            if role == "assistant":
                if text.strip():
                    items.append({"role": "assistant", "content": text})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments")
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id") or new_id("call"),
                            "name": function.get("name") or "",
                            "arguments": (
                                arguments
                                if isinstance(arguments, str)
                                else json.dumps(arguments or {})
                            ),
                        }
                    )
                continue
            if text.strip() or content:
                items.append({"role": role or "user", "content": content})

        # The request may not end on a model turn. Gemini via ai-studio
        # rejects one outright:
        #
        #   400 invalid_prompt -- Provider 'ai-studio' rejected the request:
        #   Requests ending with a model turn are not supported.
        #
        # Toda hits this because build_agent_messages replays session history
        # verbatim and never appends the goal as a closing user turn, so the
        # tail is simply whatever was last stored -- often Toda's own previous
        # reply. Chat-completions providers read a trailing assistant turn as
        # a prefill to continue; ai-studio refuses it.
        #
        # Append rather than trim, following Hermes: dropping the tail to
        # satisfy the rule would delete the most recent context, which is
        # exactly the context the model needs.
        if items and (items[-1].get("type") or items[-1].get("role")) in _MODEL_TURNS:
            items.append({"role": "user", "content": _CONTINUE_TURN})
        return items, "\n\n".join(instructions)

    @staticmethod
    def _to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """OpenAI's nested tool schema -> Concentrate's flat one."""
        flattened: list[dict[str, Any]] = []
        for tool in tools or []:
            function = tool.get("function") or {}
            flattened.append(
                {
                    "type": "function",
                    "name": function.get("name") or tool.get("name") or "",
                    "description": function.get("description")
                    or tool.get("description")
                    or "",
                    "parameters": function.get("parameters")
                    or tool.get("parameters")
                    or {},
                }
            )
        return flattened

    def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
        reasoning: bool | None = None,
    ) -> ModelResponse:
        items, instructions = self._to_input_items(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            body["instructions"] = instructions
        if tools:
            body["tools"] = self._to_tools(tools)
            body["tool_choice"] = "auto"
        if response_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "toda_response",
                    "strict": True,
                    "schema": response_schema,
                }
            }
        if reasoning is False:
            body["reasoning"] = {"effort": "none"}
        elif self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return self._send(body)

    def _send(self, body: dict[str, Any]) -> ModelResponse:
        response = httpx.post(
            f"{self.base_url}/responses",
            headers=self._request_headers(),
            json=body,
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.provider_name} {response.status_code} for model {self.model}: "
                f"{response.text[:2000]}"
            ) from exc
        return self._parse(response.json())

    def _parse(self, payload: dict[str, Any]) -> ModelResponse:
        error = payload.get("error")
        if error:
            raise RuntimeError(
                f"{self.provider_name} returned an error for model {self.model}: "
                f"{json.dumps(error)[:1000]}"
            )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ModelToolCall] = []
        for item in payload.get("output") or []:
            item_type = item.get("type")
            if item_type == "function_call":
                raw_args = item.get("arguments") or "{}"
                try:
                    arguments = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"invalid model tool arguments for tool "
                        f"{item.get('name') or '<unknown>'!r} "
                        f"(status={payload.get('status')!r}, "
                        f"arguments_length={len(raw_args) if isinstance(raw_args, str) else 'n/a'}): "
                        f"{exc}"
                    ) from exc
                calls.append(
                    ModelToolCall(
                        id=item.get("call_id") or new_id("call"),
                        name=item.get("name") or "",
                        arguments=arguments or {},
                    )
                )
            elif item_type == "reasoning":
                reasoning_parts.extend(_collect_text(item.get("summary")))
                reasoning_parts.extend(_collect_text(item.get("content")))
            elif item_type == "message" or item.get("role") == "assistant":
                content_parts.extend(_collect_text(item.get("content")))
        usage = payload.get("usage") or {}
        # "incomplete" is this API's way of saying the output budget ran out,
        # which the recovery path reads as budget exhaustion.
        status = str(payload.get("status") or "completed")
        finish_reason = {
            "completed": "tool_calls" if calls else "stop",
            "incomplete": "length",
        }.get(status, status)
        return ModelResponse(
            content="\n".join(part for part in content_parts if part).strip(),
            tool_calls=calls,
            finish_reason=finish_reason,
            model=str(payload.get("model") or self.model),
            usage={
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "completion_tokens_details": usage.get("output_tokens_details") or {},
            },
            raw=payload,
            reasoning="\n".join(part for part in reasoning_parts if part).strip(),
        )


def _collect_text(node: Any) -> list[str]:
    """Pull display text out of a Responses content node.

    The field is a string, a list of typed parts, or absent, depending on
    the item -- so this normalizes all three rather than each call site
    guessing.
    """
    if node is None:
        return []
    if isinstance(node, str):
        return [node]
    parts: list[str] = []
    if isinstance(node, list):
        for entry in node:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return parts


class AnthropicModelClient:
    """Native Anthropic Messages transport behind Toda's stable model contract."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = headers or {}

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
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                system_parts.append(str(message.get("content") or ""))
                continue
            if role == "tool":
                converted = {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id"),
                        "content": str(message.get("content") or ""),
                    }],
                }
            elif role == "assistant" and message.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": str(message["content"])})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments,
                    })
                converted = {"role": "assistant", "content": blocks}
            else:
                converted = {"role": role, "content": message.get("content") or ""}
            if anthropic_messages and anthropic_messages[-1]["role"] == converted["role"]:
                previous = anthropic_messages[-1]
                old = previous["content"]
                new = converted["content"]
                old_blocks = old if isinstance(old, list) else [{"type": "text", "text": str(old)}]
                new_blocks = new if isinstance(new, list) else [{"type": "text", "text": str(new)}]
                previous["content"] = [*old_blocks, *new_blocks]
            else:
                anthropic_messages.append(converted)
        if response_schema:
            system_parts.append(
                "Return only JSON matching this schema: "
                + json.dumps(response_schema, separators=(",", ":"))
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"].get("parameters", {"type": "object"}),
                }
                for tool in tools
                if tool.get("type") == "function" and tool.get("function")
            ]
        response = httpx.post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                **self.headers,
            },
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        text: list[str] = []
        calls: list[ModelToolCall] = []
        for block in payload.get("content") or []:
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ModelToolCall(
                        id=block.get("id") or new_id("call"),
                        name=block.get("name") or "",
                        arguments=block.get("input") or {},
                    )
                )
        return ModelResponse(
            content="".join(text),
            tool_calls=calls,
            finish_reason=payload.get("stop_reason") or "stop",
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
    config = config or _loaded_settings
    resolver = ProviderResolver(config.models.providers, config.models.assignments)
    main_resolved = resolver.resolve("main")
    if main_resolved is None:
        deterministic = DeterministicHarnessModel()
        return deterministic, deterministic, deterministic, deterministic

    def build(role: str, fallback: str = "main") -> ModelClient:
        resolved = resolver.resolve(role, fallback_role=fallback)
        assert resolved is not None
        return _build_resolved_model(
            resolved,
            config,
            reasoning_effort=config.models.main_reasoning_effort if role == "main" else None,
        )

    return build("main"), build("attention"), build("extraction"), build("tool_summary")


def _build_resolved_model(
    resolved: ResolvedModel,
    config: Settings,
    *,
    reasoning_effort: str | None = None,
) -> ModelClient:
    api_key = resolved.connection.api_key or "local"
    base_url = resolved.profile.endpoint(resolved.connection)
    headers = {**dict(resolved.profile.headers), **resolved.connection.headers}
    if resolved.profile.api_mode == "anthropic_messages":
        return AnthropicModelClient(
            api_key=api_key,
            model=resolved.assignment.model,
            base_url=base_url,
            timeout_seconds=config.models.timeout_seconds,
            headers=headers,
        )
    if resolved.profile.api_mode == "concentrate_responses":
        return ConcentrateResponsesModelClient(
            api_key=api_key,
            model=resolved.assignment.model,
            base_url=base_url,
            timeout_seconds=config.models.timeout_seconds,
            reasoning_effort=reasoning_effort,
            headers=headers,
            provider_name=resolved.profile.name,
        )
    return OpenAICompatibleModelClient(
        api_key=api_key,
        model=resolved.assignment.model,
        base_url=base_url,
        timeout_seconds=config.models.timeout_seconds,
        reasoning_effort=reasoning_effort,
        headers=headers,
        provider_name=resolved.profile.display_name,
    )


def build_vision_model(config: Settings, main_model: ModelClient) -> ModelClient | None:
    """Resolve the optional vision role, falling back to capable main model."""
    resolver = ProviderResolver(config.models.providers, config.models.assignments)
    resolved = resolver.resolve("vision")
    if resolved:
        return _build_resolved_model(resolved, config)
    return main_model if config.models.main_supports_vision else None
