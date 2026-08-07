"""Provider requests use only required authentication and content headers."""

from __future__ import annotations

from docket.harness.model_client import OpenRouterModelClient


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

    monkeypatch.setattr("docket.harness.model_client.httpx.post", post)
    OpenRouterModelClient(api_key="test", model="model", base_url="https://example.test").generate(
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

    monkeypatch.setattr("docket.harness.model_client.httpx.post", post)
    OpenRouterModelClient(
        api_key="test",
        model="model",
        base_url="https://example.test",
        reasoning_effort="medium",
    ).generate(messages=[{"role": "user", "content": "hello"}])
    assert captured["json"]["reasoning"] == {"effort": "medium"}
