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

    assert len(sent) == 2, "should retry exactly once"
    assert sent[0]["reasoning"] == {"effort": "medium"}
    assert sent[1]["reasoning"] == {"enabled": False}
    assert result.content == "Here is the answer."


def test_reasoning_retry_does_not_loop(monkeypatch):
    """The retry never retries itself, so a persistently silent model fails fast."""
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


def test_empty_reasoning_turn_is_retried_with_reasoning_disabled(monkeypatch):
    """A reasoning model can end its turn having produced only thinking:
    no content, no tool call, the output living in the `reasoning` field
    this contract does not surface. Observed in production against
    DeepSeek with finish_reason "stop", which an earlier "length"-only
    guard let straight through to a failed Task."""
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
        if len(bodies) == 1:
            return Response(
                {
                    "choices": [
                        {
                            "message": {"content": "", "reasoning": "thought hard"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        return Response(
            {"choices": [{"message": {"content": "the answer"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    result = OpenAICompatibleModelClient(
        api_key="test",
        model="model",
        base_url="https://example.test",
        reasoning_effort="medium",
    ).generate(messages=[{"role": "user", "content": "hello"}])

    assert len(bodies) == 2, "an empty reasoning turn must be retried once"
    assert bodies[0]["reasoning"] == {"effort": "medium"}
    assert bodies[1]["reasoning"] == {"enabled": False}
    assert result.content == "the answer"


def test_the_retry_happens_only_once(monkeypatch):
    """Two empty turns in a row surface the failure rather than looping."""
    calls = {"n": 0}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            calls["n"] += 1
            return {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}

    monkeypatch.setattr("toda.harness.model_client.httpx.post", lambda *a, **k: Response())
    result = OpenAICompatibleModelClient(
        api_key="test",
        model="model",
        base_url="https://example.test",
        reasoning_effort="medium",
    ).generate(messages=[{"role": "user", "content": "hello"}])
    assert calls["n"] == 2
    assert result.content == ""
