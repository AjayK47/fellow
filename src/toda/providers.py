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
    # Curated picker lists as (model_id, note) pairs, kept in the order they
    # should appear. A raw provider catalog is hundreds of entries and makes an
    # unusable dropdown, so the picker shows the curated list intersected with
    # whatever the live catalog confirms is actually available (the approach
    # Hermes' model picker uses).
    chat_catalog: tuple[tuple[str, str], ...] = ()
    embedding_catalog: tuple[tuple[str, str], ...] = ()
    # Models known to accept image input, for providers whose catalog does not
    # publish modalities. OpenRouter does publish them, so it needs none here.
    vision_models: tuple[str, ...] = ()
    image_catalog: tuple[tuple[str, str], ...] = ()
    transcription_catalog: tuple[tuple[str, str], ...] = ()
    tts_catalog: tuple[tuple[str, str], ...] = ()
    recommended_image_model: str = ""
    recommended_transcription_model: str = ""
    recommended_tts_model: str = ""
    # Roles this provider can actually serve beyond chat. Anthropic, xAI and
    # Groq have no image / speech / transcription APIs at all, so offering them
    # for those roles would only produce a runtime failure later.
    media_roles: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    # Setup-facing metadata. The onboarding picker reads these instead of
    # keeping its own parallel copy of provider facts (ported from Hermes'
    # ProviderProfile, which drives every one of its pickers the same way).
    description: str = ""
    signup_url: str = ""
    # The model the picker pre-selects. Deliberately NOT catalog entry [0]:
    # aggregators order their catalogs flagship-first, so defaulting to the
    # head of the list silently selects the most expensive model available.
    recommended_model: str = ""
    # Embeddings are a separate API surface, not a model role every provider
    # serves. Anthropic has no embeddings endpoint at all.
    supports_embeddings: bool = True
    embedding_models: tuple[str, ...] = ()
    recommended_embedding_model: str = ""

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
        capability: str = "chat",
        timeout: float = 20.0,
    ) -> list[dict[str, Any]]:
        """Return the picker list for *connection* and *capability*.

        The curated catalog on the profile is the source of order and content.
        It is intersected with the provider's live catalog so models that were
        retired upstream disappear without a Toda release, and chat models that
        do not advertise tool calling are dropped -- the agent loop cannot drive
        them and they would fail at the first tool call.
        """
        profile = self.get(connection.provider)
        # "chat" keeps the unprefixed field names it has always used; every
        # other capability follows <capability>_catalog / recommended_<x>_model,
        # so adding a role is two fields on the profile and nothing here.
        if capability == "chat":
            curated, preferred = profile.chat_catalog, profile.recommended_model
        else:
            curated = getattr(profile, f"{capability}_catalog", ())
            preferred = getattr(profile, f"recommended_{capability}_model", "")
        def entry(model_id: str, note: str, item: dict[str, Any] | None) -> dict[str, Any]:
            return {
                "id": model_id,
                "note": note,
                "free": _is_free((item or {}).get("pricing")),
                "recommended": model_id == preferred,
                "vision": _accepts_images(model_id, item, profile),
            }

        # Only chat models are intersected with the live catalog: /models lists
        # chat models only, so an embedding, image, speech or transcription
        # model that works perfectly well (OpenRouter serves /embeddings but
        # never lists them) would be filtered out of its own picker.
        if capability != "chat":
            return [entry(model_id, note, None) for model_id, note in curated]

        live = self._live_catalog(connection, profile, timeout)

        if live is None:
            # No reachable catalog (no key yet, or a provider without a REST
            # listing). The curated list is still the right answer.
            return [entry(model_id, note, None) for model_id, note in curated]

        result = [
            entry(model_id, note, live[model_id])
            for model_id, note in curated
            if model_id in live
            and (capability != "chat" or _supports_tools(live[model_id]))
        ]
        if result:
            return result
        # Curated and live disagree entirely -- a private mirror, a renamed
        # deployment, or a self-hosted endpoint. Show the live catalog rather
        # than an empty dropdown.
        return [
            entry(model_id, "", item)
            for model_id, item in live.items()
            if _matches_capability(model_id, item, capability)
        ][:200]

    def _live_catalog(
        self, connection: ProviderConnection, profile: ProviderProfile, timeout: float
    ) -> dict[str, dict[str, Any]] | None:
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
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return None
        catalog: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, str):
                catalog[item] = {"id": item}
            elif isinstance(item, dict) and item.get("id"):
                catalog[str(item["id"])] = item
        return catalog or None

    def probe(
        self,
        connection: ProviderConnection,
        model: str,
        *,
        capability: str = "chat",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make one real call so setup can prove a key and model actually work.

        Assignments that merely look right are the failure this closes: a typo'd
        key or a model the account cannot reach otherwise stays invisible until
        the first live request, long after setup said everything was fine.
        """
        profile = self.get(connection.provider)
        if profile.api_key_required and not connection.api_key:
            return {"ok": False, "error": f"{profile.display_name} requires an API key"}
        if not model.strip():
            return {"ok": False, "error": "Choose a model first"}
        if timeout is None:
            # An embedding model that has gone cold can take far longer to
            # answer its first request than a chat completion does, so it gets
            # real headroom rather than a short budget that would fail a
            # working configuration.
            timeout = 90.0 if capability == "embedding" else 25.0
        base = profile.endpoint(connection)
        headers = {**dict(profile.headers), **connection.headers}
        if connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"
            if profile.api_mode == "anthropic_messages":
                headers["x-api-key"] = connection.api_key
        if capability == "embedding":
            url, payload = f"{base}/embeddings", {"model": model, "input": "toda setup probe"}
        elif profile.api_mode == "anthropic_messages":
            url = f"{base}/messages"
            payload = {
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
        else:
            url = f"{base}/chat/completions"
            payload = {
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                # A provider that accepts the request but never routes it would
                # otherwise leave setup spinning on "Testing…" forever, so the
                # read budget is bounded and reported as its own outcome.
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        except httpx.TimeoutException:
            return {
                "ok": False,
                "error": (
                    f"{profile.display_name} did not respond within {int(timeout)}s for "
                    f"{model}. Try again — a cold model often answers on the second "
                    f"attempt — or pick a different one."
                ),
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Could not reach {profile.display_name}: {exc}"}
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": _provider_error(response, profile.display_name, model),
            }
        try:
            body = response.json()
        except ValueError:
            return {"ok": False, "error": "Provider returned a response Toda could not read"}
        if capability == "embedding":
            vectors = body.get("data") or []
            length = len(((vectors or [{}])[0] or {}).get("embedding") or [])
            if not length:
                return {"ok": False, "error": "Provider returned no embedding vector"}
            return {"ok": True, "detail": f"{length}-dimension vectors", "dimensions": length}
        return {"ok": True, "detail": f"{model} answered"}


def _provider_error(response: httpx.Response, label: str, model: str) -> str:
    """Turn a provider error into a sentence that says what to do next.

    Provider wording is often meaningless out of context -- OpenRouter answers
    a bad key with "User not found.", which reads like the model or the account
    is missing rather than the credential being wrong. The status code is the
    reliable signal, so it leads; the provider's own text is appended only when
    it adds something.
    """
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            detail = str(error["message"])
        elif isinstance(error, str):
            detail = error
        elif body.get("message"):
            detail = str(body["message"])

    code = response.status_code
    if code in {401, 403}:
        return f"{label} rejected that API key."
    if code == 404:
        return f"{label} does not offer {model} on this account."
    if code == 402:
        return f"{label} accepted the key but the account has no credit."
    if code == 429:
        return f"{label} accepted the key but is rate limiting it."
    if code >= 500:
        return f"{label} is having a problem right now (HTTP {code}). Try again shortly."
    if detail:
        return f"{label}: {detail}"
    return f"{label} returned HTTP {code}."


def _is_free(pricing: Any) -> bool:
    """True when both prompt and completion pricing are explicitly zero."""
    if not isinstance(pricing, dict):
        return False
    try:
        return (
            float(pricing.get("prompt", "0")) == 0
            and float(pricing.get("completion", "0")) == 0
        )
    except (TypeError, ValueError):
        return False


def _accepts_images(
    model_id: str, item: dict[str, Any] | None, profile: ProviderProfile
) -> bool:
    """Whether this model can read images, so no separate vision model is needed.

    Most current text models are already multimodal, and OpenRouter publishes it
    per model as ``architecture.input_modalities``. Providers that publish
    nothing fall back to the profile's curated list, so the answer is never a
    guess made from the model's name.
    """
    architecture = (item or {}).get("architecture")
    if isinstance(architecture, dict):
        modalities = architecture.get("input_modalities")
        if isinstance(modalities, list):
            return "image" in modalities
    return model_id in profile.vision_models


def _supports_tools(item: dict[str, Any]) -> bool:
    """True unless the model explicitly advertises no tool-calling support.

    Permissive when the field is absent: several OpenAI-compatible gateways
    never populate it, and treating that as "no" would empty the picker.
    """
    parameters = item.get("supported_parameters")
    if not isinstance(parameters, list):
        return True
    return "tools" in parameters


def _matches_capability(model_id: str, item: dict[str, Any], capability: str) -> bool:
    looks_like_embedding = "embed" in model_id.lower()
    if capability == "embedding":
        return looks_like_embedding
    return not looks_like_embedding and _supports_tools(item)


providers = ProviderRegistry()
for _profile in (
    ProviderProfile(
        name="openrouter",
        display_name="OpenRouter",
        description="One key for models from every major lab.",
        signup_url="https://openrouter.ai/keys",
        base_url="https://openrouter.ai/api/v1",
        chat_catalog=(
            ("anthropic/claude-sonnet-4.6", ""),
            ("anthropic/claude-opus-4.6", "most capable, highest cost"),
            ("anthropic/claude-haiku-4.5", "fastest, lowest cost"),
            ("openai/gpt-5.4", ""),
            ("openai/gpt-5.4-mini", "lower cost"),
            ("openai/gpt-5-mini", "lower cost"),
            ("google/gemini-3.1-pro-preview", ""),
            ("google/gemini-3.6-flash", "fast"),
            ("x-ai/grok-4.5", ""),
            ("deepseek/deepseek-v4-pro", ""),
            ("deepseek/deepseek-chat", "low cost"),
            ("qwen/qwen3-max", ""),
            ("moonshotai/kimi-k2", ""),
            ("z-ai/glm-4.6", ""),
            ("mistralai/mistral-large", ""),
            ("meta-llama/llama-4-maverick", ""),
        ),
        recommended_model="anthropic/claude-sonnet-4.6",
        media_roles=("image",),
        image_catalog=(
            ("google/gemini-2.5-flash-image", ""),
            ("openai/gpt-image-1", ""),
        ),
        recommended_image_model="google/gemini-2.5-flash-image",
        # Embedding models are absent from OpenRouter's /models listing, which
        # covers chat models only -- they are served all the same. Never infer
        # embedding support from that catalog.
        embedding_catalog=(
            ("qwen/qwen3-embedding-8b", ""),
            ("openai/text-embedding-3-large", "highest quality"),
            ("openai/text-embedding-3-small", "lower cost"),
        ),
        recommended_embedding_model="qwen/qwen3-embedding-8b",
    ),
    ProviderProfile(
        name="openai",
        display_name="OpenAI",
        description="GPT models and embeddings, direct.",
        signup_url="https://platform.openai.com/api-keys",
        base_url="https://api.openai.com/v1",
        chat_catalog=(
            ("gpt-5.4", ""),
            ("gpt-5.4-mini", "lower cost"),
            ("gpt-5-mini", "lower cost"),
            ("gpt-4.1", ""),
            ("gpt-4o", ""),
            ("gpt-4o-mini", "lower cost"),
        ),
        recommended_model="gpt-5.4",
        vision_models=("gpt-5.4", "gpt-5.4-mini", "gpt-5-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"),
        media_roles=("image", "transcription", "tts"),
        image_catalog=(("gpt-image-1", ""), ("dall-e-3", "")),
        recommended_image_model="gpt-image-1",
        transcription_catalog=(
            ("gpt-4o-transcribe", ""),
            ("gpt-4o-mini-transcribe", "lower cost"),
            ("whisper-1", ""),
        ),
        recommended_transcription_model="gpt-4o-transcribe",
        tts_catalog=(("gpt-4o-mini-tts", ""), ("tts-1", ""), ("tts-1-hd", "higher quality"))
        ,
        recommended_tts_model="gpt-4o-mini-tts",
        embedding_catalog=(
            ("text-embedding-3-large", "highest quality"),
            ("text-embedding-3-small", "lower cost"),
        ),
        recommended_embedding_model="text-embedding-3-large",
    ),
    ProviderProfile(
        name="anthropic",
        display_name="Anthropic",
        description="Claude models, direct. No embeddings API.",
        signup_url="https://console.anthropic.com/settings/keys",
        base_url="https://api.anthropic.com/v1",
        api_mode="anthropic_messages",
        chat_catalog=(
            ("claude-sonnet-4-6", ""),
            ("claude-opus-4-6", "most capable, highest cost"),
            ("claude-haiku-4-5-20251001", "fastest, lowest cost"),
            ("claude-sonnet-4-5-20250929", ""),
        ),
        recommended_model="claude-sonnet-4-6",
        vision_models=(
            "claude-sonnet-4-6", "claude-opus-4-6",
            "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
        ),
        headers=(("anthropic-version", "2023-06-01"),),
        supports_embeddings=False,
    ),
    ProviderProfile(
        name="xai",
        display_name="xAI",
        description="Grok models, direct.",
        signup_url="https://console.x.ai",
        base_url="https://api.x.ai/v1",
        chat_catalog=(
            ("grok-4-fast", "fast, lower cost"),
            ("grok-4", ""),
        ),
        recommended_model="grok-4-fast",
        supports_embeddings=False,
    ),
    ProviderProfile(
        name="groq",
        display_name="Groq",
        description="Open models at very high throughput.",
        signup_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        chat_catalog=(
            ("openai/gpt-oss-120b", ""),
            ("openai/gpt-oss-20b", "lower cost"),
            ("moonshotai/kimi-k2-instruct", ""),
        ),
        recommended_model="openai/gpt-oss-120b",
        supports_embeddings=False,
    ),
    ProviderProfile(
        name="deepinfra",
        display_name="DeepInfra",
        description="Open models and embeddings, pay per token.",
        signup_url="https://deepinfra.com/dash/api_keys",
        base_url="https://api.deepinfra.com/v1/openai",
        chat_catalog=(
            ("deepseek-ai/DeepSeek-V3", ""),
            ("meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", ""),
            ("Qwen/Qwen3-235B-A22B", ""),
        ),
        recommended_model="deepseek-ai/DeepSeek-V3",
        embedding_catalog=(
            ("BAAI/bge-m3", ""),
            ("Qwen/Qwen3-Embedding-8B", ""),
        ),
        recommended_embedding_model="BAAI/bge-m3",
    ),
    ProviderProfile(
        name="custom",
        display_name="Custom / local OpenAI-compatible",
        description="Ollama, vLLM, LM Studio, or any OpenAI-compatible endpoint.",
        base_url="http://127.0.0.1:11434/v1",
        api_key_required=False,
        media_roles=("image", "transcription", "tts"),
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
