from __future__ import annotations

import pytest

from toda.providers import ModelAssignment, ProviderConnection, ProviderResolver, providers


def test_live_model_catalog_normalizes_openai_compatible_response(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "model-b"}, {"id": "model-a", "context_length": 42}]}

    monkeypatch.setattr("toda.providers.httpx.get", lambda *args, **kwargs: Response())
    models = providers.models(
        ProviderConnection(provider="openrouter", api_key="secret")
    )
    assert [item["id"] for item in models] == ["model-b", "model-a"]
    assert models[1]["context_length"] == 42


def test_model_catalog_falls_back_when_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "toda.providers.httpx.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")),
    )
    models = providers.models(ProviderConnection(provider="anthropic", api_key="secret"))
    assert models[0] == {"id": "claude-sonnet-4-6", "fallback": True}


def test_resolver_rejects_assignment_to_missing_connection():
    resolver = ProviderResolver(
        {}, {"main": ModelAssignment(connection="missing", model="some-model")}
    )
    with pytest.raises(ValueError, match="missing connection"):
        resolver.resolve("main")


def test_custom_local_provider_does_not_require_api_key():
    resolver = ProviderResolver(
        {"ollama": ProviderConnection(provider="custom", base_url="http://localhost:11434/v1")},
        {"main": ModelAssignment(connection="ollama", model="qwen3")},
    )
    assert resolver.resolve("main").assignment.model == "qwen3"
