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


ApiMode = Literal["chat_completions", "anthropic_messages", "concentrate_responses"]


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
    embedding_supported: bool = True
    media_roles: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    description: str = ""
    signup_url: str = ""

    def endpoint(self, connection: ProviderConnection) -> str:
        return (connection.base_url or self.base_url).rstrip("/")


# Canonical role and capability contracts — import these in routes.py
# instead of duplicating the sets.
VALID_ROLES: frozenset[str] = frozenset({
    "main", "attention", "extraction", "summary", "review",
    "vision", "embedding", "image", "transcription", "tts",
})
VALID_CAPABILITIES: frozenset[str] = frozenset({
    "chat", "embedding", "image", "transcription", "tts",
})


def capability_for(*, role: str, capability: str) -> str:
    """Return the runtime transport required by a picker request."""
    if role in {"embedding", "image", "transcription", "tts"}:
        return role
    return capability


class ProviderRegistry:
    def __init__(self, *, catalog: Any | None = None) -> None:
        self._profiles: dict[str, ProviderProfile] = {}
        self._catalog = catalog

    def set_catalog(self, catalog: Any | None) -> None:
        """Attach the process' pinned models.dev metadata service."""
        self._catalog = catalog

    def _get_catalog(self) -> Any:
        if self._catalog is not None:
            return self._catalog
        try:
            from toda.services.models_catalog import models_catalog
            return models_catalog
        except Exception:
            return None

    def register(self, profile: ProviderProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ProviderProfile:
        try:
            return self._profiles[name]
        except KeyError:
            raise KeyError(f"Unknown provider '{name}'") from None

    def list(self) -> list[ProviderProfile]:
        return list(self._profiles.values())

    def models(
        self,
        connection: ProviderConnection,
        *,
        capability: str = "chat",
        timeout: float = 20.0,
        explicit_model: str | None = None,
        role: str = "main",
        policy: Any = None,
        allow_models: list[str] | None = None,
        deny_models: list[str] | None = None,
        min_context: int | None = None,
        max_input_price_per_1m: float | None = None,
        max_output_price_per_1m: float | None = None,
        allow_unknown_price: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the picker list for *connection* and *capability* using models.dev."""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}")
        if capability not in VALID_CAPABILITIES:
            raise ValueError(f"Invalid capability '{capability}'. Must be one of: {', '.join(sorted(VALID_CAPABILITIES))}")

        preference_order: list[str] | None = None
        if policy is not None:
            allow_models = allow_models if allow_models is not None else getattr(policy, "allow_models", None)
            deny_models = deny_models if deny_models is not None else getattr(policy, "deny_models", None)
            min_context = min_context if min_context is not None else getattr(policy, "min_context", None)
            max_input_price_per_1m = max_input_price_per_1m if max_input_price_per_1m is not None else getattr(policy, "max_input_price_per_1m", None)
            max_output_price_per_1m = max_output_price_per_1m if max_output_price_per_1m is not None else getattr(policy, "max_output_price_per_1m", None)
            allow_unknown_price = allow_unknown_price or getattr(policy, "allow_unknown_price", False)
            preference_order = getattr(policy, "preference_order", None)

        profile = self.get(connection.provider)
        transport_capability = capability_for(role=role, capability=capability)
        if transport_capability == "embedding" and (
            not profile.embedding_supported or profile.api_mode != "chat_completions"
        ):
            return []
        if transport_capability in {"image", "transcription", "tts"} and (
            transport_capability not in profile.media_roles
        ):
            return []
        catalog = self._get_catalog()
        candidates: list[dict[str, Any]] = []
        if catalog is not None:
            try:
                candidates = catalog.models(connection.provider)
            except Exception:
                candidates = []

        all_catalog_model_ids = {str(m.get("model_id") or "") for m in candidates if m.get("model_id")}

        # Filter out deprecated / retired lifecycle states
        active = [
            m for m in candidates
            if str(m.get("lifecycle") or "").lower() not in {"deprecated", "retired", "shutdown", "discontinued"}
        ]

        # Role and capability filtering - exact directional evidence required
        eligible: list[dict[str, Any]] = []

        for item in active:
            caps = set(item.get("capabilities") or [])
            in_mods = set(item.get("input_modalities") or [])
            out_mods = set(item.get("output_modalities") or [])

            # Modality and capability evidence without assuming missing fields
            if capability == "embedding" or role == "embedding":
                if "embedding" in caps:
                    eligible.append(item)
            elif capability == "image" or role == "image":
                if "image" in out_mods or "image_generation" in caps:
                    eligible.append(item)
            elif capability == "transcription" or role == "transcription":
                if "audio" in in_mods and "text" in out_mods:
                    eligible.append(item)
            elif capability == "tts" or role == "tts":
                if "text" in in_mods and "audio" in out_mods:
                    eligible.append(item)
            elif role == "vision":
                if "image" in in_mods and "text" in out_mods:
                    eligible.append(item)
            elif role == "main":
                if "tool_calling" in caps and "text" in out_mods:
                    if min_context is not None and (item.get("context_length") or 0) < min_context:
                        continue
                    eligible.append(item)
            elif role in {"attention", "extraction", "summary", "review"}:
                if "text" in out_mods:
                    eligible.append(item)

        # Policy allow / deny / budget filtering
        denied_set = set(deny_models or [])
        allowed_set = set(allow_models) if allow_models is not None else None

        filtered_active: list[dict[str, Any]] = []
        for item in eligible:
            mid = str(item.get("model_id") or "")
            qmid = f"{connection.provider}/{mid}"
            if mid in denied_set or qmid in denied_set:
                continue
            if allowed_set is not None and mid not in allowed_set and qmid not in allowed_set:
                continue
            if max_input_price_per_1m is not None:
                cost = item.get("input_usd_per_1m")
                if cost is None:
                    if not allow_unknown_price:
                        continue
                elif cost > max_input_price_per_1m:
                    continue
            if max_output_price_per_1m is not None:
                cost = item.get("output_usd_per_1m")
                if cost is None:
                    if not allow_unknown_price:
                        continue
                elif cost > max_output_price_per_1m:
                    continue
            filtered_active.append(item)

        # Provider /models is authoritative for chat-style model transports.
        # Media registries use separate endpoints and are filtered by the
        # explicit transport contract above.
        live = (
            self._live_catalog(connection, profile, timeout=min(timeout, 5.0))
            if transport_capability == "chat"
            else None
        )
        live_ids = set(live.keys()) if live is not None else None

        results: list[dict[str, Any]] = []

        for item in filtered_active:
            model_id = str(item.get("model_id") or "")
            if not model_id:
                continue

            if live_ids is not None:
                # Live list is reachable, model must be present in live list
                if model_id not in live_ids and f"{connection.provider}/{model_id}" not in live_ids:
                    continue
                live_item = live.get(model_id) or live.get(f"{connection.provider}/{model_id}") or {}
                if role == "main" and not _supports_tools(live_item):
                    continue
                source = "models.dev+provider_live"
                entry_meta = {**item, "live": live_item}
            else:
                source = "models.dev"
                entry_meta = item

            results.append(
                {
                    "id": model_id,
                    "name": item.get("name") or model_id,
                    "note": "models.dev",
                    "free": (item.get("input_usd_per_1m") == 0 and item.get("output_usd_per_1m") == 0),
                    "recommended": False,
                    "vision": "vision" in (item.get("capabilities") or []) or "image" in (item.get("input_modalities") or []),
                    "source": source,
                    "metadata": entry_meta,
                    "eligibility": "catalog_eligible",
                    "eligibility_reason": {
                        "code": "catalog_and_policy_eligible",
                        "display": "Meets transport, modality, lifecycle, and organization policy requirements",
                    },
                    "ranking_reason": "",
                    "fact_sources": {
                        "capabilities": "models.dev",
                        "limits": "models.dev",
                        "pricing": "models.dev",
                        "lifecycle": "models.dev",
                    },
                }
            )

        # Preserve explicit custom model ONLY when absent from upstream models.dev
        if explicit_model:
            explicit_id = str(explicit_model)
            if explicit_id not in all_catalog_model_ids:
                live_item = (live or {}).get(explicit_id)
                results.append(
                    {
                        "id": explicit_id,
                        "name": explicit_id,
                        "note": "explicit custom model; metadata unknown",
                        "free": False,
                        "recommended": False,
                        "vision": False,
                        "source": "custom",
                        "metadata": {"model_id": explicit_id, "metadata_unknown": True, "live": live_item},
                        "eligibility": "explicit_custom_unknown",
                        "eligibility_reason": {
                            "code": "explicit_model_absent_upstream",
                            "display": "Explicit custom model is absent from models.dev",
                        },
                        "ranking_reason": {
                            "code": "custom_unverified",
                            "display": "Custom model without upstream verification",
                        },
                        "fact_sources": {
                            "capabilities": "unknown",
                            "limits": "unknown",
                            "pricing": "unknown",
                            "lifecycle": "unknown",
                        },
                    }
                )

        # Deterministic ranking & recommendation flag
        results = self._rank_models(
            results,
            role=role,
            allow_models=allow_models,
            preference_order=preference_order,
        )
        return results

    def _rank_models(
        self,
        models: list[dict[str, Any]],
        *,
        role: str,
        allow_models: list[str] | None = None,
        preference_order: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        pref_indices = {name: idx for idx, name in enumerate(preference_order or [])}
        allow_indices = {name: idx for idx, name in enumerate(allow_models or [])}

        def _sort_key(item: dict[str, Any]) -> tuple:
            meta = item.get("metadata") or {}
            is_custom = 1 if item.get("eligibility") == "explicit_custom_unknown" else 0
            pref_order = pref_indices.get(item["id"], pref_indices.get(meta.get("model_id", ""), 999))
            allow_order = allow_indices.get(item["id"], allow_indices.get(meta.get("model_id", ""), 999))
            # Sort only by organization preference and allow-list order.
            # No global quality ranking — Toda must not invent universal preferences.
            return (is_custom, pref_order, allow_order, item["id"])

        sorted_models = sorted(models, key=_sort_key)
        for rank, item in enumerate(sorted_models, start=1):
            meta = item.get("metadata") or {}
            model_id = item["id"]

            if item.get("eligibility") == "explicit_custom_unknown":
                item["recommended"] = False
                item["ranking_reason"] = {
                    "code": "custom_unverified",
                    "display": "Custom model without upstream verification",
                }
            else:
                # Determine the reason code for this ranking position
                if model_id in pref_indices or meta.get("model_id", "") in pref_indices:
                    code = "org_preference"
                elif model_id in allow_indices or meta.get("model_id", "") in allow_indices:
                    code = "allow_list"
                else:
                    code = "alphabetical"

                item["ranking_reason"] = {
                    "code": code,
                    "rank": rank,
                    "display": f"Rank {rank}: {code}",
                }
                item["recommended"] = (rank == 1)

        return sorted_models

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
        """Make one real call so setup can prove a key and model actually work."""
        profile = self.get(connection.provider)
        if profile.api_key_required and not connection.api_key:
            return {"ok": False, "error": f"{profile.display_name} requires an API key"}
        if not model.strip():
            return {"ok": False, "error": "Choose a model first"}
        if timeout is None:
            timeout = 90.0 if capability == "embedding" else 25.0
        if capability in {"image", "transcription", "tts"}:
            if capability not in profile.media_roles:
                return {
                    "ok": False,
                    "error": f"{profile.display_name} has no {capability} transport",
                }
            live = self._live_catalog(connection, profile, timeout=min(timeout, 10.0))
            if live is None:
                return {
                    "ok": False,
                    "error": f"{profile.display_name} model availability probe failed",
                }
            if model not in live and f"{connection.provider}/{model}" not in live:
                return {
                    "ok": False,
                    "error": f"{profile.display_name} does not advertise {model}",
                }
            return {
                "ok": True,
                "detail": f"{model} is advertised for the {capability} transport",
                "probe": "authenticated_model_catalog",
            }
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


def _supports_tools(item: dict[str, Any]) -> bool:
    parameters = item.get("supported_parameters")
    if not isinstance(parameters, list):
        return True
    return "tools" in parameters


providers = ProviderRegistry()
for _profile in (
    ProviderProfile(
        name="openrouter",
        display_name="OpenRouter",
        description="One key for models from every major lab.",
        signup_url="https://openrouter.ai/keys",
        base_url="https://openrouter.ai/api/v1",
        media_roles=("image", "transcription", "tts"),
    ),
    ProviderProfile(
        name="openai",
        display_name="OpenAI",
        description="GPT models and embeddings, direct.",
        signup_url="https://platform.openai.com/api-keys",
        base_url="https://api.openai.com/v1",
        media_roles=("image", "transcription", "tts"),
    ),
    ProviderProfile(
        name="concentrate",
        display_name="Concentrate",
        description="Unified Responses API across providers.",
        signup_url="https://concentrate.ai",
        base_url="https://api.concentrate.ai/v1",
        api_mode="concentrate_responses",
        embedding_supported=False,
    ),
    ProviderProfile(
        name="anthropic",
        display_name="Anthropic",
        description="Claude models, direct. No embeddings API.",
        signup_url="https://console.anthropic.com/settings/keys",
        base_url="https://api.anthropic.com/v1",
        api_mode="anthropic_messages",
        embedding_supported=False,
        headers=(("anthropic-version", "2023-06-01"),),
    ),
    ProviderProfile(
        name="xai",
        display_name="xAI",
        description="Grok models, direct.",
        signup_url="https://console.x.ai",
        base_url="https://api.x.ai/v1",
        embedding_supported=False,
    ),
    ProviderProfile(
        name="groq",
        display_name="Groq",
        description="Open models at very high throughput.",
        signup_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        embedding_supported=False,
    ),
    ProviderProfile(
        name="deepinfra",
        display_name="DeepInfra",
        description="Open models and embeddings, pay per token.",
        signup_url="https://deepinfra.com/dash/api_keys",
        base_url="https://api.deepinfra.com/v1/openai",
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
