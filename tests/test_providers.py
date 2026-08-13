from __future__ import annotations

import pytest

from toda.providers import ModelAssignment, ProviderConnection, ProviderResolver, providers


def _catalog(monkeypatch, payload):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr("toda.providers.httpx.get", lambda *args, **kwargs: Response())


def test_chat_catalog_keeps_curated_order_and_drops_uncurated_models(monkeypatch):
    """The picker shows the curated list, not the provider's whole catalog."""
    _catalog(
        monkeypatch,
        {
            "data": [
                {"id": "openai/gpt-5.4"},
                {"id": "some-vendor/never-curated-model"},
                {"id": "anthropic/claude-sonnet-4.6"},
            ]
        },
    )
    models = providers.models(ProviderConnection(provider="openrouter", api_key="secret"))
    ids = [item["id"] for item in models]
    assert ids == ["anthropic/claude-sonnet-4.6", "openai/gpt-5.4"]
    assert "some-vendor/never-curated-model" not in ids
    assert models[0]["recommended"] is True


def test_chat_catalog_hides_models_without_tool_calling(monkeypatch):
    """A model that cannot call tools would fail at the agent's first tool call."""
    _catalog(
        monkeypatch,
        {
            "data": [
                {"id": "anthropic/claude-sonnet-4.6", "supported_parameters": ["tools"]},
                {"id": "openai/gpt-5.4", "supported_parameters": ["temperature"]},
            ]
        },
    )
    ids = [
        item["id"]
        for item in providers.models(ProviderConnection(provider="openrouter", api_key="k"))
    ]
    assert ids == ["anthropic/claude-sonnet-4.6"]


def test_embedding_catalog_is_not_filtered_by_the_chat_listing(monkeypatch):
    """/models lists chat models only, so embeddings must not be intersected."""
    _catalog(monkeypatch, {"data": [{"id": "anthropic/claude-sonnet-4.6"}]})
    ids = [
        item["id"]
        for item in providers.models(
            ProviderConnection(provider="openrouter", api_key="k"), capability="embedding"
        )
    ]
    assert "qwen/qwen3-embedding-8b" in ids


def test_model_catalog_falls_back_when_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "toda.providers.httpx.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")),
    )
    models = providers.models(ProviderConnection(provider="anthropic", api_key="secret"))
    assert models[0]["id"] == "claude-sonnet-4-6"
    assert models[0]["recommended"] is True


def test_probe_translates_a_rejected_key_into_a_usable_sentence(monkeypatch):
    """OpenRouter answers a bad key with "User not found.", which reads as if
    the model or account is missing rather than the credential being wrong."""

    class Response:
        status_code = 401

        def json(self):
            return {"error": {"message": "User not found."}}

    monkeypatch.setattr("toda.providers.httpx.post", lambda *args, **kwargs: Response())
    result = providers.probe(
        ProviderConnection(provider="openrouter", api_key="bad"), "anthropic/claude-sonnet-4.6"
    )
    assert result == {"ok": False, "error": "OpenRouter rejected that API key."}


def test_probe_names_the_model_when_the_provider_does_not_offer_it(monkeypatch):
    class Response:
        status_code = 404

        def json(self):
            return {}

    monkeypatch.setattr("toda.providers.httpx.post", lambda *args, **kwargs: Response())
    result = providers.probe(
        ProviderConnection(provider="openrouter", api_key="k"), "vendor/retired-model"
    )
    assert result["ok"] is False
    assert "vendor/retired-model" in result["error"]


def test_probe_requires_a_key_before_calling_the_provider():
    result = providers.probe(ProviderConnection(provider="openrouter"), "anything")
    assert result["ok"] is False
    assert "API key" in result["error"]


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
