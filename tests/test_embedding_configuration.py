"""Provider credentials are shared while model roles remain independent."""

from __future__ import annotations

import json

import pytest

from toda.config import ConfigError, load_settings


def _write(tmp_path, payload: dict) -> "object":
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    return path


def test_provider_credentials_are_stored_once_and_roles_reference_them(tmp_path):
    path = _write(
        tmp_path,
        {
            "models": {
                "providers": {
                    "openrouter-company": {
                        "provider": "openrouter",
                        "api_key": "shared-key",
                    }
                },
                "assignments": {
                    "main": {
                        "connection": "openrouter-company",
                        "model": "openai/gpt-5.4",
                    },
                    "embedding": {
                        "connection": "openrouter-company",
                        "model": "qwen/qwen3-embedding-8b",
                    },
                },
            }
        },
    )
    settings = load_settings(path)
    assert settings.models.providers["openrouter-company"].api_key == "shared-key"
    assert settings.models.assignments["main"].connection == "openrouter-company"
    assert settings.models.assignments["embedding"].model == "qwen/qwen3-embedding-8b"


def test_a_missing_config_file_yields_defaults(tmp_path):
    """A fresh checkout must import without a config file; `toda init` writes it."""
    settings = load_settings(tmp_path / "absent.json")
    assert settings.organization.id == "org_acme"
    assert settings.models.providers == {}


def test_a_misspelled_key_is_rejected_rather_than_silently_dropped(tmp_path):
    """The failure mode of the old .env: half its keys were not settings at
    all and were discarded without a word."""
    path = _write(tmp_path, {"slack": {"bot_tokn": "xoxb-typo"}})
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    assert "bot_tokn" in str(excinfo.value)


def test_malformed_json_fails_loudly_instead_of_falling_back(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load_settings(path)
