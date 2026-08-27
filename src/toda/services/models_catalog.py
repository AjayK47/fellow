"""Metadata-only models.dev catalog with a durable, offline fallback.

The catalog describes model capabilities, limits and pricing. It never owns
provider credentials or request transports; those remain in ``providers.py``
and ``harness/model_client.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("toda.models_catalog")
PARSER_VERSION = "2"

_flight_lock = threading.Lock()
_in_flight: dict[str, threading.Event] = {}

_PROVIDER_ID_MAP = {
    "xai": "x-ai",
    "x-ai": "x-ai",
}


class _CatalogModel(BaseModel):
    """Tolerant boundary for the upstream models.dev model shape."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    provider_id: str | None = None
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    family: str | None = None
    base_model: str | None = None
    knowledge: Any = None
    knowledge_cutoff: str | None = None
    open_weights: bool | None = None
    attachment: bool | None = None
    temperature: Any = None
    interleaved: Any = None
    context_length: Any = None
    context: Any = None
    max_input_tokens: Any = None
    max_output_tokens: Any = None
    limit: Any = None
    modalities: Any = None
    capabilities: Any = None
    tool_call: bool | None = None
    tool_calling: bool | None = None
    structured_output: bool | None = None
    reasoning: bool | None = None
    pricing: Any = None
    cost: Any = None
    input_usd_per_1m: Any = None
    output_usd_per_1m: Any = None
    lifecycle: Any = None
    status: Any = None
    release_date: Any = None
    last_updated: Any = None
    updated_at: Any = None


class _ProviderMetadata(BaseModel):
    """One provider entry from the top-level models.dev catalog."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str | None = None
    api: str | None = None
    api_endpoint: str | None = None
    doc: str | None = None
    doc_url: str | None = None
    env: list[str] | None = None
    env_keys: list[str] | None = None
    npm: str | None = None
    models: dict[str, _CatalogModel] = Field(default_factory=dict)


class _CatalogPayload(BaseModel):
    """Typed, tolerant boundary for the current production response."""

    model_config = ConfigDict(extra="allow")

    models: dict[str, _CatalogModel] = Field(default_factory=dict)
    providers: dict[str, _ProviderMetadata] = Field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _price_per_million(value: Any) -> float | None:
    """Read the models.dev price, which is already USD per million tokens."""
    return _number(value)


def _numeric_field(value: Any, *keys: str) -> float | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = _number(value.get(key))
            if candidate is not None:
                return candidate
    return _number(value)


def _int_field(value: Any, *keys: str) -> int | None:
    number = _numeric_field(value, *keys)
    return int(number) if number is not None and number > 0 else None


def _directional_modalities(value: Any) -> tuple[list[str], list[str], list[str]]:
    input_mods: list[str] = []
    output_mods: list[str] = []
    if isinstance(value, dict):
        input_mods = [str(item).lower() for item in value.get("input", [])] if isinstance(value.get("input"), list) else []
        output_mods = [str(item).lower() for item in value.get("output", [])] if isinstance(value.get("output"), list) else []
    all_mods = sorted(set(input_mods + output_mods))
    return sorted(set(input_mods)), sorted(set(output_mods)), all_mods


def _capabilities(raw: dict[str, Any], input_modalities: list[str], output_modalities: list[str]) -> list[str]:
    capabilities = {str(item).lower() for item in (raw.get("capabilities") or [])} if isinstance(raw.get("capabilities"), list) else set()
    aliases = {
        "tool_call": "tool_calling",
        "tool_calling": "tool_calling",
        "structured_output": "structured_output",
        "reasoning": "reasoning",
    }
    for source, target in aliases.items():
        if raw.get(source) is True:
            capabilities.add(target)
    if "image" in input_modalities:
        capabilities.add("vision")
    if "image" in output_modalities:
        capabilities.add("image_generation")
    if "audio" in input_modalities:
        capabilities.add("transcription")
    if "audio" in output_modalities:
        capabilities.add("tts")
    return sorted(capabilities)


def _canonical_model(
    provider_id: str,
    model_id: str,
    raw: dict[str, Any],
    provider_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pricing = raw.get("pricing") or raw.get("cost") or {}
    if not isinstance(pricing, dict):
        pricing = {}
    input_price = _first_present(
        raw.get("input_usd_per_1m"),
        pricing.get("input"),
        pricing.get("prompt"),
        pricing.get("input_tokens"),
    )
    output_price = _first_present(
        raw.get("output_usd_per_1m"),
        pricing.get("output"),
        pricing.get("completion"),
        pricing.get("output_tokens"),
    )
    limits = raw.get("limit") or {}
    if not isinstance(limits, dict):
        limits = {}
    input_modalities, output_modalities, modalities = _directional_modalities(raw.get("modalities"))
    knowledge_val = raw.get("knowledge") or raw.get("knowledge_cutoff")
    knowledge_cutoff_str = (
        knowledge_val
        if isinstance(knowledge_val, str)
        else (str(knowledge_val.get("cutoff") or knowledge_val) if isinstance(knowledge_val, dict) else None)
    )
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "name": str(raw.get("name") or raw.get("display_name") or model_id),
        "description": raw.get("description"),
        "family": raw.get("family"),
        "base_model": raw.get("base_model"),
        "knowledge": knowledge_val,
        "knowledge_cutoff": knowledge_cutoff_str,
        "open_weights": raw.get("open_weights"),
        "attachment": raw.get("attachment"),
        "temperature": raw.get("temperature"),
        "interleaved": raw.get("interleaved"),
        "context_length": _int_field(
            raw.get("context_length") or raw.get("context") or limits,
            "context", "context_length", "max_input_tokens",
        ),
        "max_input_tokens": _int_field(
            raw.get("max_input_tokens") or limits, "input", "max_input_tokens"
        ),
        "max_output_tokens": _int_field(
            raw.get("max_output_tokens") or limits, "output", "max_output_tokens"
        ),
        "input_usd_per_1m": _price_per_million(input_price),
        "output_usd_per_1m": _price_per_million(output_price),
        "cache_read_usd_per_1m": _price_per_million(
            _first_present(pricing.get("cache_read"), pricing.get("cache_read_input"))
        ),
        "cache_write_usd_per_1m": _price_per_million(
            _first_present(pricing.get("cache_write"), pricing.get("cache_creation"))
        ),
        "reasoning_usd_per_1m": _price_per_million(pricing.get("reasoning")),
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "modalities": modalities,
        "capabilities": _capabilities(raw, input_modalities, output_modalities),
        "status": raw.get("status") or raw.get("lifecycle"),
        "lifecycle": raw.get("lifecycle") or raw.get("status"),
        "release_date": raw.get("release_date"),
        "last_updated": raw.get("last_updated") or raw.get("updated_at"),
        "provider_metadata": provider_meta or {},
    }


def _iter_models(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    providers = payload.get("providers")
    if isinstance(providers, dict):
        result: list[dict[str, Any]] = []
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if isinstance(models, dict):
                for model_id, item in models.items():
                    if isinstance(item, dict):
                        result.append(
                            {"id": model_id, "provider": provider_id, **item}
                        )
        if result:
            return result

    direct = payload.get("models") or payload.get("data")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    if isinstance(direct, dict):
        result = []
        for model_id, item in direct.items():
            if isinstance(item, dict):
                result.append({"id": model_id, **item})
        return result
    result = []
    for provider_id, provider in payload.items():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if isinstance(models, dict):
            for model_id, item in models.items():
                if isinstance(item, dict):
                    result.append({"id": model_id, "provider": provider_id, **item})
        elif isinstance(models, list):
            for item in models:
                if isinstance(item, dict):
                    result.append({"provider": provider_id, **item})
    return result


class ModelsCatalogService:
    def __init__(
        self,
        *,
        cache_path: Path,
        url: str = "https://models.dev/catalog.json",
        refresh_interval_seconds: int = 86_400,
        retry_backoff_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.meta_path = self.cache_path.with_suffix(self.cache_path.suffix + ".meta.json")
        self.url = url
        self.refresh_interval_seconds = max(60, int(refresh_interval_seconds))
        self.retry_backoff_seconds = max(1.0, float(retry_backoff_seconds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.enabled = enabled
        self._lock = threading.RLock()
        self._models: list[dict[str, Any]] = []
        self._providers: dict[str, dict[str, Any]] = {}
        self._metadata: dict[str, Any] = {}
        self._retry_after_mono: float = 0.0
        self._retry_after_iso: str | None = None
        self._load()

    @property
    def _bundled_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "data" / "models_catalog.json"

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _parse(
        self,
        payload: Any,
        *,
        source: str,
        raw_content_hash: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records: list[dict[str, Any]] = []
        providers_meta: dict[str, dict[str, Any]] = {}
        raw_items: list[dict[str, Any]] = []

        providers_dict: dict[str, Any] = {}
        top_models: dict[str, Any] = {}

        if isinstance(payload, dict):
            if "providers" in payload and isinstance(payload["providers"], dict):
                providers_dict = payload["providers"]
                if isinstance(payload.get("models"), dict):
                    top_models = payload["models"]
            else:
                providers_dict = {
                    k: v
                    for k, v in payload.items()
                    if isinstance(v, dict) and ("models" in v or "api" in v or "name" in v or "id" in v)
                }

        for provider_id, provider_raw in providers_dict.items():
            p_id = str(provider_id)
            if not isinstance(provider_raw, dict):
                continue

            p_meta: dict[str, Any] = {
                "id": str(provider_raw.get("id") or p_id),
                "name": str(provider_raw.get("name") or p_id),
                "api": provider_raw.get("api") or provider_raw.get("api_endpoint"),
                "api_endpoint": provider_raw.get("api") or provider_raw.get("api_endpoint"),
                "doc": provider_raw.get("doc") or provider_raw.get("doc_url"),
                "doc_url": provider_raw.get("doc") or provider_raw.get("doc_url"),
                "env": provider_raw.get("env") or provider_raw.get("env_keys") or [],
                "env_keys": provider_raw.get("env") or provider_raw.get("env_keys") or [],
                "npm": provider_raw.get("npm"),
            }
            for extra_k, extra_v in provider_raw.items():
                if extra_k not in p_meta and extra_k != "models":
                    p_meta[extra_k] = extra_v

            providers_meta[p_id] = p_meta

            models_dict = provider_raw.get("models") or {}
            if isinstance(models_dict, dict):
                for model_id, model_raw in models_dict.items():
                    if not isinstance(model_raw, dict):
                        continue
                    model_key = str(model_id)
                    qualified_id = f"{p_id}/{model_key}"
                    enrichment = top_models.get(qualified_id) or top_models.get(model_key) or {}
                    raw_items.append(
                        {
                            **enrichment,
                            **model_raw,
                            "id": model_key,
                            "provider": p_id,
                        }
                    )
            elif isinstance(models_dict, list):
                for item in models_dict:
                    if isinstance(item, dict):
                        raw_items.append({"provider": p_id, **item})

        for raw_item in raw_items:
            try:
                item = _CatalogModel.model_validate(raw_item).model_dump(exclude_none=True)
            except ValidationError:
                continue
            provider_id = str(item.get("provider_id") or item.get("provider") or "unknown")
            model_id = str(item.get("model_id") or item.get("id") or item.get("name") or "")
            if not model_id:
                continue
            p_meta = providers_meta.get(provider_id)
            records.append(_canonical_model(provider_id, model_id, item, provider_meta=p_meta))

        deduped = {(item["provider_id"], item["model_id"]): item for item in records}
        encoded = json.dumps(sorted(deduped.values(), key=lambda x: (x["provider_id"], x["model_id"])), sort_keys=True, separators=(",", ":"))
        normalized_hash = hashlib.sha256(encoded.encode()).hexdigest()

        return list(deduped.values()), {
            "source": source,
            "raw_content_hash": raw_content_hash,
            "normalized_projection_hash": normalized_hash,
            "content_hash": normalized_hash,
            "parser_version": PARSER_VERSION,
            "model_count": len(deduped),
            "provider_count": len(providers_meta),
            "provider_model_count": len(records),
            "providers": providers_meta,
            "loaded_at": _now(),
        }

    def _quarantine_corrupt_cache(self) -> None:
        try:
            timestamp = int(datetime.now(timezone.utc).timestamp())
            if self.cache_path.exists():
                corrupt_path = self.cache_path.with_name(f"{self.cache_path.name}.corrupt-{timestamp}")
                self.cache_path.rename(corrupt_path)
                log.warning("Quarantined corrupt models.dev cache to %s", corrupt_path)
            if self.meta_path.exists():
                corrupt_meta = self.meta_path.with_name(f"{self.meta_path.name}.corrupt-{timestamp}")
                self.meta_path.rename(corrupt_meta)
        except OSError as exc:
            log.warning("Could not quarantine corrupt models.dev cache: %s", exc)

    def _load(self) -> None:
        with self._lock:
            payload = None
            metadata = None
            if self.meta_path.exists():
                try:
                    metadata = json.loads(self.meta_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("models_catalog.metadata_read_failed: %s", exc)
                    metadata = None
            if self.cache_path.exists():
                try:
                    payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("models_catalog.cache_read_failed: %s", exc)
                    self._quarantine_corrupt_cache()
                    payload = None
                    metadata = None
            source = "cache"
            models: list[dict[str, Any]] = []
            parsed: dict[str, Any] = {}
            if payload is not None:
                try:
                    models, parsed = self._parse(
                        payload,
                        source="cache",
                        raw_content_hash=(metadata or {}).get("raw_content_hash")
                        if isinstance(metadata, dict)
                        else None,
                    )
                    if not models or (isinstance(payload, dict) and "providers" in payload and parsed.get("provider_model_count", 0) == 0):
                        log.warning("models_catalog.cache_semantically_corrupt: empty records")
                        self._quarantine_corrupt_cache()
                        models = []
                        parsed = {}
                except Exception as exc:
                    log.warning("models_catalog.cache_parse_failed: %s", exc)
                    self._quarantine_corrupt_cache()
                    models = []
                    parsed = {}
            if not models:
                payload = self._read_json(self._bundled_path) or {"models": []}
                source = "bundled"
                models, parsed = self._parse(payload, source=source)
            self._models = models
            self._providers = parsed.get("providers", {})
            # Preserve the leader's refresh result from the on-disk metadata
            # if present; only default to "loaded" when no prior result exists.
            preserved_result = (metadata or {}).get("last_refresh_result") or parsed.get("last_refresh_result") or "loaded"
            self._metadata = {
                **(metadata if isinstance(metadata, dict) else {}),
                **parsed,
                "fallback": source == "bundled",
                "last_refresh_result": preserved_result,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            now_mono = time.monotonic()
            retry_remaining = max(0.0, self._retry_after_mono - now_mono) if self._retry_after_mono > now_mono else 0.0
            return {
                **self._metadata,
                "url": self.url,
                "source_url": self._metadata.get("source_url") or self.url,
                "enabled": self.enabled,
                "stale": self._is_stale(),
                "fallback": bool(self._metadata.get("fallback", True)),
                "etag": self._metadata.get("etag"),
                "last_modified": self._metadata.get("last_modified"),
                "fetched_at": self._metadata.get("fetched_at"),
                "last_successful_fetch_at": self._metadata.get("last_successful_fetch_at"),
                "last_attempt_at": self._metadata.get("last_attempt_at"),
                "last_error": self._metadata.get("last_error"),
                "retry_after_seconds": retry_remaining,
                "retry_after_until": self._retry_after_iso,
                "provider_count": len(self._providers) or self._metadata.get("provider_count", 0),
                "model_count": len(self._models),
            }

    def _is_stale(self) -> bool:
        if bool(self._metadata.get("fallback", True)):
            return True
        last_fetch = self._metadata.get("last_successful_fetch_at") or self._metadata.get("fetched_at")
        if not last_fetch:
            return True
        try:
            timestamp = datetime.fromisoformat(str(last_fetch))
            return (datetime.now(timezone.utc) - timestamp).total_seconds() >= self.refresh_interval_seconds
        except ValueError:
            return True

    def models(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if provider_id is not None:
                mapped_id = _PROVIDER_ID_MAP.get(provider_id, provider_id)
                values = [item for item in self._models if item["provider_id"] in {provider_id, mapped_id}]
            else:
                values = self._models
            return [dict(item) for item in values]

    def providers(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._providers)

    def find(self, provider_id: str, model_id: str) -> dict[str, Any] | None:
        return next((item for item in self.models(provider_id) if item["model_id"] == model_id), None)

    def estimate_cost(self, *, provider_id: str, model_id: str, usage: dict[str, Any]) -> dict[str, Any] | None:
        model = self.find(provider_id, model_id)
        if not model:
            return None
        input_rate = model.get("input_usd_per_1m")
        output_rate = model.get("output_usd_per_1m")
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
            return None
        if not isinstance(input_tokens, (int, float)) or not isinstance(output_tokens, (int, float)):
            return None
        return {
            "usd": (float(input_tokens) * input_rate + float(output_tokens) * output_rate) / 1_000_000,
            "source": "models.dev" if self._metadata.get("source") == "remote" else "bundled_catalog",
        }

    def _atomic_write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def refresh(self, *, force: bool = False, _pre_reserved_event: threading.Event | None = None) -> dict[str, Any]:
        if not self.enabled:
            return self.status()

        key = str(self.cache_path.resolve())
        if _pre_reserved_event is not None:
            # Called from refresh_in_background — the event was atomically
            # registered before the thread was spawned. We are the leader.
            event = _pre_reserved_event
            is_leader = True
        else:
            with _flight_lock:
                if key in _in_flight:
                    event = _in_flight[key]
                    is_leader = False
                else:
                    event = threading.Event()
                    _in_flight[key] = event
                    is_leader = True

        if not is_leader:
            # A concurrent refresh is currently executing: wait for it and observe the completed result
            if not event.wait(timeout=self.timeout_seconds + 5.0):
                with self._lock:
                    self._metadata["last_refresh_result"] = "concurrent_refresh_timeout"
                    self._metadata["last_error"] = "timed out waiting for catalog refresh leader"
                    return self.status()
            with self._lock:
                self._load()
            return self.status()

        try:
            with self._lock:
                if not force and not self._is_stale():
                    return self.status()
                if not force and time.monotonic() < self._retry_after_mono:
                    return self.status()
                headers: dict[str, str] = {}
                if self._metadata.get("etag") and self.cache_path.exists():
                    headers["If-None-Match"] = str(self._metadata["etag"])
                if self._metadata.get("last_modified") and self.cache_path.exists():
                    headers["If-Modified-Since"] = str(self._metadata["last_modified"])

            now = _now()
            mono_now = time.monotonic()
            try:
                timeout = httpx.Timeout(
                    self.timeout_seconds,
                    connect=min(self.timeout_seconds, 2.0),
                    read=self.timeout_seconds,
                    write=self.timeout_seconds,
                    pool=min(self.timeout_seconds, 2.0),
                )
                response = httpx.get(self.url, headers=headers, timeout=timeout)
                if response.status_code == 304:
                    with self._lock:
                        # 304 is valid only if cache is present on disk and models loaded
                        if not self.cache_path.exists() or not self._models:
                            raise ValueError("304 Not Modified received but local cache is missing")
                        self._metadata["checked_at"] = now
                        self._metadata["last_attempt_at"] = now
                        self._metadata["last_successful_fetch_at"] = now
                        self._metadata["last_refresh_result"] = "not_modified"
                        self._metadata["last_error"] = None
                        self._retry_after_mono = 0.0
                        self._retry_after_iso = None
                        self._atomic_write(self.meta_path, self._metadata)
                    return self.status()

                response.raise_for_status()
                payload = response.json()
                raw_bytes = getattr(response, "content", None)
                if not isinstance(raw_bytes, bytes):
                    raw_bytes = json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                raw_hash = hashlib.sha256(raw_bytes).hexdigest()
                models, metadata = self._parse(payload, source="remote", raw_content_hash=raw_hash)
                if not models or metadata.get("provider_model_count", 0) == 0:
                    raise ValueError(
                        "models.dev response contained no provider-model records"
                    )
                metadata.update({
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "checked_at": now,
                    "fetched_at": now,
                    "last_attempt_at": now,
                    "last_successful_fetch_at": now,
                    "source_url": self.url,
                    "fallback": False,
                    "last_refresh_result": "updated",
                    "last_error": None,
                })
                with self._lock:
                    self._atomic_write(self.cache_path, payload)
                    self._atomic_write(self.meta_path, metadata)
                    self._models, self._metadata = models, metadata
                    self._providers = metadata.get("providers", {})
                    self._retry_after_mono = 0.0
                    self._retry_after_iso = None
                    return self.status()
            except Exception as exc:
                log.warning("models_catalog.refresh_failed: %s", exc)
                with self._lock:
                    self._metadata["last_error"] = str(exc)[:500]
                    self._metadata["last_attempt_at"] = now
                    self._retry_after_mono = mono_now + self.retry_backoff_seconds
                    self._retry_after_iso = datetime.fromtimestamp(
                        datetime.now(timezone.utc).timestamp() + self.retry_backoff_seconds,
                        tz=timezone.utc,
                    ).isoformat()
                    self._metadata["last_refresh_result"] = "failed_using_fallback"
                    self._atomic_write(self.meta_path, self._metadata)
                    return self.status()
        finally:
            with _flight_lock:
                _in_flight.pop(key, None)
                event.set()

    def refresh_in_background(self, *, force: bool = False) -> None:
        key = str(self.cache_path.resolve())
        with _flight_lock:
            if key in _in_flight:
                return
            # Atomically reserve the flight key before spawning the thread
            # so no concurrent caller can slip in and create a duplicate thread.
            event = threading.Event()
            _in_flight[key] = event

        def _bg_refresh() -> None:
            # Pass the pre-reserved event so refresh() enters the leader
            # path and uses the already-registered flight event.
            self.refresh(force=force, _pre_reserved_event=event)

        threading.Thread(
            target=_bg_refresh,
            name="toda-models-catalog-refresh",
            daemon=True,
        ).start()
