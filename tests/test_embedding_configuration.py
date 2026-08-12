"""Provider credentials are shared while model roles remain independent."""

from __future__ import annotations

from toda.config import Settings


def test_provider_credentials_are_stored_once_and_roles_reference_them(monkeypatch):
    monkeypatch.setenv(
        "TODA_PROVIDER_CONNECTIONS",
        '{"openrouter-company":{"provider":"openrouter","api_key":"shared-key"}}',
    )
    monkeypatch.setenv(
        "TODA_MODEL_ASSIGNMENTS",
        '{"main":{"connection":"openrouter-company","model":"openai/gpt-5.4"},'
        '"embedding":{"connection":"openrouter-company","model":"qwen/qwen3-embedding-8b"}}',
    )
    settings = Settings(_env_file=None)
    assert settings.provider_connections["openrouter-company"].api_key == "shared-key"
    assert settings.model_assignments["main"].connection == "openrouter-company"
    assert settings.model_assignments["embedding"].model == "qwen/qwen3-embedding-8b"
