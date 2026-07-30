"""Embedding configuration recognizes standard OpenRouter environment names."""

from __future__ import annotations

from company_brain.config import Settings


def test_standard_openrouter_environment_names_are_supported(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("COMPANY_BRAIN_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
    monkeypatch.setenv("COMPANY_BRAIN_EMBEDDING_PROVIDER", "openrouter")
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key == "test-key"
    assert settings.embedding_model == "qwen/qwen3-embedding-8b"
    assert settings.embedding_provider == "openrouter"
