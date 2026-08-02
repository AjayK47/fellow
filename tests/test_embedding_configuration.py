"""Model and embedding configuration are independent of each other."""

from __future__ import annotations

from docket.config import Settings


def test_model_and_embedding_api_keys_are_configured_independently(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-model-key")
    monkeypatch.setenv("DOCKET_EMBEDDING_API_KEY", "test-embedding-key")
    monkeypatch.setenv("DOCKET_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
    monkeypatch.setenv("DOCKET_EMBEDDING_PROVIDER", "openrouter")
    settings = Settings(_env_file=None)
    assert settings.model_api_key == "test-model-key"
    assert settings.embedding_api_key == "test-embedding-key"
    assert settings.embedding_model == "qwen/qwen3-embedding-8b"
    assert settings.embedding_provider == "openrouter"
