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
