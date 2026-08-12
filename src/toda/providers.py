"""Provider connections, model assignments, catalogs, and runtime resolution.

The provider is the credential boundary.  Models and capabilities reference a
connection instead of carrying their own API keys, matching Hermes' provider
profile + model assignment architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field


ApiMode = Literal["chat_completions", "anthropic_messages"]


class ProviderConnection(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ModelAssignment(BaseModel):
    connection: str
    model: str


class ToolConnection(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    display_name: str
    base_url: str
    api_mode: ApiMode = "chat_completions"
    models_path: str = "/models"
    api_key_required: bool = True
    fallback_models: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()

    def endpoint(self, connection: ProviderConnection) -> str:
        return (connection.base_url or self.base_url).rstrip("/")


class ProviderRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfile] = {}

    def register(self, profile: ProviderProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ProviderProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise ValueError(f"Unknown model provider: {name}") from exc

    def list(self) -> tuple[ProviderProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: item.display_name))

    def models(
        self,
        connection: ProviderConnection,
        *,
        timeout: float = 20.0,
    ) -> list[dict[str, Any]]:
        profile = self.get(connection.provider)
        url = f"{profile.endpoint(connection)}{profile.models_path}"
        headers = {**dict(profile.headers), **connection.headers}
        if connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"
            if profile.api_mode == "anthropic_messages":
                headers["x-api-key"] = connection.api_key
        try:
            response = httpx.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("data", payload) if isinstance(payload, dict) else payload
            if isinstance(raw, list):
                result = []
                for item in raw:
                    if isinstance(item, str):
                        result.append({"id": item})
                    elif isinstance(item, dict) and item.get("id"):
                        result.append(item)
                if result:
                    return result
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return [{"id": model, "fallback": True} for model in profile.fallback_models]


providers = ProviderRegistry()
for _profile in (
    ProviderProfile(
        name="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        fallback_models=("gpt-5.4", "gpt-5-mini", "text-embedding-3-large"),
    ),
    ProviderProfile(
        name="openrouter",
        display_name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        fallback_models=(
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4",
            "google/gemini-3.1-pro-preview",
            "qwen/qwen3-embedding-8b",
        ),
    ),
    ProviderProfile(
        name="anthropic",
        display_name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        api_mode="anthropic_messages",
        models_path="/models",
        fallback_models=("claude-sonnet-4-6", "claude-opus-4-6"),
        headers=(("anthropic-version", "2023-06-01"),),
    ),
    ProviderProfile(
        name="xai",
        display_name="xAI",
        base_url="https://api.x.ai/v1",
        fallback_models=("grok-4", "grok-4-fast"),
    ),
    ProviderProfile(
        name="groq",
        display_name="Groq",
        base_url="https://api.groq.com/openai/v1",
        fallback_models=("openai/gpt-oss-120b", "whisper-large-v3-turbo"),
    ),
    ProviderProfile(
        name="deepinfra",
        display_name="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
    ),
    ProviderProfile(
        name="custom",
        display_name="Custom / local OpenAI-compatible",
        base_url="http://127.0.0.1:11434/v1",
        api_key_required=False,
    ),
):
    providers.register(_profile)


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    assignment: ModelAssignment
    connection: ProviderConnection
    profile: ProviderProfile


class ProviderResolver:
    def __init__(
        self,
        connections: dict[str, ProviderConnection],
        assignments: dict[str, ModelAssignment],
    ) -> None:
        self.connections = connections
        self.assignments = assignments

    def resolve(self, role: str, *, fallback_role: str | None = None) -> ResolvedModel | None:
        assignment = self.assignments.get(role)
        if assignment is None and fallback_role:
            assignment = self.assignments.get(fallback_role)
        if assignment is None:
            return None
        try:
            connection = self.connections[assignment.connection]
        except KeyError as exc:
            raise ValueError(
                f"Model role {role!r} references missing connection {assignment.connection!r}"
            ) from exc
        profile = providers.get(connection.provider)
        if profile.api_key_required and not connection.api_key:
            raise ValueError(
                f"Provider connection {assignment.connection!r} requires an API key"
            )
        return ResolvedModel(role, assignment, connection, profile)
