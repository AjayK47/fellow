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
