from __future__ import annotations

import pytest

from toda.providers import (
    ModelAssignment,
    ProviderConnection,
    ProviderProfile,
    ProviderRegistry,
    ProviderResolver,
    providers,
)
from toda.services.models_catalog import ModelsCatalogService


def test_probe_translates_a_rejected_key_into_a_usable_sentence(monkeypatch):
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


def test_injected_fixture_exact_role_filtering(tmp_path):
    cache_path = tmp_path / "models_catalog.json"
    fixture_payload = {
        "providers": {
            "mocklab": {
                "name": "MockLab",
                "models": {
                    "main-chat": {
                        "id": "main-chat",
                        "name": "Main Chat Pro",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "tool_call": True,
                        "context_length": 128000,
                        "pricing": {"input": 1.0, "output": 2.0},
                    },
                    "vision-only": {
                        "id": "vision-only",
                        "name": "Vision Only",
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "tool_call": False,
                        "context_length": 64000,
                    },
                    "embed-model": {
                        "id": "embed-model",
                        "name": "Embed Model",
                        "capabilities": ["embedding"],
                    },
                    "transcribe-model": {
                        "id": "transcribe-model",
                        "name": "Transcribe Model",
                        "modalities": {"input": ["audio"], "output": ["text"]},
                    },
                    "tts-model": {
                        "id": "tts-model",
                        "name": "TTS Model",
                        "modalities": {"input": ["text"], "output": ["audio"]},
                    },
                    "toolless-chat": {
                        "id": "toolless-chat",
                        "name": "Toolless Chat",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "tool_call": False,
                    }
                }
            }
        }
    }
    catalog_service = ModelsCatalogService(cache_path=cache_path, enabled=False)
    catalog_service._models, catalog_service._metadata = catalog_service._parse(fixture_payload, source="fixture")

    registry = ProviderRegistry()
    registry._catalog = catalog_service
    registry.register(
        ProviderProfile(
            name="mocklab",
            display_name="MockLab",
            base_url="https://mocklab.test/v1",
            media_roles=("image", "transcription", "tts"),
        )
    )
    conn = ProviderConnection(provider="mocklab", api_key="sk-mock")

    # Main role: requires tool_calling + text output
    main_res = registry.models(conn, role="main")
    assert [m["id"] for m in main_res] == ["main-chat"]
    assert main_res[0]["recommended"] is True
    assert main_res[0]["ranking_reason"]["code"] == "alphabetical"
    assert main_res[0]["ranking_reason"]["rank"] == 1

    # Vision role: requires image input + text output
    vision_res = registry.models(conn, role="vision")
    assert [m["id"] for m in vision_res] == ["vision-only"]

    # Transcription role: requires audio input + text output
    trans_res = registry.models(conn, role="transcription")
    assert [m["id"] for m in trans_res] == ["transcribe-model"]

    # TTS role: requires text input + audio output
    tts_res = registry.models(conn, role="tts")
    assert [m["id"] for m in tts_res] == ["tts-model"]

    # Embedding role: requires embedding capability
    embed_res = registry.models(conn, capability="embedding")
    assert [m["id"] for m in embed_res] == ["embed-model"]

    # Known upstream toolless model cannot be forced into main role as custom unknown
    main_ineligible = registry.models(conn, role="main", explicit_model="toolless-chat")
    assert not any(m["id"] == "toolless-chat" for m in main_ineligible)

    # Genuinely absent model is accepted as explicit_custom_unknown and NOT recommended
    custom_res = registry.models(conn, role="main", explicit_model="genuinely-absent-custom")
    custom_entry = next((m for m in custom_res if m["id"] == "genuinely-absent-custom"), None)
    assert custom_entry is not None
    assert custom_entry["eligibility"] == "explicit_custom_unknown"
    assert custom_entry["recommended"] is False


def test_policy_allow_deny_and_budget_constraints(tmp_path):
    cache_path = tmp_path / "models_catalog.json"
    fixture_payload = {
        "providers": {
            "mocklab": {
                "models": {
                    "model-a": {"id": "model-a", "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]}, "pricing": {"input": 1.0, "output": 2.0}, "context_length": 32000},
                    "model-b": {"id": "model-b", "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]}, "pricing": {"input": 5.0, "output": 10.0}, "context_length": 128000},
                    "model-c": {"id": "model-c", "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]}, "pricing": {"input": 0.5, "output": 1.0}, "context_length": 8000},
                }
            }
        }
    }
    catalog_service = ModelsCatalogService(cache_path=cache_path, enabled=False)
    catalog_service._models, catalog_service._metadata = catalog_service._parse(fixture_payload, source="fixture")

    registry = ProviderRegistry()
    registry._catalog = catalog_service
    registry.register(ProviderProfile(name="mocklab", display_name="MockLab", base_url="https://mocklab.test/v1"))
    conn = ProviderConnection(provider="mocklab", api_key="sk-mock")

    # Deny list
    denied = registry.models(conn, role="main", deny_models=["model-a"])
    assert "model-a" not in [m["id"] for m in denied]
    assert set(m["id"] for m in denied) == {"model-b", "model-c"}

    # Allow list preference
    allowed = registry.models(conn, role="main", allow_models=["model-b", "model-a"])
    assert [m["id"] for m in allowed] == ["model-b", "model-a"]
    assert allowed[0]["id"] == "model-b"
    assert allowed[0]["recommended"] is True

    # Price budget
    budgeted = registry.models(conn, role="main", max_input_price_per_1m=2.0)
    assert set(m["id"] for m in budgeted) == {"model-a", "model-c"}

    # Min context
    ctx_filtered = registry.models(conn, role="main", min_context=60000)
    assert [m["id"] for m in ctx_filtered] == ["model-b"]


def test_production_registrations_contain_no_curated_model_lists():
    for profile in providers.list():
        for forbidden in (
            "chat_catalog",
            "embedding_catalog",
            "vision_models",
            "image_catalog",
            "transcription_catalog",
            "tts_catalog",
            "recommended_model",
        ):
            assert not hasattr(profile, forbidden), f"{profile.name} has forbidden attribute {forbidden}"


def test_picker_rejects_models_without_a_runtime_transport(tmp_path):
    service = ModelsCatalogService(cache_path=tmp_path / "catalog.json", enabled=False)
    service._models, service._metadata = service._parse(
        {
            "providers": {
                "mocklab": {
                    "models": {
                        "embed": {"id": "embed", "capabilities": ["embedding"]},
                        "image": {
                            "id": "image",
                            "modalities": {"input": ["text"], "output": ["image"]},
                        },
                    }
                }
            }
        },
        source="fixture",
    )
    registry = ProviderRegistry(catalog=service)
    registry.register(
        ProviderProfile(
            name="mocklab",
            display_name="MockLab",
            base_url="https://mocklab.test/v1",
            embedding_supported=False,
        )
    )
    connection = ProviderConnection(provider="mocklab", api_key="key")

    assert registry.models(connection, role="embedding", capability="embedding") == []
    assert registry.models(connection, role="image", capability="image") == []


def test_picker_rejects_invalid_role_and_capability():
    registry = ProviderRegistry(catalog=None)
    registry.register(
        ProviderProfile(name="mocklab", display_name="MockLab", base_url="https://mocklab.test/v1")
    )
    connection = ProviderConnection(provider="mocklab", api_key="key")

    with pytest.raises(ValueError, match="Invalid role"):
        registry.models(connection, role="not-a-role")
    with pytest.raises(ValueError, match="Invalid capability"):
        registry.models(connection, capability="not-a-capability")
