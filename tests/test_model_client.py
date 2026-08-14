"""Provider requests use only required authentication and content headers."""

from __future__ import annotations

from toda.harness.model_client import AnthropicModelClient, OpenAICompatibleModelClient


def test_openrouter_request_does_not_add_application_attribution(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    def post(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    OpenAICompatibleModelClient(api_key="test", model="model", base_url="https://example.test").generate(
        messages=[{"role": "user", "content": "hello"}]
    )
    assert captured["headers"] == {"Authorization": "Bearer test", "Content-Type": "application/json"}
    assert "reasoning" not in captured["json"]


def test_reasoning_effort_enables_native_model_thinking(monkeypatch):
    """Ported from Hermes: the main model gets genuine internal
    deliberation space (via OpenRouter's provider-neutral `reasoning`
    field) before it decides on a response or tool call -- not a custom
    prompt-engineered "think step"."""
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    def post(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    OpenAICompatibleModelClient(
        api_key="test",
        model="model",
        base_url="https://example.test",
        reasoning_effort="medium",
    ).generate(messages=[{"role": "user", "content": "hello"}])
    assert captured["json"]["reasoning"] == {"effort": "medium"}


def test_anthropic_transport_maps_tools_and_tool_calls(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "claude-test",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"id": 7}},
                ],
            }

    def post(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    result = AnthropicModelClient(
        api_key="test", model="claude-test", base_url="https://api.example.test/v1"
    ).generate(
        messages=[{"role": "system", "content": "Be useful."}, {"role": "user", "content": "Find 7"}],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    )
    assert captured["json"]["system"] == "Be useful."
    assert captured["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert result.tool_calls[0].arguments == {"id": 7}


def test_reasoning_exhaustion_retries_once_with_reasoning_disabled(monkeypatch):
    """A reasoning model can burn its whole output budget thinking.

    finish_reason "length" with no content and no tool call is a dead turn that
    reached the user as "model returned neither answer nor tool call". The same
    request usually answers, so it is retried once with reasoning off.
    """
    from toda.harness.model_client import OpenAICompatibleModelClient

    sent: list[dict] = []

    class Response:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    exhausted = {
        "choices": [{"message": {"content": "", "tool_calls": []}, "finish_reason": "length"}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 40000}},
    }
    answered = {
        "choices": [{"message": {"content": "Here is the answer."}, "finish_reason": "stop"}],
        "usage": {},
    }

    def fake_post(url, **kwargs):
        sent.append(kwargs["json"])
        return Response(exhausted if len(sent) == 1 else answered)

    monkeypatch.setattr("toda.harness.model_client.httpx.post", fake_post)
    client = OpenAICompatibleModelClient(
        api_key="k", model="deepseek/deepseek-v4-flash-0731",
        base_url="https://example.test/v1", reasoning_effort="medium",
    )
    result = client.generate(messages=[{"role": "user", "content": "hi"}])

    # Budget exhaustion skips the continuation and resend ladder entirely:
    # the deliberation was spent rather than returned, so there is nothing to
    # continue from, and re-sending would buy another full-budget bill for
    # the same outcome.
    assert len(sent) == 2, "should go straight to the reasoning-disabled attempt"
    assert sent[0]["reasoning"] == {"effort": "medium"}
    assert sent[1]["reasoning"] == {"enabled": False}
    assert result.content == "Here is the answer."


def test_reasoning_retry_does_not_loop(monkeypatch):
    """Budget exhaustion costs exactly two calls even when the retry also
    comes back empty -- a persistently silent model must not be able to run
    up an unbounded bill."""
    from toda.harness.model_client import OpenAICompatibleModelClient

    sent: list[dict] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "", "tool_calls": []}, "finish_reason": "length"}
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 40000}},
            }

    def fake_post(url, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("toda.harness.model_client.httpx.post", fake_post)
    client = OpenAICompatibleModelClient(
        api_key="k", model="m", base_url="https://example.test/v1", reasoning_effort="high",
    )
    result = client.generate(messages=[{"role": "user", "content": "hi"}])

    assert len(sent) == 2
    assert result.content == ""
    assert result.finish_reason == "length"


def _client(**kw):
    from toda.harness.model_client import OpenAICompatibleModelClient
    return OpenAICompatibleModelClient(
        api_key="test", model="model", base_url="https://example.test", **kw
    )


def _replies(monkeypatch, payloads):
    """Serve `payloads` in order, recording each request body."""
    bodies: list[dict] = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def post(*_args, **kwargs):
        bodies.append(kwargs["json"])
        return Response(payloads[min(len(bodies) - 1, len(payloads) - 1)])

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    return bodies


def _thinking_only(details=None):
    message = {"content": "", "reasoning": "weighing the options"}
    if details:
        message["reasoning_details"] = details
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def _answer(text="the answer"):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}






def test_reasoning_is_exposed_but_never_treated_as_an_answer(monkeypatch):
    """Deliberation is carried on the response for diagnostics and replay,
    yet a thinking-only turn is still not a usable answer."""
    _replies(monkeypatch, [_thinking_only()])
    result = _client().generate(messages=[{"role": "user", "content": "hello"}])
    assert result.reasoning == "weighing the options"
    assert result.content == ""
    assert not result.tool_calls


def test_a_turn_with_nothing_at_all_is_resent_not_continued(monkeypatch):
    """No reasoning means there is nothing to continue from, so the
    identical request goes again rather than replaying an empty turn."""
    empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    bodies = _replies(monkeypatch, [empty, _answer()])
    result = _client().generate(messages=[{"role": "user", "content": "hello"}])
    assert len(bodies) == 2
    assert bodies[1]["messages"] == bodies[0]["messages"]
    assert result.content == "the answer"


def test_recovery_is_bounded_and_ends_with_reasoning_disabled(monkeypatch):
    """A model that never answers must not loop forever; the last attempt
    turns reasoning off, which degrades the turn but beats failing."""
    bodies = _replies(monkeypatch, [_thinking_only()])
    result = _client(reasoning_effort="medium").generate(
        messages=[{"role": "user", "content": "hello"}]
    )
    assert bodies[-1]["reasoning"] == {"enabled": False}
    assert len(bodies) <= 6
    assert result.content == ""


def test_a_usable_first_turn_costs_exactly_one_call(monkeypatch):
    bodies = _replies(monkeypatch, [_answer("done")])
    result = _client(reasoning_effort="medium").generate(
        messages=[{"role": "user", "content": "hello"}]
    )
    assert len(bodies) == 1
    assert result.content == "done"


def test_a_tool_call_without_content_is_already_usable(monkeypatch):
    """Content is not required when the model called a tool."""
    payload = {"choices": [{"message": {"content": "", "reasoning": "thinking",
        "tool_calls": [{"id": "c1", "function": {"name": "search",
        "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
    bodies = _replies(monkeypatch, [payload])
    result = _client(reasoning_effort="medium").generate(
        messages=[{"role": "user", "content": "hello"}]
    )
    assert len(bodies) == 1
    assert result.tool_calls[0].name == "search"



def test_a_response_echoing_the_system_prompt_is_not_usable(monkeypatch):
    """A length check alone calls 4,600 tokens of system prompt a fine
    answer. This is the guard that stops the harness delivering it."""
    system = (
        "You are Toda, a capable general-purpose coworker embedded in a company. "
        "Help with the full range of everyday knowledge work: questions, research, "
        "writing, analysis, planning, brainstorming, coding, and actions through tools."
    )
    echo = {"choices": [{"message": {"content": "# Toda\n\n" + system}, "finish_reason": "stop"}]}
    bodies = _replies(monkeypatch, [echo, _answer("a real answer")])
    result = _client().generate(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": "hi"}]
    )
    assert len(bodies) == 2, "the echo must trigger a resend"
    assert result.content == "a real answer"


def test_a_chat_template_control_token_is_not_usable(monkeypatch):
    """Seeing a turn-boundary token in output means the model is
    reproducing the template rather than answering within it."""
    bad = {"choices": [{"message": {"content": "<｜begin▁of▁sentence｜># Toda"},
                        "finish_reason": "stop"}]}
    bodies = _replies(monkeypatch, [bad, _answer("recovered")])
    result = _client().generate(messages=[{"role": "user", "content": "hi"}])
    assert len(bodies) == 2
    assert result.content == "recovered"


def test_quoting_one_instruction_back_is_still_usable(monkeypatch):
    """The echo guard must not fire on a model that legitimately refers to
    part of its instructions."""
    system = "You are Toda. " + "Follow the rules carefully. " * 20
    ok = {"choices": [{"message": {"content": "I follow the rules carefully, as instructed."}},
                       ]}
    ok["choices"][0]["finish_reason"] = "stop"
    bodies = _replies(monkeypatch, [ok])
    result = _client().generate(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": "hi"}]
    )
    assert len(bodies) == 1
    assert result.content.startswith("I follow the rules")

def _echo_client(**kw):
    """A target whose thinking mode requires reasoning_content echo-back."""
    from toda.harness.model_client import OpenAICompatibleModelClient
    return OpenAICompatibleModelClient(
        api_key="test", model="deepseek/deepseek-v4-flash-0731",
        base_url="https://openrouter.ai/api/v1", **kw
    )


def test_a_thinking_only_turn_continues_from_its_own_reasoning(monkeypatch):
    """Replaying the thinking as reasoning_content lets the model finish its
    own thought instead of losing the turn."""
    bodies = _replies(monkeypatch, [_thinking_only(), _answer()])
    result = _echo_client(reasoning_effort="medium").generate(
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}]
    )
    assert len(bodies) == 2
    replayed = bodies[1]["messages"][-1]
    assert replayed["role"] == "assistant"
    # The request field, not the response field. Sending `reasoning` here is
    # what made this a bare empty prefill and leaked the system prompt.
    assert replayed["reasoning_content"] == "weighing the options"
    assert "reasoning" not in replayed
    assert result.content == "the answer"


def test_a_strict_provider_resends_instead_of_prefilling(monkeypatch):
    """Its reasoning field is stripped before the wire, so a continuation
    would be a bare empty trailing assistant -- the exact shape that made
    the model resume the chat template and emit the system prompt."""
    bodies = _replies(monkeypatch, [_thinking_only(), _answer()])
    result = _client(reasoning_effort="medium").generate(
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}]
    )
    assert len(bodies) == 2
    assert all(m["role"] != "assistant" for m in bodies[1]["messages"])
    assert result.content == "the answer"


def test_every_request_passes_through_the_sanitizer(monkeypatch):
    """One chokepoint, not per call site -- the fork Hermes rejected."""
    bodies = _replies(monkeypatch, [_answer()])
    _echo_client().generate(messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "still there?"},
    ])
    assert bodies[0]["messages"][1]["content"] == "[response interrupted]"
